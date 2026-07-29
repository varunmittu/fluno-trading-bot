"""
replay_today.py (2026-07-24) — replay TODAY on the engine with the CURRENT
settings, so Sai can see what the bot would do now vs what actually happened
live. Settings applied: v5.1 (ATR 1.5, fade 0.15%), cooldown 0 (live value,
removed 07-22), conf72 OR-gate, ADX<15 chop guard. Two daily-limit cases:
STANDARD (-1000, back tomorrow) and TODAY-LIVE (off). Prints each trade.

Note: engine uses ESTIMATED premiums, so rupee P&L is close-but-not-identical
to the live Kite fills; the SEQUENCE and win/lose pattern is the real lesson.
"""
import numpy as np
import pandas as pd
from datetime import timedelta, time as dtime
import bt_engine as be
from backtest_conf72 import conf_pct
from backtest_adx_label import add_adx


def replay(frame, cfg, day, daily_limit, adx_skip=True, use_conf72=True):
    DELTA, MODEL_BROKERAGE, UNITS = be.DELTA, be.MODEL_BROKERAGE, be.UNITS
    EOD, WINDOW_START, WINDOW_END = be.EOD, be.WINDOW_START, be.WINDOW_END
    SL_MIN, SL_MAX, RUNGS = be.SL_MIN, be.SL_MAX, be.RUNGS

    dr = {d: frame.index[frame["day"] == d].tolist() for d in frame["day"].unique()}
    alld = sorted(frame["day"].unique())
    prior = [x for x in alld if x < day]
    yd_hi = frame.loc[dr[prior[-1]], "high"].max()
    yd_lo = frame.loc[dr[prior[-1]], "low"].min()
    idxs = dr[day]
    orr = [i for i in idxs if frame.loc[i, "dt"].time() < dtime(10, 15)]
    or_hi = max(frame.loc[i, "high"] for i in orr) if orr else None
    or_lo = min(frame.loc[i, "low"] for i in orr) if orr else None
    dte = 1.41 if day.weekday() == 1 else 1.0
    pos, cool, dayhi, daynet = None, None, -1e18, 0.0
    log, skips = [], {"chop": 0, "limit": 0}

    for k, i in enumerate(idxs):
        row = frame.loc[i]
        px = row["close"]
        last = k == len(idxs) - 1
        tclose = (row["dt"] + pd.Timedelta(minutes=5)).time()
        if pos:
            mv = ((px - pos["entry"]) if pos["otype"] == "CE" else (pos["entry"] - px)) * DELTA * UNITS
            pnl = mv - MODEL_BROKERAGE
            booked = None
            if pnl > pos["peak"]:
                pos["peak"] = pnl
            fl = None
            for rg in RUNGS:
                if pos["peak"] >= rg:
                    fl = rg
                else:
                    break
            if tclose >= EOD or last:
                booked = (round(pnl, 2), "EOD")
            elif fl is not None and pnl < fl:
                booked = (fl, "STAIR")
            elif fl is None and pnl <= -pos["sl"]:
                booked = (-pos["sl"], "SL")
            if booked:
                val, st = booked
                gross = val + MODEL_BROKERAGE
                sv = max(0.0, pos["inv"] + gross)
                net = round(gross - be.real_charges(pos["inv"], sv), 2)
                daynet += net
                log.append((pos["t"], pos["otype"], pos["entry"], px, net, st, pos["adx"]))
                if st == "SL":
                    cool = row["dt"] + timedelta(minutes=cfg["cooldown"])
                pos = None
        dhp = dayhi
        if row["high"] > dayhi:
            dayhi = row["high"]
        if pos:
            continue
        if daynet <= daily_limit:
            skips["limit"] += 1
            continue
        if not (WINDOW_START <= tclose <= WINDOW_END):
            continue
        if cool and row["dt"] < cool:
            continue
        otype, lvl, is_break = None, None, True
        if px > yd_hi:
            otype, lvl = "CE", yd_hi
        elif px < yd_lo:
            otype, lvl = "PE", yd_lo
        elif or_lo is not None and px < or_lo:
            otype, lvl = "PE", or_lo
        elif or_hi is not None and px > or_hi:
            otype, lvl = "CE", or_hi
        if tclose > dtime(12, 30) and otype != "PE" and dhp > 0 and px <= dhp * (1 - cfg["fade"]):
            otype, lvl, is_break = "PE", dhp * (1 - cfg["fade"]), False
        if otype is None:
            continue
        if pd.isna(row["htf_st"]):
            continue
        if otype == "CE" and not (row["htf_st"] == 1 and bool(row["htf_macd"])):
            continue
        if otype == "PE" and not (row["htf_st"] == -1 and not bool(row["htf_macd"])):
            continue
        prev_row = frame.loc[i - 1] if i - 1 in frame.index else row
        c9 = be.conf9(row, otype, abs(px - lvl) / px)
        cp = conf_pct(row, prev_row, otype, is_break)
        gate_ok = (c9 >= be.MIN_SCORE or cp > 72) if use_conf72 else (c9 >= be.MIN_SCORE)
        if not gate_ok:
            continue
        adxv = float(row["adx"]) if not pd.isna(row["adx"]) else -1
        if adx_skip and adxv < be.__dict__.get("ADX_CHOP", 15):
            skips["chop"] += 1
            continue
        a = row["atr14"]
        if np.isnan(a):
            continue
        sl = max(SL_MIN, min(SL_MAX, round(a * cfg["atr_mult"] * DELTA * UNITS / 10) * 10))
        pos = {"otype": otype, "entry": px, "sl": sl, "peak": -9e9, "t": tclose,
               "inv": round(be.premium_est(px) * dte * UNITS, 2), "adx": adxv}
    return log, daynet, skips


def show(title, frame, cfg, day, lim, adx_skip, use_conf72):
    log, net, skips = replay(frame, cfg, day, lim, adx_skip=adx_skip, use_conf72=use_conf72)
    print(f"\n=== {title} ===")
    if not log:
        print("  no trades taken")
    run = 0.0
    for t, ot, en, ex, pnl, st, adx in log:
        run += pnl
        print(f"  {t.strftime('%H:%M')}  {ot}  entry {en:.0f} -> exit {ex:.0f}  "
              f"{'WIN ' if pnl > 0 else 'LOSS'} Rs.{pnl:+7.0f}  running Rs.{run:+7.0f}  [{st}]  ADX {adx:.0f}")
    print(f"  >> DAY TOTAL Rs.{net:+,.0f} in {len(log)} trades  (chop-skips {skips['chop']})")


def main():
    print("Building frame + ADX (1 lot = your capital)...")
    frame = add_adx(be.build_5m())
    day = sorted(frame["day"].unique())[-1]
    cfg = dict(be.NEW)
    cfg["cooldown"] = 0   # live value since 2026-07-22

    print("\n########## TOMORROW'S SETTINGS (daily limit -1000 ON) ##########")
    show("OLD bot (yesterday: score>=7 gate, no chop guard)", frame, cfg, day, -1000, adx_skip=False, use_conf72=False)
    show("NEW bot (today's changes: conf72 gate + ADX<15 chop guard)", frame, cfg, day, -1000, adx_skip=True, use_conf72=True)

    print("\n########## WITH DAILY LIMIT OFF (what happened today) ##########")
    show("OLD bot, limit OFF", frame, cfg, day, -10_000_000, adx_skip=False, use_conf72=False)
    show("NEW bot, limit OFF", frame, cfg, day, -10_000_000, adx_skip=True, use_conf72=True)


if __name__ == "__main__":
    main()
