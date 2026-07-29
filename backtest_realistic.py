"""
backtest_realistic.py - the honest version (2026-07-22, Sai: "add honest
slippage so the numbers stop flattering it").

Every other backtest in this folder assumes PERFECT fills: it buys and sells at
the exact candle close / exact stop level, as if there were no bid-ask spread
and no delay. Real option orders don't fill there - you cross the spread on the
way in and the way out, and stops in a fast move fill worse still.

This runs the CURRENT live strategy (NEW v5.1) on the SAME real 5-min data as
every other backtest, but charges a flat rupee slippage per round-trip trade on
top of the real Zerodha charges. It shows the frictionless number next to the
realistic ones so you can see how much of the backtest "profit" is fantasy.

Slippage levels (per round-trip, 1 lot = 65 units):
   Rs.30  ~ half a rupee of premium each side  (very optimistic)
   Rs.60  ~ one rupee of premium each side     (typical ATM NIFTY)
   Rs.120 ~ two rupees each side / stop slip    (fast/volatile markets)

DATA HONESTY: real 5-min history only exists for ~60 days (Yahoo cap; Kite
historical needs the paid plan). This is that 60-day window - the reliable one.
"""
import bt_engine as bt

print("Fetching real 5-min data (60d)...")
F5 = bt.build_5m()
d0, d1 = F5["day"].min(), F5["day"].max()

LEVELS = [("frictionless (fantasy)", 0.0),
          ("Rs.30  slippage/trade", 30.0),
          ("Rs.60  slippage/trade (realistic)", 60.0),
          ("Rs.120 slippage/trade (harsh)", 120.0)]

print("\n" + "="*88)
print(f"CURRENT STRATEGY (v5.1) WITH HONEST SLIPPAGE - real 5-min, {d0} -> {d1}, 1 lot")
print("="*88)
print(f"  {'assumption':<36}{'NET':>12}{'per-trade':>11}{'win%':>7}{'maxDD':>10}")
print("  " + "-"*84)

base = None
for label, slip in LEVELS:
    r = bt.run(F5, bt.NEW, slip_rt=slip)
    if base is None: base = r["net"]
    print(f"  {label:<36}Rs.{r['net']:>+9,.0f}Rs.{r['per']:>+8,.0f}{r['wr']:>6.0f}%Rs.{r['mdd']:>8,.0f}")

print("  " + "-"*84)
r60 = bt.run(F5, bt.NEW, slip_rt=60.0)
lost = base - r60["net"]
print(f"\n  Reading it straight:")
print(f"    Perfect-fill backtest says : Rs.{base:+,.0f}")
print(f"    Realistic (Rs.60 slippage) : Rs.{r60['net']:+,.0f}  over {r60['n']} trades")
print(f"    Slippage alone eats        : Rs.{lost:,.0f}  (~Rs.{lost/max(r60['n'],1):,.0f}/trade)")
print(f"    Per-trade edge after costs : Rs.{r60['per']:+,.0f}")
if r60["per"] <= 0:
    print("    => After realistic slippage the edge is GONE. Do not trust live profit.")
elif r60["per"] < 60:
    print("    => Edge survives but is THIN - a bad fill or two wipes a trade's profit.")
else:
    print("    => Edge still positive after realistic slippage.")
print("\n  Note: this is still backtest, still only 60 days, still optimistic on")
print("  everything except slippage (real premiums need the paid data plan).")
print("="*88)
