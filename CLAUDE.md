# Fluno Trading Bot + Portfolio Website — Project Memory

## Who
Sai, founder of Fluno (FMCG brand). Building this as a side project, automating
NIFTY/BANKNIFTY options trading on Zerodha, plus a personal portfolio website.

## Build order (do not skip ahead)
1. Backtest the strategy with real historical data
2. Set up Kite Connect (Zerodha's API — NOT "SmartAPI", that's a different broker's product)
3. Build the bot, paper trade first, only go live after validation
4. Build the website (portfolio + calendar + news section)

## Decisions already made — follow these, don't re-litigate
- Stop loss: -₹150 per trade, automatic, no exceptions.
- Daily loss limit: -₹500. Bot stops for the day. NO "revenge trading" —
  bot never increases size or re-enters to "recover" a loss. Confirmed
  explicitly by user as a hard rule.
- Max 2 open positions at a time.
- Money movement (deposit/withdraw/UPI PIN) is 100% manual, done by Sai on
  his own banking app. The bot and Claude NEVER touch this. Only the Kite
  API key + secret are used by the bot — never the account password, never
  the UPI PIN.
- News/sentiment data (FII/DII flows, global markets, world/India news) is
  for the WEBSITE only — informational, NOT fed into the bot's trading
  decisions yet. Reason: it's unvalidated. Only add it to the bot's logic
  after backtesting shows it actually helps.
- Realistic expectations: the original brief's targets (₹10K → ₹500K in
  6 months) are NOT realistic and should not be treated as a real goal.
  Treat starting capital as money Sai can afford to lose entirely.

## Backtest finding (important, unresolved)
Ran the confidence-score formula from the original brief on synthetic data:
- Technical (40 pts) + Trend (35 pts) = 75 max WITHOUT news/sentiment (25 pts).
- Hitting the 75 threshold requires every single technical+trend condition
  to fire on the same candle — basically never happens in practice.
- Max score actually reached in test: 60. Zero trades fired.
- DECISION NEEDED from Sai before going further: either (a) lower the
  threshold, (b) build real news/sentiment scoring, or (c) rebalance the
  25 news points into technical/trend. Ask him before proceeding if not
  already decided in a later session.

## Technical setup
- Broker: Zerodha. API = Kite Connect (developers.kite.trade), NOT SmartAPI.
- Kite Connect: free Personal plan (no data) or paid Connect plan, ~₹500/month
  per API key, needed for live + historical market data. Order/account APIs
  are free.
- Static IP required to place orders via API (Zerodha requirement since
  April 2025). A small always-on VPS or the user's own static IP works.
- Stack: Python 3.11+, Flask (dashboard/website), SQLite (trade log),
  pandas (indicators — built manually with pandas/numpy, no TA-Lib needed).
- The bot must run on a machine that's always on (Sai's laptop kept on, or
  a cheap VPS) — it can't run inside a chat session.

## Website spec
- Portfolio section: positions, P&L, capital growth.
- Calendar section: one cell per day.
  - Green = profit day, amount shown.
  - Red = loss day, amount shown.
  - Grey = NSE holiday or weekend, labeled "Market closed".
  - Month-end: total P&L, win/loss day count, win rate — formatted so it's
    easy to hand to a CA at tax time.
- News/market section: global indices, NSE/BSE, FII/DII daily flows, India +
  world news tagged by sector. Read-only context, not connected to bot logic
  (see decision above).

## Communication style for this user
- Keep answers SHORT, plain words, bullet points. No long paragraphs.
- He finds long technical explanations hard to read — always simplify.
- Confirm understanding in plain bullets before building anything big.
- Flag unrealistic expectations honestly and gently, don't just agree.
