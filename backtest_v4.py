"""
Backtest — Strategy v4 (dynamic SL + trend riding + gated trades)
Period: 1 Mar 2026 → 2 Jul 2026.

Data (yfinance — Kite historical API needs a paid add-on):
  - Mar 1  – May 5 : HOURLY candles (yfinance only keeps 5-min for 60 days)
  - May 6 – Jul 2  : 5-MIN candles (same resolution the live bot uses)
March/April results are therefore approximate (exits checked once per hour).

Simulates the exact bot rules:
  signal (break yday H/L, else morning direction), entry 10:15-12:30,
  Monday expiry skip, dynamic SL 100-500 by confidence, BE lock 300,
  small trail 400/150, ride winners >=1000 while supertrend+MACD aligned
  (book on weakening or peak-300), EOD close, daily limit -750,
  SL cooldown 10 min, max 3 trades/day (#2/#3 need conf>=50, assumed
  CONFIRMED), lot scaling 3->15 on wins, reset 3 on loss.
Invest amount = estimated option premium (IV 14%) x units.
"""
import os, re, math
from datetime import datetime, date, time as dtime, timedelta
import pandas as pd, numpy as np
import yfinance as yf

os.chdir(os.path.dirname(os.path.abspath(__file__)))

# ── Indicators (identical to app.py) ─────────────────────────────────────────
def rsi(s, period=14):
    d = s.diff(); g = d.clip(lower=0); l = -d.clip(upper=0)
    ag = g.rolling(period).mean(); al = l.rolling(period).mean()
    rs = ag / al.replace(0, np.nan)
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
    hl2 = (hi + lo) / 2
    bub, blb = hl2 + mult*atr, hl2 - mult*atr
    fub, flb = bub.copy(), blb.copy()
    dr = np.ones(n)
    for i in range(1, n):
        if atr[i] == 0: dr[i] = dr[i-1]; continue
        fub[i] = bub[i] if bub[i] < fub[i-1] or cl[i-1] > fub[i-1] else fub[i-1]
        flb[i] = blb[i] if blb[i] > flb[i-1] or cl[i-1] < flb[i-1] else flb[i-1]
        dr[i]  = (1 if cl[i] >= flb[i] else -1) if dr[i-1] == 1 else (-1 if cl[i] <= fub[i] else 1)
    return pd.Series(dr, index=d.index)

def prep(frame):
    frame = frame.sort_values("dt").reset_index(drop=True)
    frame["day"]      = frame["dt"].dt.date
    frame["rsi"]      = rsi(frame["close"])
    frame["sma20"]    = frame["close"].rolling(20).mean()
    frame["sma50"]    = frame["close"].rolling(50).mean()
    m_, s_            = macd(frame["close"])
    frame["macd"]     = m_
    frame["macd_sig"] = s_
    frame["vol_avg"]  = frame["volume"].rolling(20).mean()
    frame["st_dir"]   = supertrend(frame)
    return frame

def get_yf(interval, start=None, end=None, period=None):
    kw = {"interval": interval, "progress": False}
    if period: kw["period"] = period
    else:      kw["start"], kw["end"] = start, end
    d = yf.download("^NSEI", **kw)
    d = d.reset_index()
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = [c[0].lower().replace(" ", "") for c in d.columns]
    else:
        d.columns = [str(c).lower().replace(" ", "") for c in d.columns]
    tcol = next(c for c in d.columns if c in ("datetime", "date", "index", "timestamp"))
    d = d.rename(columns={tcol: "dt"})
    d["dt"] = pd.to_datetime(d["dt"])
    try:    d["dt"] = d["dt"].dt.tz_localize(None)
    except Exception: pass
    return d[["dt", "open", "high", "low", "close", "volume"]]

print("Fetching hourly candles (Mar-Apr)...")
df_h = prep(get_yf("60m", start="2026-02-16", end="2026-05-06"))
print(f"  {len(df_h)} hourly candles ({df_h['day'].min()} -> {df_h['day'].max()})")
print("Fetching 5-min candles (May-Jul)...")
df_5 = prep(get_yf("5m", period="60d"))
print(f"  {len(df_5)} 5-min candles ({df_5['day'].min()} -> {df_5['day'].max()})")

# ── Strategy params (same as app.py) ─────────────────────────────────────────
SL_MIN, SL_MAX   = 100, 500
BE_START = BE_FLOOR = 300
SMALL_START, SMALL_DROP = 400, 150
BIG_START, BIG_SAFETY   = 1000, 300
DAILY_LIMIT      = -750
MAX_TRADES       = 3
CONF_GATE        = 50
DELTA, BROKERAGE = 0.40, 20
BASE_LOTS, MAX_LOTS, CAP_PER_LOT = 3, 15, 10000/3
START_CAPITAL    = 10000.0

def confidence(frame, i, otype, is_break):
    row, prev = frame.iloc[i], frame.iloc[i-1]
    bd = 0
    vol_ok = (not np.isnan(row["vol_avg"])) and row["volume"] > row["vol_avg"] * 1.1
    if otype == "CE":
        if row["rsi"] < 50:               bd += 15
        if row["macd"] > row["macd_sig"]: bd += 12
        if vol_ok:                        bd += 5
        if row["sma20"] > row["sma50"]:   bd += 10
        if row["close"] > row["sma50"]:   bd += 5
        if row["sma20"] > prev["sma20"]:  bd += 3
        st_ok = row["st_dir"] == 1
    else:
        if row["rsi"] > 50:               bd += 15
        if row["macd"] < row["macd_sig"]: bd += 12
        if vol_ok:                        bd += 5
        if row["sma20"] < row["sma50"]:   bd += 10
        if row["close"] < row["sma50"]:   bd += 5
        if row["sma20"] < prev["sma20"]:  bd += 3
        st_ok = row["st_dir"] == -1
    total = bd + (10 if st_ok else 0) + (10 if is_break else 0)
    conf  = max(5, min(95, round(total / 70 * 100)))
    sl    = SL_MIN + int(round(conf/100 * (SL_MAX-SL_MIN) / 50)) * 50
    return conf, max(SL_MIN, min(SL_MAX, sl))

def trend_strong(frame, i, otype):
    row = frame.iloc[i]
    st = row["st_dir"] == (1 if otype == "CE" else -1)
    mc = (row["macd"] > row["macd_sig"]) if otype == "CE" else (row["macd"] < row["macd_sig"])
    return bool(st and mc)

def premium_est(spot):
    return round(spot * 0.14 * math.sqrt(4/365) * 0.4 * 0.98, 1)

# ── Simulation over one data stream ──────────────────────────────────────────
def simulate(frame, trade_from, trade_to, capital, lots, trades):
    day_rows = {d: frame.index[frame["day"] == d].tolist() for d in frame["day"].unique()}
    all_d    = sorted(frame["day"].unique())
    days     = [d for d in all_d if trade_from <= d <= trade_to]

    for d in days:
        if d.weekday() == 0:              # Monday = NIFTY expiry → skip
            continue
        prior = [x for x in all_d if x < d]
        if not prior:
            continue
        yd    = prior[-1]
        yd_hi = frame.loc[day_rows[yd], "high"].max()
        yd_lo = frame.loc[day_rows[yd], "low"].min()
        idxs  = day_rows[d]
        f     = frame.loc[idxs[0]]
        morn  = "CE" if f["close"] >= f["open"] else "PE"

        daily_pnl, trades_today = 0.0, 0
        pos, pending = None, None
        cooldown_until, gate_cooldown = None, None

        for k, i in enumerate(idxs):
            row  = frame.loc[i]
            t    = row["dt"].time()
            px   = row["close"]
            last = (k == len(idxs) - 1)

            if pos:
                units = pos["lots"] * 25
                mv  = (px - pos["entry"])*DELTA*units if pos["otype"] == "CE" else (pos["entry"] - px)*DELTA*units
                pnl = mv - BROKERAGE
                if pnl > pos["peak"]:
                    pos["peak"] = pnl
                peak = pos["peak"]
                if peak >= BE_START:
                    pos["locked"] = True
                booked = None
                if t >= dtime(15, 25) or last:
                    booked = (round(pnl, 0), "EOD_EXIT")
                elif pos["locked"] and pnl < BE_FLOOR:
                    booked = (BE_FLOOR, "BE_LOCK")
                elif not pos["locked"] and pnl <= -pos["sl"]:
                    booked = (-pos["sl"], "STOP_LOSS")
                elif peak >= BIG_START:
                    if not trend_strong(frame, i, pos["otype"]) or pnl <= peak - BIG_SAFETY:
                        booked = (round(pnl, 0), "TRAIL_EXIT")
                elif peak >= SMALL_START and pnl <= peak - SMALL_DROP:
                    v = max(BE_FLOOR, round(pnl, 0)) if pos["locked"] else round(pnl, 0)
                    booked = (v, "PROFIT_LOCK" if v > 0 else "STOP_LOSS")
                if booked:
                    val, status = booked
                    capital   += val
                    daily_pnl += val
                    lots = min(MAX_LOTS, max(BASE_LOTS, int(capital // CAP_PER_LOT))) if val > 0 else BASE_LOTS
                    trades.append({
                        "date": d, "time": pos["t_in"], "no": pos["no"], "otype": pos["otype"],
                        "sig": pos["sig"], "conf": pos["conf"], "sl": pos["sl"],
                        "lots": pos["lots"], "invest": pos["invest"],
                        "entry": pos["entry"], "exit": px, "pnl": val, "status": status,
                        "cap": capital,
                    })
                    if status == "STOP_LOSS":
                        cooldown_until = row["dt"] + timedelta(minutes=10)
                    trades_today += 1
                    pos = None

            if daily_pnl <= DAILY_LIMIT:
                pending = None
                continue
            if pos or trades_today >= MAX_TRADES:
                continue
            if not (dtime(10, 15) <= t <= dtime(12, 30)):
                pending = None
                continue
            if cooldown_until and row["dt"] < cooldown_until:
                continue

            if pending:                            # gated + confirmed → enter now
                units = lots * 25
                pos = {"otype": pending["otype"], "entry": px, "sl": pending["sl"],
                       "conf": pending["conf"], "sig": pending["sig"], "lots": lots,
                       "peak": -9999, "locked": False, "no": trades_today + 1,
                       "t_in": t.strftime("%H:%M"), "invest": round(premium_est(px) * units)}
                pending = None
                continue

            if px > yd_hi:   otype, sig, brk = "CE", "BREAK-HI", True
            elif px < yd_lo: otype, sig, brk = "PE", "BREAK-LO", True
            else:            otype, sig, brk = morn, "MORN-" + morn, False

            conf, sl = confidence(frame, i, otype, brk)
            if trades_today == 0:                  # first trade: automatic
                units = lots * 25
                pos = {"otype": otype, "entry": px, "sl": sl, "conf": conf, "sig": sig,
                       "lots": lots, "peak": -9999, "locked": False, "no": 1,
                       "t_in": t.strftime("%H:%M"), "invest": round(premium_est(px) * units)}
            else:
                if gate_cooldown and row["dt"] < gate_cooldown:
                    continue
                if conf >= CONF_GATE:
                    pending = {"otype": otype, "sig": sig, "conf": conf, "sl": sl}
                else:
                    gate_cooldown = row["dt"] + timedelta(minutes=15)

    return capital, lots

# ── Run: hourly Mar 1-May 5, then 5-min May 6-Jul 2 (capital flows through) ──
trades = []
capital, lots = START_CAPITAL, BASE_LOTS
capital, lots = simulate(df_h, date(2026, 3, 1), date(2026, 5, 5), capital, lots, trades)
capital, lots = simulate(df_5, date(2026, 5, 6), date(2026, 7, 2), capital, lots, trades)

# ── Report ───────────────────────────────────────────────────────────────────
tdf = pd.DataFrame(trades)
tdf["month"] = tdf["date"].apply(lambda x: x.strftime("%B %Y"))

print("\n" + "=" * 104)
print("BACKTEST — STRATEGY v4 | 1 Mar 2026 - 2 Jul 2026 | Start capital Rs.10,000")
print("Mar-Apr on hourly candles (approx) | May-Jul on 5-min candles | gated trades assumed CONFIRMED")
print("=" * 104)

for month in tdf["month"].unique():
    sub = tdf[tdf["month"] == month]
    print(f"\n### {month} ###")
    print(f"{'Date':<12}{'Time':<7}{'#':<3}{'Type':<5}{'Signal':<9}{'Conf':<6}{'SL':<5}"
          f"{'Lots':<5}{'Invest':<9}{'Entry':<8}{'Exit':<8}{'P&L':<8}{'Status':<12}{'Capital':<9}")
    for _, r in sub.iterrows():
        print(f"{str(r['date']):<12}{r['time']:<7}{r['no']:<3}{r['otype']:<5}{r['sig']:<9}"
              f"{str(r['conf'])+'%':<6}{r['sl']:<5}{str(r['lots'])+'L':<5}"
              f"{r['invest']:<9.0f}{r['entry']:<8.0f}{r['exit']:<8.0f}"
              f"{r['pnl']:<+8.0f}{r['status']:<12}{r['cap']:<9.0f}")
    w = (sub["pnl"] > 0).sum(); n = len(sub)
    print(f"--- {month}: {n} trades | W:{w} L:{n-w} | win {w/n*100:.0f}% | "
          f"P&L Rs.{sub['pnl'].sum():+,.0f} | month-end capital Rs.{sub['cap'].iloc[-1]:,.0f}")

w = (tdf["pnl"] > 0).sum(); n = len(tdf)
print("\n" + "=" * 104)
print(f"TOTAL: {n} trades | W:{w} L:{n-w} | win rate {w/n*100:.1f}%")
print(f"P&L: Rs.{tdf['pnl'].sum():+,.0f} | Final capital: Rs.{capital:,.0f} "
      f"({(capital-START_CAPITAL)/START_CAPITAL*100:+.1f}%)")
print(f"Best trade: Rs.{tdf['pnl'].max():+,.0f} | Worst: Rs.{tdf['pnl'].min():+,.0f}")
if (tdf["pnl"] > 0).any() and (tdf["pnl"] <= 0).any():
    print(f"Avg win: Rs.{tdf[tdf.pnl>0]['pnl'].mean():+,.0f} | Avg loss: Rs.{tdf[tdf.pnl<=0]['pnl'].mean():+,.0f}")
print("\nExit breakdown:")
for s, r in tdf.groupby("status")["pnl"].agg(["count", "sum"]).iterrows():
    print(f"  {s:<12} x{int(r['count']):<4} Rs.{r['sum']:+,.0f}")
print("=" * 104)
