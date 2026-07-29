"""
COMBINATION TEST (2026-07-20) — follow-up to backtest_tune_v51.py.

The single-knob sweep found 3 changes that each beat the live config in BOTH
halves of the data: ATR multiplier 1.2/1.5, SL cooldown 20/30 min, and fade
trigger 0.0015/0.001. Individually robust does NOT mean they stack — they may
be fixing the same underlying problem (too many marginal re-entries getting
stopped out by noise). This tests the combinations, again requiring a win in
BOTH halves, and also watches WORST DAY, which matters more than net profit
for an account this small (a Rs.1,000 loss is ~5% of Sai's Rs.19k capital).
"""
import os
import importlib.util
os.chdir(os.path.dirname(os.path.abspath(__file__)))

src = open("backtest_tune_v51.py", encoding="utf-8").read()
cut = src.index("days_all = sorted(")
ns = {"__file__": os.path.abspath("backtest_tune_v51.py")}
exec(compile(src[:cut], "tune_partial", "exec"), ns)

simulate, LIVE, DF = ns["simulate"], ns["LIVE"], ns["DF"]
from datetime import time as dtime

days_all = sorted(DF["day"].unique())
MID = days_all[len(days_all)//2]
H1 = lambda d: d < MID
H2 = lambda d: d >= MID

base, base_h1, base_h2 = simulate({}), simulate({}, H1), simulate({}, H2)

COMBOS = {
    "LIVE (baseline)":                                  {},
    "A: ATR 1.2":                                       {"atr_mult": 1.2},
    "B: ATR 1.5":                                       {"atr_mult": 1.5},
    "C: cooldown 30":                                   {"cooldown": 30},
    "D: fade 0.0015":                                   {"fade_pct": 0.0015},
    "E: ATR 1.5 + cooldown 30":                         {"atr_mult": 1.5, "cooldown": 30},
    "F: ATR 1.5 + fade 0.0015":                         {"atr_mult": 1.5, "fade_pct": 0.0015},
    "G: ATR 1.5 + cooldown 30 + fade 0.0015":           {"atr_mult": 1.5, "cooldown": 30, "fade_pct": 0.0015},
    "H: ATR 1.2 + cooldown 20 + fade 0.0015 (milder)":  {"atr_mult": 1.2, "cooldown": 20, "fade_pct": 0.0015},
    "I: ATR 1.5 + SLmax 1200":                          {"atr_mult": 1.5, "sl_max": 1200},
    "J: ATR 1.5 + SLmax 800":                           {"atr_mult": 1.5, "sl_max": 800},
    "K: ATR 1.5 + cooldown 30, SLmax 800":              {"atr_mult": 1.5, "cooldown": 30, "sl_max": 800},
}

print("\n" + "="*104)
print(f"COMBINATION TEST | {days_all[0]} -> {days_all[-1]} | real 5-min, 1 lot, real charges, 15:00 window")
print("="*104)
print(f"{'variant':<48}{'NET':>10}{'trades':>8}{'win%':>6}{'per tr':>8}{'maxDD':>9}{'worst day':>11}  robust?")
print("-"*104)

results = {}
for name, cfg in COMBOS.items():
    r  = simulate(cfg)
    r1 = simulate(cfg, H1)
    r2 = simulate(cfg, H2)
    results[name] = (r, r1, r2)
    if not cfg:
        flag = "[baseline]"
    else:
        both = r1["per_trade"] > base_h1["per_trade"] and r2["per_trade"] > base_h2["per_trade"]
        flag = "YES" if both else "no (one half only)"
    print(f"{name:<48}{r['net']:>+10,.0f}{r['n']:>8}{r['wr']:>6.0f}{r['per_trade']:>+8,.0f}"
          f"{r['mdd']:>9,.0f}{r['worst']:>+11,.0f}  {flag}")

print("\n" + "-"*104)
print("HALF-BY-HALF DETAIL for the stacked variants (per-trade Rs.)")
print("-"*104)
print(f"{'variant':<48}{'half1':>10}{'half2':>10}   (baseline: {base_h1['per_trade']:+.0f} / {base_h2['per_trade']:+.0f})")
for name in ["B: ATR 1.5", "C: cooldown 30", "D: fade 0.0015",
             "E: ATR 1.5 + cooldown 30", "G: ATR 1.5 + cooldown 30 + fade 0.0015",
             "H: ATR 1.2 + cooldown 20 + fade 0.0015 (milder)", "K: ATR 1.5 + cooldown 30, SLmax 800"]:
    r, r1, r2 = results[name]
    print(f"{name:<48}{r1['per_trade']:>+10,.0f}{r2['per_trade']:>+10,.0f}")

print("\n" + "="*104)
print("READ THIS BEFORE APPLYING ANYTHING")
print("="*104)
print("A wider ATR stop (1.5x) means FEWER stop-outs but each loss is BIGGER.")
print("Check the 'worst day' column, not just NET — that is what actually hurts a")
print("Rs.19k account. Reject any variant whose worst day is materially worse than")
print("baseline even if its NET looks better.")
print("="*104)
