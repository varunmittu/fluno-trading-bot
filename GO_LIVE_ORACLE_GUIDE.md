# GO-LIVE ON ORACLE — Step-by-Step Guide

Written 2026-07-27 for Sai. This is the **complete path** from the current
laptop paper bot to a real-money bot running on the Oracle server with a
static IP. Follow it **only when trust score ≥ 6** (your own rule).

**Nothing here is urgent.** The bot works fine on the laptop meanwhile. Do
this with help available (renewed Pro, a coder friend, or a future Claude) —
NOT alone on the last day of a Pro subscription.

---

## THE ORDER MATTERS. Do these in sequence, not out of order.

### Gate 0 — Precondition
- [ ] Trust score ≥ 6 on the dashboard (this is the only reason to start).
- [ ] ₹15k+ funds sitting in the Zerodha account (real orders need margin).
- [ ] You have help available for the next 2–3 days in case something breaks.

### STEP 1 — Reserve the Oracle IP (so it never changes)
Your instance IP (`92.4.79.51`) may be "ephemeral" and can change if the
instance is ever recreated. Reserve it first, or you'll waste your one
Zerodha IP change per week.
1. Oracle console → ☰ → **Networking → Reserved public IPs** (or Instance →
   attached VNIC → IPv4 → the public IP → **Edit** → change to **Reserved**).
2. Confirm the instance keeps the **same** IP after reserving.
3. Write the final reserved IP here: `________________`

### STEP 2 — Migrate the bot onto the Oracle server (Linux)
The bot must RUN on Oracle so its orders come from the reserved IP.
On the Oracle server (SSH in with the key):
```
ssh -i "C:\Users\avina\Downloads\ssh-key-2026-07-26 (1).key" ubuntu@<IP>
sudo apt update && sudo apt install -y python3 python3-pip git
```
Copy the whole `varun trading` folder to the server (e.g. via `scp` or git).
Then port these Windows-specific bits to Linux:
- `sync_to_vercel()` uses `git = r"C:\Program Files\Git\bin\git.exe"` →
  change to `git = "git"` (Linux git is on PATH).
- The `.bat`/`.vbs` watchdog → replaced by systemd in STEP 3.
- `subprocess.check_call([... pip install ...])` works as-is on Linux.
- Put `config.py.txt`, `telegram_token.txt`, `telegram_chat.txt` on the
  server (NEVER commit them — they're gitignored).
Install Python deps: `pip3 install flask kiteconnect pandas numpy yfinance requests`.
Open the dashboard port if you want localhost-style access:
- Oracle console → the VCN's **security list** → add ingress rule TCP 5000.
- On the server: `sudo iptables -I INPUT -p tcp --dport 5000 -j ACCEPT`.
Test it runs in PAPER first: `python3 app.py` → check `http://<IP>:5000`.

### STEP 3 — Make it self-healing (systemd auto-restart)
This is what keeps the bot alive without you. Create the service file:
```
sudo nano /etc/systemd/system/flunobot.service
```
Paste:
```
[Unit]
Description=Fluno Trading Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/varun trading
ExecStart=/usr/bin/python3 /home/ubuntu/varun trading/app.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```
Enable + start:
```
sudo systemctl daemon-reload
sudo systemctl enable flunobot
sudo systemctl start flunobot
```
Now the bot **auto-restarts on crash AND on server reboot** — forever. Check
it: `sudo systemctl status flunobot` and `journalctl -u flunobot -f` for logs.
The single-instance lock (port 54501) stays — never remove it.

### STEP 4 — Register the reserved IP with Zerodha
Only NOW (bot is running on Oracle, orders will come from the reserved IP):
1. developers.kite.trade → **Profile** (top-right) → **IP Whitelist**.
2. Paste the reserved IP → **Update**.
3. Note: **1 IP change per WEEK**, takes effect on next login/token. Get it
   right the first time.
4. Send `/token` from Telegram (the bot on Oracle handles the login flow).

### STEP 5 — Supervised 1-lot TEST (still risking almost nothing)
This proves the ORDER CODE works. It does NOT mean full go-live.
1. In app.py set `LIVE_ENABLED = True` (the master lock) AND on Telegram send
   `/real` (twice, to confirm). Both are required to place a real order.
2. Wait for ONE signal. Watch on Kite:
   - [ ] Order actually placed → appears in Kite orderbook with an order ID
   - [ ] Telegram shows "LIVE ORDER PLACED" with the ID
   - [ ] The exit order fires (stop/staircase/EOD) → position CLOSED in Kite
   - [ ] Bot's logged entry/exit ≈ Kite's real fill (small slippage OK)
3. If ANYTHING is wrong (no exit, stuck position) → send `/paper`, set
   `LIVE_ENABLED=False`, close the position by hand in Kite, and STOP. Debug
   before trying again.
4. Know your manual kill switch: how to stop the bot (`sudo systemctl stop
   flunobot`) and close a position by hand in the Kite app.

### STEP 6 — Go live for real (small)
- [ ] `BASE_LOTS = 1` — one lot only, no scale-up for 2 green weeks.
- [ ] Daily loss stop confirmed active (`DAILY_LIMIT = -1000`).
- [ ] Keep `/paper` handy — one command stops real trading instantly.
- [ ] Reconcile `trade_log.db` vs the Kite orderbook EVERY day. Any mismatch
      → stop and investigate before the next session.

---

## Daily routine once live (every trading morning)
1. Send `/token` on Telegram before 9:15 (Kite login expires daily).
2. Glance at the Vercel dashboard (fluno-trading-bot.vercel.app) — is it live?
3. That's it. systemd keeps it running; Telegram alerts you to anything.

## If it breaks and you're alone (no Claude / no coder)
- **Bot crashed / server rebooted** → systemd already restarted it. Do nothing.
- **"Kite offline" on Telegram** → send `/token`. Fixed.
- **Something looks wrong with money** → send `/paper` immediately (stops real
  orders), then close any open position by hand in the Kite app. You are now
  safe; sort out the code later with help.
- **Want to fully stop** → `sudo systemctl stop flunobot`.

## BANK NIFTY — dual-instrument, CAPITAL-GATED (Sai's rule, 2026-07-28)
Build this as its own careful project — do NOT rush it into the live NIFTY bot,
it needs its own testing.
- **Hard gate:** BANK NIFTY trades ONLY when running capital ≥ **Rs.30,000**
  (add `BANKNIFTY_MIN_CAPITAL = 30000`; check it before any BN entry). Reason:
  1 BN lot = ~Rs.29,000 (monthly-only, verified 2026-07-28). Below Rs.30k it
  cannot afford even one lot, so it must stay dormant.
- **Never trade cheap deep-OTM BN options** to fake affordability — they barely
  move (delta ~0.05) and bleed on time decay; they LOSE. BN must trade ATM,
  which is why the Rs.30k gate exists.
- **Dual-instrument logic (approved spec):** each entry cycle, analyze NIFTY
  AND BANKNIFTY independently (same signal ladder + 15m MTF + conf score);
  enter the BETTER setup; if BOTH qualify AND capital affords both premiums,
  take both (max 1 position per instrument). Below Rs.30k, only NIFTY runs.
- **BN needs its own money params** — the SL (Rs.200-1000), staircase rungs
  (150-900) and premium model are NIFTF-calibrated; BN moves ~2× and its ATM
  premium is ~Rs.900/unit, so re-tune before trading BN real. Directional edge
  IS proven (backtest_banknifty_6mo.py: +Rs.142,921 / 73% / 6mo).
- Build order: extend the backtest to a dual-instrument capital-split test
  FIRST, then refactor app.py's entry loop, then paper-validate BN separately.

## FUTURE ROADMAP — Multi-broker (Sai's idea, 2026-07-28)
Vision: connect several brokers (Kite, Groww, Upstox, Angel…) and trade the
same strategy across all of them from one bot. Build ONLY after the strategy
is proven profitable on Kite over months.
- **Architecture:** a "broker adapter" layer. The strategy stays one codebase;
  each broker plugs in behind a common interface (connect / get_ltp /
  place_order / get_positions / exit). execute_order() already IS that
  chokepoint — generalise it to call the active broker adapter(s).
- **Per broker = a small dev project** (each API differs: auth flow, order
  format, option symbol convention, data plan, rate limits, static-IP rule).
  Once an adapter exists, adding YOUR account for that broker is just
  paste-key-and-secret → connected.
- **UX Sai wants:** a simple "add broker → paste API key + secret → connect"
  screen; the bot then trades that account too. Store creds in gitignored
  per-broker files (never commit), same pattern as config.py.txt.
- **HONEST FRAMING (told Sai, keep saying it):** multiple brokers is CAPITAL
  SCALING, not multiplied income. Same strategy on 3 accounts = same edge per
  rupee on 3× the money — more profit if it works, more loss if it doesn't.
  Not a money multiplier. Prove ONE broker first; multiplying an unproven
  strategy just multiplies risk.
- **Watch-outs:** per-broker static IP whitelisting; per-broker data costs
  (Kite ₹500, Dhan/Groww ₹499, Upstox/Angel cheaper/free); position
  reconciliation across accounts; one broker's order failing mid-basket.

## Hard rules (never break)
- Real money = only what you can lose entirely. Never borrow to trade.
- The bot NEVER touches deposits/withdrawals/UPI — only the API key.
- One instrument (NIFTY), one lot, until proven over weeks live.
- Never run TWO copies of the bot (double orders — happened 2026-07-03). The
  port-54501 lock prevents it; keep it.
- Do not flip `LIVE_ENABLED = True` until STEP 5's test has passed once.
