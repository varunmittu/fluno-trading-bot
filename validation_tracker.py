"""
validation_tracker.py - is the LIVE bot actually matching the backtest? (2026-07-22)

The whole go-live question comes down to one thing: does the bot make money for
REAL, or only in the backtest? This measures that, honestly:

  1. Pulls every live (paper) trade from trade_log.db since the validation start.
  2. Runs the SAME strategy (bt_engine, v5.1) over the SAME calendar days, with
     realistic slippage, so it's a fair like-for-like comparison.
  3. Reports the gap, real statistics (is the edge more than luck?), and a
     TRANSPARENT trust score you can check yourself - no black box.

Validation clock started 2026-07-14 (the day the 5-min decision bug was fixed -
before that the live bot was deciding on 1-min noise, so older trades don't
count as a fair test). Override:  python validation_tracker.py 2026-07-14

TRUST IS EVIDENCE, NOT A SETTING. compute() cannot be "tuned" to a higher
number - the score only rises when real trades accumulate and prove an edge.

Used two ways:
  - CLI:      python validation_tracker.py [start_date]   (full report)
  - imported: validation_tracker.compute()  ->  dict      (bot + dashboard)
"""
import sys, os, sqlite3, math, statistics
from datetime import datetime

DEFAULT_START = "2026-07-14"
TARGET_N = 60          # trades needed before an edge can be called real
SLIP_RT  = 60.0        # realistic slippage per trade for the fair backtest
DB       = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_log.db")


def compute(start=DEFAULT_START, do_backtest=True):
    """Run the full validation and return a dict of results. Never raises for the
    normal 'no data / no internet' cases - it degrades gracefully so the bot and
    dashboard can always show *something* truthful."""
    conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT date,pnl,charges FROM trades WHERE date>=? ORDER BY date,id", (start,)
    ).fetchall()
    conn.close()

    if not rows:
        return {"start": start, "n": 0, "trust": 0.0, "empty": True,
                "verdict": f"No live trades since {start} yet."}

    live_pnl = [r["pnl"] for r in rows]
    n        = len(live_pnl)
    wins     = [p for p in live_pnl if p > 0]
    live_net = sum(live_pnl)
    live_per = live_net / n
    live_wr  = len(wins) / n * 100
    mean     = statistics.mean(live_pnl)
    sd       = statistics.pstdev(live_pnl) if n < 2 else statistics.stdev(live_pnl)
    se       = sd / math.sqrt(n) if n else 0.0
    t_stat   = mean / se if se else 0.0
    ci_lo, ci_hi = mean - 1.96*se, mean + 1.96*se
    edge_real = n >= 2 and ci_lo > 0

    live_day, day_cnt = {}, {}
    for r in rows:
        live_day[r["date"]] = live_day.get(r["date"], 0.0) + r["pnl"]
        day_cnt[r["date"]]  = day_cnt.get(r["date"], 0) + 1
    green = sum(1 for v in live_day.values() if v > 0)
    red   = sum(1 for v in live_day.values() if v <= 0)

    # fair same-day backtest (best effort - needs internet + bt_engine)
    bt_ok = False; bt = {}
    if do_backtest:
        try:
            import bt_engine as E
            F5 = E.build_5m()
            live_dates = {datetime.strptime(d, "%Y-%m-%d").date() for d in live_day}
            same  = E.run(F5, E.NEW, slip_rt=SLIP_RT, day_filter=lambda d: d in live_dates)
            frict = E.run(F5, E.NEW, slip_rt=0.0,     day_filter=lambda d: d in live_dates)
            btcnt = {}
            for d, _ in same["trades"]:
                btcnt[d] = btcnt.get(d, 0) + 1
            bt = {"n": same["n"], "net": same["net"], "per": same["per"], "wr": same["wr"],
                  "frict_net": frict["net"], "frict_per": frict["per"],
                  "day_net": {d.strftime("%Y-%m-%d"): v for d, v in same["day_net"].items()},
                  "day_cnt": {d.strftime("%Y-%m-%d"): c for d, c in btcnt.items()}}
            bt_ok = True
        except Exception as e:
            bt = {"error": str(e)}

    bt_per = bt.get("per", 130.0)   # 130 ~ realistic baseline when backtest unavailable

    # ── transparent trust score ──────────────────────────────────────────────
    prog     = min(n / TARGET_N, 1.0)
    tracking = (mean > 0) if bt_per <= 0 else (mean >= 0.5 * bt_per)
    comp = {"data": round(3*prog, 1), "profitable": 1 if mean > 0 else 0,
            "tracking": 1 if tracking else 0, "edge_real": 3 if edge_real else 0,
            "full_sample": 2 if n >= TARGET_N else 0}
    trust = round(min(10.0, sum(comp.values())), 1)
    if n < 15: trust = min(trust, 3.0)

    if trust >= 7:
        verdict = "Earned - real money defensible (still walk the checklist gates)."
    elif trust >= 5:
        verdict = "Getting there - edge positive but not proven. Keep collecting trades."
    else:
        verdict = f"Not ready - need ~{max(0, TARGET_N-n)} more trades, surviving a bad week."

    return {"start": start, "empty": False, "as_of": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "n": n, "target_n": TARGET_N, "live_net": round(live_net, 0),
            "live_per": round(live_per, 0), "live_wr": round(live_wr), "sd": round(sd),
            "ci_lo": round(ci_lo), "ci_hi": round(ci_hi), "t_stat": round(t_stat, 2),
            "green": green, "red": red, "live_chg": round(sum((r["charges"] or 0) for r in rows)),
            "edge_real": edge_real, "bt_ok": bt_ok, "bt": bt, "bt_per": round(bt_per),
            "gap": round(live_per - bt_per), "trust": trust, "components": comp,
            "verdict": verdict, "live_day": live_day, "day_cnt": day_cnt,
            "capped": n < 15}


def telegram_text(res):
    """Short clean-labels Telegram summary (matches the bot's message style)."""
    if res.get("empty"):
        return f"VALIDATION\n{res['verdict']}"
    edge = "yes" if res["edge_real"] else "no (CI includes 0)"
    rows = [
        ("Trust",    f"{res['trust']}/10"),
        ("Trades",   f"{res['n']}/{res['target_n']}"),
        ("Win rate", f"{res['live_wr']}%"),
        ("Per trade",f"Rs.{res['live_per']:+,.0f} live"),
        ("Edge real",edge),
    ]
    w = max(len(l) for l, _ in rows)
    box = "\n".join(f"{l.ljust(w)} : {v}" for l, v in rows)
    return (f"\U0001F4CA <b>VALIDATION - trust check</b>\n"
            f"<pre>{box}</pre>\n{res['verdict']}")


def print_report(res):
    """Full console report."""
    def money(x): return f"Rs.{x:+,.0f}"
    if res.get("empty"):
        print(res["verdict"]); return
    print("\n" + "="*76)
    print(f"VALIDATION TRACKER - live vs backtest since {res['start']}  (as of {res['as_of']})")
    print("="*76)
    bt_ok = res["bt_ok"]; bt = res["bt"]
    print(f"\n  {'day':<12}{'live n':>7}{'live P&L':>12}", end="")
    if bt_ok: print(f"{'bt n':>7}{'bt P&L':>12}", end="")
    print()
    for ds in sorted(res["live_day"]):
        print(f"  {ds:<12}{res['day_cnt'][ds]:>7}{money(res['live_day'][ds]):>12}", end="")
        if bt_ok:
            print(f"{bt['day_cnt'].get(ds,0):>7}{money(bt['day_net'].get(ds,0.0)):>12}", end="")
        print()
    print("\n  " + "-"*72)
    print("  LIVE (real paper trades - the number that actually matters):")
    print(f"    trades        : {res['n']}         win rate : {res['live_wr']}%   green/red days : {res['green']}/{res['red']}")
    print(f"    net P&L       : {money(res['live_net'])}   (charges paid Rs.{res['live_chg']:,.0f})")
    print(f"    per trade     : {money(res['live_per'])}   (sd Rs.{res['sd']:,.0f})")
    print(f"    95% CI/trade  : {money(res['ci_lo'])}  to  {money(res['ci_hi'])}    (t = {res['t_stat']})")
    print("    -> CI ABOVE zero: edge is statistically real." if res["edge_real"]
          else "    -> CI includes zero/negative: NOT yet proof of a real edge (could be luck).")
    if bt_ok:
        print("\n  BACKTEST, same days, realistic slippage (the fair comparison):")
        print(f"    trades        : {bt['n']}         win rate : {bt['wr']:.0f}%")
        print(f"    net P&L       : {money(bt['net'])}   per trade : {money(bt['per'])}")
        print(f"    (frictionless fantasy for reference: {money(bt['frict_net'])} / {money(bt['frict_per'])}/trade)")
        print(f"\n  GAP  live minus backtest : {money(res['gap'])}/trade", end="")
        print("   (live BEATS backtest)" if res["gap"] >= 0 else "   (live LAGS backtest - the real-world tax)")
    else:
        print(f"\n  BACKTEST unavailable ({bt.get('error','?')}) - trust uses ~Rs.130/trade baseline.")
    c = res["components"]
    print("\n  " + "="*72)
    print(f"  TRUST FOR REAL MONEY : {res['trust']} / 10")
    print("  " + "="*72)
    print("  how it's scored (check it yourself):")
    print(f"    data collected      {res['n']}/{res['target_n']} trades         -> +{c['data']}  (of 3)")
    print(f"    live is profitable  {'yes' if c['profitable'] else 'no'}                    -> +{c['profitable']}  (of 1)")
    print(f"    tracking backtest   {'yes' if c['tracking'] else 'no'}                    -> +{c['tracking']}  (of 1)")
    print(f"    edge is real (CI>0) {'yes' if c['edge_real'] else 'no'}                    -> +{c['edge_real']}  (of 3)")
    print(f"    full sample reached {'yes' if c['full_sample'] else 'no'}                    -> +{c['full_sample']}  (of 2)")
    if res["capped"]: print("    (capped at 3/10 - under 15 trades is never enough to trust)")
    print(f"\n  WHAT THIS MEANS:\n    {res['verdict']}")
    print("="*76)


if __name__ == "__main__":
    start = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_START
    print("Fetching real 5-min data to backtest the same days (needs internet)...")
    print_report(compute(start))
