"""
Backtest - Multi-TimeFrame check: v4 vs v4 + 15-min trend filter
Same data, same rules, only difference: the MTF filter.

MTF filter rule: a trade is only allowed if the last COMPLETED 15-min candle
agrees with the direction - supertrend AND MACD on 15-min chart:
  CE needs 15m supertrend bullish + 15m MACD above signal
  PE needs 15m supertrend bearish + 15m MACD below signal

Data: 5-min candles, last 60 days (yfinance limit).
Charges: real Zerodha (brokerage, STT, txn, SEBI, stamp, GST).
No lookahead: 15m candle is only used after it has fully closed.
"""
import os, math
from datetime import date, time as dtime, timedelta
import pandas as pd, numpy as np
import yfinance as yf

os.chdir(os.path.dirname(os.path.abspath(__file__)))

# -- indicators (same as app.py / backtest_v4_charges.py) ---------------------
def rsi(s, period=14):
    d = s.diff(); g = d.clip(lower=0); l = -d.clip(upper=0)
    rs = g.rolling(period).mean() / l.rolling(period).mean().replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def macd(s, fast=12, slow=26, sig=9):
    m = s.ewm(span=fast).mean() - s.ewm(span=slow).mean()
    return m, m.ewm(span=sig).mean()

def supertrend(d, period=7, mult=3):
    hi, lo, cl = d["high"].values, d["low"].values, d["close"].values
    n = len(d); tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(hi[i]-lo[i], abs(hi[i]-cl[i-1]), abs(lo[i]-cl[i-1]))
    atr = np.zeros(n)
    for i in range(period, n):
        atr[i] = np.mean(tr[i-period+1:i+1]) if atr[i-1] == 0 else (atr[i-1]*(period-1)+tr[i])/period
    hl2 = (hi+lo)/2; bub, blb = hl2+mult*atr, hl2-mult*atr
    fub, flb = bub.copy(), blb.copy(); dr = np.ones(n)
    for i in range(1, n):
        if atr[i] == 0: dr[i] = dr[i-1]; continue
        fub[i] = bub[i] if bub[i] < fub[i-1] or cl[i-1] > fub[i-1] else fub[i-1]
        flb[i] = blb[i] if blb[i] > flb[i-1] or cl[i-1] < flb[i-1] else flb[i-1]
        dr[i]  = (1 if cl[i] >= flb[i] else -1) if dr[i-1] == 1 else (-1 if cl[i] <= fub[i] else 1)
    return pd.Series(dr, index=d.index)

def prep(f):
    f = f.sort_values("dt").reset_index(drop=True)
    f["day"] = f["dt"].dt.date
    f["rsi"] = rsi(f["close"])
    f["sma20"] = f["close"].rolling(20).mean()
    f["sma50"] = f["close"].rolling(50).mean()
    m_, s_ = macd(f["close"]); f["macd"], f["macd_sig"] = m_, s_
    f["vol_avg"] = f["volume"].rolling(20).mean()
    f["st_dir"]  = supertrend(f)
    return f

def get_yf(interval, period):
    d = yf.download("^NSEI", interval=interval, period=period, progress=False).reset_index()
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = [c[0].lower().replace(" ", "") for c in d.columns]
    else:
        d.columns = [str(c).lower().replace(" ", "") for c in d.columns]
    tcol = next(c for c in d.columns if c in ("datetime", "date", "index", "timestamp"))
    d = d.rename(columns={tcol: "dt"})
    d["dt"] = pd.to_datetime(d["dt"])
    try:    d["dt"] = d["dt"].dt.tz_localize(None)
    except Exception: pass
    return d[["dt", "open", "high", "low", "close", "volume"]]

print("Fetching 5-min data (60 days)...")
raw5 = get_yf("5m", "60d")
df5  = prep(raw5)

# -- build 15-min frame + map its trend onto each 5-min row (no lookahead) ----
r = raw5.set_index("dt")
f15 = pd.DataFrame({
    "open":   r["open"].resample("15min").first(),
    "high":   r["high"].resample("15min").max(),
    "low":    r["low"].resample("15min").min(),
    "close":  r["close"].resample("15min").last(),
    "volume": r["volume"].resample("15min").sum(),
}).dropna().reset_index()
f15["st_dir"] = supertrend(f15)
m15, s15 = macd(f15["close"])
f15["macd15"], f15["macd_sig15"] = m15, s15
# a 15m candle starting at S closes at S+15; a 5m row starting at s knows its
# close at s+5 -> 15m candle usable when s+5 >= S+15 i.e. s >= S+10
f15["usable_from"] = (f15["dt"] + pd.Timedelta(minutes=10)).astype("datetime64[ns]")
df5["dt"] = df5["dt"].astype("datetime64[ns]")
htf = pd.merge_asof(
    df5[["dt"]].sort_values("dt"),
    f15[["usable_from", "st_dir", "macd15", "macd_sig15"]].sort_values("usable_from"),
    left_on="dt", right_on="usable_from", direction="backward",
)
df5["htf_st"]   = htf["st_dir"].values
df5["htf_macd"] = (htf["macd15"] > htf["macd_sig15"]).values

def htf_agrees(frame, i, otype):
    row = frame.loc[i]
    if np.isnan(row["htf_st"]): return False
    if otype == "CE": return row["htf_st"] == 1  and bool(row["htf_macd"])
    else:             return row["htf_st"] == -1 and not bool(row["htf_macd"])

# -- params (same as bot) -----------------------------------------------------
SL_MIN, SL_MAX = 100, 500
BE_START = BE_FLOOR = 300
SMALL_START, SMALL_DROP = 400, 150
BIG_START, BIG_SAFETY = 1000, 300
DAILY_LIMIT, MAX_TRADES, CONF_GATE = -750, 3, 50
DELTA, MODEL_BROKERAGE = 0.40, 20
BASE_LOTS, MAX_LOTS, CAP_PER_LOT = 3, 15, 10000/3
START_CAPITAL = 10000.0

def confidence(f, i, otype, brk):
    row, prev = f.iloc[i], f.iloc[i-1]
    bd = 0
    vol_ok = (not np.isnan(row["vol_avg"])) and row["volume"] > row["vol_avg"]*1.1
    if otype == "CE":
        if row["rsi"] < 50: bd += 15
        if row["macd"] > row["macd_sig"]: bd += 12
        if vol_ok: bd += 5
        if row["sma20"] > row["sma50"]: bd += 10
        if row["close"] > row["sma50"]: bd += 5
        if row["sma20"] > prev["sma20"]: bd += 3
        st_ok = row["st_dir"] == 1
    else:
        if row["rsi"] > 50: bd += 15
        if row["macd"] < row["macd_sig"]: bd += 12
        if vol_ok: bd += 5
        if row["sma20"] < row["sma50"]: bd += 10
        if row["close"] < row["sma50"]: bd += 5
        if row["sma20"] < prev["sma20"]: bd += 3
        st_ok = row["st_dir"] == -1
    total = bd + (10 if st_ok else 0) + (10 if brk else 0)
    conf  = max(5, min(95, round(total/70*100)))
    sl    = SL_MIN + int(round(conf/100*(SL_MAX-SL_MIN)/50))*50
    return conf, max(SL_MIN, min(SL_MAX, sl))

def trend_strong(f, i, otype):
    row = f.iloc[i]
    st = row["st_dir"] == (1 if otype == "CE" else -1)
    mc = (row["macd"] > row["macd_sig"]) if otype == "CE" else (row["macd"] < row["macd_sig"])
    return bool(st and mc)

def premium_est(spot):
    return round(spot * 0.14 * math.sqrt(4/365) * 0.4 * 0.98, 1)

def real_charges(buy_val, sell_val):
    brokerage = 20.0 * 2
    stt   = 0.001     * sell_val
    txn   = 0.0003503 * (buy_val + sell_val)
    sebi  = 0.000001  * (buy_val + sell_val)
    stamp = 0.00003   * buy_val
    gst   = 0.18 * (brokerage + txn + sebi)
    return round(brokerage + stt + txn + sebi + stamp + gst, 2)

# -- simulation (identical to v4-charges, plus optional MTF filter) -----------
def simulate(frame, use_mtf):
    capital, lots = START_CAPITAL, BASE_LOTS
    dr = {d: frame.index[frame["day"] == d].tolist() for d in frame["day"].unique()}
    all_d = sorted(frame["day"].unique())
    trades = []          # (date, status, net)
    mtf_blocked = 0
    day_net = {}

    for d in all_d[1:]:
        if d.weekday() == 0:      # expiry Monday - bot skips
            continue
        prior = [x for x in all_d if x < d]
        yd_hi = frame.loc[dr[prior[-1]], "high"].max()
        yd_lo = frame.loc[dr[prior[-1]], "low"].min()
        idxs  = dr[d]
        f0    = frame.loc[idxs[0]]
        morn  = "CE" if f0["close"] >= f0["open"] else "PE"

        daily_pnl, trades_today = 0.0, 0
        pos, pending = None, None
        cooldown, gatecool = None, None

        for k, i in enumerate(idxs):
            row = frame.loc[i]; t = row["dt"].time(); px = row["close"]
            last = k == len(idxs) - 1

            if pos:
                units = pos["lots"]*25
                mv  = (px-pos["entry"])*DELTA*units if pos["otype"] == "CE" else (pos["entry"]-px)*DELTA*units
                pnl = mv - MODEL_BROKERAGE
                if pnl > pos["peak"]: pos["peak"] = pnl
                peak = pos["peak"]
                if peak >= BE_START: pos["locked"] = True
                booked = None
                if t >= dtime(15, 25) or last:                 booked = (round(pnl, 2), "EOD")
                elif pos["locked"] and pnl < BE_FLOOR:         booked = (BE_FLOOR, "BE")
                elif not pos["locked"] and pnl <= -pos["sl"]:  booked = (-pos["sl"], "SL")
                elif peak >= BIG_START:
                    if not trend_strong(frame, i, pos["otype"]) or pnl <= peak-BIG_SAFETY:
                        booked = (round(pnl, 2), "TRAIL")
                elif peak >= SMALL_START and pnl <= peak-SMALL_DROP:
                    v = max(BE_FLOOR, round(pnl, 2)) if pos["locked"] else round(pnl, 2)
                    booked = (v, "LOCK" if v > 0 else "SL")
                if booked:
                    val, status = booked
                    gross    = val + MODEL_BROKERAGE
                    sell_val = max(0.0, pos["inv"] + gross)
                    chg      = real_charges(pos["inv"], sell_val)
                    net      = round(gross - chg, 2)
                    capital += net
                    daily_pnl += net
                    lots = min(MAX_LOTS, max(BASE_LOTS, int(capital//CAP_PER_LOT))) if net > 0 else BASE_LOTS
                    trades.append((d, status, net))
                    day_net[d] = day_net.get(d, 0.0) + net
                    if status == "SL":
                        cooldown = row["dt"] + timedelta(minutes=10)
                    trades_today += 1
                    pos = None

            if daily_pnl <= DAILY_LIMIT:
                pending = None; continue
            if pos or trades_today >= MAX_TRADES: continue
            if not (dtime(10, 15) <= t <= dtime(12, 30)):
                pending = None; continue
            if cooldown and row["dt"] < cooldown: continue

            def try_open(otype, sig, conf, sl):
                nonlocal pos
                prem = premium_est(px)
                afford = int(capital // (prem*25))
                if afford < 1: return False
                use = min(lots, afford)
                pos = {"otype": otype, "entry": px, "sl": sl, "conf": conf,
                       "lots": use, "peak": -9999, "locked": False,
                       "inv": round(prem*use*25, 2)}
                return True

            if pending:
                try_open(pending["otype"], pending["sig"], pending["conf"], pending["sl"])
                pending = None
                continue

            if px > yd_hi:   otype, sig, brk = "CE", "BRK-HI", True
            elif px < yd_lo: otype, sig, brk = "PE", "BRK-LO", True
            else:            otype, sig, brk = morn, "MORN", False

            # -- THE ONLY DIFFERENCE: 15-min trend must agree --------------
            if use_mtf and not htf_agrees(frame, i, otype):
                mtf_blocked += 1
                continue

            conf, sl = confidence(frame, i, otype, brk)
            if trades_today == 0:
                try_open(otype, sig, conf, sl)
            else:
                if gatecool and row["dt"] < gatecool: continue
                if conf >= CONF_GATE:
                    pending = {"otype": otype, "sig": sig, "conf": conf, "sl": sl}
                else:
                    gatecool = row["dt"] + timedelta(minutes=15)

    return capital, trades, mtf_blocked, day_net

def report(name, capital, trades, blocked, day_net):
    wins   = [t for t in trades if t[2] > 0]
    losses = [t for t in trades if t[2] <= 0]
    gdays  = sum(1 for v in day_net.values() if v > 0)
    rdays  = sum(1 for v in day_net.values() if v < 0)
    worst_day = min(day_net.values()) if day_net else 0
    best_day  = max(day_net.values()) if day_net else 0
    print(f"\n-- {name} " + "-" * (70 - len(name)))
    print(f"Trades         : {len(trades)}  (wins {len(wins)} / losses {len(losses)})")
    wr = len(wins)/len(trades)*100 if trades else 0
    print(f"Win rate       : {wr:.0f}%")
    print(f"Green/red days : {gdays} / {rdays}")
    print(f"Best day       : Rs.{best_day:+,.0f}   Worst day: Rs.{worst_day:+,.0f}")
    print(f"Avg win        : Rs.{np.mean([t[2] for t in wins]):+,.0f}" if wins else "Avg win        : --")
    print(f"Avg loss       : Rs.{np.mean([t[2] for t in losses]):+,.0f}" if losses else "Avg loss       : --")
    print(f"NET P&L        : Rs.{capital-START_CAPITAL:+,.2f}")
    print(f"FINAL CAPITAL  : Rs.{capital:,.2f}  ({(capital-START_CAPITAL)/START_CAPITAL*100:+.1f}%)")
    if blocked:
        print(f"Entries blocked by 15-min filter: {blocked}")

d0, d1 = df5["day"].min(), df5["day"].max()
print("\n" + "=" * 78)
print(f"A/B BACKTEST - v4 vs v4 + 15-MIN TREND FILTER | {d0} -> {d1}")
print(f"Same 5-min data, same rules, real Zerodha charges | Start Rs.{START_CAPITAL:,.0f}")
print("=" * 78)

cap_a, tr_a, _,    dn_a = simulate(df5, use_mtf=False)
cap_b, tr_b, blk,  dn_b = simulate(df5, use_mtf=True)

report("A) v4 - CURRENT BOT", cap_a, tr_a, 0, dn_a)
report("B) v4 + 15-MIN TREND FILTER", cap_b, tr_b, blk, dn_b)

print("\n" + "=" * 78)
diff = cap_b - cap_a
if diff > 500:
    print(f"VERDICT: 15-min filter HELPED - Rs.{diff:+,.2f} better. Worth adding to the bot.")
elif diff < -500:
    print(f"VERDICT: 15-min filter HURT - Rs.{diff:+,.2f} worse. Do NOT add it.")
else:
    print(f"VERDICT: roughly the same (Rs.{diff:+,.2f}). Not enough evidence to change the bot.")
print("=" * 78)
