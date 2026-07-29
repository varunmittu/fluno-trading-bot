"""
backtest_2days_realprice.py (2026-07-27) — Sai's real question: on 24-Jul (Fri)
and 27-Jul (Mon), using the REAL option premium history (now that the paid Kite
plan is on), did the stop actually hit — and did the option REVERSE up after the
stop? Uses the exact contracts the bot traded (28-Jul monthly expiry):
   24-Jul: NIFTY 23550 PUT   |   27-Jul: NIFTY 24000 CALL
1 lot (65u), Rs.15,000 start, real 5-min premium candles, real stop + staircase.
Shows the full premium path so we can SEE the touch-and-reverse.
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

# find the option instrument tokens
insts = k.instruments("NFO")
sym2tok = {i["tradingsymbol"]: i["instrument_token"] for i in insts if i["name"] == "NIFTY"}
UNITS, BRK = 65, 40
RUNGS = be.RUNGS

# atr-based stop for each day (from the index) — compute quickly via yfinance-free:
# use a fixed Rs.1000/lot stop (the cap that bound on these high-ATR days; the
# live 24-Jul trade carried sl_rs=1000). Good enough for a real-price check.
SL_RS = 1000

def real_replay(symbol, day, entry_after="10:15"):
    if symbol not in sym2tok:
        print(f"  contract {symbol} not found in NFO list"); return
    t = sym2tok[symbol]
    d0 = datetime(day.year, day.month, day.day, 9, 0)
    d1 = datetime(day.year, day.month, day.day, 15, 40)
    cand = k.historical_data(t, d0, d1, "5minute")
    if not cand:
        print(f"  no premium data for {symbol} on {day}"); return
    df = pd.DataFrame(cand)
    df["t"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
    df["hm"] = df["t"].dt.strftime("%H:%M")
    eh, em = map(int, entry_after.split(":"))
    # entry = first candle at/after 10:15
    ent = df[df["t"].dt.time >= dtime(eh, em)]
    if ent.empty:
        print("  no candle after entry time"); return
    ei = ent.index[0]
    entry_prem = float(df.loc[ei, "open"])   # enter at the open of the signal candle
    entry_hm = df.loc[ei, "hm"]
    stop_prem = entry_prem - SL_RS / UNITS
    print(f"  ENTER {symbol}  {entry_hm}  premium Rs.{entry_prem:.1f}  "
          f"(stop if premium falls to Rs.{stop_prem:.1f} = -Rs.{SL_RS})")
    peak, floor, booked = -9e9, None, None
    path = []
    for i in range(ei, len(df)):
        prem = float(df.loc[i, "close"])
        pnl = (prem - entry_prem) * UNITS - BRK
        path.append((df.loc[i, "hm"], prem, pnl))
        if booked is None:
            if pnl > peak: peak = pnl
            fl = None
            for rg in RUNGS:
                if peak >= rg: fl = rg
                else: break
            floor = fl
            last = i == len(df) - 1 or df.loc[i, "t"].time() >= dtime(15, 25)
            if last: booked = (pnl, df.loc[i, "hm"], "day close")
            elif fl is not None and pnl < fl: booked = (pnl, df.loc[i, "hm"], "profit booked (staircase)")
            elif fl is None and pnl <= -SL_RS: booked = (pnl, df.loc[i, "hm"], "STOP LOSS")
    # show the premium path
    print(f"  {'time':<7}{'premium':>9}{'P&L':>9}")
    exit_hm = booked[1] if booked else None
    for hm, prem, pnl in path:
        mark = "  <-- EXIT" if hm == exit_hm and booked else ""
        # only print every candle up to a bit past exit to keep it readable
        print(f"  {hm:<7}{prem:>9.1f}{pnl:>+9.0f}{mark}")
        if booked and hm == exit_hm:
            # after exit, show whether it reversed
            after = [p for (h, p, _) in path if h > exit_hm]
            if after:
                hi_after = max(after)
                pnl_if_held = (hi_after - entry_prem) * UNITS - BRK
                print(f"  ... after the exit, premium rose as high as Rs.{hi_after:.1f} "
                      f"(would've been Rs.{pnl_if_held:+.0f} if held)")
            break
    net = booked[0] if booked else 0
    print(f"  RESULT: Rs.{net:+.0f}  [{booked[2] if booked else 'n/a'}]\n")
    return net

bal = 15000.0
print("\n############ 24-JUL (FRIDAY) — bot bought NIFTY 23550 PUT ############")
n1 = real_replay("NIFTY26JUL23550PE", datetime(2026, 7, 24).date())
if n1 is not None: bal += n1
print("############ 27-JUL (MONDAY) — bot bought NIFTY 24000 CALL ############")
n2 = real_replay("NIFTY26JUL24000CE", datetime(2026, 7, 27).date())
if n2 is not None: bal += n2

print(f"{'='*52}\nSTART Rs.15,000  ->  FINAL Rs.{bal:,.0f}   ({bal-15000:+,.0f})\n{'='*52}")
print("This uses the REAL traded option prices — not the estimate.")
