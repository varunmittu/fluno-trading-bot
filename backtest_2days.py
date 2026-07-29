"""
backtest_2days.py (2026-07-27) — Sai: replay ONLY 24-Jul and 27-Jul with
Rs.15,000, 1 lot, proper live rules (daily stop -1000, ADX15 chop guard,
conf72 gate, real charges + Rs.60 slippage). Show every trade + result.
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
print("connected:", k.profile().get("user_name"), "\n")

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

DELTA, BRK, UNITS = be.DELTA, be.MODEL_BROKERAGE, be.UNITS
EOD, WS, WE = be.EOD, be.WINDOW_START, be.WINDOW_END
SL_MIN, SL_MAX, RUNGS = be.SL_MIN, be.SL_MAX, be.RUNGS
cfg = dict(be.NEW); cfg["cooldown"] = 0
SLIP, DAILY_LIMIT = 60.0, -1000

TARGETS = [datetime(2026,7,24).date(), datetime(2026,7,27).date()]
dr = {d: f.index[f["day"] == d].tolist() for d in f["day"].unique()}
alld = sorted(f["day"].unique())
bal = 15000.0

for d in TARGETS:
    if d not in dr:
        print(f"{d}: no data"); continue
    prior = [x for x in alld if x < d]
    yd_hi = f.loc[dr[prior[-1]],"high"].max(); yd_lo = f.loc[dr[prior[-1]],"low"].min()
    idxs = dr[d]; orr = [i for i in idxs if f.loc[i,"dt"].time() < dtime(10,15)]
    or_hi = max(f.loc[i,"high"] for i in orr) if orr else None
    or_lo = min(f.loc[i,"low"] for i in orr) if orr else None
    dte = 1.41 if d.weekday()==1 else 1.0; pos, cool, dayhi = None, None, -1e18
    day_pnl = 0.0; day_trades = []
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
            if tclose>=EOD or last: booked=(round(pnl,2),"day close")
            elif fl is not None and pnl<fl: booked=(fl,"profit booked")
            elif fl is None and pnl<=-pos["sl"]: booked=(-pos["sl"],"stop loss")
            if booked:
                val,st=booked; gross=val+BRK; sv=max(0.0,pos["inv"]+gross)
                net=round(gross-be.real_charges(pos["inv"],sv)-SLIP,2)
                bal+=net; day_pnl+=net
                day_trades.append((pos["t"],pos["otype"],pos["entry"],px,net,st))
                if st=="stop loss": cool=row["dt"]+timedelta(minutes=cfg["cooldown"])
                pos=None
        dhp=dayhi
        if row["high"]>dayhi: dayhi=row["high"]
        if pos: continue
        if day_pnl<=DAILY_LIMIT: continue
        if not (WS<=tclose<=WE): continue
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
        sl=max(SL_MIN,min(SL_MAX,round(a*cfg["atr_mult"]*DELTA*UNITS/10)*10))
        pos={"otype":otype,"entry":px,"sl":sl,"peak":-9e9,"t":tclose,
             "inv":round(be.premium_est(px)*dte*UNITS,2)}
    wd = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][d.weekday()]
    print(f"===== {d} ({wd}) — yday H/L {yd_hi:.0f}/{yd_lo:.0f} =====")
    if not day_trades:
        print("  no trades taken (no valid signal / blocked by filters)")
    for t,ot,en,ex,net,st in day_trades:
        w = "CALL" if ot=="CE" else "PUT"
        print(f"  {t.strftime('%H:%M')}  {w:<4} entry NIFTY {en:.0f} -> exit {ex:.0f}  "
              f"{'WIN ' if net>0 else 'LOSS'} Rs.{net:+.0f}   [{st}]")
    print(f"  DAY P&L: Rs.{day_pnl:+,.0f}   balance now Rs.{bal:,.0f}\n")

print(f"{'='*50}\nSTART Rs.15,000  ->  FINAL Rs.{bal:,.0f}   ({bal-15000:+,.0f})\n{'='*50}")
print("NOTE: proper rules here = 1 lot + daily stop -1000. Today LIVE the")
print("daily stop was accidentally OFF (the bug we fixed at 11:09), so live")
print("took more trades — that's why live and this differ.")
