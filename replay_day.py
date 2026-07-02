"""
PRACTICE REPLAY — run any past day through the CURRENT v4 strategy.
Usage: py replay_day.py 2026-07-02
Pure simulation: nothing is written to trade_log.db.
Shows candle-by-candle what the bot would have done: signal, dynamic SL,
BE lock, trails, ride-winners, gated trades (assumed /confirm-ed),
capital check, and real Zerodha charges.
"""
import sys, math
from datetime import date, time as dtime, timedelta, datetime
import pandas as pd, numpy as np
import yfinance as yf

TARGET  = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date(2026, 7, 2)
CAPITAL = float(sys.argv[2]) if len(sys.argv) > 2 else 11820.0   # bot capital at day start
LOTS    = 3

# ── indicators (same as app.py) ──────────────────────────────────────────────
def rsi(s, p=14):
    d = s.diff(); g = d.clip(lower=0); l = -d.clip(upper=0)
    rs = g.rolling(p).mean() / l.rolling(p).mean().replace(0, np.nan)
    return 100 - (100/(1+rs))

def macd(s):
    m = s.ewm(span=12).mean() - s.ewm(span=26).mean()
    return m, m.ewm(span=9).mean()

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

print(f"Fetching 5-min NIFTY data around {TARGET}...")
d = yf.download("^NSEI", interval="5m", period="60d", progress=False).reset_index()
if isinstance(d.columns, pd.MultiIndex):
    d.columns = [c[0].lower().replace(" ", "") for c in d.columns]
else:
    d.columns = [str(c).lower().replace(" ", "") for c in d.columns]
tcol = next(c for c in d.columns if c in ("datetime", "date", "index"))
d = d.rename(columns={tcol: "dt"})
d["dt"] = pd.to_datetime(d["dt"])
try:    d["dt"] = d["dt"].dt.tz_localize(None)
except Exception: pass
d = d.sort_values("dt").reset_index(drop=True)
d["day"]      = d["dt"].dt.date
d["rsi"]      = rsi(d["close"])
d["sma20"]    = d["close"].rolling(20).mean()
d["sma50"]    = d["close"].rolling(50).mean()
m_, s_        = macd(d["close"]); d["macd"], d["macd_sig"] = m_, s_
d["vol_avg"]  = d["volume"].rolling(20).mean()
d["st_dir"]   = supertrend(d)

days   = sorted(d["day"].unique())
if TARGET not in days:
    sys.exit(f"No data for {TARGET}")
yd     = [x for x in days if x < TARGET][-1]
yd_hi  = d[d["day"] == yd]["high"].max()
yd_lo  = d[d["day"] == yd]["low"].min()
idxs   = d.index[d["day"] == TARGET].tolist()
f0     = d.loc[idxs[0]]
morn   = "CE" if f0["close"] >= f0["open"] else "PE"

SL_MIN, SL_MAX = 100, 500
BE = 300; SM_S, SM_D = 400, 150; BG_S, BG_SAFE = 100, 300
DAILY_LIMIT, MAX_T, GATE = -750, 3, 50
DELTA, MB = 0.40, 20

def conf_sl(i, otype, brk):
    row, prev = d.iloc[i], d.iloc[i-1]
    bd = 0
    vol_ok = (not np.isnan(row["vol_avg"])) and row["volume"] > row["vol_avg"]*1.1
    checks = ([row["rsi"] < 50, row["macd"] > row["macd_sig"], vol_ok,
               row["sma20"] > row["sma50"], row["close"] > row["sma50"],
               row["sma20"] > prev["sma20"]] if otype == "CE" else
              [row["rsi"] > 50, row["macd"] < row["macd_sig"], vol_ok,
               row["sma20"] < row["sma50"], row["close"] < row["sma50"],
               row["sma20"] < prev["sma20"]])
    pts   = [15, 12, 5, 10, 5, 3]
    score = sum(p for c, p in zip(checks, pts) if c)
    st_ok = row["st_dir"] == (1 if otype == "CE" else -1)
    total = score + (10 if st_ok else 0) + (10 if brk else 0)
    conf  = max(5, min(95, round(total/70*100)))
    sl    = SL_MIN + int(round(conf/100*(SL_MAX-SL_MIN)/50))*50
    return conf, max(SL_MIN, min(SL_MAX, sl))

def strong(i, otype):
    row = d.iloc[i]
    st = row["st_dir"] == (1 if otype == "CE" else -1)
    mc = (row["macd"] > row["macd_sig"]) if otype == "CE" else (row["macd"] < row["macd_sig"])
    return bool(st and mc)

def prem(px): return round(px*0.14*math.sqrt(4/365)*0.4*0.98, 1)

def charges(bv, sv):
    br = 40.0
    return round(br + 0.001*sv + 0.0003503*(bv+sv) + 0.000001*(bv+sv)
                 + 0.00003*bv + 0.18*(br + 0.0003503*(bv+sv) + 0.000001*(bv+sv)), 2)

W = 100
print("\n" + "=" * W)
print(f"PRACTICE REPLAY — {TARGET.strftime('%d %b %Y')} | strategy v4 | start capital Rs.{CAPITAL:,.0f}")
print(f"Yday High: {yd_hi:.0f} | Yday Low: {yd_lo:.0f} | Morning direction: {morn}")
print("=" * W)

capital, lots = CAPITAL, LOTS
daily = 0.0; trades_today = 0
pos = pending = None
cooldown = gatecool = None
results = []

for k, i in enumerate(idxs):
    row = d.loc[i]; t = row["dt"].time(); px = row["close"]
    ts = row["dt"].strftime("%H:%M")
    last = k == len(idxs)-1

    if pos:
        units = pos["lots"]*25
        mv  = (px-pos["e"])*DELTA*units if pos["o"] == "CE" else (pos["e"]-px)*DELTA*units
        pnl = mv - MB
        if pnl > pos["peak"]:
            pos["peak"] = pnl
        pk = pos["peak"]
        if pk >= BE and not pos["lk"]:
            pos["lk"] = True
            print(f"{ts}  BE LOCK ON — peak Rs.{pk:.0f}, floor now Rs.{BE} (cannot lose)")
        booked = None
        if t >= dtime(15, 25) or last:              booked = (round(pnl, 2), "EOD_EXIT")
        elif pos["lk"] and pnl < BE:                booked = (BE, "BE_LOCK")
        elif not pos["lk"] and pnl <= -pos["sl"]:   booked = (-pos["sl"], "STOP_LOSS")
        elif pk >= BG_S:
            if not strong(i, pos["o"]) or pnl <= pk-BG_SAFE:
                booked = (round(pnl, 2), "TRAIL_EXIT")
            else:
                print(f"{ts}  RIDING — peak Rs.{pk:.0f} now Rs.{pnl:.0f} | trend STRONG, holding")
        elif pk >= SM_S and pnl <= pk-SM_D:
            v = max(BE, round(pnl, 2)) if pos["lk"] else round(pnl, 2)
            booked = (v, "PROFIT_LOCK" if v > 0 else "STOP_LOSS")
        if booked:
            val, st_ = booked
            gross = val + MB
            sv    = max(0.0, pos["inv"] + gross)
            ch    = charges(pos["inv"], sv)
            net   = round(gross - ch, 2)
            capital += net; daily += net; trades_today += 1
            lots = min(15, max(3, int(capital//3333.33))) if net > 0 else 3
            results.append((pos["n"], pos["o"], pos["e"], px, val, ch, net, st_, pos["lots"], pos["inv"]))
            print(f"{ts}  EXIT #{pos['n']} {st_} — entry {pos['e']:.0f} exit {px:.0f} | "
                  f"model Rs.{val:+.0f} | charges Rs.{ch:.2f} | NET Rs.{net:+.2f} | capital Rs.{capital:,.2f}")
            if st_ == "STOP_LOSS":
                cooldown = row["dt"] + timedelta(minutes=10)
            pos = None

    if daily <= DAILY_LIMIT:
        pending = None
        continue
    if pos or trades_today >= MAX_T:
        continue
    if not (dtime(10, 15) <= t <= dtime(12, 30)):
        pending = None
        continue
    if cooldown and row["dt"] < cooldown:
        continue

    def open_pos(o, sig, cf, sl, note=""):
        global pos
        p  = prem(px)
        af = int(capital // (p*25))
        if af < 1:
            print(f"{ts}  SKIP — capital Rs.{capital:,.0f} can't afford 1 lot (Rs.{p*25:.0f})")
            return
        use = min(lots, af)
        cap_note = f" (capped from {lots}L by capital)" if use < lots else ""
        inv = round(p*use*25, 2)
        pos = {"o": o, "e": px, "sl": sl, "peak": -9999, "lk": False,
               "lots": use, "inv": inv, "n": trades_today+1}
        lbl = "BULLISH — BUY CE" if o == "CE" else "BEARISH — BUY PE"
        print(f"{ts}  ENTER #{trades_today+1} {lbl} @ {px:.0f} | {sig} | conf {cf}% | "
              f"SL Rs.{sl} | {use}L{cap_note} | invested Rs.{inv:,.0f}{note}")

    if pending:
        open_pos(pending[0], pending[1], pending[2], pending[3], "  [gated, /confirm assumed]")
        pending = None
        continue

    if px > yd_hi:   o, sig, brk = "CE", f"BREAK HIGH {yd_hi:.0f}", True
    elif px < yd_lo: o, sig, brk = "PE", f"BREAK LOW {yd_lo:.0f}", True
    else:            o, sig, brk = morn, f"MORN {morn}", False
    cf, sl = conf_sl(i, o, brk)

    if trades_today == 0:
        open_pos(o, sig, cf, sl)
    else:
        if gatecool and row["dt"] < gatecool:
            continue
        if cf >= GATE:
            print(f"{ts}  GATE — trade #{trades_today+1} signal {sig} | profit prob {cf}% / loss {100-cf}% "
                  f"| Telegram sent, waiting 2 min for /confirm")
            pending = (o, sig, cf, sl)
        else:
            print(f"{ts}  silent skip — confidence {cf}% < 50% (no Telegram, 15 min cooldown)")
            gatecool = row["dt"] + timedelta(minutes=15)

print("\n" + "=" * W)
net_day = sum(r[6] for r in results)
chg_day = sum(r[5] for r in results)
inv_day = sum(r[9] for r in results)
res = f"Profit Rs.{net_day:,.2f}" if net_day > 0 else (f"Loss Rs.{abs(net_day):,.2f}" if net_day < 0 else "Flat")
print(f"{TARGET.strftime('%B')} {TARGET.day} -> Invested: Rs.{inv_day:,.0f} | "
      f"Lot size: {','.join(sorted({str(r[8])+'L' for r in results}))} | Result: {res}")
print(f"Trades: {len(results)} | Charges: Rs.{chg_day:,.2f} | "
      f"Capital: Rs.{CAPITAL:,.0f} -> Rs.{capital:,.2f}")
print("=" * W)
