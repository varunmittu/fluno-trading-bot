"""
FIX-THE-LOSSES test (2026-07-13). Live paper results were losing:
avg win +307 vs avg loss -567, 33 trades / Rs.2,187 charges in 5 days,
5 same-direction SLs on 07-10. Variants target exactly those 3 problems,
tested on the LIVE config: dense rungs, ATR 1.0x SL (200-1000), daily -1000.

A CURRENT      = live config as-is (baseline)
B DIR-LOCK     = after 2 SLs in the same direction in a day, that direction
                 is blocked for the rest of the day
C TIGHT-SL     = skip the trade entirely if ATR SL would be > Rs.600
                 (wide-stop trades were the big losers)
D COOL-30      = SL cooldown 10 -> 30 min
E MAX-4        = max 4 trades per day (charge churn control)
F B+C          = direction lock + tight SL
G B+C+D        = all three loss controls
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
UNITS = 65
FADE_PCT = 0.002
WINDOW_END = dtime(14, 30)
DAILY_LIMIT = -1000

RUNGS = [150, 250, 300, 450, 500, 700, 850, 900] + list(range(1050, 300001, 150))

def premium_est(spot):
    return round(spot * 0.14 * math.sqrt(4/365) * 0.4 * 0.98, 1)

def real_charges(buy_val, sell_val):
    brokerage = 20.0*2; stt = 0.001*sell_val
    txn = 0.0003503*(buy_val+sell_val); sebi = 0.000001*(buy_val+sell_val)
    stamp = 0.00003*buy_val; gst = 0.18*(brokerage+txn+sebi)
    return round(brokerage+stt+txn+sebi+stamp+gst, 2)

def simulate(dir_lock=False, sl_skip=None, cooldown_min=10, max_trades=99):
    frame = df5
    dr = {d: frame.index[frame["day"] == d].tolist() for d in frame["day"].unique()}
    all_d = sorted(frame["day"].unique())
    trades, day_net = [], {}

    for d in all_d[1:]:
        prior = [x for x in all_d if x < d]
        yd_hi = frame.loc[dr[prior[-1]], "high"].max()
        yd_lo = frame.loc[dr[prior[-1]], "low"].min()
        idxs = dr[d]
        or_rows = [i for i in idxs if frame.loc[i, "dt"].time() < dtime(10, 15)]
        or_hi = max(frame.loc[i, "high"] for i in or_rows) if or_rows else None
        or_lo = min(frame.loc[i, "low"] for i in or_rows) if or_rows else None
        _dte_mult = 1.41 if d.weekday() == 1 else 1.0
        pos, cooldown = None, None
        day_hi_run = -1e18
        sl_count = {"CE": 0, "PE": 0}
        n_today = 0

        for k, i in enumerate(idxs):
            row = frame.loc[i]; t = row["dt"].time(); px = row["close"]
            last = k == len(idxs) - 1
            if pos:
                mv = (px-pos["entry"])*DELTA*UNITS if pos["otype"] == "CE" else (pos["entry"]-px)*DELTA*UNITS
                pnl = mv - MODEL_BROKERAGE
                if pnl > pos["peak"]: pos["peak"] = pnl
                peak = pos["peak"]
                floor = None
                for rg in RUNGS:
                    if peak >= rg: floor = rg
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
                    trades.append(net); day_net[d] = day_net.get(d, 0.0) + net
                    if status == "SL":
                        cooldown = row["dt"] + timedelta(minutes=cooldown_min)
                        sl_count[pos["otype"]] += 1
                    pos = None
            day_hi_prior = day_hi_run
            if row["high"] > day_hi_run: day_hi_run = row["high"]
            if pos: continue
            if day_net.get(d, 0.0) <= DAILY_LIMIT: continue
            if n_today >= max_trades: continue
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
            if dir_lock and sl_count[otype] >= 2: continue
            if not htf_ok(frame, i, otype): continue
            a = frame.loc[i, "atr"]
            if np.isnan(a): continue
            sl = max(200, min(1000, round(a * 1.0 * DELTA * UNITS / 10) * 10))
            if sl_skip and sl > sl_skip: continue
            prem = premium_est(px) * _dte_mult
            pos = {"otype": otype, "entry": px, "sl": sl, "peak": -9e9,
                   "inv": round(prem*UNITS, 2)}
            n_today += 1

    wins = [x for x in trades if x > 0]; loss = [x for x in trades if x <= 0]
    wr = len(wins)/len(trades)*100 if trades else 0
    worst_day = min(day_net.values()) if day_net else 0.0
    eq, pk, mdd = 0.0, 0.0, 0.0
    for d in sorted(day_net):
        eq += day_net[d]; pk = max(pk, eq); mdd = max(mdd, pk-eq)
    g = sum(1 for v in day_net.values() if v > 0); rr = sum(1 for v in day_net.values() if v < 0)
    return dict(net=sum(trades), n=len(trades), wr=wr, worst_day=worst_day,
                mdd=mdd, green=g, red=rr,
                avg_win=np.mean(wins) if wins else 0,
                avg_loss=np.mean(loss) if loss else 0)

VARIANTS = {
    "A CURRENT (baseline)":            dict(),
    "B DIR-LOCK (2 SL same dir/day)":  dict(dir_lock=True),
    "C TIGHT-SL (skip if SL>600)":     dict(sl_skip=600),
    "D COOL-30 (30min SL cooldown)":   dict(cooldown_min=30),
    "E MAX-4 trades/day":              dict(max_trades=4),
    "F B+C":                           dict(dir_lock=True, sl_skip=600),
    "G B+C+D":                         dict(dir_lock=True, sl_skip=600, cooldown_min=30),
}

d0, d1 = df5["day"].min(), df5["day"].max()
print("\n" + "=" * 78)
print(f"FIX-LOSSES TEST | {d0} -> {d1} | dense rungs, ATR SL, daily -1000, 1 lot")
print("=" * 78)
for name, kw in VARIANTS.items():
    r_ = simulate(**kw)
    print(f"\n{name}")
    print(f"   NET Rs.{r_['net']:+,.2f} | trades {r_['n']} | win {r_['wr']:.0f}% | green/red {r_['green']}/{r_['red']}")
    print(f"   avg win Rs.{r_['avg_win']:+,.0f} avg loss Rs.{r_['avg_loss']:+,.0f} | worst day Rs.{r_['worst_day']:+,.0f} | maxDD Rs.{r_['mdd']:,.0f}")
print("\n" + "=" * 78)
