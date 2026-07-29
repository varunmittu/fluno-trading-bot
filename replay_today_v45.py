"""
Replay today (2026-07-08) 10:15-15:30 with v4.5 settings fully applied.
Compare: what actually happened (mixed old/new) vs v4.5 clean.

v4.5 = ATR 1.0x SL (200-1000/lot), NO daily limit, dense staircase
(150/250/300/450/500/700/850/900/+150), 15m MTF, 0.2% fade, expiry-day
trading, real charges.
"""
import os, math
from datetime import date, time as dtime, timedelta
import pandas as pd, numpy as np, yfinance as yf

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
    f["rsi"] = rsi(f["close"]); f["sma20"] = f["close"].rolling(20).mean()
    f["sma50"] = f["close"].rolling(50).mean()
    m_, s_ = macd(f["close"]); f["macd"], f["macd_sig"] = m_, s_
    f["vol_avg"] = f["volume"].rolling(20).mean(); f["st_dir"] = supertrend(f)
    pc = f["close"].shift(1)
    tr = pd.concat([f["high"]-f["low"], (f["high"]-pc).abs(), (f["low"]-pc).abs()], axis=1).max(axis=1)
    f["atr"] = tr.rolling(14).mean()
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
raw5 = get_yf("5m", "10d")
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
UNITS = 65
FADE_PCT = 0.002
RUNGS = [150, 250, 300, 450, 500, 700, 850, 900] + list(range(1050, 300001, 150))
WINDOW_END = dtime(14, 30)

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
    return max(5, min(95, round(total/70*100)))

def premium_est(spot):
    return round(spot * 0.14 * math.sqrt(4/365) * 0.4 * 0.98, 1)

def real_charges(buy_val, sell_val):
    brokerage = 20.0*2; stt = 0.001*sell_val
    txn = 0.0003503*(buy_val+sell_val); sebi = 0.000001*(buy_val+sell_val)
    stamp = 0.00003*buy_val; gst = 0.18*(brokerage+txn+sebi)
    return round(brokerage+stt+txn+sebi+stamp+gst, 2)

def simulate_today(daily_limit):
    frame = df5[df5["day"] == date(2026, 7, 8)].reset_index(drop=True)
    if len(frame) == 0:
        return None

    prior_day = df5[df5["day"] == date(2026, 7, 7)]
    yd_hi = prior_day["high"].max()
    yd_lo = prior_day["low"].min()

    or_rows = [i for i in range(len(frame)) if frame.loc[i, "dt"].time() < dtime(10, 15)]
    or_hi = max(frame.loc[i, "high"] for i in or_rows) if or_rows else None
    or_lo = min(frame.loc[i, "low"] for i in or_rows) if or_rows else None

    _dte_mult = 1.41 if date(2026, 7, 8).weekday() == 1 else 1.0
    daily_pnl, trades = 0.0, []
    pos, cooldown = None, None
    day_hi_run = -1e18
    trades_detail = []

    for k in range(len(frame)):
        i = k
        row = frame.loc[i]; t = row["dt"].time(); px = row["close"]
        last = k == len(frame) - 1
        if pos:
            units = pos["lots"] * UNITS
            mv = (px-pos["entry"])*DELTA*units if pos["otype"] == "CE" else (pos["entry"]-px)*DELTA*units
            pnl = mv - MODEL_BROKERAGE
            if pnl > pos["peak"]: pos["peak"] = pnl
            peak = pos["peak"]
            floor = None
            for rng in RUNGS:
                if peak >= rng: floor = rng
                else: break
            booked = None
            if t >= dtime(15, 25) or last:
                booked = (round(pnl, 2), "EOD")
            elif floor is not None and pnl < floor:
                booked = (floor, "STAIR")
            elif floor is None and pnl <= -pos["sl"]:
                booked = (-pos["sl"], "SL")
            if booked:
                val, status = booked
                gross = val + MODEL_BROKERAGE
                sell_val = max(0.0, pos["inv"] + gross)
                chg = real_charges(pos["inv"], sell_val)
                net = round(gross - chg, 2)
                daily_pnl += net
                trades.append(net)
                trades_detail.append(f"{t.strftime('%H:%M')} {pos['otype']} entry {pos['entry']:.0f} -> {px:.0f} {status:5s} net Rs.{net:+.0f}")
                if status == "SL": cooldown = row["dt"] + timedelta(minutes=10)
                pos = None
        day_hi_prior = day_hi_run
        if row["high"] > day_hi_run: day_hi_run = row["high"]
        if daily_pnl <= daily_limit: continue
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
        conf = confidence(frame, i, otype, brk)
        a = frame.loc[i, "atr"]
        if np.isnan(a): continue
        sl = max(200, min(1000, round(a * 1.0 * DELTA * UNITS / 10) * 10))
        prem = premium_est(px) * _dte_mult
        pos = {"otype": otype, "entry": px, "sl": sl, "peak": -9e9,
               "inv": round(prem*UNITS, 2), "lots": 1}

    return dict(net=sum(trades), n=len(trades), detail=trades_detail, daily_pnl=daily_pnl)

result = simulate_today(-10**9)  # no daily limit (v4.5)
if result:
    print("\n" + "="*70)
    print("REPLAY 2026-07-08 WITH v4.5 (ATR SL, NO DAILY LIMIT, DENSE RUNGS)")
    print("="*70)
    for line in result["detail"]:
        print(f"  {line}")
    print(f"\nNET: Rs.{result['net']:+,.2f} | trades: {result['n']} | daily_pnl: Rs.{result['daily_pnl']:+,.2f}")
    print("="*70)
    print(f"\nACTUAL TODAY (mixed old/new): -Rs.357 | 13 trades")
    print(f"v4.5 CLEAN ALL DAY:          Rs.{result['net']:+,.2f} | {result['n']} trades")
    print("="*70)
