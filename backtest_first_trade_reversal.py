"""
backtest_first_trade_reversal.py (2026-07-27) — Sai: of the FIRST trade each
day (after 10:15), how many hit the stop, and how many of those were a
"fakeout" — the first breakout reversed and went back the trade's way after
stopping. 6 months real Kite index data. Stop + reversal measured on the index.
"""
import sys, os, re
from datetime import datetime, timedelta, time as dtime
sys.path.insert(0, r"C:\Users\avina\Downloads\varun trading")
import pandas as pd, numpy as np
import bt_engine as be
from backtest_conf72 import conf_pct
from backtest_adx_label import add_adx
from kiteconnect import KiteConnect

BASE = r"C:\Users\avina\Downloads\varun trading"
api_key = ""
for line in open(os.path.join(BASE, "config.py.txt")):
    m = re.match(r'\s*API_KEY\s*=\s*["\']([^"\']+)["\']', line)
    if m: api_key = m.group(1)
tok = open(os.path.join(BASE, "kite_token.txt")).read().strip()
k = KiteConnect(api_key=api_key); k.set_access_token(tok)
print("connected:", k.profile().get("user_name"))

NIFTY50 = 256265
rows, cur, end = [], datetime(2026, 6, 1), datetime.now()
while cur < end:
    nxt = min(cur + timedelta(days=90), end)
    rows += k.historical_data(NIFTY50, cur, nxt, "5minute")
    cur = nxt + timedelta(minutes=5)
df = pd.DataFrame(rows).rename(columns={"date": "dt"})
df["dt"] = pd.to_datetime(df["dt"]).dt.tz_localize(None).astype("datetime64[ns]")
df = df[["dt","open","high","low","close","volume"]].drop_duplicates("dt").sort_values("dt").reset_index(drop=True)
f = add_adx(be.prep(df))
r = df.set_index("dt")
h = pd.DataFrame({"open": r["open"].resample("15min").first(), "high": r["high"].resample("15min").max(),
                  "low": r["low"].resample("15min").min(), "close": r["close"].resample("15min").last()}).dropna().reset_index()
h["st_dir"] = be.supertrend(h)
mh, sh = be.macd(h["close"]); h["macd_up"] = mh > sh
h["usable_from"] = (h["dt"] + pd.Timedelta(minutes=10)).astype("datetime64[ns]")
f["dt"] = f["dt"].astype("datetime64[ns]")
j = pd.merge_asof(f[["dt"]].sort_values("dt"), h[["usable_from","st_dir","macd_up"]].sort_values("usable_from"),
                  left_on="dt", right_on="usable_from", direction="backward")
f["htf_st"] = j["st_dir"].values; f["htf_macd"] = j["macd_up"].values

DELTA, UNITS = be.DELTA, be.UNITS
SL_MIN, SL_MAX = be.SL_MIN, be.SL_MAX
dr = {d: f.index[f["day"] == d].tolist() for d in f["day"].unique()}
alld = sorted(f["day"].unique())
days = [d for d in alld if d >= datetime(2026, 6, 1).date()]

n_days = won = stopped = reversed_fakeout = genuine = 0
rev_pts = []
detail = []
for d in days:
    prior = [x for x in alld if x < d]
    if len(prior) < 2: continue
    yd_hi = f.loc[dr[prior[-1]],"high"].max(); yd_lo = f.loc[dr[prior[-1]],"low"].min()
    idxs = dr[d]; orr = [i for i in idxs if f.loc[i,"dt"].time() < dtime(10,15)]
    or_hi = max(f.loc[i,"high"] for i in orr) if orr else None
    or_lo = min(f.loc[i,"low"] for i in orr) if orr else None
    dayhi = -1e18; entry_i = None; otype = None; E = None; sl_pts = None
    # find FIRST valid trade after 10:15
    for kk, i in enumerate(idxs):
        row = f.loc[i]; px = row["close"]; tclose = (row["dt"]+pd.Timedelta(minutes=5)).time()
        if row["high"] > dayhi: dayhi = row["high"]
        if not (dtime(10,15) <= tclose <= dtime(15,0)): continue
        ot, lvl, isbrk = None, None, True
        if px > yd_hi: ot, lvl = "CE", yd_hi
        elif px < yd_lo: ot, lvl = "PE", yd_lo
        elif or_lo is not None and px < or_lo: ot, lvl = "PE", or_lo
        elif or_hi is not None and px > or_hi: ot, lvl = "CE", or_hi
        if tclose > dtime(12,30) and ot != "PE" and dayhi > 0 and px <= dayhi*(1-0.0015):
            ot, lvl, isbrk = "PE", dayhi*(1-0.0015), False
        if ot is None: continue
        if pd.isna(row["htf_st"]): continue
        if ot=="CE" and not (row["htf_st"]==1 and bool(row["htf_macd"])): continue
        if ot=="PE" and not (row["htf_st"]==-1 and not bool(row["htf_macd"])): continue
        prev = f.loc[i-1] if i-1 in f.index else row
        c9 = be.conf9(row, ot, abs(px-lvl)/px); cp = conf_pct(row, prev, ot, isbrk)
        if not (c9>=be.MIN_SCORE or cp>72): continue
        if not pd.isna(row["adx"]) and row["adx"]<15: continue
        a = row["atr14"]
        if np.isnan(a): continue
        sl_rs = max(SL_MIN, min(SL_MAX, round(1.5*a*DELTA*UNITS/10)*10))
        entry_i, otype, E, sl_pts = i, ot, px, sl_rs/(DELTA*UNITS)
        break
    if entry_i is None: continue
    n_days += 1
    # simulate the first trade on the index, 5-min closes
    stop_lvl = E - sl_pts if otype=="CE" else E + sl_pts
    stop_hit_i = None
    for i in range(entry_i+1, idxs[-1]+1):
        if i not in f.index: break
        c = f.loc[i,"close"]
        if (otype=="CE" and c <= stop_lvl) or (otype=="PE" and c >= stop_lvl):
            stop_hit_i = i; break
        if f.loc[i,"dt"].time() >= dtime(15,25): break
    if stop_hit_i is None:
        won += 1  # first trade did not stop (rode to profit / EOD)
        continue
    stopped += 1
    # after the stop, did the index REVERSE back past entry (fakeout)?
    rest = [f.loc[i,"close"] for i in range(stop_hit_i+1, idxs[-1]+1) if i in f.index]
    if rest:
        if otype=="CE": best = max(rest); rev = best - E        # rose back above entry?
        else:           best = min(rest); rev = E - best        # fell back below entry?
    else:
        rev = -999
    if rev > 0:
        reversed_fakeout += 1; rev_pts.append(rev)
        detail.append((d, otype, E, stop_lvl, rev))
    else:
        genuine += 1

print(f"\n{'='*64}\nFIRST TRADE OF THE DAY (after 10:15) — June 1 to July 27\n{'='*64}")
print(f"  days with a first trade      : {n_days}")
print(f"  first trade WON (no stop)    : {won}   ({won/n_days*100:.0f}%)")
print(f"  first trade STOPPED OUT      : {stopped}   ({stopped/n_days*100:.0f}%)")
print(f"     - of those, FAKEOUT       : {reversed_fakeout}   (stopped, then reversed back past entry)")
print(f"     - of those, genuine move  : {genuine}   (stopped and stayed against you)")
if rev_pts:
    print(f"  avg reversal after fakeout   : {np.mean(rev_pts):.0f} index pts past entry")
print(f"\n  => {reversed_fakeout}/{stopped} of the morning stop-outs were fakeouts that reversed.")
print(f"\nDATES where the first trade was a fakeout (stopped then reversed):")
for d, ot, E, sl, rev in detail:
    print(f"  {d}  {('CALL' if ot=='CE' else 'PUT'):<4} entry {E:.0f}  stop {sl:.0f}  reversed {rev:.0f} pts past entry")
