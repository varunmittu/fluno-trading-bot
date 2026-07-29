"""
backtest_stopfix.py - does giving the stop room actually help? (2026-07-22)

Sai observed the last several stops each reversed back into profit within ~30
min - the stop was catching the local extreme right before the turn. This tests
whether letting a marginal breach reverse before booking is a NET win, or just
trades saved-whipsaws for bigger real losses (which is how widening the stop
backfired before).

Variants (all on the current live strategy v5.1, real 5-min 60d, realistic
Rs.60 slippage). The stop LEVEL is unchanged; only WHEN it fires changes:
  current       - book the moment a 5-min close is past the stop (today's bot)
  confirm-2     - wait for 2 consecutive 5-min closes past the stop
  buffer-10pt   - only fire once price is 10 index-pts beyond the stop
  buffer-20pt   - 20 index-pts beyond
  confirm2+buf10- both

A winner must beat 'current' in BOTH halves of the data (the project's rule -
anything that wins in only one half is treated as luck). Watch worst-day and
drawdown too: the whole risk is that the non-reversing losses get bigger.
"""
import bt_engine as bt

print("Fetching real 5-min data (60d)...")
F5 = bt.build_5m()
days = sorted(F5["day"].unique()); MID = days[len(days)//2]
SLIP = 60.0

VARIANTS = [
    ("current (immediate)", dict(stop_confirm=1, stop_buffer_pts=0)),
    ("confirm-2 closes",    dict(stop_confirm=2, stop_buffer_pts=0)),
    ("buffer-10pt",         dict(stop_confirm=1, stop_buffer_pts=10)),
    ("buffer-20pt",         dict(stop_confirm=1, stop_buffer_pts=20)),
    ("confirm2 + buf-10pt", dict(stop_confirm=2, stop_buffer_pts=10)),
]

def show(tag, r):
    print(f"  {tag:<22} NET Rs.{r['net']:>+8,.0f} | {r['n']:>3} tr | win {r['wr']:>3.0f}% | "
          f"Rs.{r['per']:>+5,.0f}/tr | maxDD Rs.{r['mdd']:>6,.0f} | worst day Rs.{r['worst']:>+7,.0f}")

print("\n" + "="*96)
print(f"STOP-LOSS TIMING TEST - v5.1, real 5-min {days[0]} -> {days[-1]}, Rs.60 slippage, 1 lot")
print("="*96)

base = bt.run(F5, bt.NEW, slip_rt=SLIP, **VARIANTS[0][1])
results = {}
for tag, kw in VARIANTS:
    full = bt.run(F5, bt.NEW, slip_rt=SLIP, **kw)
    h1   = bt.run(F5, bt.NEW, slip_rt=SLIP, day_filter=lambda d: d <  MID, **kw)
    h2   = bt.run(F5, bt.NEW, slip_rt=SLIP, day_filter=lambda d: d >= MID, **kw)
    results[tag] = (full, h1, h2)
    show(tag, full)

b1 = bt.run(F5, bt.NEW, slip_rt=SLIP, day_filter=lambda d: d <  MID, **VARIANTS[0][1])
b2 = bt.run(F5, bt.NEW, slip_rt=SLIP, day_filter=lambda d: d >= MID, **VARIANTS[0][1])

print("\n  Both-halves check (per-trade Rs.; must beat current in BOTH to be real):")
print(f"    {'variant':<22}{'half-1':>10}{'half-2':>10}{'vs current':>26}")
print(f"    {'current':<22}{b1['per']:>+10,.0f}{b2['per']:>+10,.0f}")
winner = None
for tag, kw in VARIANTS[1:]:
    _, h1, h2 = results[tag]
    b = (h1['per'] > b1['per']) and (h2['per'] > b2['per'])
    verdict = "WINS both halves" if b else ("wins one half only" if (h1['per']>b1['per'] or h2['per']>b2['per']) else "loses both")
    print(f"    {tag:<22}{h1['per']:>+10,.0f}{h2['per']:>+10,.0f}{verdict:>26}")
    if b and (winner is None or results[tag][0]['per'] > results[winner][0]['per']):
        winner = tag

print("\n" + "="*96)
if winner:
    w = results[winner][0]
    print(f"RECOMMEND: '{winner}' - beats current in both halves "
          f"(Rs.{w['per']:+,.0f}/tr vs Rs.{base['per']:+,.0f}, worst day Rs.{w['worst']:+,.0f} vs {base['worst']:+,.0f}).")
    print("Still verify worst-day/drawdown didn't blow out above before deploying.")
else:
    print("NO variant beats current in both halves. The reversals you saw are real, but")
    print("giving the stop room loses more on the trades that DON'T reverse. Keep current stop.")
print("="*96)
