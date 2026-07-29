"""
backtest_banknifty_6mo.py (2026-07-28) — Sai asked: backtest BANK NIFTY 6mo,
same v5.1 strategy. Uses REAL Kite 5-min BANKNIFTY index data. BN lot = 30
units. Also fetches a REAL current BN monthly option premium to show the
1-lot cost (the affordability blocker). Compares directional edge to NIFTY.
"""
import sys, os, re
from datetime import datetime, timedelta, time as dtime
sys.path.insert(0, r"C:\Users\avina\Downloads\varun trading")
import pandas as pd, numpy as np
import bt_engine as be
from kiteconnect import KiteConnect

BASE = r"C:\Users\avina\Downloads\varun trading"
api_key = ""
for line in open(os.path.join(BASE, "config.py.txt")):
    m = re.match(r'\s*API_KEY\s*=\s*["\']([^"\']+)["\']', line)
    if m: api_key = m.group(1)
tok = open(os.path.join(BASE, "kite_token.txt")).read().strip()
k = KiteConnect(api_key=api_key); k.set_access_token(tok)
print("connected:", k.profile().get("user_name"))

BANKNIFTY = 260105  # NSE:NIFTY BANK index token
rows, cur, end = [], datetime.now() - timedelta(days=182), datetime.now()
while cur < end:
    nxt = min(cur + timedelta(days=90), end)
    rows += k.historical_data(BANKNIFTY, cur, nxt, "5minute")
    cur = nxt + timedelta(minutes=5)
df = pd.DataFrame(rows).rename(columns={"date": "dt"})
df["dt"] = pd.to_datetime(df["dt"]).dt.tz_localize(None).astype("datetime64[ns]")
df = df[["dt","open","high","low","close","volume"]].drop_duplicates("dt").sort_values("dt").reset_index(drop=True)
print(f"BANKNIFTY real 5-min candles: {len(df)}  spot now ~{round(df['close'].iloc[-1])}")

# build frame like bt_engine.build()
f = be.prep(df)
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

# run engine with BN lot size (30 units instead of NIFTY's 65)
be.UNITS = 30
print(f"trading days: {f['day'].nunique()}\n")
for label, slip in [("FRICTIONLESS", 0.0), ("REALISTIC (Rs.60 slip)", 60.0)]:
    res = be.run(f, be.NEW, slip_rt=slip)
    print(f"=== BANKNIFTY {label} (30u lot) ===")
    print(f"  net Rs.{res['net']:+,.0f}  trades {res['n']}  win {res['wr']:.0f}%  per-trade Rs.{res['per']:+.0f}  maxDD Rs.{res['mdd']:,.0f}  worst Rs.{res['worst']:+,.0f}\n")

# --- affordability: real BN monthly option premium ---
try:
    insts = k.instruments("NFO")
    spot = df["close"].iloc[-1]
    atm = round(spot/100)*100
    bn = [i for i in insts if i["name"]=="BANKNIFTY" and i["instrument_type"] in ("CE","PE")]
    exps = sorted({i["expiry"] for i in bn if i["expiry"]})
    near = exps[0] if exps else None
    cand = [i for i in bn if i["expiry"]==near and i["instrument_type"]=="CE" and abs(i["strike"]-atm)<=100]
    if cand:
        sym = cand[0]["tradingsymbol"]
        ltp = k.ltp(["NFO:"+sym])["NFO:"+sym]["last_price"]
        print(f"REAL BANKNIFTY option: {sym}  premium Rs.{ltp}/unit")
        print(f"  1 lot = 30 units = Rs.{ltp*30:,.0f} to buy   (your capital: Rs.15,000)")
        print(f"  nearest expiry: {near}  (BN is monthly-only)")
except Exception as e:
    print("BN option lookup failed:", e)
