"""
backtest_adx_label.py (2026-07-24) — Sai wants PSV forex's "second opinion"
(ADX trend-strength label on offers) in this bot after a -3,000 real day of
chop losses. PSV finding (forex): ADX>=25 trades won ~5x more per trade;
label-only, blocks nothing. THIS script checks whether ADX separates winners
on NIFTY 5-min too, before wiring the label in (independent implementation —
no files shared between projects).

Method: live v5.1 config + current live gate (score>=7 OR conf>72), 60d real
5-min, same engine as every decision here; each trade tagged with ADX(14) of
the entry candle, results bucketed by ADX band.
"""
import numpy as np
import pandas as pd
from datetime import timedelta, time as dtime
import bt_engine as be
from backtest_conf72 import conf_pct


def add_adx(frame, period=14):
    h, l, c = frame["high"], frame["low"], frame["close"]
    up = h.diff()
    dn = -l.diff()
    plus_dm = ((up > dn) & (up > 0)) * up
    minus_dm = ((dn > up) & (dn > 0)) * dn
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    pdi = 100 * (plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr)
    mdi = 100 * (minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr)
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    frame["adx"] = dx.ewm(alpha=1 / period, adjust=False).mean()
    return frame


def run_tagged(frame, cfg):
    """Engine run with the LIVE gate (score>=7 OR conf>72); trades carry entry ADX."""
    DELTA, MODEL_BROKERAGE, UNITS = be.DELTA, be.MODEL_BROKERAGE, be.UNITS
    EOD, WINDOW_START, WINDOW_END = be.EOD, be.WINDOW_START, be.WINDOW_END
    DAILY_LIMIT = be.DAILY_LIMIT
    SL_MIN, SL_MAX, RUNGS = be.SL_MIN, be.SL_MAX, be.RUNGS

    dr = {d: frame.index[frame["day"] == d].tolist() for d in frame["day"].unique()}
    alld = sorted(frame["day"].unique())
    trades, day_net = [], {}
    step = pd.Timedelta(minutes=5)

    for d in alld:
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
                    trades.append((d, net, pos["adx"]))
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
            c9 = be.conf9(row, otype, abs(px - lvl) / px)
            cp = conf_pct(row, prev_row, otype, is_break)
            if not (c9 >= be.MIN_SCORE or cp > 72):
                continue
            a = row["atr14"]
            if np.isnan(a):
                continue
            sl = max(SL_MIN, min(SL_MAX, round(a * cfg["atr_mult"] * DELTA * UNITS / 10) * 10))
            pos = {"otype": otype, "entry": px, "sl": sl, "peak": -9e9,
                   "inv": round(be.premium_est(px) * dte * UNITS, 2),
                   "adx": float(row["adx"]) if not pd.isna(row["adx"]) else -1}

    return trades


def main():
    print("Building 60d real 5-min frame + ADX...")
    frame = add_adx(be.build_5m())
    trades = run_tagged(frame, be.NEW)
    print(f"total trades: {len(trades)}  net {sum(n for _, n, _ in trades):+,.0f}")
    bands = [(0, 15), (15, 20), (20, 25), (25, 30), (30, 999)]
    print(f"{'ADX band':>10} {'n':>4} {'win%':>5} {'avg':>8} {'total':>10}")
    for lo, hi in bands:
        sel = [n for _, n, a in trades if lo <= a < hi]
        if not sel:
            print(f"{lo}-{hi if hi < 999 else '+':>7}: none")
            continue
        wins = sum(1 for n in sel if n > 0)
        print(f"{f'{lo}-{hi if hi<999 else chr(43)}':>10} {len(sel):>4} {wins/len(sel)*100:>4.0f}% "
              f"{sum(sel)/len(sel):>+8.0f} {sum(sel):>+10,.0f}")
    # what a hard filter WOULD do (info only — PSV precedent says label-only)
    for cut in (20, 25):
        kept = [n for _, n, a in trades if a >= cut]
        print(f"filter ADX>={cut}: n={len(kept)} net {sum(kept):+,.0f} "
              f"(vs all {sum(n for _, n, _ in trades):+,.0f})")


if __name__ == "__main__":
    main()
