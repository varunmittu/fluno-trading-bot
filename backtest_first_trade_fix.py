"""
backtest_first_trade_fix.py (2026-07-27) — Sai: the first trade often loses big
then the bot claws it back. Can we make the first trade hurt less? Test on 6mo
real Kite data:
  V0 BASELINE           — current bot
  V1 SKIP first trade    — don't take the day's first signal at all
  V2 first-trade SL 500  — tighter stop on trade #1 only
  V3 first-trade SL 300  — even tighter on trade #1
  V4 start 10:45         — skip the volatile first 30 min
  V5 start 11:15         — skip the whole first hour
Also breaks out first-trade P&L vs rest-of-day P&L on the baseline.
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
rows, cur, end = [], datetime.now() - timedelta(days=182), datetime.now()
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
print(f"6-month real data: {f['day'].nunique()} days\n")

DELTA, BRK, UNITS = be.DELTA, be.MODEL_BROKERAGE, be.UNITS
EOD = be.EOD
WE = be.WINDOW_END
SL_MIN, SL_MAX, RUNGS = be.SL_MIN, be.SL_MAX, be.RUNGS
SLIP, DAILY_LIMIT = 60.0, -1000
dr = {d: f.index[f["day"] == d].tolist() for d in f["day"].unique()}
alld = sorted(f["day"].unique())

def run(skip_first=False, first_sl_cap=None, wstart=dtime(10,15), track_first=False):
    cfg = dict(be.NEW); cfg["cooldown"] = 0
    trades, day_net = [], {}; first_net = []; rest_net = []
    for d in alld:
        prior = [x for x in alld if x < d]
        if len(prior) < 2: continue
        yd_hi = f.loc[dr[prior[-1]],"high"].max(); yd_lo = f.loc[dr[prior[-1]],"low"].min()
        idxs = dr[d]; orr = [i for i in idxs if f.loc[i,"dt"].time() < dtime(10,15)]
        or_hi = max(f.loc[i,"high"] for i in orr) if orr else None
        or_lo = min(f.loc[i,"low"] for i in orr) if orr else None
        dte = 1.41 if d.weekday()==1 else 1.0
        pos, cool, dayhi, ntoday, skipped = None, None, -1e18, 0, False
        for kk, i in enumerate(idxs):
            row = f.loc[i]; px = row["close"]; last = kk==len(idxs)-1
            tclose = (row["dt"]+pd.Timedelta(minutes=5)).time()
            if pos:
                mv = ((px-pos["entry"]) if pos["otype"]=="CE" else (pos["entry"]-px))*DELTA*UNITS
                pnl = mv-BRK; booked=None
                if pnl>pos["peak"]: pos["peak"]=pnl
                fl=None
                for rg in RUNGS:
                    if pos["peak"]>=rg: fl=rg
                    else: break
                if tclose>=EOD or last: booked=(round(pnl,2),"EOD")
                elif fl is not None and pnl<fl: booked=(fl,"STAIR")
                elif fl is None and pnl<=-pos["sl"]: booked=(-pos["sl"],"SL")
                if booked:
                    val,st=booked; gross=val+BRK; sv=max(0.0,pos["inv"]+gross)
                    net=round(gross-be.real_charges(pos["inv"],sv)-SLIP,2)
                    trades.append(net); day_net[d]=day_net.get(d,0.0)+net
                    (first_net if pos["isfirst"] else rest_net).append(net)
                    if st=="SL": cool=row["dt"]+timedelta(minutes=cfg["cooldown"])
                    pos=None
            dhp=dayhi
            if row["high"]>dayhi: dayhi=row["high"]
            if pos: continue
            if day_net.get(d,0.0)<=DAILY_LIMIT: continue
            if not (wstart<=tclose<=WE): continue
            if cool and row["dt"]<cool: continue
            otype,lvl,isbrk=None,None,True
            if px>yd_hi: otype,lvl="CE",yd_hi
            elif px<yd_lo: otype,lvl="PE",yd_lo
            elif or_lo is not None and px<or_lo: otype,lvl="PE",or_lo
            elif or_hi is not None and px>or_hi: otype,lvl="CE",or_hi
            if tclose>dtime(12,30) and otype!="PE" and dhp>0 and px<=dhp*(1-cfg["fade"]):
                otype,lvl,isbrk="PE",dhp*(1-cfg["fade"]),False
            if otype is None: continue
            if pd.isna(row["htf_st"]): continue
            if otype=="CE" and not (row["htf_st"]==1 and bool(row["htf_macd"])): continue
            if otype=="PE" and not (row["htf_st"]==-1 and not bool(row["htf_macd"])): continue
            prev=f.loc[i-1] if i-1 in f.index else row
            c9=be.conf9(row,otype,abs(px-lvl)/px); cp=conf_pct(row,prev,otype,isbrk)
            if not (c9>=be.MIN_SCORE or cp>72): continue
            if not pd.isna(row["adx"]) and row["adx"]<15: continue
            a=row["atr14"]
            if np.isnan(a): continue
            isfirst = (ntoday==0)
            if skip_first and isfirst and not skipped:
                skipped=True; continue   # skip the day's first signal
            cap = first_sl_cap if (isfirst and first_sl_cap) else SL_MAX
            sl=max(SL_MIN,min(cap,round(a*cfg["atr_mult"]*DELTA*UNITS/10)*10))
            pos={"otype":otype,"entry":px,"sl":sl,"peak":-9e9,"isfirst":isfirst,
                 "inv":round(be.premium_est(px)*dte*UNITS,2)}
            ntoday += 1
    nets=trades; wins=[n for n in nets if n>0]
    eq=pk=mdd=0.0
    for dd in sorted(day_net):
        eq+=day_net[dd]; pk=max(pk,eq); mdd=max(mdd,pk-eq)
    out = dict(net=sum(nets), n=len(nets), wr=len(wins)/len(nets)*100 if nets else 0,
               mdd=mdd, worst=min(day_net.values()) if day_net else 0)
    if track_first:
        out["first_net"]=sum(first_net); out["first_n"]=len(first_net)
        out["rest_net"]=sum(rest_net); out["rest_n"]=len(rest_net)
    return out

b = run(track_first=True)
print("WHERE THE MONEY COMES FROM (baseline):")
print(f"  first trades of the day : {b['first_n']} trades  ->  Rs.{b['first_net']:+,.0f}  (Rs.{b['first_net']/b['first_n']:+.0f}/trade)")
print(f"  all later trades        : {b['rest_n']} trades  ->  Rs.{b['rest_net']:+,.0f}  (Rs.{b['rest_net']/b['rest_n']:+.0f}/trade)\n")

variants = [
    ("V0 BASELINE (current)",           dict()),
    ("V1 SKIP the first trade",         dict(skip_first=True)),
    ("V2 first-trade stop capped 500",  dict(first_sl_cap=500)),
    ("V3 first-trade stop capped 300",  dict(first_sl_cap=300)),
    ("V4 start entries at 10:45",       dict(wstart=dtime(10,45))),
    ("V5 start entries at 11:15",       dict(wstart=dtime(11,15))),
]
print(f"{'variant':<34}{'net':>11}{'trades':>7}{'win%':>6}{'maxDD':>8}{'worstDay':>10}")
for name, kw in variants:
    r = run(**kw)
    print(f"{name:<34}{r['net']:>+11,.0f}{r['n']:>7}{r['wr']:>5.0f}%{r['mdd']:>8,.0f}{r['worst']:>+10,.0f}")
print("\nA fix 'works' only if net stays ~same/higher while worst-day/maxDD improve.")
