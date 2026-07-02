"""
Backtest — Strategy v4 + CAPITAL CHECK + REAL ZERODHA CHARGES
Period: 1 Mar 2026 -> 30 Jun 2026 | Start: Rs.10,000

Charges per round trip (Zerodha options, 2026 rates):
  brokerage Rs.20 x 2 orders | STT 0.1% on sell premium
  NSE txn 0.03503% both sides | SEBI Rs.10/crore | stamp 0.003% buy side
  GST 18% on (brokerage + txn + SEBI)

Capital rule: lots capped at capital // (premium x 25); skip if < 1 lot.
Data: hourly candles Mar 1-May 5 (yfinance limit), 5-min May 6-Jun 30.
Gated 2nd/3rd trades assumed CONFIRMED. Premium estimated at IV 14%.
"""
import os, math
from datetime import date, time as dtime, timedelta
import pandas as pd, numpy as np
import yfinance as yf

os.chdir(os.path.dirname(os.path.abspath(__file__)))

# ── indicators (same as app.py) ──────────────────────────────────────────────
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
    f["rsi"] = rsi(f["close"])
    f["sma20"] = f["close"].rolling(20).mean()
    f["sma50"] = f["close"].rolling(50).mean()
    m_, s_ = macd(f["close"]); f["macd"], f["macd_sig"] = m_, s_
    f["vol_avg"] = f["volume"].rolling(20).mean()
    f["st_dir"]  = supertrend(f)
    return f

def get_yf(interval, start=None, end=None, period=None):
    kw = {"interval": interval, "progress": False}
    if period: kw["period"] = period
    else:      kw["start"], kw["end"] = start, end
    d = yf.download("^NSEI", **kw).reset_index()
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

print("Fetching data...")
df_h = prep(get_yf("60m", start="2026-02-16", end="2026-05-06"))
df_5 = prep(get_yf("5m", period="60d"))

# ── params (same as bot) ─────────────────────────────────────────────────────
SL_MIN, SL_MAX = 100, 500
BE_START = BE_FLOOR = 300
SMALL_START, SMALL_DROP = 400, 150
BIG_START, BIG_SAFETY = 1000, 300
DAILY_LIMIT, MAX_TRADES, CONF_GATE = -750, 3, 50
DELTA, MODEL_BROKERAGE = 0.40, 20
BASE_LOTS, MAX_LOTS, CAP_PER_LOT = 3, 15, 10000/3
START_CAPITAL = 10000.0

def confidence(f, i, otype, brk):
    row, prev = f.iloc[i], f.iloc[i-1]
    bd = 0
    vol_ok = (not np.isnan(row["vol_avg"])) and row["volume"] > row["vol_avg"]*1.1
    if otype == "CE":
        if row["rsi"] < 50: bd += 15
        if row["macd"] > row["macd_sig"]: bd += 12
        if vol_ok: bd += 5
        if row["sma20"] > row["sma50"]: bd += 10
        if row["close"] > row["sma50"]: bd += 5
        if row["sma20"] > prev["sma20"]: bd += 3
        st_ok = row["st_dir"] == 1
    else:
        if row["rsi"] > 50: bd += 15
        if row["macd"] < row["macd_sig"]: bd += 12
        if vol_ok: bd += 5
        if row["sma20"] < row["sma50"]: bd += 10
        if row["close"] < row["sma50"]: bd += 5
        if row["sma20"] < prev["sma20"]: bd += 3
        st_ok = row["st_dir"] == -1
    total = bd + (10 if st_ok else 0) + (10 if brk else 0)
    conf  = max(5, min(95, round(total/70*100)))
    sl    = SL_MIN + int(round(conf/100*(SL_MAX-SL_MIN)/50))*50
    return conf, max(SL_MIN, min(SL_MAX, sl))

def trend_strong(f, i, otype):
    row = f.iloc[i]
    st = row["st_dir"] == (1 if otype == "CE" else -1)
    mc = (row["macd"] > row["macd_sig"]) if otype == "CE" else (row["macd"] < row["macd_sig"])
    return bool(st and mc)

def premium_est(spot):
    return round(spot * 0.14 * math.sqrt(4/365) * 0.4 * 0.98, 1)

def real_charges(buy_val, sell_val):
    """Zerodha + statutory charges for one option round trip."""
    brokerage = 20.0 * 2
    stt   = 0.001    * sell_val
    txn   = 0.0003503 * (buy_val + sell_val)
    sebi  = 0.000001  * (buy_val + sell_val)
    stamp = 0.00003   * buy_val
    gst   = 0.18 * (brokerage + txn + sebi)
    return round(brokerage + stt + txn + sebi + stamp + gst, 2)

# ── simulation ───────────────────────────────────────────────────────────────
def simulate(frame, t_from, t_to, capital, lots, day_rows_out, skipped):
    dr = {d: frame.index[frame["day"] == d].tolist() for d in frame["day"].unique()}
    all_d = sorted(frame["day"].unique())
    for d in [x for x in all_d if t_from <= x <= t_to]:
        if d.weekday() == 0:
            skipped.append((d, "expiry day (Mon)"))
            continue
        prior = [x for x in all_d if x < d]
        if not prior: continue
        yd_hi = frame.loc[dr[prior[-1]], "high"].max()
        yd_lo = frame.loc[dr[prior[-1]], "low"].min()
        idxs  = dr[d]
        f0    = frame.loc[idxs[0]]
        morn  = "CE" if f0["close"] >= f0["open"] else "PE"

        day = {"date": d, "inv": 0.0, "lots": set(), "gross": 0.0, "chg": 0.0,
               "net": 0.0, "n": 0, "skips": 0}
        daily_pnl, trades_today = 0.0, 0
        pos, pending = None, None
        cooldown, gatecool = None, None

        for k, i in enumerate(idxs):
            row = frame.loc[i]; t = row["dt"].time(); px = row["close"]
            last = k == len(idxs) - 1

            if pos:
                units = pos["lots"]*25
                mv  = (px-pos["entry"])*DELTA*units if pos["otype"] == "CE" else (pos["entry"]-px)*DELTA*units
                pnl = mv - MODEL_BROKERAGE
                if pnl > pos["peak"]: pos["peak"] = pnl
                peak = pos["peak"]
                if peak >= BE_START: pos["locked"] = True
                booked = None
                if t >= dtime(15, 25) or last:                 booked = (round(pnl, 2), "EOD")
                elif pos["locked"] and pnl < BE_FLOOR:         booked = (BE_FLOOR, "BE")
                elif not pos["locked"] and pnl <= -pos["sl"]:  booked = (-pos["sl"], "SL")
                elif peak >= BIG_START:
                    if not trend_strong(frame, i, pos["otype"]) or pnl <= peak-BIG_SAFETY:
                        booked = (round(pnl, 2), "TRAIL")
                elif peak >= SMALL_START and pnl <= peak-SMALL_DROP:
                    v = max(BE_FLOOR, round(pnl, 2)) if pos["locked"] else round(pnl, 2)
                    booked = (v, "LOCK" if v > 0 else "SL")
                if booked:
                    val, status = booked
                    gross    = val + MODEL_BROKERAGE            # undo model Rs.20
                    sell_val = max(0.0, pos["inv"] + gross)     # premium out
                    chg      = real_charges(pos["inv"], sell_val)
                    net      = round(gross - chg, 2)
                    capital += net
                    daily_pnl += net
                    lots = min(MAX_LOTS, max(BASE_LOTS, int(capital//CAP_PER_LOT))) if net > 0 else BASE_LOTS
                    day["inv"]  += pos["inv"];  day["lots"].add(pos["lots"])
                    day["gross"] += gross;      day["chg"] += chg
                    day["net"]  += net;         day["n"] += 1
                    if status == "SL":
                        cooldown = row["dt"] + timedelta(minutes=10)
                    trades_today += 1
                    pos = None

            if daily_pnl <= DAILY_LIMIT:
                pending = None; continue
            if pos or trades_today >= MAX_TRADES: continue
            if not (dtime(10, 15) <= t <= dtime(12, 30)):
                pending = None; continue
            if cooldown and row["dt"] < cooldown: continue

            def try_open(otype, sig, conf, sl):
                nonlocal pos
                prem = premium_est(px)
                afford = int(capital // (prem*25))
                if afford < 1:
                    day["skips"] += 1
                    return False
                use = min(lots, afford)
                pos = {"otype": otype, "entry": px, "sl": sl, "conf": conf,
                       "lots": use, "peak": -9999, "locked": False,
                       "inv": round(prem*use*25, 2)}
                return True

            if pending:
                try_open(pending["otype"], pending["sig"], pending["conf"], pending["sl"])
                pending = None
                continue

            if px > yd_hi:   otype, sig, brk = "CE", "BRK-HI", True
            elif px < yd_lo: otype, sig, brk = "PE", "BRK-LO", True
            else:            otype, sig, brk = morn, "MORN", False
            conf, sl = confidence(frame, i, otype, brk)

            if trades_today == 0:
                try_open(otype, sig, conf, sl)
            else:
                if gatecool and row["dt"] < gatecool: continue
                if conf >= CONF_GATE:
                    pending = {"otype": otype, "sig": sig, "conf": conf, "sl": sl}
                else:
                    gatecool = row["dt"] + timedelta(minutes=15)

        if day["n"] or day["skips"]:
            day_rows_out.append(day)
    return capital, lots

days_out, skipped = [], []
capital, lots = START_CAPITAL, BASE_LOTS
capital, lots = simulate(df_h, date(2026, 3, 1), date(2026, 5, 5), capital, lots, days_out, skipped)
capital, lots = simulate(df_5, date(2026, 5, 6), date(2026, 6, 30), capital, lots, days_out, skipped)

# ── report ───────────────────────────────────────────────────────────────────
print("\n" + "=" * 96)
print("DAY-BY-DAY REPORT | Strategy v4 + capital check + REAL Zerodha charges")
print("1 Mar - 30 Jun 2026 | Start Rs.10,000 | Mar-Apr hourly data (approx), May-Jun 5-min")
print("=" * 96)

cur_month, m_net, m_chg, m_days = "", 0.0, 0.0, 0
cap_track = START_CAPITAL
tot_inv = tot_gross = tot_chg = tot_net = 0.0
tot_trades = 0

def month_close():
    if cur_month:
        print(f"  ---- {cur_month}: {m_days} trading days | net P&L Rs.{m_net:+,.2f} | charges Rs.{m_chg:,.2f}")

for day in days_out:
    mo = day["date"].strftime("%B %Y")
    if mo != cur_month:
        month_close()
        cur_month, m_net, m_chg, m_days = mo, 0.0, 0.0, 0
        print(f"\n== {mo} ==")
    cap_track += day["net"]
    m_net += day["net"]; m_chg += day["chg"]; m_days += 1
    tot_inv += day["inv"]; tot_gross += day["gross"]; tot_chg += day["chg"]; tot_net += day["net"]
    tot_trades += day["n"]
    lots_s = ",".join(f"{x}L" for x in sorted(day["lots"])) if day["lots"] else "--"
    res = f"Profit Rs.{day['net']:,.2f}" if day["net"] > 0 else (f"Loss Rs.{abs(day['net']):,.2f}" if day["net"] < 0 else "Flat")
    label = f"{day['date'].strftime('%b')} {day['date'].day:<2}"
    extra = f" | skipped {day['skips']} (no capital)" if day["skips"] else ""
    print(f"{label} -> Invested: Rs.{day['inv']:>9,.0f} | Lots: {lots_s:<9} | Trades: {day['n']} | "
          f"Charges: Rs.{day['chg']:>7,.2f} | {res}{extra} | Capital: Rs.{cap_track:,.2f}")
month_close()

print("\n" + "=" * 96)
print("FINAL SETTLEMENT — 30 Jun 2026")
print("=" * 96)
print(f"Starting capital     : Rs.{START_CAPITAL:,.2f}")
print(f"Total trades         : {tot_trades}")
print(f"Total invested (sum) : Rs.{tot_inv:,.2f}")
print(f"Gross P&L            : Rs.{tot_gross:+,.2f}")
print(f"Total charges paid   : Rs.{tot_chg:,.2f}  (brokerage+STT+txn+SEBI+stamp+GST)")
print(f"NET P&L              : Rs.{tot_net:+,.2f}")
print(f"FINAL CAPITAL        : Rs.{capital:,.2f}  ({(capital-START_CAPITAL)/START_CAPITAL*100:+.1f}%)")
print(f"Expiry Mondays skipped: {len(skipped)}")
print("=" * 96)
