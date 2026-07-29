"""
OLD BOT vs NEW BOT — 6 MONTHS (2026-07-20, Sai: "check the old and new versions
bot from past 6 months").

  OLD = v4.5 / v5.0, what ran until today: SL 1.0xATR, 10-min SL cooldown,
        0.2% afternoon fade.
  NEW = v5.1, applied today: SL 1.5xATR, 30-min SL cooldown, 0.15% fade.
  Everything else identical in both (breakout ladder, 15m MTF filter, 9-pt
  gate >= 7.0, dense staircase, ATR clamp Rs.200-1000/lot, daily stop -1000,
  entry window 10:15-15:00, EOD 15:25, real Zerodha charges, 1 lot).

DATA HONESTY — the same limit as always, stated again so nobody over-reads it:
  PERIOD A (Apr 28 - Jul 20, ~2.7 months): REAL 5-min candles. TRUSTWORTHY.
  PERIOD B (Jan 21 - Apr 27, ~3.3 months): HOURLY candles = APPROXIMATION.
     Real 5-min history simply does not exist beyond 60 days on the free data
     (Yahoo caps it; Kite historical needs the paid plan). In hourly mode the
     stop is checked against the candle's ADVERSE extreme FIRST (pessimistic
     ordering) and 5-min ATR is estimated as hourly ATR / sqrt(12).
     Period B is a directional sanity check, NOT a precise rupee figure.
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

def build(interval, period, htf_rule, lag_min):
    raw = get_yf(interval, period)[["dt","open","high","low","close","volume"]]
    f = prep(raw)
    r = raw.set_index("dt")
    h = pd.DataFrame({"open": r["open"].resample(htf_rule).first(),
                      "high": r["high"].resample(htf_rule).max(),
                      "low":  r["low"].resample(htf_rule).min(),
                      "close":r["close"].resample(htf_rule).last()}).dropna().reset_index()
    h["st_dir"] = supertrend(h)
    mh, sh = macd(h["close"]); h["macd_up"] = mh > sh
    h["usable_from"] = (h["dt"] + pd.Timedelta(minutes=lag_min)).astype("datetime64[ns]")
    j = pd.merge_asof(f[["dt"]].sort_values("dt"),
                      h[["usable_from","st_dir","macd_up"]].sort_values("usable_from"),
                      left_on="dt", right_on="usable_from", direction="backward")
    f["htf_st"] = j["st_dir"].values; f["htf_macd"] = j["macd_up"].values
    return f

print("Fetching real 5-min data (60d)...")
F5  = build("5m", "60d", "15min", 10)
print("Fetching 6-month hourly data (approximation)...")
F1H = build("1h", "6mo", "3h", 120)

DELTA, MODEL_BROKERAGE, UNITS = 0.40, 20, 65
EOD, WINDOW_START, WINDOW_END = dtime(15,25), dtime(10,15), dtime(15,0)
DAILY_LIMIT, MIN_SCORE = -1000, 7.0
SL_MIN, SL_MAX = 200, 1000
RUNGS = [150,250,300,450,500,700,850,900] + list(range(1050,300001,150))

OLD = dict(name="OLD v4.5/v5.0", atr_mult=1.0, cooldown=10, fade=0.002)
NEW = dict(name="NEW v5.1",      atr_mult=1.5, cooldown=30, fade=0.0015)

def premium_est(spot): return round(spot*0.14*math.sqrt(4/365)*0.4*0.98, 1)

def real_charges(b, s):
    brok = 40.0; stt = 0.001*s; txn = 0.0003503*(b+s); sebi = 0.000001*(b+s)
    stamp = 0.00003*b; gst = 0.18*(brok+txn+sebi)
    return round(brok+stt+txn+sebi+stamp+gst, 2)

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
    if row["volume"] > row["vol_avg"]*1.5: s += 1
    elif row["volume"] > row["vol_avg"]*1.1: s += 0.5
    if (row["sma20"] > row["sma50"]) == bull: s += 1
    if (row["close"] > row["sma50"]) == bull: s += 1
    s += 1 if margin >= 0.0005 else 0.5
    return s

def run(frame, cfg, intracandle=False, atr_scale=1.0, day_filter=None):
    dr = {d: frame.index[frame["day"]==d].tolist() for d in frame["day"].unique()}
    alld = sorted(frame["day"].unique())
    days = [d for d in alld if (day_filter is None or day_filter(d))]
    trades, day_net = [], {}
    step = pd.Timedelta(hours=1) if intracandle else pd.Timedelta(minutes=5)

    for d in days:
        prior = [x for x in alld if x < d]
        if len(prior) < 2: continue
        yd_hi = frame.loc[dr[prior[-1]], "high"].max()
        yd_lo = frame.loc[dr[prior[-1]], "low"].min()
        idxs = dr[d]
        orr = [i for i in idxs if frame.loc[i,"dt"].time() < dtime(10,15)]
        or_hi = max(frame.loc[i,"high"] for i in orr) if orr else None
        or_lo = min(frame.loc[i,"low"]  for i in orr) if orr else None
        dte = 1.41 if d.weekday()==1 else 1.0
        pos, cool, dayhi = None, None, -1e18

        for k, i in enumerate(idxs):
            row = frame.loc[i]; px = row["close"]; last = k == len(idxs)-1
            tclose = (row["dt"] + step).time()
            if pos:
                booked = None
                if intracandle:
                    adv = row["low"] if pos["otype"]=="CE" else row["high"]
                    advp = ((adv-pos["entry"]) if pos["otype"]=="CE" else (pos["entry"]-adv))*DELTA*UNITS - MODEL_BROKERAGE
                    fav = row["high"] if pos["otype"]=="CE" else row["low"]
                    favp = ((fav-pos["entry"]) if pos["otype"]=="CE" else (pos["entry"]-fav))*DELTA*UNITS - MODEL_BROKERAGE
                    fb = None
                    for rg in RUNGS:
                        if pos["peak"] >= rg: fb = rg
                        else: break
                    if fb is None and advp <= -pos["sl"]: booked = (-pos["sl"], "SL")
                    elif fb is not None and advp < fb:    booked = (fb, "STAIR")
                    else:
                        if favp > pos["peak"]: pos["peak"] = favp
                mv = ((px-pos["entry"]) if pos["otype"]=="CE" else (pos["entry"]-px))*DELTA*UNITS
                pnl = mv - MODEL_BROKERAGE
                if booked is None:
                    if pnl > pos["peak"]: pos["peak"] = pnl
                    fl = None
                    for rg in RUNGS:
                        if pos["peak"] >= rg: fl = rg
                        else: break
                    if tclose >= EOD or last: booked = (round(pnl,2), "EOD")
                    elif fl is not None and pnl < fl: booked = (fl, "STAIR")
                    elif fl is None and pnl <= -pos["sl"]: booked = (-pos["sl"], "SL")
                if booked:
                    val, st = booked
                    gross = val + MODEL_BROKERAGE
                    sv = max(0.0, pos["inv"]+gross)
                    net = round(gross - real_charges(pos["inv"], sv), 2)
                    trades.append((d, net)); day_net[d] = day_net.get(d,0.0)+net
                    if st == "SL": cool = row["dt"] + timedelta(minutes=cfg["cooldown"])
                    pos = None
            dhp = dayhi
            if row["high"] > dayhi: dayhi = row["high"]
            if pos: continue
            if day_net.get(d,0.0) <= DAILY_LIMIT: continue
            if not (WINDOW_START <= tclose <= WINDOW_END): continue
            if cool and row["dt"] < cool: continue

            otype, lvl = None, None
            if px > yd_hi: otype, lvl = "CE", yd_hi
            elif px < yd_lo: otype, lvl = "PE", yd_lo
            elif or_lo is not None and px < or_lo: otype, lvl = "PE", or_lo
            elif or_hi is not None and px > or_hi: otype, lvl = "CE", or_hi
            if tclose > dtime(12,30) and otype != "PE" and dhp > 0 and px <= dhp*(1-cfg["fade"]):
                otype, lvl = "PE", dhp*(1-cfg["fade"])
            if otype is None: continue
            if pd.isna(row["htf_st"]): continue
            if otype=="CE" and not (row["htf_st"]==1 and bool(row["htf_macd"])): continue
            if otype=="PE" and not (row["htf_st"]==-1 and not bool(row["htf_macd"])): continue
            if conf9(row, otype, abs(px-lvl)/px) < MIN_SCORE: continue
            a = row["atr14"]
            if np.isnan(a): continue
            sl = max(SL_MIN, min(SL_MAX, round(a*atr_scale*cfg["atr_mult"]*DELTA*UNITS/10)*10))
            pos = {"otype":otype, "entry":px, "sl":sl, "peak":-9e9,
                   "inv":round(premium_est(px)*dte*UNITS,2)}

    nets = [n for _, n in trades]
    wins = [n for n in nets if n > 0]
    eq=pk=mdd=0.0
    for dd in sorted(day_net):
        eq += day_net[dd]; pk = max(pk,eq); mdd = max(mdd, pk-eq)
    return dict(net=sum(nets), n=len(nets),
                wr=(len(wins)/len(nets)*100 if nets else 0),
                mdd=mdd, worst=(min(day_net.values()) if day_net else 0),
                per=(sum(nets)/len(nets) if nets else 0),
                trades=trades, day_net=day_net)

def line(tag, r):
    print(f"  {tag:<16} NET Rs.{r['net']:>+9,.0f} | {r['n']:>4} trades | win {r['wr']:>3.0f}% | "
          f"Rs.{r['per']:>+5,.0f}/trade | maxDD Rs.{r['mdd']:>6,.0f} | worst day Rs.{r['worst']:>+7,.0f}")

d5a, d5b = F5["day"].min(), F5["day"].max()
CUT = d5a

print("\n" + "="*100)
print(f"PERIOD A — REAL 5-MIN DATA ({d5a} -> {d5b}).  THIS IS THE RELIABLE COMPARISON.")
print("="*100)
oa, na = run(F5, OLD), run(F5, NEW)
line(OLD["name"], oa); line(NEW["name"], na)
print(f"\n  DIFFERENCE: NET Rs.{na['net']-oa['net']:+,.0f} | per-trade Rs.{na['per']-oa['per']:+,.0f} | "
      f"win rate {na['wr']-oa['wr']:+.0f} pts | drawdown Rs.{na['mdd']-oa['mdd']:+,.0f} | "
      f"worst day Rs.{na['worst']-oa['worst']:+,.0f}")

days5 = sorted(F5["day"].unique()); MID = days5[len(days5)//2]
print("\n  Robustness — same comparison on each half separately:")
for lbl, filt in [("first half", lambda d: d < MID), ("second half", lambda d: d >= MID)]:
    o_, n_ = run(F5, OLD, day_filter=filt), run(F5, NEW, day_filter=filt)
    verdict = "NEW wins" if n_["per"] > o_["per"] else "OLD wins"
    print(f"    {lbl:<12} OLD Rs.{o_['per']:+5,.0f}/trade  vs  NEW Rs.{n_['per']:+5,.0f}/trade   -> {verdict}")

print("\n" + "="*100)
print(f"PERIOD B — OLDER MONTHS ON HOURLY CANDLES (before {CUT}).  APPROXIMATION ONLY.")
print("="*100)
ob = run(F1H, OLD, intracandle=True, atr_scale=1/math.sqrt(12), day_filter=lambda d: d < CUT)
nb = run(F1H, NEW, intracandle=True, atr_scale=1/math.sqrt(12), day_filter=lambda d: d < CUT)
line(OLD["name"], ob); line(NEW["name"], nb)
print(f"\n  DIFFERENCE: NET Rs.{nb['net']-ob['net']:+,.0f} | per-trade Rs.{nb['per']-ob['per']:+,.0f}")

print("\n" + "="*100)
print("MONTH BY MONTH (A = real 5-min, B = hourly approximation)")
print("="*100)
print(f"  {'month':<12}{'source':<8}{'OLD net':>12}{'NEW net':>12}{'diff':>12}   winner")
def bymonth(r):
    out = {}
    for d, n in r["trades"]:
        out[f"{d.year}-{d.month:02d}"] = out.get(f"{d.year}-{d.month:02d}", 0.0) + n
    return out
mo_o, mo_n = bymonth(ob), bymonth(nb)
mo_o5, mo_n5 = bymonth(oa), bymonth(na)
for m in sorted(set(list(mo_o)+list(mo_n))):
    o, n = mo_o.get(m,0), mo_n.get(m,0)
    print(f"  {m:<12}{'B':<8}{o:>+12,.0f}{n:>+12,.0f}{n-o:>+12,.0f}   {'NEW' if n>o else 'OLD'}")
for m in sorted(set(list(mo_o5)+list(mo_n5))):
    o, n = mo_o5.get(m,0), mo_n5.get(m,0)
    print(f"  {m:<12}{'A':<8}{o:>+12,.0f}{n:>+12,.0f}{n-o:>+12,.0f}   {'NEW' if n>o else 'OLD'}")

print("\n" + "="*100)
print("6-MONTH TOTAL (Period A real + Period B approximate — mixed quality, read with care)")
print("="*100)
print(f"  OLD v4.5/v5.0 : Rs.{oa['net']+ob['net']:+,.0f}   ({oa['n']+ob['n']} trades)")
print(f"  NEW v5.1      : Rs.{na['net']+nb['net']:+,.0f}   ({na['n']+nb['n']} trades)")
print(f"  NEW advantage : Rs.{(na['net']+nb['net'])-(oa['net']+ob['net']):+,.0f}")
print("="*100)
