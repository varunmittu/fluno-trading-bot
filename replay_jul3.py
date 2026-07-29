"""
Replay 3 Jul 2026 through the NEW v4.2 strategy (verbose, trade by trade).
Start capital Rs.15,000 (what Sai actually had this morning).
Reuses data + helpers from backtest_reversal.py.
"""
from datetime import date, time as dtime, timedelta
import backtest_reversal as br

br.MAX_TRADES = 99          # current live setting: no daily trade limit

TARGET  = date(2026, 7, 3)
CAPITAL = 15000.0

frame = br.df5
dr    = {d: frame.index[frame["day"] == d].tolist() for d in frame["day"].unique()}
all_d = sorted(frame["day"].unique())
idxs  = dr[TARGET]
prior = [x for x in all_d if x < TARGET]
yd_hi = frame.loc[dr[prior[-1]], "high"].max()
yd_lo = frame.loc[dr[prior[-1]], "low"].min()

or_rows = [i for i in idxs if frame.loc[i, "dt"].time() < dtime(10, 15)]
or_hi = max(frame.loc[i, "high"] for i in or_rows) if or_rows else None
or_lo = min(frame.loc[i, "low"]  for i in or_rows) if or_rows else None

print("=" * 70)
print(f"REPLAY {TARGET} | strategy v4.2 | start capital Rs.{CAPITAL:,.0f}")
print(f"Yesterday H/L: {yd_hi:.0f} / {yd_lo:.0f} | Opening range: {or_hi:.0f} / {or_lo:.0f}")
print("=" * 70)

capital = CAPITAL
lots    = min(br.MAX_LOTS, max(br.BASE_LOTS, int(capital // br.CAP_PER_LOT)))
daily_pnl, trades_today = 0.0, 0
pos, pending = None, None
cooldown, gatecool = None, None
day_hi_run = -1e18
t_end = dtime(14, 30)

for k, i in enumerate(idxs):
    row = frame.loc[i]; t = row["dt"].time(); px = row["close"]
    last = k == len(idxs) - 1
    hhmm = row["dt"].strftime("%H:%M")

    if pos:
        units = pos["lots"] * 25
        mv  = (px - pos["entry"]) * br.DELTA * units if pos["otype"] == "CE" else (pos["entry"] - px) * br.DELTA * units
        pnl = mv - br.MODEL_BROKERAGE
        if pnl > pos["peak"]: pos["peak"] = pnl
        peak = pos["peak"]
        if peak >= br.BE_START: pos["locked"] = True
        booked = None
        if t >= dtime(15, 25) or last:                    booked = (round(pnl, 2), "EOD")
        elif pos["locked"] and pnl < br.BE_FLOOR:         booked = (br.BE_FLOOR, "BE-LOCK")
        elif not pos["locked"] and pnl <= -pos["sl"]:     booked = (-pos["sl"], "STOP-LOSS")
        elif peak >= br.BIG_START:
            if not br.trend_strong(frame, i, pos["otype"]) or pnl <= peak - br.BIG_SAFETY:
                booked = (round(pnl, 2), "TRAIL")
        elif peak >= br.SMALL_START and pnl <= peak - br.SMALL_DROP:
            v = max(br.BE_FLOOR, round(pnl, 2)) if pos["locked"] else round(pnl, 2)
            booked = (v, "PROFIT-LOCK" if v > 0 else "STOP-LOSS")
        if booked:
            val, status = booked
            gross    = val + br.MODEL_BROKERAGE
            sell_val = max(0.0, pos["inv"] + gross)
            chg      = br.real_charges(pos["inv"], sell_val)
            net      = round(gross - chg, 2)
            capital += net; daily_pnl += net
            lots = min(br.MAX_LOTS, max(br.BASE_LOTS, int(capital // br.CAP_PER_LOT))) if net > 0 else br.BASE_LOTS
            trades_today += 1
            print(f"{hhmm}  EXIT  {pos['otype']} | {status:<11} | entry {pos['entry']:.0f} -> {px:.0f} | "
                  f"peak Rs.{pos['peak']:.0f} | NET Rs.{net:+,.2f} | capital Rs.{capital:,.2f}")
            if status == "STOP-LOSS":
                cooldown = row["dt"] + timedelta(minutes=10)
            pos = None

    day_hi_prior = day_hi_run
    if row["high"] > day_hi_run: day_hi_run = row["high"]

    if daily_pnl <= br.DAILY_LIMIT:
        pending = None; continue
    if pos or trades_today >= br.MAX_TRADES: continue
    if not (dtime(10, 15) <= t <= t_end):
        pending = None; continue
    if cooldown and row["dt"] < cooldown: continue

    def try_open(otype, sig, conf, sl):
        global pos
        prem = br.premium_est(px)
        afford = int(capital // (prem * 25))
        if afford < 1: return False
        use = min(lots, afford)
        pos = {"otype": otype, "entry": px, "sl": sl, "conf": conf,
               "lots": use, "peak": -9999, "locked": False,
               "inv": round(prem * use * 25, 2)}
        print(f"{hhmm}  ENTER {otype} | {sig:<12} | NIFTY {px:.0f} | conf {conf}% SL Rs.{sl} | "
              f"{use} lots, invested Rs.{pos['inv']:,.0f}")
        return True

    if pending:
        try_open(pending["otype"], pending["sig"], pending["conf"], pending["sl"])
        pending = None
        continue

    otype = None
    if px > yd_hi:   otype, sig, brk = "CE", "BRK-HI", True
    elif px < yd_lo: otype, sig, brk = "PE", "BRK-LO", True
    elif or_lo is not None and px < or_lo: otype, sig, brk = "PE", "OR-LO", True
    elif or_hi is not None and px > or_hi: otype, sig, brk = "CE", "OR-HI", True
    if t > dtime(12, 30) and otype != "PE" and day_hi_prior > 0 \
       and px <= day_hi_prior * (1 - br.FADE_PCT):
        otype, sig, brk = "PE", "FADE", True

    if otype is None: continue
    if not br.htf_agrees(frame, i, otype): continue

    conf, sl = br.confidence(frame, i, otype, brk)
    if trades_today == 0:
        try_open(otype, sig, conf, sl)
    else:
        if gatecool and row["dt"] < gatecool: continue
        pending = {"otype": otype, "sig": sig, "conf": conf, "sl": sl}
        print(f"{hhmm}  GATE  {otype} | {sig:<12} | conf {conf}% — Telegram /confirm sent (assumed YES)")

print("=" * 70)
print(f"DAY RESULT: {trades_today} trades | net Rs.{daily_pnl:+,.2f} | capital Rs.{capital:,.2f}")
print(f"ACTUAL today (old rules + duplicate bug): net Rs.-395.50 -> capital Rs.14,604.50")
print("=" * 70)
