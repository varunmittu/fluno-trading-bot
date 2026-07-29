"""
ENSEMBLE COUNCIL BACKTEST (2026-07-20). Same methodology as every other
backtest in this project: 60 days of real 5-min ^NSEI data, real Zerodha F&O
charges, dense staircase rungs, ATR(1.0x) stop-loss, daily limit -1000,
1 lot, live-identical 5-min decision timing. Only the ENTRY GATE changes:
  BASELINE = current live gate (9-point score >= 7.0, from confidence_score9)
  ENSEMBLE = new 6-mini-bot mother_decide() gate, at a few confidence
             thresholds, using ensemble_bots.py DIRECTLY (not reimplemented)
             so backtest and live math can never drift apart.

Also runs a small hand-picked weight-preset search (not full combinatorial —
60 days is a small sample, so this deliberately avoids overfitting to noise)
and reports the best preset. Saves nothing automatically; app.py integration
and ensemble_weights.json are only written after Sai/Claude reviews these
numbers, matching this project's "backtest before changing" rule.
"""
import os, math
from datetime import time as dtime, timedelta
import pandas as pd, numpy as np
import yfinance as yf

os.chdir(os.path.dirname(os.path.abspath(__file__)))
import ensemble_bots as eb

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

print("Fetching 5-min NIFTY data...")
raw5 = get_yf("5m", "60d")[["dt", "open", "high", "low", "close", "volume"]]
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

print("Fetching daily India VIX (best-effort, VIX moves slowly so daily is fine)...")
try:
    vix_daily = get_yf("1d", "60d", symbol="^INDIAVIX")[["dt", "close"]]
    vix_daily["day"] = vix_daily["dt"].dt.date
    vix_map = dict(zip(vix_daily["day"], vix_daily["close"]))
except Exception as e:
    print(f"  VIX fetch failed ({e}) — proceeding with vix=None everywhere")
    vix_map = {}

def htf_ok(frame, i, otype):
    row = frame.loc[i]
    if pd.isna(row["htf_st"]): return False
    if otype == "CE": return row["htf_st"] == 1 and bool(row["htf_macd"])
    return row["htf_st"] == -1 and not bool(row["htf_macd"])

def conf_score_9pt(frame, i, otype, brk_margin_pct):
    row = frame.loc[i]; s = 0.0; bull = otype == "CE"
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

DELTA, MODEL_BROKERAGE, UNITS = 0.40, 20, 65
# WINDOW_END fixed 2026-07-20: live bot enters until 15:00, not 14:30.
FADE_PCT, WINDOW_END, DAILY_LIMIT = 0.002, dtime(15, 0), -1000
RUNGS = [150, 250, 300, 450, 500, 700, 850, 900] + list(range(1050, 300001, 150))

def premium_est(spot):
    return round(spot * 0.14 * math.sqrt(4/365) * 0.4 * 0.98, 1)

def real_charges(buy_val, sell_val):
    brokerage = 20.0*2; stt = 0.001*sell_val
    txn = 0.0003503*(buy_val+sell_val); sebi = 0.000001*(buy_val+sell_val)
    stamp = 0.00003*buy_val; gst = 0.18*(brokerage+txn+sebi)
    return round(brokerage+stt+txn+sebi+stamp+gst, 2)

def simulate(gate_mode="baseline", min_score=7.0, min_conf_pct=None, weights=None):
    """
    gate_mode: "baseline" (9-point score gate) or "ensemble" (mother_decide gate).
    """
    frame = df5
    dr = {d: frame.index[frame["day"] == d].tolist() for d in frame["day"].unique()}
    all_d = sorted(frame["day"].unique())
    trades, day_net = [], {}
    scores_taken = []

    for d in all_d[2:]:   # need >=200 bars history for sma200 -> skip first couple days
        prior = [x for x in all_d if x < d]
        yd_hi = frame.loc[dr[prior[-1]], "high"].max()
        yd_lo = frame.loc[dr[prior[-1]], "low"].min()
        idxs = dr[d]
        or_rows = [i for i in idxs if frame.loc[i, "dt"].time() < dtime(10, 15)]
        or_hi = max(frame.loc[i, "high"] for i in or_rows) if or_rows else None
        or_lo = min(frame.loc[i, "low"] for i in or_rows) if or_rows else None
        _dte_mult = 1.41 if d.weekday() == 1 else 1.0
        vix_today = vix_map.get(d)
        pos, cooldown = None, None
        day_hi_run = -1e18

        for k, i in enumerate(idxs):
            row = frame.loc[i]; t = row["dt"].time(); px = row["close"]
            last = k == len(idxs) - 1
            if pos:
                mv = (px-pos["entry"])*DELTA*UNITS if pos["otype"] == "CE" else (pos["entry"]-px)*DELTA*UNITS
                pnl = mv - MODEL_BROKERAGE
                if pnl > pos["peak"]: pos["peak"] = pnl
                peak = pos["peak"]; floor = None
                for rg in RUNGS:
                    if peak >= rg: floor = rg
                    else: break
                booked = None
                if t >= dtime(15, 25) or last: booked = (round(pnl, 2), "EOD")
                elif floor is not None and pnl < floor: booked = (floor, "STAIR")
                elif floor is None and pnl <= -pos["sl"]: booked = (-pos["sl"], "SL")
                if booked:
                    val, status = booked
                    gross = val + MODEL_BROKERAGE
                    sell_val = max(0.0, pos["inv"] + gross)
                    chg = real_charges(pos["inv"], sell_val)
                    net = round(gross - chg, 2)
                    trades.append(net); day_net[d] = day_net.get(d, 0.0) + net
                    if status == "SL": cooldown = row["dt"] + timedelta(minutes=10)
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

            if gate_mode == "baseline":
                sc = conf_score_9pt(frame, i, otype, margin)
                if sc < min_score: continue
                scores_taken.append(sc)
            elif gate_mode == "baseline_plus_risk_filter":
                sc = conf_score_9pt(frame, i, otype, margin)
                if sc < min_score: continue
                sub = frame.iloc[max(0, i-60):i+1].reset_index(drop=True)
                if len(sub) < 55: continue
                risk_mult, _ = eb.volatility_bot(sub, vix_today)
                if risk_mult < min_conf_pct:   # reusing param as risk_mult threshold here
                    continue
                scores_taken.append(sc)
            else:
                sub = frame.iloc[max(0, i-60):i+1].reset_index(drop=True)
                if len(sub) < 55: continue
                st15_val = row["htf_st"] == 1 if otype == "CE" else row["htf_st"] == -1
                res = eb.mother_decide(otype, level, px, sub, st15=bool(st15_val),
                                        macd15_up=bool(row["htf_macd"]), vix=vix_today, weights=weights)
                if res["confidence_pct"] < min_conf_pct: continue
                scores_taken.append(res["confidence_pct"])

            a = frame.loc[i, "atr14"]
            if np.isnan(a): continue
            sl = max(200, min(1000, round(a * 1.0 * DELTA * UNITS / 10) * 10))
            prem = premium_est(px) * _dte_mult
            pos = {"otype": otype, "entry": px, "sl": sl, "peak": -9e9, "inv": round(prem*UNITS, 2)}

    wins = [x for x in trades if x > 0]; loss = [x for x in trades if x <= 0]
    wr = len(wins)/len(trades)*100 if trades else 0
    worst_day = min(day_net.values()) if day_net else 0.0
    eq, pk, mdd = 0.0, 0.0, 0.0
    for d in sorted(day_net):
        eq += day_net[d]; pk = max(pk, eq); mdd = max(mdd, pk-eq)
    g = sum(1 for v in day_net.values() if v > 0); rr = sum(1 for v in day_net.values() if v < 0)
    return dict(net=sum(trades), n=len(trades), wr=wr, worst_day=worst_day, mdd=mdd,
                green=g, red=rr, avg_win=np.mean(wins) if wins else 0,
                avg_loss=np.mean(loss) if loss else 0, scores=scores_taken)

def show(name, r_):
    print(f"\n{name}")
    print(f"   NET Rs.{r_['net']:+,.2f} | trades {r_['n']} | win {r_['wr']:.0f}% | green/red days {r_['green']}/{r_['red']}")
    print(f"   avg win Rs.{r_['avg_win']:+,.0f} | avg loss Rs.{r_['avg_loss']:+,.0f} | worst day Rs.{r_['worst_day']:+,.0f} | maxDD Rs.{r_['mdd']:,.0f}")

d0, d1 = df5["day"].min(), df5["day"].max()
print("\n" + "=" * 82)
print(f"ENSEMBLE COUNCIL BACKTEST | {d0} -> {d1} | live config, 1 lot, real charges")
print("=" * 82)

baseline = simulate(gate_mode="baseline", min_score=7.0)
show("BASELINE — current live gate (9-point score >= 7.0)", baseline)

for risk_thr in [0.95, 0.90, 0.80]:
    r_ = simulate(gate_mode="baseline_plus_risk_filter", min_score=7.0, min_conf_pct=risk_thr)
    show(f"BASELINE + risk-bot filter (skip if risk multiplier < {risk_thr})", r_)

WEIGHT_PRESETS = {
    "default (breakout-heavy)": None,   # uses ensemble_bots.DEFAULT_WEIGHTS
    "balanced":     {"breakout": 1.0, "trend": 1.0, "momentum": 1.0, "volume": 1.0, "pattern": 1.0},
    "trend-heavy":  {"breakout": 1.2, "trend": 1.8, "momentum": 1.2, "volume": 0.5, "pattern": 0.3},
    "breakout-only-conf": {"breakout": 3.0, "trend": 0.4, "momentum": 0.4, "volume": 0.2, "pattern": 0.1},
}
THRESHOLDS = [50, 60, 70]

best = None
for wname, w in WEIGHT_PRESETS.items():
    for thr in THRESHOLDS:
        r_ = simulate(gate_mode="ensemble", min_conf_pct=thr, weights=w)
        label = f"ENSEMBLE — weights={wname}, gate>={thr}%"
        show(label, r_)
        if r_["n"] >= 15:   # ignore variants with too few trades to be meaningful
            score = r_["net"] - r_["mdd"]*0.5   # reward profit, penalize drawdown
            if best is None or score > best[0]:
                best = (score, label, wname, thr, r_)

print("\n" + "=" * 82)
if best:
    _, label, wname, thr, r_ = best
    print(f"BEST ENSEMBLE VARIANT: {label}")
    print(f"   vs BASELINE: net Rs.{r_['net']-baseline['net']:+,.0f}, "
          f"worst day Rs.{r_['worst_day']-baseline['worst_day']:+,.0f}, "
          f"maxDD Rs.{r_['mdd']-baseline['mdd']:+,.0f}, trades {r_['n']} vs {baseline['n']}")
else:
    print("No ensemble variant produced enough trades (>=15) to compare meaningfully.")
print("=" * 82)
