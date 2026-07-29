# NIFTY Bot — GO-LIVE CHECKLIST (real money)

Created 2026-07-06. Go live ONLY when every box is ticked, in order.
The switch itself is one line: `PAPER_TRADE = False` in app.py (line ~54).
Everything else on this list exists to make that one line safe.

## GATE 1 — Validation must pass first (do not skip)
- [ ] Paper validation complete: 2026-07-07 to ~2026-07-20 on v4.4
      (staircase + expiry-Tuesday trading), NO strategy changes mid-run.
- [ ] Paper result is POSITIVE and roughly matches backtest shape
      (~45-55% win rate). If it loses money → do NOT go live; investigate.
- [ ] No bot crashes / duplicate instances / missed exits during the run.
- [ ] Sai actually answered /confirm prompts ~5x/day during validation
      (the backtest assumed instant YES — this must be proven realistic).

## GATE 2 — Zerodha account ready
- [ ] New Zerodha account approved and F&O segment ACTIVATED
      (needs income proof for F&O — check in Console → Segments).
- [ ] Fund the account: Rs.10,000-15,000. This is money Sai can afford
      to lose ENTIRELY (agreed rule). Sai transfers it himself — the bot
      and Claude never touch money movement.
- [ ] Kite Connect app created at developers.kite.trade with the NEW
      account; API key + secret in config. Order APIs are free.
- [ ] PAID Kite Connect data plan (~Rs.500/month) ACTIVE. Not optional:
      without it premiums are ESTIMATED — fine for paper, dangerous for
      real money (staircase exits need real option prices).
- [ ] Verify live: kite_ltp() returns a real NIFTY option premium with
      no PermissionException.

## GATE 3 — Infrastructure
- [ ] STATIC IP working (Zerodha requirement for API orders since Apr 2025).
      Either from the ISP or a small always-on VPS running the bot.
- [ ] Bot machine stays on 9:00-15:35 every trading day: power settings
      set to never sleep, auto-restart of bot after reboot (start_bot.bat
      in Startup or Task Scheduler).
- [ ] Daily token routine tested: Kite access token DIES every morning
      (~7:30 AM). Bot sends the login link on Telegram — Sai must tap it
      and finish login BEFORE 9:15 every trading day. Miss it = bot can
      fetch no data and place no orders that day.
- [ ] Single-instance lock (port 54501) confirmed working — never remove.

## GATE 4 — Dry-run the live plumbing (1 day, smallest size)
- [ ] Flip PAPER_TRADE=False for ONE day with 1 lot (65 units) only.
- [ ] Confirm: order placed → appears in Kite orderbook → Telegram alert
      shows order ID → exit order fires → position actually closed.
- [ ] Compare bot's logged entry/exit price vs Kite's actual fill price
      (slippage check — market orders can fill worse than the signal price).
- [ ] Confirm MIS auto square-off: Zerodha force-closes MIS positions
      ~15:20-15:25. Bot must be flat before then (it exits by 15:15 —
      verify this held).
- [ ] Test the manual kill switch: know how to stop the bot AND close a
      position by hand in the Kite app if the bot dies mid-trade.

## GATE 5 — Live risk settings (day 1 of real money)
- [ ] BASE_LOTS = 1 (65 units, ~Rs.9.5k premium). NO scale-up for at
      least 2 green weeks live.
- [ ] Daily loss limit -Rs.750 confirmed active. Hard SL cap Rs.500.
- [ ] Max 1 open position. Trade 1 auto, all others need /confirm.
- [ ] BANKNIFTY stays OUT (shelved until capital ~Rs.25k+, monthly-only
      expiry makes it unaffordable — decided 2026-07-05).
- [ ] Never restart the bot 10:15-14:30 unless critical; check
      pending_trade.json first (incident rule from 2026-07-03).

## Ongoing (first 2 live weeks)
- [ ] Reconcile trade_log.db vs Kite orderbook + contract notes DAILY —
      any mismatch = stop and investigate before next session.
- [ ] Track real charges (brokerage/STT/etc.) vs backtest's assumed
      charges — if real costs are higher, re-run the backtest with them.
- [ ] Weekly review: if live P&L is far below paper P&L, pause and
      diagnose (slippage? missed confirms? data lag?) instead of pushing on.
- [ ] Forex SIM bot keeps running evenings — it stays paper-only (FEMA),
      results reviewed separately.

## Hard rules (never change without a backtest + Sai's explicit OK)
- Real money = only what Sai can lose entirely. No borrowing to trade.
- Bot never touches deposits/withdrawals/UPI. API key+secret only.
- One instrument (NIFTY), one position, until proven live.
- Losing days are normal. The plan is judged over weeks, not days.
