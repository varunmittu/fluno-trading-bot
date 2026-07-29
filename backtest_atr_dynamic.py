"""
ATR-BASED STOP LOSS test — Sai's 2026-07-08 observation: entry-to-SL gap is
too narrow, normal candle noise hits the stop even when direction is right.
Root cause: Rs.100-500 SL range was designed for the old (wrong) 25-unit
lots; at real 65u lots it gives only ~4-19 index points of room.

Fix candidate: SL placed from live volatility (ATR of 5-min candles) so the
stop sits just OUTSIDE normal noise. Rupees/lot = ATR_pts x mult x 0.40 x 65.

A = CURRENT   conf-based Rs.100-500
B = ATR 1.0x  (capped Rs.200-1000 per lot)
C = ATR 1.5x  (capped Rs.200-1000 per lot)
D = ATR 2.0x  (capped Rs.200-1000 per lot)
E = ATR 1.5x  uncapped

All with daily limit -1000 (deployed today), v4.4 staircase, 15m MTF,
fade 0.2%, expiry-Tue next week, lot scaling, Rs.15,000, real charges.
"""
import os, math
from datetime import time as dtime, timedelta
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

def atr_series(f, period=14):
    hi, lo, cl = f["high"], f["low"], f["close"].shift(1)
    tr = pd.concat([hi-lo, (hi-cl).abs(), (lo-cl).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def prep(f):
    f = f.sort_values("dt").reset_index(drop=True)
    f["day"] = f["dt"].dt.date
    f["rsi"] = rsi(f["close"]); f["sma20"] = f["close"].rolling(20).mean()
    f["sma50"] = f["close"].rolling(50).mean()
    m_, s_ = macd(f["close"]); f["macd"], f["macd_sig"] = m_, s_
    f["vol_avg"] = f["volume"].rolling(20).mean(); f["st_dir"] = supertrend(f)
    f["atr"] = atr_series(f)
    return f

def get_yf(interval, period):
    d = yf.download("^NSEI", interval=interval, period=period, progress=False).reset_index()
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = [c[0].lower().replace(" ", "") for c in d.columns]
    else:
        d.columns = [str(c).lower().replace(" ", "") for c in d.columns]
    tcol = next(c for c in d.columns if c in ("datetime", "date", "index", "timestamp"))
    d = d.rename(columns={tcol: "dt"}); d["dt"] = pd.to_datetime(d["dt"])
    try: d["dt"] = d["dt"].dt.tz_localize(None)
    except Exception: pass
    d["dt"] = d["dt"].astype("datetime64[ns]")
    return d[["dt", "open", "high", "low", "close", "volume"]]

print("Fetching 5-min data...")
raw5 = get_yf("5m", "60d")
df5  = prep(raw5)
r = raw5.set_index("dt")
f15 = pd.DataFrame({"open": r["open"].resample("15min").first(),
                    "high": r["high"].resample("15min").max(),
                    "low": r["low"].resample("15min").min(),
                    "close": r["close"].resample("15min").last()}).dropna().reset_index()
f15["st_dir"] = supertrend(f15)
m15, s15 = macd(f15["close"]); f15["macd_up15"] = m15 > s15
f15["usable_from"] = (f15["dt"] + pd.Timedelta(minutes=10)).astype("datetime64[ns]")
htf = pd.merge_asof(df5[["dt"]].sort_values("dt"),
                    f15[["usable_from","st_dir","macd_up15"]].sort_values("usable_from"),
                    left_on="dt", right_on="usable_from", direction="backward")
df5["htf_st"] = htf["st_dir"].values; df5["htf_macd"] = htf["macd_up15"].values

def htf_ok(frame, i, otype):
    row = frame.loc[i]
    if pd.isna(row["htf_st"]): return False
    if otype == "CE": return row["htf_st"] == 1 and bool(row["htf_macd"])
    return row["htf_st"] == -1 and not bool(row["htf_macd"])

DELTA, MODEL_BROKERAGE = 0.40, 20
UNITS, CAP_PER_LOT, MAX_LOTS = 65, 10000, 15
START_CAPITAL = 15000.0
FADE_PCT = 0.002
RUNGS = [150, 300, 500, 700, 850, 900] + list(range(1050, 300001, 150))
WINDOW_END = dtime(14, 30)
DAILY_LIMIT = -1000

def confidence(f, i, otype, brk):
    row, prev = f.iloc[i], f.iloc[i-1]; bd = 0
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
    conf = max(5, min(95, round(total/70*100)))
    sl = 100 + int(round(conf/100*(500-100)/50))*50
    return conf, max(100, min(500, sl))

def premium_est(spot):
    return round(spot * 0.14 * math.sqrt(4/365) * 0.4 * 0.98, 1)

def real_charges(buy_val, sell_val):
    brokerage = 20.0*2; stt = 0.001*sell_val
    txn = 0.0003503*(buy_val+sell_val); sebi = 0.000001*(buy_val+sell_val)
    stamp = 0.00003*buy_val; gst = 0.18*(brokerage+txn+sebi)
    return round(brokerage+stt+txn+sebi+stamp+gst, 2)

def simulate(atr_mult=None, sl_cap=1000, sl_floor=200):
    frame = df5
    capital, lots = START_CAPITAL, 1
    dr = {d: frame.index[frame["day"] == d].tolist() for d in frame["day"].unique()}
    all_d = sorted(frame["day"].unique())
    trades, day_net, sl_pts_used = [], {}, []

    for d in all_d[1:]:
        prior = [x for x in all_d if x < d]
        yd_hi = frame.loc[dr[prior[-1]], "high"].max()
        yd_lo = frame.loc[dr[prior[-1]], "low"].min()
        idxs = dr[d]
        or_rows = [i for i in idxs if frame.loc[i, "dt"].time() < dtime(10, 15)]
        or_hi = max(frame.loc[i, "high"] for i in or_rows) if or_rows else None
        or_lo = min(frame.loc[i, "low"] for i in or_rows) if or_rows else None
        _dte_mult = 1.41 if d.weekday() == 1 else 1.0
        daily_pnl = 0.0
        pos, cooldown = None, None
        day_hi_run = -1e18

        for k, i in enumerate(idxs):
            row = frame.loc[i]; t = row["dt"].time(); px = row["close"]
            last = k == len(idxs) - 1
            if pos:
                units = pos["lots"]*UNITS
                mv = (px-pos["entry"])*DELTA*units if pos["otype"] == "CE" else (pos["entry"]-px)*DELTA*units
                pnl = mv - MODEL_BROKERAGE
                if pnl > pos["peak"]: pos["peak"] = pnl
                peak = pos["peak"]
                scale = pos["lots"]
                floor = None
                for rung in RUNGS:
                    if peak >= rung*scale: floor = rung*scale
                    else: break
                booked = None
                if t >= dtime(15, 25) or last:
                    booked = (round(pnl, 2), "EOD")
                elif floor is not None and pnl < floor:
                    booked = (floor, "STEP")
                elif floor is None and pnl <= -pos["sl"]:
                    booked = (-pos["sl"], "SL")
                if booked:
                    val, status = booked
                    gross = val + MODEL_BROKERAGE
                    sell_val = max(0.0, pos["inv"] + gross)
                    chg = real_charges(pos["inv"], sell_val)
                    net = round(gross - chg, 2)
                    capital += net; daily_pnl += net
                    lots = min(MAX_LOTS, max(1, int(capital//CAP_PER_LOT))) if net > 0 else 1
                    trades.append(net); day_net[d] = day_net.get(d, 0.0) + net
                    if status == "SL": cooldown = row["dt"] + timedelta(minutes=10)
                    pos = None
            day_hi_prior = day_hi_run
            if row["high"] > day_hi_run: day_hi_run = row["high"]
            if daily_pnl <= DAILY_LIMIT: continue
            if pos: continue
            if not (dtime(10, 15) <= t <= WINDOW_END): continue
            if cooldown and row["dt"] < cooldown: continue

            otype = None
            if px > yd_hi: otype, brk = "CE", True
            elif px < yd_lo: otype, brk = "PE", True
            elif or_lo is not None and px < or_lo: otype, brk = "PE", True
            elif or_hi is not None and px > or_hi: otype, brk = "CE", True
            if t > dtime(12, 30) and otype != "PE" and day_hi_prior > 0 and px <= day_hi_prior*(1-FADE_PCT):
                otype, brk = "PE", True
            if otype is None: continue
            if not htf_ok(frame, i, otype): continue
            conf, sl_conf = confidence(frame, i, otype, brk)
            if atr_mult is None:
                sl_per_lot = sl_conf
            else:
                a = frame.loc[i, "atr"]
                if np.isnan(a): continue
                sl_per_lot = a * atr_mult * DELTA * UNITS
                sl_per_lot = max(sl_floor, min(sl_cap, sl_per_lot)) if sl_cap else max(sl_floor, sl_per_lot)
            sl_pts_used.append(sl_per_lot / (DELTA*UNITS))
            prem = premium_est(px) * _dte_mult
            afford = int(capital // (prem*UNITS))
            if afford < 1: continue
            use = min(lots, afford)
            pos = {"otype": otype, "entry": px, "sl": sl_per_lot*use, "lots": use,
                   "peak": -9e9, "inv": round(prem*use*UNITS, 2)}

    wins = [x for x in trades if x > 0]
    wr = len(wins)/len(trades)*100 if trades else 0
    g = sum(1 for v in day_net.values() if v > 0); rr = sum(1 for v in day_net.values() if v < 0)
    eq, pk, mdd = START_CAPITAL, START_CAPITAL, 0.0
    for d in sorted(day_net):
        eq += day_net[d]; pk = max(pk, eq); mdd = max(mdd, pk-eq)
    worst_day = min(day_net.values()) if day_net else 0.0
    return dict(capital=capital, n=len(trades), wr=wr, green=g, red=rr, mdd=mdd,
                worst_day=worst_day, worst_trade=min(trades) if trades else 0,
                avg_sl_pts=np.mean(sl_pts_used) if sl_pts_used else 0)

d0, d1 = df5["day"].min(), df5["day"].max()
print("\n" + "=" * 78)
print(f"ATR STOP-LOSS TEST | {d0} -> {d1} | Rs.15,000 | daily -1000 | real charges")
print("=" * 78)
for kw, name in [
    (dict(atr_mult=None),                       "A) CURRENT  conf-based Rs.100-500"),
    (dict(atr_mult=1.0),                        "B) ATR 1.0x (Rs.200-1000/lot)"),
    (dict(atr_mult=1.5),                        "C) ATR 1.5x (Rs.200-1000/lot)"),
    (dict(atr_mult=2.0),                        "D) ATR 2.0x (Rs.200-1000/lot)"),
    (dict(atr_mult=1.5, sl_cap=None),           "E) ATR 1.5x uncapped")]:
    r_ = simulate(**kw)
    print(f"\n{name}")
    print(f"   trades {r_['n']} | win {r_['wr']:.0f}% | green/red days {r_['green']}/{r_['red']} | avg SL room {r_['avg_sl_pts']:.1f} index pts")
    print(f"   worst trade Rs.{r_['worst_trade']:+,.0f} | worst day Rs.{r_['worst_day']:+,.0f} | max drawdown Rs.{r_['mdd']:,.0f}")
    print(f"   NET Rs.{r_['capital']-START_CAPITAL:+,.2f} -> final Rs.{r_['capital']:,.2f}")
print("\n" + "=" * 78)
