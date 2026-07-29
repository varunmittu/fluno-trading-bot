"""
6-MONTH REALITY CHECK (2026-07-20, Sai: "is this actually working or giving me
a loss — past 6 months with the six bots and AI strategy").

DATA HONESTY (read this before trusting any number below):
  * Real 5-min NIFTY data only exists for the LAST 60 DAYS. Yahoo hard-caps
    intraday history at 60 days; Zerodha's historical API returns
    PermissionException on Sai's free plan (verified 2026-07-20). So a true
    6-month 5-min backtest is IMPOSSIBLE right now. Anyone claiming one is
    fabricating it.
  * PERIOD A (Apr 28 - Jul 20, ~60d): REAL 5-min candles, exact live method
    (5-min close decisions, ATR SL, dense staircase, real charges). TRUST THIS.
  * PERIOD B (Jan 21 - Apr 27, ~3.5mo): HOURLY candles only — an APPROXIMATION.
    5-min ATR is estimated from hourly ATR / sqrt(12) (volatility time-scaling).
    Intra-candle path is unknown, so the sim checks the STOP LOSS FIRST using
    the candle's adverse extreme (pessimistic/conservative ordering) before
    letting the staircase see the favourable extreme. Treat Period B as
    "did the directional edge exist", NOT as a precise rupee figure.

WHAT IS ACTUALLY BEING TESTED:
  BASELINE  = what is LIVE right now: breakout ladder v4.2-v4.5 + 15m MTF
              filter + 9-point confidence gate >= 7.0.
  COUNCIL   = the 6 mini-bot ensemble (ensemble_bots.py) used AS THE ENTRY
              GATE instead. NOTE: this is NOT what runs live. The council is
              currently DISPLAY-ONLY, because the 60-day test showed gating on
              it performs worse. This script re-checks that over a longer
              horizon, so the decision is based on data, not on one window.

Also runs an OUT-OF-SAMPLE SPLIT on the real 5-min data (first half vs second
half) — the standard check that a result isn't just curve-fitted to one period.
"""
import os, math
from datetime import time as dtime, timedelta
import pandas as pd, numpy as np
import yfinance as yf

os.chdir(os.path.dirname(os.path.abspath(__file__)))
import ensemble_bots as eb

# ── indicators (identical math to app.py) ───────────────────────────────────
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
    f["sma50"] = f["close"].rolling(50).mean(); f["sma200"] = f["close"].rolling(200).mean()
    m_, s_ = macd(f["close"]); f["macd"], f["macd_sig"] = m_, s_
    f["vol_avg"] = f["volume"].rolling(20).mean(); f["st_dir"] = supertrend(f)
    pc = f["close"].shift(1)
    tr = pd.concat([f["high"]-f["low"], (f["high"]-pc).abs(), (f["low"]-pc).abs()], axis=1).max(axis=1)
    f["atr14"] = tr.rolling(14).mean()
    return f

def get_yf(interval, period, symbol="^NSEI"):
    d = yf.download(symbol, interval=interval, period=period, progress=False).reset_index()
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = [c[0].lower().replace(" ", "") for c in d.columns]
    else:
        d.columns = [str(c).lower().replace(" ", "") for c in d.columns]
    tcol = next(c for c in d.columns if c in ("datetime", "date", "index", "timestamp"))
    d = d.rename(columns={tcol: "dt"}); d["dt"] = pd.to_datetime(d["dt"])
    try: d["dt"] = d["dt"].dt.tz_localize(None)
    except Exception: pass
    d["dt"] = d["dt"].astype("datetime64[ns]")
    return d

DELTA, MODEL_BROKERAGE, UNITS = 0.40, 20, 65
FADE_PCT, DAILY_LIMIT = 0.002, -1000
# FIXED 2026-07-20: entry window must match the LIVE bot, which enters until
# 15:00 (extended 2026-07-06 by backtest_window.py: till 15:00 = +Rs.47,378 vs
# till 14:30 = +Rs.37,213). Earlier scripts here still used 14:30 and were
# therefore simulating a DIFFERENT bot than the one running.
WINDOW_START, WINDOW_END, EOD = dtime(10, 15), dtime(15, 0), dtime(15, 25)
RUNGS = [150, 250, 300, 450, 500, 700, 850, 900] + list(range(1050, 300001, 150))

def premium_est(spot):
    return round(spot * 0.14 * math.sqrt(4/365) * 0.4 * 0.98, 1)

def real_charges(buy_val, sell_val):
    brokerage = 20.0*2; stt = 0.001*sell_val
    txn = 0.0003503*(buy_val+sell_val); sebi = 0.000001*(buy_val+sell_val)
    stamp = 0.00003*buy_val; gst = 0.18*(brokerage+txn+sebi)
    return round(brokerage+stt+txn+sebi+stamp+gst, 2)

def conf_score_9pt(row, otype, brk_margin_pct):
    s = 0.0; bull = otype == "CE"
    if (row["st_dir"] == 1) == bull: s += 1
    if (row["macd"] > row["macd_sig"]) == bull: s += 1
    if not pd.isna(row["htf_st"]):
        if (row["htf_st"] == 1) == bull: s += 1
        if bool(row["htf_macd"]) == bull: s += 1
    rv = row["rsi"]
    if not np.isnan(rv):
        if (rv < 50) if bull else (rv > 50): s += 1
        elif (rv < 60) if bull else (rv > 40): s += 0.5
    if row["volume"] > row["vol_avg"] * 1.5: s += 1
    elif row["volume"] > row["vol_avg"] * 1.1: s += 0.5
    if (row["sma20"] > row["sma50"]) == bull: s += 1
    if (row["close"] > row["sma50"]) == bull: s += 1
    if brk_margin_pct >= 0.0005: s += 1
    else: s += 0.5
    return s

def build_frame(interval, period, htf_rule):
    """htf_rule: '15min' for 5-min data, '3H' for hourly data (same 3x ratio)."""
    raw = get_yf(interval, period)[["dt","open","high","low","close","volume"]]
    f = prep(raw)
    r = raw.set_index("dt")
    htf = pd.DataFrame({"open": r["open"].resample(htf_rule).first(),
                        "high": r["high"].resample(htf_rule).max(),
                        "low":  r["low"].resample(htf_rule).min(),
                        "close":r["close"].resample(htf_rule).last()}).dropna().reset_index()
    htf["st_dir"] = supertrend(htf)
    mh, sh = macd(htf["close"]); htf["macd_up"] = mh > sh
    lag = pd.Timedelta(minutes=10) if htf_rule == "15min" else pd.Timedelta(hours=2)
    htf["usable_from"] = (htf["dt"] + lag).astype("datetime64[ns]")
    j = pd.merge_asof(f[["dt"]].sort_values("dt"),
                      htf[["usable_from","st_dir","macd_up"]].sort_values("usable_from"),
                      left_on="dt", right_on="usable_from", direction="backward")
    f["htf_st"] = j["st_dir"].values; f["htf_macd"] = j["macd_up"].values
    return f

def simulate(frame, gate="baseline", min_score=7.0, min_conf_pct=60, weights=None,
             intracandle=False, atr_scale=1.0, day_filter=None, vix_map=None):
    """
    intracandle=True  -> hourly mode: check SL against the candle's ADVERSE
                         extreme FIRST (pessimistic), then let staircase see
                         the favourable extreme. Used only for Period B.
    atr_scale         -> multiply candle ATR to approximate 5-min ATR (hourly: 1/sqrt(12)).
    """
    days = [d for d in sorted(frame["day"].unique()) if (day_filter is None or day_filter(d))]
    dr = {d: frame.index[frame["day"] == d].tolist() for d in frame["day"].unique()}
    all_days_sorted = sorted(frame["day"].unique())
    trades, day_net = [], {}

    for d in days:
        prior = [x for x in all_days_sorted if x < d]
        if len(prior) < 2: continue
        yd_hi = frame.loc[dr[prior[-1]], "high"].max()
        yd_lo = frame.loc[dr[prior[-1]], "low"].min()
        idxs = dr[d]
        or_rows = [i for i in idxs if frame.loc[i, "dt"].time() < dtime(10, 15)]
        or_hi = max(frame.loc[i, "high"] for i in or_rows) if or_rows else None
        or_lo = min(frame.loc[i, "low"] for i in or_rows) if or_rows else None
        dte_mult = 1.41 if d.weekday() == 1 else 1.0
        vix_today = (vix_map or {}).get(d)
        pos, cooldown, day_hi_run = None, None, -1e18

        for k, i in enumerate(idxs):
            row = frame.loc[i]; px = row["close"]; last = k == len(idxs) - 1
            # decision time = candle close
            t_close = (row["dt"] + (pd.Timedelta(hours=1) if intracandle else pd.Timedelta(minutes=5))).time()

            if pos:
                booked = None
                if intracandle:
                    # pessimistic ordering: adverse extreme first
                    adverse = row["low"] if pos["otype"] == "CE" else row["high"]
                    adv_mv = ((adverse - pos["entry"]) if pos["otype"] == "CE"
                              else (pos["entry"] - adverse)) * DELTA * UNITS - MODEL_BROKERAGE
                    fav = row["high"] if pos["otype"] == "CE" else row["low"]
                    fav_mv = ((fav - pos["entry"]) if pos["otype"] == "CE"
                              else (pos["entry"] - fav)) * DELTA * UNITS - MODEL_BROKERAGE
                    floor_before = None
                    for rg in RUNGS:
                        if pos["peak"] >= rg: floor_before = rg
                        else: break
                    if floor_before is None and adv_mv <= -pos["sl"]:
                        booked = (-pos["sl"], "SL")
                    elif floor_before is not None and adv_mv < floor_before:
                        booked = (floor_before, "STAIR")
                    else:
                        if fav_mv > pos["peak"]: pos["peak"] = fav_mv
                cur_mv = ((px - pos["entry"]) if pos["otype"] == "CE"
                          else (pos["entry"] - px)) * DELTA * UNITS
                pnl = cur_mv - MODEL_BROKERAGE
                if booked is None:
                    if pnl > pos["peak"]: pos["peak"] = pnl
                    floor = None
                    for rg in RUNGS:
                        if pos["peak"] >= rg: floor = rg
                        else: break
                    if t_close >= EOD or last: booked = (round(pnl, 2), "EOD")
                    elif floor is not None and pnl < floor: booked = (floor, "STAIR")
                    elif floor is None and pnl <= -pos["sl"]: booked = (-pos["sl"], "SL")
                if booked:
                    val, status = booked
                    gross = val + MODEL_BROKERAGE
                    sell_val = max(0.0, pos["inv"] + gross)
                    net = round(gross - real_charges(pos["inv"], sell_val), 2)
                    trades.append(net); day_net[d] = day_net.get(d, 0.0) + net
                    if status == "SL":
                        cooldown = row["dt"] + timedelta(minutes=10)
                    pos = None

            day_hi_prior = day_hi_run
            if row["high"] > day_hi_run: day_hi_run = row["high"]
            if pos: continue
            if day_net.get(d, 0.0) <= DAILY_LIMIT: continue
            if not (WINDOW_START <= t_close <= WINDOW_END): continue
            if cooldown and row["dt"] < cooldown: continue

            otype, level = None, None
            if px > yd_hi: otype, level = "CE", yd_hi
            elif px < yd_lo: otype, level = "PE", yd_lo
            elif or_lo is not None and px < or_lo: otype, level = "PE", or_lo
            elif or_hi is not None and px > or_hi: otype, level = "CE", or_hi
            if t_close > dtime(12, 30) and otype != "PE" and day_hi_prior > 0 and px <= day_hi_prior*(1-FADE_PCT):
                otype, level = "PE", day_hi_prior*(1-FADE_PCT)
            if otype is None: continue
            if pd.isna(row["htf_st"]): continue
            if otype == "CE" and not (row["htf_st"] == 1 and bool(row["htf_macd"])): continue
            if otype == "PE" and not (row["htf_st"] == -1 and not bool(row["htf_macd"])): continue

            margin = abs(px - level) / px
            if gate == "baseline":
                if conf_score_9pt(row, otype, margin) < min_score: continue
            else:
                sub = frame.iloc[max(0, i-60):i+1].reset_index(drop=True)
                if len(sub) < 55: continue
                st15 = (row["htf_st"] == 1) if otype == "CE" else (row["htf_st"] == -1)
                res = eb.mother_decide(otype, level, px, sub, st15=bool(st15),
                                       macd15_up=bool(row["htf_macd"]), vix=vix_today,
                                       weights=weights)
                if res["confidence_pct"] < min_conf_pct: continue

            a = row["atr14"]
            if np.isnan(a): continue
            sl = max(200, min(1000, round(a * atr_scale * 1.0 * DELTA * UNITS / 10) * 10))
            pos = {"otype": otype, "entry": px, "sl": sl, "peak": -9e9,
                   "inv": round(premium_est(px) * dte_mult * UNITS, 2)}

    wins = [x for x in trades if x > 0]; loss = [x for x in trades if x <= 0]
    eq, pk, mdd = 0.0, 0.0, 0.0
    for dd in sorted(day_net):
        eq += day_net[dd]; pk = max(pk, eq); mdd = max(mdd, pk-eq)
    return dict(net=sum(trades), n=len(trades),
                wr=(len(wins)/len(trades)*100 if trades else 0),
                worst_day=(min(day_net.values()) if day_net else 0.0), mdd=mdd,
                green=sum(1 for v in day_net.values() if v > 0),
                red=sum(1 for v in day_net.values() if v < 0),
                avg_win=(np.mean(wins) if wins else 0),
                avg_loss=(np.mean(loss) if loss else 0),
                per_trade=(sum(trades)/len(trades) if trades else 0),
                days=len(day_net))

def show(name, r):
    print(f"\n{name}")
    print(f"   NET Rs.{r['net']:+,.0f} | trades {r['n']} | win {r['wr']:.0f}% | "
          f"Rs.{r['per_trade']:+,.0f}/trade | green/red days {r['green']}/{r['red']}")
    print(f"   avg win Rs.{r['avg_win']:+,.0f} | avg loss Rs.{r['avg_loss']:+,.0f} | "
          f"worst day Rs.{r['worst_day']:+,.0f} | maxDD Rs.{r['mdd']:,.0f}")

print("Fetching real 5-min data (last 60 days — the accurate window)...")
f5 = build_frame("5m", "60d", "15min")
print("Fetching 6-month hourly data (approximation for the older months)...")
f1h = build_frame("1h", "6mo", "3h")

try:
    vd = get_yf("1d", "6mo", "^INDIAVIX")[["dt","close"]]
    vd["day"] = vd["dt"].dt.date
    VIX = dict(zip(vd["day"], vd["close"]))
except Exception:
    VIX = {}

d5_min, d5_max = f5["day"].min(), f5["day"].max()
print("\n" + "="*84)
print("PART 1 — REAL 5-MIN DATA (exact live method). THIS IS THE TRUSTWORTHY TEST.")
print(f"          {d5_min} -> {d5_max}")
print("="*84)
b5 = simulate(f5, gate="baseline", vix_map=VIX)
show("LIVE STRATEGY (what actually runs now): breakout + 15m filter + 9-pt gate>=7.0", b5)
c5 = simulate(f5, gate="council", min_conf_pct=60, vix_map=VIX)
show("COUNCIL-AS-GATE (6 mini-bots decide entries) — NOT live, tested only", c5)

# out-of-sample split
days5 = sorted(f5["day"].unique())
mid = days5[len(days5)//2]
print("\n" + "-"*84)
print(f"OUT-OF-SAMPLE SPLIT (guards against curve-fitting): first half vs second half, split at {mid}")
print("-"*84)
show("LIVE STRATEGY — first half",  simulate(f5, gate="baseline", vix_map=VIX, day_filter=lambda d: d <  mid))
show("LIVE STRATEGY — second half", simulate(f5, gate="baseline", vix_map=VIX, day_filter=lambda d: d >= mid))
show("COUNCIL GATE — first half",   simulate(f5, gate="council", min_conf_pct=60, vix_map=VIX, day_filter=lambda d: d <  mid))
show("COUNCIL GATE — second half",  simulate(f5, gate="council", min_conf_pct=60, vix_map=VIX, day_filter=lambda d: d >= mid))

older = d5_min
print("\n" + "="*84)
print("PART 2 — 6-MONTH VIEW. Older months use HOURLY candles = APPROXIMATION ONLY.")
print(f"          Hourly ATR scaled by 1/sqrt(12) to approximate the 5-min ATR stop.")
print(f"          Stop-loss checked against the adverse extreme FIRST (pessimistic).")
print("="*84)
hb_old = simulate(f1h, gate="baseline", intracandle=True, atr_scale=1/math.sqrt(12),
                  vix_map=VIX, day_filter=lambda d: d < older)
show(f"LIVE STRATEGY — older months only (hourly approx, before {older})", hb_old)
hc_old = simulate(f1h, gate="council", min_conf_pct=60, intracandle=True, atr_scale=1/math.sqrt(12),
                  vix_map=VIX, day_filter=lambda d: d < older)
show(f"COUNCIL GATE — older months only (hourly approx, before {older})", hc_old)

print("\n" + "="*84)
print("BOTTOM LINE")
print("="*84)
combined_live = b5["net"] + hb_old["net"]
combined_council = c5["net"] + hc_old["net"]
print(f"LIVE STRATEGY   : real 60d Rs.{b5['net']:+,.0f}  +  older-months approx Rs.{hb_old['net']:+,.0f}"
      f"  =  ~Rs.{combined_live:+,.0f} over ~6 months (1 lot)")
print(f"COUNCIL AS GATE : real 60d Rs.{c5['net']:+,.0f}  +  older-months approx Rs.{hc_old['net']:+,.0f}"
      f"  =  ~Rs.{combined_council:+,.0f}")
print(f"\nPer-trade quality (the fair comparison, ignores trade-count inflation):")
print(f"   LIVE    Rs.{b5['per_trade']:+,.0f}/trade (real 5-min data)")
print(f"   COUNCIL Rs.{c5['per_trade']:+,.0f}/trade (real 5-min data)")
print("\nReminder: the council is DISPLAY-ONLY in the live bot. These council")
print("numbers are a test of whether it SHOULD gate entries — not what ran.")
print("="*84)
