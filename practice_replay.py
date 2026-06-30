"""
BREAKOUT STRATEGY v3 -- NIFTY ONLY
Rules:
  1. Signal: CE if price > yday high, PE if price < yday low. If flat, use morning direction.
  2. Dynamic lots: lots = max(3, floor(capital / 3333))  after each WIN
     After LOSS → always revert to 3 lots
  3. SL = Rs.150 fixed per trade (always)
  4. Let Winners Run: big trail at Rs.1000 peak (close if drops Rs.200 from peak)
  5. Tough day rule: small trail at Rs.400 peak (close if drops Rs.150 -- lock profit early)
     Both trails active; big trail takes priority on strong trend days
April, May, June 2026 -- separate tables
"""

import sys, math
import subprocess
subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "yfinance", "pandas", "numpy"])

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import date

BASE_LOTS         = 3
UNITS_PER_LOT     = 25
DELTA             = 0.40
BROKERAGE         = 20
STOP_LOSS         = -150          # fixed Rs.150 SL always
BIG_TRAIL_START   = 1000          # let winners run -- trail activates here
BIG_TRAIL_DROP    = 200           # close if drops Rs.200 from peak
SMALL_TRAIL_START = 400           # tough day -- book early if stalling
SMALL_TRAIL_DROP  = 150           # close if drops Rs.150 from peak (lock ~Rs.250)
EXPIRY_WEEKDAY    = 3             # Thursday
BASE_CAPITAL      = 10000.0
CAPITAL_PER_LOT   = BASE_CAPITAL / BASE_LOTS   # Rs.3333 per lot

MONTHS = [
    ("APRIL 2026", date(2026, 4, 1),  date(2026, 4, 30)),
    ("MAY 2026",   date(2026, 5, 1),  date(2026, 5, 31)),
    ("JUNE 2026",  date(2026, 6, 1),  date(2026, 6, 30)),
]

# -- Download -----------------------------------------------------------------
print("\nDownloading NIFTY hourly data (Apr-Jun 2026)...")
raw = yf.download("^NSEI", period="90d", interval="1h", progress=False)
raw = raw.reset_index()
if isinstance(raw.columns, pd.MultiIndex):
    raw.columns = [c[0].lower() for c in raw.columns]
else:
    raw.columns = [str(c).lower() for c in raw.columns]
tcol = next(c for c in raw.columns if c in ("datetime","date","timestamp"))
raw  = raw.rename(columns={tcol: "datetime"})
raw["datetime"] = pd.to_datetime(raw["datetime"])
print(f"  {len(raw)} candles | Apr-Jun 2026")

all_days = sorted(raw["datetime"].dt.date.unique())

# Daily high/low from hourly data
daily_hl = {}
for d in all_days:
    dd = raw[raw["datetime"].dt.date == d]
    if len(dd):
        daily_hl[d] = {
            "high":  dd["high"].max()  if "high"  in dd.columns else dd["close"].max(),
            "low":   dd["low"].min()   if "low"   in dd.columns else dd["close"].min(),
            "open":  dd.iloc[0]["open"] if "open" in dd.columns else dd.iloc[0]["close"],
            "close": dd.iloc[-1]["close"],
        }

# -- Shared running capital across all months ---------------------------------
capital     = BASE_CAPITAL
lots_today  = BASE_LOTS
grand_total = 0.0

for month_name, m_start, m_end in MONTHS:
    month_days = [d for d in all_days if m_start <= d <= m_end]
    month_trades = []
    month_start_cap = capital
    W = 118

    print(f"\n{'='*W}")
    print(f"  {month_name}  |  NIFTY BREAKOUT + LET WINNERS RUN  |  SL=Rs.150 fixed")
    print(f"  Capital at start of month: Rs.{capital:.0f}  |  Lots: {lots_today}")
    print(f"  Dynamic lots: WIN=> increase, LOSS=> revert to {BASE_LOTS} lots")
    print(f"{'='*W}")
    print(f"  {'Date':<11} {'Lots':>4} {'Signal':<22} {'Entry':>6} {'In':>5} {'Out':>5} {'Exit':<7} {'P&L':>9}  {'Capital':>10}  Next")
    print(f"  {'-'*W}")

    for day in month_days:
        day_str   = day.strftime("%d %b %a")
        daily_pnl = 0.0
        tag       = "[ --- ]"
        signal_s  = "-"
        entry_s   = "-"
        in_t = out_t = "-"
        exit_s    = "-"

        # Expiry
        if day.weekday() == EXPIRY_WEEKDAY:
            lots_disp = f"{lots_today}L"
            print(f"  [SKIP] {day_str:<10} {lots_disp:>4} {'Expiry -- skip':<22} {'--':>6} {'--':>5} {'--':>5} {'--':<7} {'Rs.0':>9}  {('Rs.%.0f'%capital):>10}  {lots_today}L")
            continue

        # Yesterday's H/L
        prev = [d for d in all_days if d < day]
        if not prev or prev[-1] not in daily_hl:
            continue
        yd       = prev[-1]
        yd_high  = daily_hl[yd]["high"]
        yd_low   = daily_hl[yd]["low"]

        # Today's data
        all_today = raw[raw["datetime"].dt.date == day].reset_index(drop=True)
        if len(all_today) < 2:
            continue

        first_open  = all_today.iloc[0]["open"] if "open" in all_today.columns else all_today.iloc[0]["close"]
        first_close = all_today.iloc[0]["close"]

        # Entry candles: 10:15 onwards (skip opening hour)
        df_day = all_today[all_today["datetime"].dt.time >= pd.Timestamp("10:15").time()].reset_index(drop=True)
        if len(df_day) < 1:
            continue

        units    = lots_today * UNITS_PER_LOT
        rs_per_pt = units * DELTA
        open_pos = None
        traded   = False

        for ci in range(len(df_day)):
            row = df_day.iloc[ci]
            px  = row["close"]
            t   = row["datetime"].strftime("%H:%M")

            # -- Manage open position -----------------------------------------
            if open_pos:
                otype = open_pos["otype"]
                move  = ((px - open_pos["entry"]) * rs_per_pt if otype == "CE"
                         else (open_pos["entry"] - px) * rs_per_pt)
                pnl   = move - BROKERAGE
                if pnl > open_pos["peak"]: open_pos["peak"] = pnl
                peak = open_pos["peak"]

                # 1. Hard SL
                if pnl <= STOP_LOSS:
                    out_t = t; exit_s = "SL"
                    daily_pnl = STOP_LOSS; tag = "[LOSS] "
                    month_trades.append({"pnl": daily_pnl, "result": "LOSS"})
                    open_pos = None; continue

                # 2. Big trail (let winners run on strong days)
                if peak >= BIG_TRAIL_START and pnl <= peak - BIG_TRAIL_DROP:
                    out_t = t; exit_s = "TRAIL"
                    daily_pnl = round(pnl, 0); tag = "[WIN]  "
                    month_trades.append({"pnl": daily_pnl, "result": "WIN"})
                    open_pos = None; continue

                # 3. Small trail (book profit on tough/slow days)
                if peak >= SMALL_TRAIL_START and pnl <= peak - SMALL_TRAIL_DROP:
                    out_t = t; exit_s = "LOCK"
                    daily_pnl = round(pnl, 0)
                    tag = "[WIN]  " if daily_pnl > 0 else "[LOSS] "
                    month_trades.append({"pnl": daily_pnl, "result": "WIN" if daily_pnl > 0 else "LOSS"})
                    open_pos = None; continue
                continue

            if traded: continue

            # -- Entry signal -------------------------------------------------
            if px > yd_high:
                otype    = "CE"
                signal_s = f"BREAK HIGH {yd_high:.0f}"
            elif px < yd_low:
                otype    = "PE"
                signal_s = f"BREAK LOW  {yd_low:.0f}"
            elif ci == 0:
                otype    = "CE" if first_close >= first_open else "PE"
                direction = "UP" if otype == "CE" else "DN"
                signal_s = f"MORN {direction} ({first_close:.0f})"
            else:
                continue

            in_t     = t
            entry_s  = f"{px:.0f}"
            open_pos = {"entry": px, "otype": otype, "peak": -9999}
            traded   = True

        # EOD exit
        if open_pos:
            px_eod = df_day.iloc[-1]["close"]
            otype  = open_pos["otype"]
            move   = ((px_eod - open_pos["entry"]) * rs_per_pt if otype == "CE"
                      else (open_pos["entry"] - px_eod) * rs_per_pt)
            pnl    = round(move - BROKERAGE, 0)
            out_t  = "14:30"; exit_s = "EOD"
            daily_pnl = pnl
            tag    = "[WIN]  " if pnl > 0 else "[LOSS] "
            month_trades.append({"pnl": daily_pnl, "result": "WIN" if pnl > 0 else "LOSS"})
            open_pos = None

        if not traded:
            tag = "[ --- ]"

        # -- Update capital and lots ------------------------------------------
        capital    += daily_pnl
        prev_lots   = lots_today

        if daily_pnl > 0:
            # WIN: scale up lots based on new capital
            lots_today = max(BASE_LOTS, int(capital // CAPITAL_PER_LOT))
        elif daily_pnl < 0:
            # LOSS: revert to base lots
            lots_today = BASE_LOTS

        # Cap lots
        lots_today = min(lots_today, 15)

        next_s  = f"->{lots_today}L" if lots_today != prev_lots else f" {lots_today}L"
        pnl_s   = f"Rs.{daily_pnl:+.0f}" if daily_pnl != 0 else "Rs.0"
        cap_s   = f"Rs.{capital:.0f}"
        lots_s  = f"{prev_lots}L"

        print(f"  {tag} {day_str:<10} {lots_s:>4} {signal_s:<22} {entry_s:>6} {in_t:>5} {out_t:>5} {exit_s:<7} {pnl_s:>9}  {cap_s:>10}  {next_s}")

    # Month summary
    if month_trades:
        df_m  = pd.DataFrame(month_trades)
        wins  = (df_m["result"] == "WIN").sum()
        n     = len(df_m)
        tot   = df_m["pnl"].sum()
        w_avg = df_m[df_m["pnl"]>0]["pnl"].mean() if wins else 0
        l_avg = df_m[df_m["pnl"]<0]["pnl"].mean() if (n-wins) else 0
        print(f"  {'='*W}")
        print(f"  {month_name}  |  {n} trades  |  {wins} wins ({wins/n*100:.0f}%)  |  {n-wins} losses")
        print(f"  Avg win: Rs.{w_avg:+.0f}  |  Avg loss: Rs.{l_avg:+.0f}  |  Month P&L: Rs.{tot:+.0f}")
        print(f"  Capital: Rs.{month_start_cap:.0f} -> Rs.{capital:.0f}")
    grand_total = capital - BASE_CAPITAL
    print()

print(f"\n  {'='*70}")
print(f"  GRAND TOTAL  April + May + June 2026")
print(f"  Starting capital : Rs.{BASE_CAPITAL:.0f}")
print(f"  Ending capital   : Rs.{capital:.0f}")
print(f"  Total profit     : Rs.{grand_total:+.0f}")
print(f"  Lots now         : {lots_today}L (based on current capital)")
print(f"  {'='*70}\n")
