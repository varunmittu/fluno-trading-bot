"""
PARAMETER RE-TUNE SWEEP (2026-07-20, Sai: "improve the version of this current
bot if there is anything to improve"). Council is NOT involved anywhere here.

WHY THIS EXISTS: nearly every tuned number in the live bot (confidence gate
7.0, ATR multiplier 1.0, SL clamps 200-1000, dense staircase rungs, 10-min SL
cooldown, 0.2% fade) was chosen by a backtest that used a 14:30 entry cutoff —
but the live bot enters until 15:00. Those scripts were tuning a slightly
different bot. This re-checks each knob against the CORRECTED engine.

METHOD (deliberately conservative — 60 days is a small sample and it is very
easy to curve-fit noise):
  1. Sweep ONE parameter at a time, holding everything else at live values.
     (No giant combinatorial grid — that finds noise and calls it alpha.)
  2. Any candidate that beats baseline gets re-tested on BOTH halves of the
     data separately (out-of-sample robustness).
  3. A change is only RECOMMENDED if it beats baseline in BOTH halves AND
     does not worsen max drawdown badly. Anything that wins only in one half
     is treated as luck and rejected.
Report prints a clear RECOMMENDED / REJECTED verdict per knob. Nothing is
applied automatically — app.py is edited only after reviewing this output.
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
    f["atr14"] = tr.rolling(14).mean()
    return f

def get_yf(interval, period, symbol="^NSEI"):
    d = yf.download(symbol, interval=interval, period=period, progress=False).reset_index()
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = [c[0].lower().replace(" ", "") for c in d.columns]
    else:
        d.columns = [str(c).lower().replace(" ", "") for c in d.columns]
    tcol = next(c for c in d.columns if c in ("datetime","date","index","timestamp"))
    d = d.rename(columns={tcol: "dt"}); d["dt"] = pd.to_datetime(d["dt"])
    try: d["dt"] = d["dt"].dt.tz_localize(None)
    except Exception: pass
    d["dt"] = d["dt"].astype("datetime64[ns]")
    return d

print("Fetching real 5-min data (60d)...")
raw5 = get_yf("5m", "60d")[["dt","open","high","low","close","volume"]]
DF = prep(raw5)
r = raw5.set_index("dt")
f15 = pd.DataFrame({"open": r["open"].resample("15min").first(),
                    "high": r["high"].resample("15min").max(),
                    "low":  r["low"].resample("15min").min(),
                    "close":r["close"].resample("15min").last()}).dropna().reset_index()
f15["st_dir"] = supertrend(f15)
m15, s15 = macd(f15["close"]); f15["macd_up15"] = m15 > s15
f15["usable_from"] = (f15["dt"] + pd.Timedelta(minutes=10)).astype("datetime64[ns]")
j = pd.merge_asof(DF[["dt"]].sort_values("dt"),
                  f15[["usable_from","st_dir","macd_up15"]].sort_values("usable_from"),
                  left_on="dt", right_on="usable_from", direction="backward")
DF["htf_st"] = j["st_dir"].values; DF["htf_macd"] = j["macd_up15"].values

DELTA, MODEL_BROKERAGE, UNITS = 0.40, 20, 65
EOD = dtime(15, 25)

# ── LIVE CONFIG (the baseline everything is measured against) ───────────────
LIVE = dict(
    min_score   = 7.0,
    atr_mult    = 1.0,
    sl_min      = 200,
    sl_max      = 1000,
    window_end  = dtime(15, 0),
    cooldown    = 10,
    fade_pct    = 0.002,
    or_end      = dtime(10, 15),
    rungs       = [150, 250, 300, 450, 500, 700, 850, 900] + list(range(1050, 300001, 150)),
    daily_limit = -1000,
)

def premium_est(spot):
    return round(spot * 0.14 * math.sqrt(4/365) * 0.4 * 0.98, 1)

def real_charges(buy_val, sell_val):
    brokerage = 20.0*2; stt = 0.001*sell_val
    txn = 0.0003503*(buy_val+sell_val); sebi = 0.000001*(buy_val+sell_val)
    stamp = 0.00003*buy_val; gst = 0.18*(brokerage+txn+sebi)
    return round(brokerage+stt+txn+sebi+stamp+gst, 2)

def conf9(row, otype, margin):
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
    s += 1 if margin >= 0.0005 else 0.5
    return s

def simulate(cfg, day_filter=None):
    c = {**LIVE, **cfg}
    frame = DF
    dr = {d: frame.index[frame["day"] == d].tolist() for d in frame["day"].unique()}
    all_days = sorted(frame["day"].unique())
    days = [d for d in all_days if (day_filter is None or day_filter(d))]
    trades, day_net = [], {}

    for d in days:
        prior = [x for x in all_days if x < d]
        if not prior: continue
        yd_hi = frame.loc[dr[prior[-1]], "high"].max()
        yd_lo = frame.loc[dr[prior[-1]], "low"].min()
        idxs = dr[d]
        or_rows = [i for i in idxs if frame.loc[i, "dt"].time() < c["or_end"]]
        or_hi = max(frame.loc[i, "high"] for i in or_rows) if or_rows else None
        or_lo = min(frame.loc[i, "low"] for i in or_rows) if or_rows else None
        dte_mult = 1.41 if d.weekday() == 1 else 1.0
        pos, cooldown_until, day_hi_run = None, None, -1e18

        for k, i in enumerate(idxs):
            row = frame.loc[i]; t = row["dt"].time(); px = row["close"]
            last = k == len(idxs) - 1
            if pos:
                mv = (px-pos["entry"])*DELTA*UNITS if pos["otype"]=="CE" else (pos["entry"]-px)*DELTA*UNITS
                pnl = mv - MODEL_BROKERAGE
                if pnl > pos["peak"]: pos["peak"] = pnl
                floor = None
                for rg in c["rungs"]:
                    if pos["peak"] >= rg: floor = rg
                    else: break
                booked = None
                if t >= EOD or last: booked = (round(pnl,2), "EOD")
                elif floor is not None and pnl < floor: booked = (floor, "STAIR")
                elif floor is None and pnl <= -pos["sl"]: booked = (-pos["sl"], "SL")
                if booked:
                    val, status = booked
                    gross = val + MODEL_BROKERAGE
                    sell_val = max(0.0, pos["inv"] + gross)
                    net = round(gross - real_charges(pos["inv"], sell_val), 2)
                    trades.append(net); day_net[d] = day_net.get(d,0.0) + net
                    if status == "SL" and c["cooldown"] > 0:
                        cooldown_until = row["dt"] + timedelta(minutes=c["cooldown"])
                    pos = None
            day_hi_prior = day_hi_run
            if row["high"] > day_hi_run: day_hi_run = row["high"]
            if pos: continue
            if day_net.get(d,0.0) <= c["daily_limit"]: continue
            if not (dtime(10,15) <= t <= c["window_end"]): continue
            if cooldown_until and row["dt"] < cooldown_until: continue

            otype, level = None, None
            if px > yd_hi: otype, level = "CE", yd_hi
            elif px < yd_lo: otype, level = "PE", yd_lo
            elif or_lo is not None and px < or_lo: otype, level = "PE", or_lo
            elif or_hi is not None and px > or_hi: otype, level = "CE", or_hi
            if t > dtime(12,30) and otype != "PE" and day_hi_prior > 0 and px <= day_hi_prior*(1-c["fade_pct"]):
                otype, level = "PE", day_hi_prior*(1-c["fade_pct"])
            if otype is None: continue
            if pd.isna(row["htf_st"]): continue
            if otype == "CE" and not (row["htf_st"]==1 and bool(row["htf_macd"])): continue
            if otype == "PE" and not (row["htf_st"]==-1 and not bool(row["htf_macd"])): continue
            margin = abs(px-level)/px
            if conf9(row, otype, margin) < c["min_score"]: continue
            a = row["atr14"]
            if np.isnan(a): continue
            sl = max(c["sl_min"], min(c["sl_max"], round(a*c["atr_mult"]*DELTA*UNITS/10)*10))
            pos = {"otype": otype, "entry": px, "sl": sl, "peak": -9e9,
                   "inv": round(premium_est(px)*dte_mult*UNITS, 2)}

    wins = [x for x in trades if x > 0]
    eq, pk, mdd = 0.0, 0.0, 0.0
    for dd in sorted(day_net):
        eq += day_net[dd]; pk = max(pk, eq); mdd = max(mdd, pk-eq)
    return dict(net=sum(trades), n=len(trades),
                wr=(len(wins)/len(trades)*100 if trades else 0),
                mdd=mdd, worst=(min(day_net.values()) if day_net else 0),
                per_trade=(sum(trades)/len(trades) if trades else 0))

days_all = sorted(DF["day"].unique())
MID = days_all[len(days_all)//2]
H1 = lambda d: d < MID
H2 = lambda d: d >= MID

base      = simulate({})
base_h1   = simulate({}, H1)
base_h2   = simulate({}, H2)

print("\n" + "="*90)
print(f"BASELINE = LIVE CONFIG (corrected 15:00 window) | {days_all[0]} -> {days_all[-1]}")
print("="*90)
print(f"  NET Rs.{base['net']:+,.0f} | {base['n']} trades | win {base['wr']:.0f}% | "
      f"Rs.{base['per_trade']:+,.0f}/trade | maxDD Rs.{base['mdd']:,.0f} | worst day Rs.{base['worst']:+,.0f}")
print(f"  half1 Rs.{base_h1['net']:+,.0f} ({base_h1['per_trade']:+,.0f}/trade) | "
      f"half2 Rs.{base_h2['net']:+,.0f} ({base_h2['per_trade']:+,.0f}/trade)")

SWEEPS = [
    ("CONFIDENCE GATE",   "min_score",  [0.0, 6.0, 6.5, 7.0, 7.5, 8.0]),
    ("ATR MULTIPLIER",    "atr_mult",   [0.8, 1.0, 1.2, 1.5, 2.0]),
    ("SL MIN (Rs/lot)",   "sl_min",     [100, 200, 300, 400]),
    ("SL MAX (Rs/lot)",   "sl_max",     [600, 800, 1000, 1500]),
    ("ENTRY WINDOW END",  "window_end", [dtime(14,0), dtime(14,30), dtime(15,0), dtime(15,15)]),
    ("SL COOLDOWN (min)", "cooldown",   [0, 5, 10, 20, 30]),
    ("FADE TRIGGER %",    "fade_pct",   [0.001, 0.0015, 0.002, 0.003, 0.005]),
    ("OPENING RANGE END", "or_end",     [dtime(9,45), dtime(10,0), dtime(10,15), dtime(10,45)]),
]

RUNG_LADDERS = {
    "current dense (150/250/300/450/500/700/850/900)": LIVE["rungs"],
    "no tiny first rung (300/500/700/900)":            [300,500,700,900] + list(range(1050,300001,150)),
    "higher first rung (250/400/550/700/850)":         [250,400,550,700,850] + list(range(1000,300001,150)),
    "very tight (100/200/300/400/500/600/700)":        [100,200,300,400,500,600,700] + list(range(850,300001,150)),
    "wide (200/500/900)":                              [200,500,900] + list(range(1200,300001,300)),
}

candidates = []

def verdict(label, cand, cand_h1, cand_h2):
    """Only recommend if it beats baseline in BOTH halves (out-of-sample robust)."""
    better_full = cand["net"] > base["net"]
    better_h1   = cand_h1["per_trade"] > base_h1["per_trade"]
    better_h2   = cand_h2["per_trade"] > base_h2["per_trade"]
    dd_ok       = cand["mdd"] <= base["mdd"] * 1.25
    if better_full and better_h1 and better_h2 and dd_ok:
        candidates.append((label, cand, cand_h1, cand_h2))
        return "  <-- ROBUST (beats baseline in BOTH halves)"
    if better_full and not (better_h1 and better_h2):
        return "  (better overall but NOT in both halves -> likely luck, rejected)"
    if better_full and not dd_ok:
        return "  (better profit but drawdown too much worse -> rejected)"
    return ""

for title, key, values in SWEEPS:
    print("\n" + "-"*90)
    print(f"{title}   (live value = {LIVE[key]})")
    print("-"*90)
    for v in values:
        cfg = {key: v}
        rr = simulate(cfg)
        tag = "  [LIVE]" if v == LIVE[key] else ""
        note = ""
        if v != LIVE[key]:
            note = verdict(f"{title} = {v}", rr, simulate(cfg, H1), simulate(cfg, H2))
        print(f"  {str(v):<12} NET Rs.{rr['net']:>+9,.0f} | {rr['n']:>4} trades | win {rr['wr']:>3.0f}% | "
              f"Rs.{rr['per_trade']:>+5,.0f}/trade | DD Rs.{rr['mdd']:>6,.0f}{tag}{note}")

print("\n" + "-"*90)
print("STAIRCASE RUNG LADDER")
print("-"*90)
for name, ladder in RUNG_LADDERS.items():
    cfg = {"rungs": ladder}
    rr = simulate(cfg)
    tag = "  [LIVE]" if ladder == LIVE["rungs"] else ""
    note = "" if ladder == LIVE["rungs"] else verdict(f"RUNGS = {name}", rr, simulate(cfg, H1), simulate(cfg, H2))
    print(f"  {name:<50} NET Rs.{rr['net']:>+9,.0f} | {rr['n']:>4} tr | "
          f"Rs.{rr['per_trade']:>+5,.0f}/tr | DD Rs.{rr['mdd']:>6,.0f}{tag}{note}")

print("\n" + "="*90)
print("ROBUST CANDIDATES (beat baseline in BOTH halves — the only ones worth applying)")
print("="*90)
if not candidates:
    print("  NONE. Every alternative either lost to the live config or won only in one")
    print("  half (= luck, not edge). Recommendation: CHANGE NOTHING. The current")
    print("  settings are already at or near the best this data supports.")
else:
    for label, cand, c1, c2 in sorted(candidates, key=lambda x: -x[1]["net"]):
        print(f"\n  {label}")
        print(f"     full: Rs.{cand['net']:+,.0f} ({cand['per_trade']:+,.0f}/trade) vs baseline Rs.{base['net']:+,.0f} ({base['per_trade']:+,.0f}/trade)")
        print(f"     half1: Rs.{c1['per_trade']:+,.0f}/tr vs {base_h1['per_trade']:+,.0f}  |  "
              f"half2: Rs.{c2['per_trade']:+,.0f}/tr vs {base_h2['per_trade']:+,.0f}")
        print(f"     maxDD Rs.{cand['mdd']:,.0f} vs baseline Rs.{base['mdd']:,.0f}")
print("\n" + "="*90)
