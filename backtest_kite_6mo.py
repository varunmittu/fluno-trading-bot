"""
backtest_kite_6mo.py (2026-07-27) — now that the PAID Kite plan unlocks
Historical chart data, run the proven v5.1 engine on ~6 MONTHS of REAL 5-min
NIFTY data (previously impossible: yfinance caps at 60d, free Kite blocked it).
Uses NIFTY 50 index (token 256265). Reports frictionless + realistic slippage.
CAVEAT printed below: index historical has volume=0, so the conf-score volume
sub-point can't fire — slightly fewer/أdifferent trades vs the yfinance runs.
"""
import sys, os, re
from datetime import datetime, timedelta
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

k = KiteConnect(api_key=api_key)
k.set_access_token(tok)
print("connected:", k.profile().get("user_name"))

NIFTY50 = 256265
end = datetime.now()
start = end - timedelta(days=182)
rows, cur = [], start
while cur < end:
    nxt = min(cur + timedelta(days=90), end)
    data = k.historical_data(NIFTY50, cur, nxt, "5minute")
    rows += data
    print(f"  fetched {cur.date()} → {nxt.date()}: {len(data)} candles")
    cur = nxt + timedelta(minutes=5)

df = pd.DataFrame(rows)
df = df.rename(columns={"date": "dt"})
df["dt"] = pd.to_datetime(df["dt"]).dt.tz_localize(None).astype("datetime64[ns]")
df = df[["dt", "open", "high", "low", "close", "volume"]].drop_duplicates("dt").sort_values("dt").reset_index(drop=True)
print(f"\nTOTAL real 5-min candles: {len(df)}  range {df['dt'].min()} → {df['dt'].max()}")

# build frame exactly like bt_engine.build(), but from Kite data
f = be.prep(df)
r = df.set_index("dt")
h = pd.DataFrame({"open": r["open"].resample("15min").first(),
                  "high": r["high"].resample("15min").max(),
                  "low":  r["low"].resample("15min").min(),
                  "close":r["close"].resample("15min").last()}).dropna().reset_index()
h["st_dir"] = be.supertrend(h)
mh, sh = be.macd(h["close"]); h["macd_up"] = mh > sh
h["usable_from"] = (h["dt"] + pd.Timedelta(minutes=10)).astype("datetime64[ns]")
f["dt"] = f["dt"].astype("datetime64[ns]")
j = pd.merge_asof(f[["dt"]].sort_values("dt"),
                  h[["usable_from", "st_dir", "macd_up"]].sort_values("usable_from"),
                  left_on="dt", right_on="usable_from", direction="backward")
f["htf_st"] = j["st_dir"].values
f["htf_macd"] = j["macd_up"].values

ndays = f["day"].nunique()
print(f"trading days: {ndays}\n")

for label, slip in [("FRICTIONLESS (optimistic)", 0.0), ("REALISTIC (Rs.60 slippage/trade)", 60.0)]:
    res = be.run(f, be.NEW, slip_rt=slip)
    print(f"=== {label} ===")
    print(f"  net Rs.{res['net']:+,.0f}   trades {res['n']}   win {res['wr']:.0f}%   "
          f"per-trade Rs.{res['per']:+.0f}")
    print(f"  max drawdown Rs.{res['mdd']:,.0f}   worst day Rs.{res['worst']:+,.0f}\n")

print("CAVEAT: index data has volume=0, so the volume confidence point never")
print("fires here — real live trades (which have option volume via Kite) may")
print("score slightly higher. This is REAL 5-min price data, not approximation.")
