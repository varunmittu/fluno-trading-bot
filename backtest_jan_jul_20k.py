"""
PRACTICE RUN - 1 Jan 2026 -> 3 Jul 2026 with TODAY'S settings (v4.2):
  yd-level breakout + opening-range breakout + afternoon fade PE
  15-min trend filter | window 10:15-14:30 | ride winners from Rs.1000
  NO daily trade limit | NO confidence gate | daily stop -Rs.750
  dynamic SL 100-500 | BE lock 300 | small trail 400/150 | real charges

Data reality (be honest):
  Jan 1 - Apr 9 : HOURLY candles (yfinance 5-min only keeps 60 days).
                  Approximation - trend filter runs on 1h, fills are coarse.
  Apr 10 - Jul 3: real 5-min candles + true 15-min filter. Exact method.
Start capital Rs.10,000. All /confirm assumed YES.
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

def get_yf(interval, start=None, end=None, period=None):
    kw = {"interval": interval, "progress": False}
    if period: kw["period"] = period
    else:      kw["start"], kw["end"] = start, end
    d = yf.download("^NSEI", **kw).reset_index()
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

print("Fetching hourly (Dec 2025 - Apr 2026) and 5-min (last 60d) data...")
df_h = prep(get_yf("60m", start="2025-12-01", end="2026-04-11"))
raw5 = get_yf("5m", period="60d")
df_5 = prep(raw5)

# hourly frame: trend filter = the hourly trend itself (best available)
df_h["htf_st"]   = df_h["st_dir"]
df_h["htf_macd"] = df_h["macd"] > df_h["macd_sig"]

# 5-min frame: true 15-min filter (same as live bot)
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
htf = pd.merge_asof(df_5[["dt"]].sort_values("dt"),
                    f15[["usable_from", "st_dir", "macd_up15"]].sort_values("usable_from"),
                    left_on="dt", right_on="usable_from", direction="backward")
df_5["htf_st"]   = htf["st_dir"].values
df_5["htf_macd"] = htf["macd_up15"].values

def htf_agrees(frame, i, otype):
    row = frame.loc[i]
    if pd.isna(row["htf_st"]): return False
    if otype == "CE": return row["htf_st"] == 1  and bool(row["htf_macd"])
    return row["htf_st"] == -1 and not bool(row["htf_macd"])

# -- TODAY'S live settings ----------------------------------------------------
SL_MIN, SL_MAX = 100, 500
BE_START = BE_FLOOR = 300
SMALL_START, SMALL_DROP = 400, 150
BIG_START, BIG_SAFETY = 1000, 300
DAILY_LIMIT   = -750
MAX_TRADES    = 99          # no limit (Sai, 2026-07-03)
DELTA, MODEL_BROKERAGE = 0.40, 20
BASE_LOTS, MAX_LOTS, CAP_PER_LOT = 1, 15, 10000
UNITS = 65   # real NIFTY lot (Kite-verified)
START_CAPITAL = 20000.0
FADE_PCT      = 0.003

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

def simulate(frame, t_from, t_to, capital, lots, trades_out, day_net):
    dr = {d: frame.index[frame["day"] == d].tolist() for d in frame["day"].unique()}
    all_d = sorted(frame["day"].unique())
    for d in [x for x in all_d if t_from <= x <= t_to]:
        if d.weekday() == 1: continue          # expiry TUESDAY skip (corrected)
        prior = [x for x in all_d if x < d]
        if not prior: continue
        yd_hi = frame.loc[dr[prior[-1]], "high"].max()
        yd_lo = frame.loc[dr[prior[-1]], "low"].min()
        idxs  = dr[d]

        or_rows = [i for i in idxs if frame.loc[i, "dt"].time() < dtime(10, 15)]
        or_hi = max(frame.loc[i, "high"] for i in or_rows) if or_rows else None
        or_lo = min(frame.loc[i, "low"]  for i in or_rows) if or_rows else None

        daily_pnl, trades_today = 0.0, 0
        pos, pending = None, None
        cooldown, gatecool = None, None
        day_hi_run = -1e18

        for k, i in enumerate(idxs):
            row = frame.loc[i]; t = row["dt"].time(); px = row["close"]
            last = k == len(idxs) - 1

            if pos:
                units = pos["lots"]*UNITS
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
                    trades_out.append((d, status, net))
                    day_net[d] = day_net.get(d, 0.0) + net
                    if status == "SL": cooldown = row["dt"] + timedelta(minutes=10)
                    trades_today += 1
                    pos = None

            day_hi_prior = day_hi_run
            if row["high"] > day_hi_run: day_hi_run = row["high"]

            if daily_pnl <= DAILY_LIMIT:
                pending = None; continue
            if pos or trades_today >= MAX_TRADES: continue
            if not (dtime(10, 15) <= t <= dtime(14, 30)):
                pending = None; continue
            if cooldown and row["dt"] < cooldown: continue

            def try_open(otype, sig, conf, sl):
                nonlocal pos
                prem = premium_est(px)
                afford = int(capital // (prem*UNITS))
                if afford < 1: return False
                use = min(lots, afford)
                pos = {"otype": otype, "entry": px, "sl": sl, "conf": conf,
                       "lots": use, "peak": -9999, "locked": False,
                       "inv": round(prem*use*UNITS, 2)}
                return True

            if pending:
                try_open(pending["otype"], pending["sig"], pending["conf"], pending["sl"])
                pending = None
                continue

            otype = None
            if px > yd_hi:   otype, sig, brk = "CE", "BRK-HI", True
            elif px < yd_lo: otype, sig, brk = "PE", "BRK-LO", True
            elif or_lo is not None and px < or_lo: otype, sig, brk = "PE", "OR-LO", True
            elif or_hi is not None and px > or_hi: otype, sig, brk = "CE", "OR-HI", True
            if t > dtime(12, 30) and otype != "PE" and day_hi_prior > 0 \
               and px <= day_hi_prior * (1 - FADE_PCT):
                otype, sig, brk = "PE", "FADE", True

            if otype is None: continue
            if not htf_agrees(frame, i, otype): continue

            conf, sl = confidence(frame, i, otype, brk)
            if trades_today == 0:
                try_open(otype, sig, conf, sl)
            else:
                if gatecool and row["dt"] < gatecool: continue
                pending = {"otype": otype, "sig": sig, "conf": conf, "sl": sl}
    return capital, lots

trades, day_net = [], {}
capital, lots = START_CAPITAL, BASE_LOTS
capital, lots = simulate(df_h, date(2026, 1, 1), date(2026, 4, 9),  capital, lots, trades, day_net)
capital, lots = simulate(df_5, date(2026, 4, 10), date(2026, 7, 3), capital, lots, trades, day_net)

print("\n" + "=" * 74)
print("REAL-LOTS RUN 1 Jan -> 3 Jul 2026 | Rs.20,000 start | 65u lots | Tue skip")
print("Jan-Apr9: hourly data (approx) | Apr10-Jul3: 5-min data (exact)")
print("=" * 74)

# monthly breakdown
months = {}
for d, net in day_net.items():
    key = d.strftime("%b %Y")
    m = months.setdefault(key, {"net": 0.0, "g": 0, "r": 0})
    m["net"] += net
    if net > 0: m["g"] += 1
    elif net < 0: m["r"] += 1

eq = START_CAPITAL
order = sorted(months, key=lambda k: pd.to_datetime("01 " + k))
print(f"{'month':>9} | {'green':>5} | {'red':>3} | {'month P&L':>12} | {'capital':>12}")
print("-" * 74)
for k in order:
    m = months[k]
    eq += m["net"]
    print(f"{k:>9} | {m['g']:>5} | {m['r']:>3} | Rs.{m['net']:>+10,.2f} | Rs.{eq:>10,.2f}")

wins   = [t[2] for t in trades if t[2] > 0]
losses = [t[2] for t in trades if t[2] <= 0]
streak = worst_streak = 0
for t in trades:
    streak = streak + 1 if t[2] <= 0 else 0
    worst_streak = max(worst_streak, streak)
eq2, pk, mdd = START_CAPITAL, START_CAPITAL, 0.0
for d in sorted(day_net):
    eq2 += day_net[d]; pk = max(pk, eq2); mdd = max(mdd, pk - eq2)

print("=" * 74)
print(f"Trades   : {len(trades)} | wins {len(wins)} ({len(wins)/len(trades)*100:.0f}%) | avg win Rs.{np.mean(wins):+,.0f} | avg loss Rs.{np.mean(losses):+,.0f}")
print(f"Days     : {sum(1 for v in day_net.values() if v>0)} green / {sum(1 for v in day_net.values() if v<0)} red")
print(f"Risk     : worst day Rs.{min(day_net.values()):+,.0f} | max drawdown Rs.{mdd:,.0f} | worst streak {worst_streak} losses")
print(f"NET P&L  : Rs.{capital-START_CAPITAL:+,.2f}")
print(f"FINAL    : Rs.{capital:,.2f}  ({(capital-START_CAPITAL)/START_CAPITAL*100:+.1f}%) from Rs.10,000")
print("=" * 74)
