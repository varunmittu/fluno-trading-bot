"""
backtest_choppy.py (2026-07-24, after the -3k chop day) — Sai: "I want a
setup for separately choppy days, bot must detect the choppy days before".

Honest framing: nobody can flag a chop day at 9:15. What CAN be detected:
 (a) BEFORE each entry — ADX of the entry candle (chop band = <15 on NIFTY,
     see backtest_adx_label.py), and
 (b) DURING the day — breakouts that keep failing (SL count). Two failed
     breakouts in one day = the market is reversing everything it offers.

Variants (live v5.1 + conf72 gate, 60d real 5-min, same method as always):
  A BASELINE — current live behaviour
  B ADX-SKIP — refuse entries whose entry-candle ADX < 15
  C 2-SL DAY-STOP — after 2 stop-loss exits in a day, done for the day
  D B + C
Winner must beat baseline in BOTH halves separately or it is luck.
"""
import numpy as np
import pandas as pd
from datetime import timedelta, time as dtime
import bt_engine as be
from backtest_conf72 import conf_pct
from backtest_adx_label import add_adx


def run_v(frame, cfg, adx_skip=False, sl_stop=None, day_filter=None):
    DELTA, MODEL_BROKERAGE, UNITS = be.DELTA, be.MODEL_BROKERAGE, be.UNITS
    EOD, WINDOW_START, WINDOW_END = be.EOD, be.WINDOW_START, be.WINDOW_END
    DAILY_LIMIT = be.DAILY_LIMIT
    SL_MIN, SL_MAX, RUNGS = be.SL_MIN, be.SL_MAX, be.RUNGS

    dr = {d: frame.index[frame["day"] == d].tolist() for d in frame["day"].unique()}
    alld = sorted(frame["day"].unique())
    days = [d for d in alld if (day_filter is None or day_filter(d))]
    trades, day_net = [], {}
    step = pd.Timedelta(minutes=5)

    for d in days:
        prior = [x for x in alld if x < d]
        if len(prior) < 2:
            continue
        yd_hi = frame.loc[dr[prior[-1]], "high"].max()
        yd_lo = frame.loc[dr[prior[-1]], "low"].min()
        idxs = dr[d]
        orr = [i for i in idxs if frame.loc[i, "dt"].time() < dtime(10, 15)]
        or_hi = max(frame.loc[i, "high"] for i in orr) if orr else None
        or_lo = min(frame.loc[i, "low"] for i in orr) if orr else None
        dte = 1.41 if d.weekday() == 1 else 1.0
        pos, cool, dayhi, sl_count = None, None, -1e18, 0

        for k, i in enumerate(idxs):
            row = frame.loc[i]
            px = row["close"]
            last = k == len(idxs) - 1
            tclose = (row["dt"] + step).time()
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
                    trades.append((d, net))
                    day_net[d] = day_net.get(d, 0.0) + net
                    if st == "SL":
                        cool = row["dt"] + timedelta(minutes=cfg["cooldown"])
                        sl_count += 1
                    pos = None
            dhp = dayhi
            if row["high"] > dayhi:
                dayhi = row["high"]
            if pos:
                continue
            if day_net.get(d, 0.0) <= DAILY_LIMIT:
                continue
            if sl_stop is not None and sl_count >= sl_stop:
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
            if not (c9 >= be.MIN_SCORE or cp > 72):
                continue
            if adx_skip and (pd.isna(row["adx"]) or row["adx"] < 15):
                continue
            a = row["atr14"]
            if np.isnan(a):
                continue
            sl = max(SL_MIN, min(SL_MAX, round(a * cfg["atr_mult"] * DELTA * UNITS / 10) * 10))
            pos = {"otype": otype, "entry": px, "sl": sl, "peak": -9e9,
                   "inv": round(be.premium_est(px) * dte * UNITS, 2)}

    nets = [n for _, n in trades]
    wins = [n for n in nets if n > 0]
    eq = pk = mdd = 0.0
    for dd in sorted(day_net):
        eq += day_net[dd]
        pk = max(pk, eq)
        mdd = max(mdd, pk - eq)
    return dict(net=sum(nets), n=len(nets),
                wr=(len(wins) / len(nets) * 100 if nets else 0),
                mdd=mdd, worst=(min(day_net.values()) if day_net else 0),
                per=(sum(nets) / len(nets) if nets else 0))


def main():
    print("Building 60d frame + ADX...")
    frame = add_adx(be.build_5m())
    cfg = be.NEW
    alld = sorted(frame["day"].unique())
    mid = alld[len(alld) // 2]
    halves = {"FULL": None, "H1": lambda d: d < mid, "H2": lambda d: d >= mid}
    variants = {
        "A BASELINE":      dict(),
        "B ADX-SKIP<15":   dict(adx_skip=True),
        "C 2-SL DAY-STOP": dict(sl_stop=2),
        "D B+C":           dict(adx_skip=True, sl_stop=2),
    }
    for name, kw in variants.items():
        parts = [name.ljust(16)]
        for h, filt in halves.items():
            r = run_v(frame, cfg, day_filter=filt, **kw)
            parts.append(f"{h}: {r['net']:+9,.0f} n={r['n']:3d} wr={r['wr']:2.0f}% "
                         f"per={r['per']:+5.0f} mdd={r['mdd']:5,.0f} worst={r['worst']:+6,.0f}")
        print(" | ".join(parts))


if __name__ == "__main__":
    main()
