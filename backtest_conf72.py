"""
backtest_conf72.py (2026-07-24) — Sai's rule: "if confidence is above 72%
the bot needs to take the trade".

The old confidence % (analyze_setup: bull/bear 50-pt score +10 supertrend
+10 breakout, /70*100, clamp 5-95) is replicated here on the engine frame.
Variants, all on the LIVE v5.1 config (same 60d real 5-min method as every
decision in this project, frictionless like the historical tuning runs):

  A) BASELINE  — score9 >= 7                  (what the bot runs today)
  B) OR-BYPASS — score9 >= 7 OR conf% > 72    (Sai's rule added on top)
  C) CONF-ONLY — conf% > 72                   (Sai's rule replaces the gate)
  D) AND       — score9 >= 7 AND conf% > 72   (stricter)

A winner must beat baseline in BOTH halves of the data separately
(the v5.1 robustness standard) — one-half wins are luck and rejected.
"""
import numpy as np
import pandas as pd
from datetime import timedelta, time as dtime
import bt_engine as be


def conf_pct(row, prev, otype, is_break):
    """Replica of app.py analyze_setup() confidence %, on engine columns."""
    bull = otype == "CE"
    s = 0
    if (row["rsi"] < 50) if bull else (row["rsi"] > 50):                s += 15
    if (row["macd"] > row["macd_sig"]) == bull:                          s += 12
    if row["volume"] > row["vol_avg"] * 1.1:                             s += 5
    if (row["sma20"] > row["sma50"]) == bull:                            s += 10
    if (row["close"] > row["sma50"]) == bull:                            s += 5
    if (row["sma20"] > prev["sma20"]) == bull:                           s += 3
    if (row["st_dir"] == 1) == bull:                                     s += 10
    if is_break:                                                         s += 10
    return max(5, min(95, round(s / 70 * 100)))


def run_gate(frame, cfg, gate, day_filter=None):
    """bt_engine.run() verbatim, with the entry gate swapped for gate(...)."""
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
        pos, cool, dayhi = None, None, -1e18

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
                    pos = None
            dhp = dayhi
            if row["high"] > dayhi:
                dayhi = row["high"]
            if pos:
                continue
            if day_net.get(d, 0.0) <= DAILY_LIMIT:
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
            if not gate(row, prev_row, otype, abs(px - lvl) / px, is_break):
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
    print("Building 60d real 5-min frame...")
    frame = be.build_5m()
    cfg = be.NEW  # live v5.1

    gates = {
        "A BASELINE score>=7":      lambda r, p, o, m, b: be.conf9(r, o, m) >= be.MIN_SCORE,
        "B score>=7 OR conf>72":    lambda r, p, o, m, b: be.conf9(r, o, m) >= be.MIN_SCORE or conf_pct(r, p, o, b) > 72,
        "C conf>72 ONLY":           lambda r, p, o, m, b: conf_pct(r, p, o, b) > 72,
        "D score>=7 AND conf>72":   lambda r, p, o, m, b: be.conf9(r, o, m) >= be.MIN_SCORE and conf_pct(r, p, o, b) > 72,
    }

    alld = sorted(frame["day"].unique())
    mid = alld[len(alld) // 2]
    halves = {"FULL": None,
              "H1": (lambda d, _m=mid: d < _m),
              "H2": (lambda d, _m=mid: d >= _m)}

    for name, gate in gates.items():
        line = [name.ljust(24)]
        for hname, filt in halves.items():
            r = run_gate(frame, cfg, gate, day_filter=filt)
            line.append(f"{hname}: net {r['net']:+9,.0f} n={r['n']:3d} wr={r['wr']:2.0f}% "
                        f"per={r['per']:+6.0f} mdd={r['mdd']:5,.0f} worst={r['worst']:+6,.0f}")
        print(" | ".join(line))


if __name__ == "__main__":
    main()
