"""
backtest_15k_account.py (2026-07-27) — Sai's real question: "I put Rs.15,000
in June 2026. Trading the bot to 27-Jul, what is my P&L, each trade + final
settlement?" This is an ACCOUNT simulation on REAL Kite 5-min data:
  * start capital Rs.15,000 on the first June trading day
  * 1 lot (65u) — what Rs.15k affords; skip a signal if the premium won't fit
  * real Zerodha charges + Rs.60/trade slippage (honest fills)
  * daily loss stop -Rs.1,000, v5.1 logic + conf72 gate + ADX<15 chop guard
Prints EVERY trade with running balance, then the final settlement.
STILL a backtest: assumes every signal is taken; real live lags 1-3 min.
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

# fetch real 5-min NIFTY 50 index, May 20 (warmup) -> now
NIFTY50 = 256265
end = datetime.now(); start = datetime(2026, 5, 20)
rows, cur = [], start
while cur < end:
    nxt = min(cur + timedelta(days=90), end)
    rows += k.historical_data(NIFTY50, cur, nxt, "5minute")
    cur = nxt + timedelta(minutes=5)
df = pd.DataFrame(rows).rename(columns={"date": "dt"})
df["dt"] = pd.to_datetime(df["dt"]).dt.tz_localize(None).astype("datetime64[ns]")
df = df[["dt","open","high","low","close","volume"]].drop_duplicates("dt").sort_values("dt").reset_index(drop=True)

f = add_adx(be.prep(df))
r = df.set_index("dt")
h = pd.DataFrame({"open": r["open"].resample("15min").first(),
                  "high": r["high"].resample("15min").max(),
                  "low":  r["low"].resample("15min").min(),
                  "close":r["close"].resample("15min").last()}).dropna().reset_index()
h["st_dir"] = be.supertrend(h)
mh, sh = be.macd(h["close"]); h["macd_up"] = mh > sh
h["usable_from"] = (h["dt"] + pd.Timedelta(minutes=10)).astype("datetime64[ns]")
f["dt"] = f["dt"].astype("datetime64[ns]")
j = pd.merge_asof(f[["dt"]].sort_values("dt"), h[["usable_from","st_dir","macd_up"]].sort_values("usable_from"),
                  left_on="dt", right_on="usable_from", direction="backward")
f["htf_st"] = j["st_dir"].values; f["htf_macd"] = j["macd_up"].values

# ---- account sim from June 1 ----
DELTA, BRK, UNITS = be.DELTA, be.MODEL_BROKERAGE, be.UNITS
EOD, WS, WE = be.EOD, be.WINDOW_START, be.WINDOW_END
SL_MIN, SL_MAX, RUNGS = be.SL_MIN, be.SL_MAX, be.RUNGS
cfg = dict(be.NEW); cfg["cooldown"] = 0
SLIP, DAILY_LIMIT, START_CAP = 60.0, -1000, 15000.0

dr = {d: f.index[f["day"] == d].tolist() for d in f["day"].unique()}
alld = sorted(f["day"].unique())
days = [d for d in alld if d >= datetime(2026, 6, 1).date()]
bal = START_CAP; log = []; day_net = {}; charges_total = 0.0; skipped_afford = 0
peak_eq = START_CAP; mdd = 0.0

for d in days:
    prior = [x for x in alld if x < d]
    if len(prior) < 2: continue
    yd_hi = f.loc[dr[prior[-1]], "high"].max(); yd_lo = f.loc[dr[prior[-1]], "low"].min()
    idxs = dr[d]
    orr = [i for i in idxs if f.loc[i, "dt"].time() < dtime(10, 15)]
    or_hi = max(f.loc[i,"high"] for i in orr) if orr else None
    or_lo = min(f.loc[i,"low"] for i in orr) if orr else None
    dte = 1.41 if d.weekday() == 1 else 1.0
    pos, cool, dayhi = None, None, -1e18
    for kk, i in enumerate(idxs):
        row = f.loc[i]; px = row["close"]; last = kk == len(idxs)-1
        tclose = (row["dt"] + pd.Timedelta(minutes=5)).time()
        if pos:
            mv = ((px-pos["entry"]) if pos["otype"]=="CE" else (pos["entry"]-px))*DELTA*UNITS
            pnl = mv - BRK; booked = None
            if pnl > pos["peak"]: pos["peak"] = pnl
            fl = None
            for rg in RUNGS:
                if pos["peak"] >= rg: fl = rg
                else: break
            if tclose >= EOD or last: booked = (round(pnl,2), "EOD")
            elif fl is not None and pnl < fl: booked = (fl, "STAIR")
            elif fl is None and pnl <= -pos["sl"]: booked = (-pos["sl"], "SL")
            if booked:
                val, st = booked; gross = val + BRK
                sv = max(0.0, pos["inv"]+gross); chg = be.real_charges(pos["inv"], sv)
                net = round(gross - chg - SLIP, 2)
                bal += net; charges_total += chg + SLIP
                day_net[d] = day_net.get(d,0.0)+net
                peak_eq = max(peak_eq, bal); mdd = max(mdd, peak_eq-bal)
                log.append((d, pos["t"], pos["otype"], pos["entry"], px, net, st, bal))
                if st == "SL": cool = row["dt"] + timedelta(minutes=cfg["cooldown"])
                pos = None
        dhp = dayhi
        if row["high"] > dayhi: dayhi = row["high"]
        if pos: continue
        if day_net.get(d,0.0) <= DAILY_LIMIT: continue
        if not (WS <= tclose <= WE): continue
        if cool and row["dt"] < cool: continue
        otype, lvl, isbrk = None, None, True
        if px > yd_hi: otype, lvl = "CE", yd_hi
        elif px < yd_lo: otype, lvl = "PE", yd_lo
        elif or_lo is not None and px < or_lo: otype, lvl = "PE", or_lo
        elif or_hi is not None and px > or_hi: otype, lvl = "CE", or_hi
        if tclose > dtime(12,30) and otype != "PE" and dhp > 0 and px <= dhp*(1-cfg["fade"]):
            otype, lvl, isbrk = "PE", dhp*(1-cfg["fade"]), False
        if otype is None: continue
        if pd.isna(row["htf_st"]): continue
        if otype=="CE" and not (row["htf_st"]==1 and bool(row["htf_macd"])): continue
        if otype=="PE" and not (row["htf_st"]==-1 and not bool(row["htf_macd"])): continue
        prev = f.loc[i-1] if i-1 in f.index else row
        c9 = be.conf9(row, otype, abs(px-lvl)/px); cp = conf_pct(row, prev, otype, isbrk)
        if not (c9 >= be.MIN_SCORE or cp > 72): continue
        if not pd.isna(row["adx"]) and row["adx"] < 15: continue
        a = row["atr14"]
        if np.isnan(a): continue
        inv = round(be.premium_est(px)*dte*UNITS, 2)
        if inv > bal: skipped_afford += 1; continue   # can't afford 1 lot
        sl = max(SL_MIN, min(SL_MAX, round(a*cfg["atr_mult"]*DELTA*UNITS/10)*10))
        pos = {"otype":otype, "entry":px, "sl":sl, "peak":-9e9, "t":tclose, "inv":inv}

print(f"\n{'='*72}\nACCOUNT: start Rs.15,000 on {days[2] if len(days)>2 else days[0]}  (1 lot, real charges + slippage)\n{'='*72}")
print(f"{'date':<12}{'time':<7}{'dir':<5}{'entry':>8}{'exit':>9}{'P&L':>9}{'balance':>11}  how")
for d, t, ot, en, ex, net, st, b in log:
    print(f"{str(d):<12}{t.strftime('%H:%M'):<7}{ot:<5}{en:>8.0f}{ex:>9.0f}{net:>+9.0f}{b:>11,.0f}  {st}")

nets = [x[5] for x in log]; wins = [n for n in nets if n > 0]
green = sum(1 for v in day_net.values() if v > 0); red = sum(1 for v in day_net.values() if v < 0)
print(f"\n{'='*72}\nFINAL SETTLEMENT (June 1 → July 27, 2026)\n{'='*72}")
print(f"  trades taken        : {len(nets)}")
print(f"  wins / losses       : {len(wins)} / {len(nets)-len(wins)}   ({len(wins)/len(nets)*100:.0f}% win)" if nets else "  no trades")
print(f"  green / red days    : {green} / {red}")
print(f"  gross+charges paid  : Rs.{charges_total:,.0f}")
print(f"  TOTAL NET P&L       : Rs.{sum(nets):+,.0f}")
print(f"  STARTING CAPITAL    : Rs.15,000")
print(f"  FINAL BALANCE       : Rs.{bal:,.0f}   ({(bal/START_CAP-1)*100:+.1f}%)")
print(f"  best day / worst day: Rs.{max(day_net.values()):+,.0f} / Rs.{min(day_net.values()):+,.0f}")
print(f"  max drawdown        : Rs.{mdd:,.0f}")
if skipped_afford: print(f"  (skipped {skipped_afford} signals — premium didn't fit the balance)")
print(f"\n  NOTE: real prices, but a BACKTEST — assumes every signal taken instantly.")
print(f"  Your real live copy would be 1-3 min slower, so somewhat lower.")
