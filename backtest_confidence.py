"""
CONFIDENCE SCORE 0-9 GATE TEST (2026-07-14, Sai's design).
Score each signal 0-9 from 9 checks (halves allowed):
  1. 5m supertrend aligned      (1)
  2. 5m MACD aligned            (1)
  3. 15m supertrend aligned     (1)   <- always 1 at entry (MTF filter)
  4. 15m MACD aligned           (1)   <- always 1 at entry (MTF filter)
  5. RSI room (CE<50 / PE>50 =1, within 10 of midline = 0.5)
  6. Volume (>1.5x avg = 1, >1.1x = 0.5)
  7. SMA20 vs SMA50 aligned     (1)
  8. Price vs SMA50 aligned     (1)
  9. Breakout strength (>=0.05% past level = 1, any break = 0.5)

Sai's spec: Telegram alert at score >= 7, AUTO entry only at >= 7.5.
Variants (all on the LIVE config: dense rungs, ATR SL, daily -1000, 1 lot,
5-min candle-close decisions = live behaviour since 2026-07-13 fix):
  A CURRENT   = no score gate (baseline)
  B GATE 7.0  = enter only if score >= 7.0
  C GATE 7.5  = enter only if score >= 7.5  (Sai's auto-mode spec)
  D GATE 8.0  = enter only if score >= 8.0
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

def conf_score(frame, i, otype, brk_margin_pct):
    """Same 9-check score the live bot will use. Returns 0-9 in 0.5 steps."""
    row = frame.loc[i]
    s = 0.0
    bull = otype == "CE"
    # 1-2: 5m supertrend + MACD
    if (row["st_dir"] == 1) == bull:                       s += 1
    if (row["macd"] > row["macd_sig"]) == bull:            s += 1
    # 3-4: 15m supertrend + MACD (MTF filter guarantees these at entry)
    if not pd.isna(row["htf_st"]):
        if (row["htf_st"] == 1) == bull:                   s += 1
        if bool(row["htf_macd"]) == bull:                  s += 1
    # 5: RSI room
    rv = row["rsi"]
    if not np.isnan(rv):
        if (rv < 50) if bull else (rv > 50):               s += 1
        elif (rv < 60) if bull else (rv > 40):             s += 0.5
    # 6: volume
    if row["volume"] > row["vol_avg"] * 1.5:               s += 1
    elif row["volume"] > row["vol_avg"] * 1.1:             s += 0.5
    # 7-8: SMA structure
    if (row["sma20"] > row["sma50"]) == bull:              s += 1
    if (row["close"] > row["sma50"]) == bull:              s += 1
    # 9: breakout strength
    if brk_margin_pct >= 0.0005:                           s += 1
    else:                                                  s += 0.5
    return s

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

def simulate(min_score=None):
    frame = df5
    dr = {d: frame.index[frame["day"] == d].tolist() for d in frame["day"].unique()}
    all_d = sorted(frame["day"].unique())
    trades, day_net = [], {}
    scores_taken, scores_skipped = [], []

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
                        cooldown = row["dt"] + timedelta(minutes=10)
                    pos = None
            day_hi_prior = day_hi_run
            if row["high"] > day_hi_run: day_hi_run = row["high"]
            if pos: continue
            if day_net.get(d, 0.0) <= DAILY_LIMIT: continue
            if not (dtime(10, 15) <= t <= WINDOW_END): continue
            if cooldown and row["dt"] < cooldown: continue

            otype, level = None, None
            if px > yd_hi: otype, level = "CE", yd_hi
            elif px < yd_lo: otype, level = "PE", yd_lo
            elif or_lo is not None and px < or_lo: otype, level = "PE", or_lo
            elif or_hi is not None and px > or_hi: otype, level = "CE", or_hi
            if t > dtime(12, 30) and otype != "PE" and day_hi_prior > 0 and px <= day_hi_prior*(1-FADE_PCT):
                otype, level = "PE", day_hi_prior*(1-FADE_PCT)
            if otype is None: continue
            if not htf_ok(frame, i, otype): continue
            margin = abs(px - level) / px
            sc = conf_score(frame, i, otype, margin)
            if min_score is not None and sc < min_score:
                scores_skipped.append(sc); continue
            scores_taken.append(sc)
            a = frame.loc[i, "atr"]
            if np.isnan(a): continue
            sl = max(200, min(1000, round(a * 1.0 * DELTA * UNITS / 10) * 10))
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
                avg_loss=np.mean(loss) if loss else 0,
                taken=scores_taken, skipped=scores_skipped)

VARIANTS = {
    "A CURRENT (no score gate)": None,
    "B GATE 7.0":                7.0,
    "C GATE 7.5 (Sai's spec)":   7.5,
    "D GATE 8.0":                8.0,
}

d0, d1 = df5["day"].min(), df5["day"].max()
print("\n" + "=" * 78)
print(f"CONFIDENCE GATE TEST | {d0} -> {d1} | live config, 1 lot, real charges")
print("=" * 78)
base = None
for name, gate in VARIANTS.items():
    r_ = simulate(min_score=gate)
    if base is None: base = r_
    print(f"\n{name}")
    print(f"   NET Rs.{r_['net']:+,.2f} | trades {r_['n']} | win {r_['wr']:.0f}% | green/red {r_['green']}/{r_['red']}")
    print(f"   avg win Rs.{r_['avg_win']:+,.0f} avg loss Rs.{r_['avg_loss']:+,.0f} | worst day Rs.{r_['worst_day']:+,.0f} | maxDD Rs.{r_['mdd']:,.0f}")
    if r_["taken"]:
        ts = pd.Series(r_["taken"])
        print(f"   scores of taken entries: min {ts.min():.1f} / median {ts.median():.1f} / max {ts.max():.1f}")
    if gate is not None and r_["skipped"]:
        ss = pd.Series(r_["skipped"])
        print(f"   signals skipped by gate: {len(ss)} (median score {ss.median():.1f})")

# distribution of scores on ALL signals (variant A takes everything)
if base["taken"]:
    ts = pd.Series(base["taken"])
    print("\nScore distribution of ALL entered signals (no gate):")
    print(ts.value_counts().sort_index().to_string())
print("\n" + "=" * 78)
