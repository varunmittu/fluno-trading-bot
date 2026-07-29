"""
Backtest - add a 1-MIN confirmation on top of v4.1 (5m + 15m filter)?
A = v4.1 (current bot: 5-min indicators + 15-min trend filter)
B = v4.1 + 1-min MACD aligned
C = v4.1 + 1-min MACD aligned + last 1-min candle in trade direction

Data limit: yfinance only gives 7 days of 1-min candles, so all three
run on the SAME last-7-days window for a fair comparison.
Real Zerodha charges. No lookahead: only fully closed candles are used.
"""
import os, math
from datetime import date, time as dtime, timedelta
import pandas as pd, numpy as np
import yfinance as yf

os.chdir(os.path.dirname(os.path.abspath(__file__)))

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
    d["dt"] = d["dt"].astype("datetime64[ns]")
    return d[["dt", "open", "high", "low", "close", "volume"]]

print("Fetching 5-min (60d) and 1-min (7d) data...")
raw5 = get_yf("5m", "60d")
raw1 = get_yf("1m", "7d")
df5  = prep(raw5)

# -- 15-min trend mapped onto 5-min rows (same as backtest_mtf.py) ------------
r = raw5.set_index("dt")
f15 = pd.DataFrame({
    "open":   r["open"].resample("15min").first(),
    "high":   r["high"].resample("15min").max(),
    "low":    r["low"].resample("15min").min(),
    "close":  r["close"].resample("15min").last(),
}).dropna().reset_index()
f15["st_dir"] = supertrend(f15)
m15, s15 = macd(f15["close"])
f15["macd_up15"] = m15 > s15
f15["usable_from"] = (f15["dt"] + pd.Timedelta(minutes=10)).astype("datetime64[ns]")
htf = pd.merge_asof(df5[["dt"]].sort_values("dt"),
                    f15[["usable_from", "st_dir", "macd_up15"]].sort_values("usable_from"),
                    left_on="dt", right_on="usable_from", direction="backward")
df5["htf_st"]   = htf["st_dir"].values
df5["htf_macd"] = htf["macd_up15"].values

# -- 1-min trend mapped onto 5-min rows ---------------------------------------
# decision happens at 5m close (dt+5). last closed 1m candle starts at dt+4.
m1, s1 = macd(raw1["close"])
f1 = raw1.copy()
f1["macd_up1"] = m1 > s1
f1["green1"]   = f1["close"] >= f1["open"]
f1["usable_key"] = (f1["dt"] - pd.Timedelta(minutes=4)).astype("datetime64[ns]")
ltf = pd.merge_asof(df5[["dt"]].sort_values("dt"),
                    f1[["usable_key", "macd_up1", "green1"]].sort_values("usable_key"),
                    left_on="dt", right_on="usable_key", direction="backward",
                    tolerance=pd.Timedelta(minutes=4))
df5["ltf_macd"]  = ltf["macd_up1"].values
df5["ltf_green"] = ltf["green1"].values

def htf_agrees(frame, i, otype):
    row = frame.loc[i]
    if pd.isna(row["htf_st"]): return False
    if otype == "CE": return row["htf_st"] == 1  and bool(row["htf_macd"])
    return row["htf_st"] == -1 and not bool(row["htf_macd"])

def ltf_agrees(frame, i, otype, need_candle):
    row = frame.loc[i]
    if pd.isna(row["ltf_macd"]): return False
    macd_ok = bool(row["ltf_macd"]) if otype == "CE" else not bool(row["ltf_macd"])
    if not need_candle:
        return macd_ok
    if pd.isna(row["ltf_green"]): return False
    cand_ok = bool(row["ltf_green"]) if otype == "CE" else not bool(row["ltf_green"])
    return macd_ok and cand_ok

# -- params (same as bot) ------------------------------------------------------
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

# only days where 1-min data exists (fair A/B/C)
days_1m = set(raw1["dt"].dt.date.unique())

def simulate(frame, mode):
    """mode: 'A' = 15m only | 'B' = +1m MACD | 'C' = +1m MACD +1m candle"""
    capital, lots = START_CAPITAL, BASE_LOTS
    dr = {d: frame.index[frame["day"] == d].tolist() for d in frame["day"].unique()}
    all_d = sorted(frame["day"].unique())
    trades, blocked, day_net = [], 0, {}

    for d in all_d[1:]:
        if d not in days_1m: continue
        if d.weekday() == 0: continue
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
                    capital += net; daily_pnl += net
                    lots = min(MAX_LOTS, max(BASE_LOTS, int(capital//CAP_PER_LOT))) if net > 0 else BASE_LOTS
                    trades.append((d, status, net))
                    day_net[d] = day_net.get(d, 0.0) + net
                    if status == "SL": cooldown = row["dt"] + timedelta(minutes=10)
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

            if not htf_agrees(frame, i, otype):
                blocked += 1; continue
            if mode in ("B", "C") and not ltf_agrees(frame, i, otype, mode == "C"):
                blocked += 1; continue

            conf, sl = confidence(frame, i, otype, brk)
            if trades_today == 0:
                try_open(otype, sig, conf, sl)
            else:
                if gatecool and row["dt"] < gatecool: continue
                if conf >= CONF_GATE:
                    pending = {"otype": otype, "sig": sig, "conf": conf, "sl": sl}
                else:
                    gatecool = row["dt"] + timedelta(minutes=15)

    return capital, trades, blocked, day_net

def report(name, capital, trades, blocked, day_net):
    wins   = [t for t in trades if t[2] > 0]
    losses = [t for t in trades if t[2] <= 0]
    wr = len(wins)/len(trades)*100 if trades else 0
    print(f"\n-- {name} " + "-" * max(1, 66 - len(name)))
    print(f"Trades   : {len(trades)} (wins {len(wins)} / losses {len(losses)}) | win rate {wr:.0f}%")
    gdays = sum(1 for v in day_net.values() if v > 0); rdays = sum(1 for v in day_net.values() if v < 0)
    print(f"Days     : {gdays} green / {rdays} red | entries blocked: {blocked}")
    print(f"NET P&L  : Rs.{capital-START_CAPITAL:+,.2f}  ->  final Rs.{capital:,.2f}")

test_days = sorted(d for d in days_1m)
print("\n" + "=" * 74)
print(f"A/B/C BACKTEST - is a 1-MIN check worth adding on top of v4.1?")
print(f"Window: {test_days[0]} -> {test_days[-1]} (1-min data limit = 7 days)")
print("=" * 74)

cap_a, tr_a, bl_a, dn_a = simulate(df5, "A")
cap_b, tr_b, bl_b, dn_b = simulate(df5, "B")
cap_c, tr_c, bl_c, dn_c = simulate(df5, "C")

report("A) v4.1 (5m + 15m) - CURRENT BOT", cap_a, tr_a, bl_a, dn_a)
report("B) + 1-min MACD aligned",          cap_b, tr_b, bl_b, dn_b)
report("C) + 1-min MACD + 1-min candle",   cap_c, tr_c, bl_c, dn_c)

print("\n" + "=" * 74)
best = max([("A", cap_a), ("B", cap_b), ("C", cap_c)], key=lambda x: x[1])
print(f"BEST IN THIS WINDOW: {best[0]}  (small 7-day sample - treat with caution)")
print("=" * 74)
