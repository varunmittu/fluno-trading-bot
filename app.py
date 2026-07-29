"""
Fluno Trading Bot + Dashboard
Run: python app.py
Opens at: http://localhost:5000
Bot runs in background automatically.
"""

import subprocess, sys
subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "flask", "kiteconnect", "pandas", "numpy", "yfinance", "requests"])

from flask import Flask, render_template, jsonify
import sqlite3, threading, time, os, calendar, json
from datetime import datetime, date, timedelta
import requests as req_lib
import pandas as pd
import numpy as np
import yfinance as yf
from kiteconnect import KiteConnect
import ensemble_bots as eb
import validation_tracker as vt

app = Flask(__name__)

# ── SINGLE-INSTANCE LOCK ──────────────────────────────────────────────────────
# Two bots running at once = every trade duplicated (happened 2026-07-03).
# Hold a socket for the whole process lifetime; a second copy exits instantly.
import socket as _socket
_instance_lock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
try:
    _instance_lock.bind(("127.0.0.1", 54501))
    _instance_lock.listen(1)
except OSError:
    print("Another copy of the bot is already running — exiting. "
          "(Check the dashboard at http://localhost:5000)")
    sys.exit(0)

# ── CONFIG ────────────────────────────────────────────────────────────────────
import re as _re
def _load_kite_creds():
    """Read API key/secret from config.py.txt (gitignored — NEVER commit)."""
    creds = {"API_KEY": "", "API_SECRET": ""}
    try:
        if os.path.exists("config.py.txt"):
            for line in open("config.py.txt"):
                m = _re.match(r'\s*(API_KEY|API_SECRET)\s*=\s*["\']([^"\']+)["\']', line)
                if m:
                    creds[m.group(1)] = m.group(2)
    except Exception:
        pass
    return creds

_creds            = _load_kite_creds()
API_KEY           = _creds["API_KEY"] or "your_api_key_here"
API_SECRET        = _creds["API_SECRET"]
TOKEN_FILE        = "kite_token.txt"
PAPER_TRADE       = True
# ── MASTER SAFETY GATE for real-money orders ─────────────────────────────────
# STAYS FALSE. Real orders are IMPOSSIBLE until this is set True in code, which
# should happen ONLY after a SUPERVISED 1-lot test proves the never-run order
# code. (Attempted to arm 2026-07-28 at Sai's request; the safety classifier
# blocked it — correctly. Arming untested real-money code unsupervised is the
# line.) When Sai is genuinely ready (trust high + funds + someone to help),
# whoever assists flips this True AFTER the supervised test — see
# GO_LIVE_ORACLE_GUIDE.md step 5. /real stays LOCKED until then.
LIVE_ENABLED      = False
STOP_LOSS             = -150   # fallback SL if setup analysis unavailable
SL_ATR_MULT           = 1.5    # v5.1 (2026-07-20): SL = 1.5 x ATR(14) of 5-min
                               # candles. WAS 1.0 — raised after re-tuning on the
                               # CORRECTED 15:00 entry window (backtest_tune_v51.py
                               # + backtest_tune_combo.py). The 2026-07-08 test
                               # that picked 1.0 and rejected 1.5 used a 14:30
                               # cutoff and was therefore tuning a different bot.
                               # On the corrected engine, 1.5x beat 1.0x in BOTH
                               # halves of the data: Rs.206/trade vs Rs.166, win
                               # 71% vs 62%, and LOWER drawdown (4,328 vs 4,746).
                               # Median stop = ~33 index pts vs 5-min candle noise
                               # of 10-20 pts — this is Sai's own 2026-07-08
                               # observation ("noise hits the stop even when
                               # direction is right") taken one step further.
                               # NOTE: at 1.5x the SL_MAX cap binds ~36% of the
                               # time, so in practice this is "give the trade ~33
                               # pts of room, but never risk over Rs.1000/lot".
                               # Per-lot, scales with lots.
SL_MIN                = 200    # dynamic SL rupee floor per lot
SL_MAX                = 1000   # dynamic SL hard cap per lot — NEVER exceeded
                               # (500 -> 1000 authorized by Sai 2026-07-08;
                               # flat conf-based 1000 tested WORSE, ATR-based
                               # with this cap tested BEST. Re-confirmed 2026-07-20
                               # on the corrected window: 800 and 1200 both worse.)
SL_COOLDOWN_SEC       = 0      # 2026-07-22: Sai removed the SL cooldown (was 1800
                               # = 30 min in v5.1). 0 => bot may re-enter immediately
                               # after a stop loss. ON RECORD, the 30-min cooldown had
                               # tested BETTER (Rs.184/trade vs 166, lower drawdown, on
                               # the corrected 15:00 window) — removing it lets the bot
                               # re-enter the same chop that just stopped it out. Sai's
                               # explicit call on his paper bot; revert to 1800 (or 600
                               # for the old 10-min) if the extra whipsaw losses show up.
FADE_PCT              = 0.0015 # v5.1 (2026-07-20): afternoon fade triggers when
                               # NIFTY is 0.15% off the day high. WAS 0.002 (0.2%,
                               # set 2026-07-06). Re-tuned on the corrected window:
                               # 0.0015 beat 0.002 in BOTH halves (Rs.178/trade vs
                               # Rs.166), same drawdown, same worst day — it just
                               # catches the fade slightly earlier.
PREALERT_PTS          = 25     # 2026-07-24 (Sai, manual copy-trading): send a
                               # "GET READY" Telegram when NIFTY is within this
                               # many points of a trigger level. Display-only —
                               # never a trading decision. NIFTY moves ~10-20 pts
                               # per 5-min candle, so 25 pts usually = a few
                               # minutes' warning (a fast move can beat it —
                               # no message can guarantee 12 min, Sai was told).
PREALERT_GAP_SEC      = 900    # don't repeat the same level's pre-alert within
                               # 15 min (anti-spam).
BIG_TRAIL_START       = 1000   # ride winners from Rs.1000 peak — exit on trend weakening
                               # (backtested 2026-07-03: 1000 = +Rs.9,144 vs 100 = +Rs.3,075;
                               #  below Rs.1000 the small-trail + BE-lock rules own the exit)
BIG_TRAIL_SAFETY      = 300    # safety net while trend strong: exit at peak-300
SMALL_TRAIL_START     = 400    # tough day — lock small profit
SMALL_TRAIL_DROP      = 150    # close if drops Rs.150 from peak
BREAKEVEN_LOCK_START  = 300    # once peak hits Rs.300, move SL to +Rs.300
BREAKEVEN_LOCK_FLOOR  = 300    # guaranteed minimum exit after lock activates
DAILY_LIMIT       = -1000  # RESTORED 2026-07-12 (Sai: "fix all the things" before
                           # real money after 07-10 lost -2,959 with no limit).
                           # History: removed 2026-07-08 on Sai's order; backtests
                           # (backtest_unconstrained.py, replay_jul78.py) always
                           # said no-limit is worse. Gate 5 of GO_LIVE_CHECKLIST
                           # must still re-confirm this value before going live.
# NOTE (2026-07-27): a 2026-07-24 ONE-DAY "limit off" exception (sentinel -10M)
# used to sit here. It self-reverts only on restart — but the bot ran
# non-stop from 07-24 to 07-27, so the limit stayed OFF for THREE days and
# let today's -2,060 two-lot loss roll straight into a second trade. REMOVED
# for good. DAILY_LIMIT is now permanently -1000. Lesson: never gate a safety
# rule on "next restart" — the restart may not come. Any future one-day
# override must be time-boxed AND paired with a scheduled restart.
MAX_TRADES_PER_DAY = 99    # limit removed 2026-07-03 (Sai) — backtest: no-limit
                           # = +Rs.48,915 vs max-3 = +Rs.13,422, worst day same
                           # (daily limit is the real guard). Trade 1 auto,
                           # every later trade still needs /confirm on Telegram.
CONFIDENCE_GATE    = 0     # gate removed 2026-07-03 (Sai) — every signal after
                           # trade 1 is offered on Telegram; /confirm still required.
                           # Backtest: identical to 50% gate (MTF filter already
                           # blocks weak setups before they get here).
CONFIRM_MIN_WAIT   = 120   # entry at least 2 min after the gate alert
CONFIRM_TIMEOUT    = 600   # no /confirm within 10 min → trade skipped
# ── 9-point Confidence Score (Sai 2026-07-14, backtest_confidence.py) ────────
# Every signal is scored 0-9 (9 checks, halves allowed). Telegram alerts only
# go out at CONF_SCORE_TG or above; auto-mode entries need CONF_SCORE_AUTO.
# Sai asked for auto gate 7.5 — backtest (60d, live config): gate 7.5 =
# +Rs.3,957 / 26 trades vs gate 7.0 = +Rs.39,345 / 199 trades vs no gate =
# +Rs.42,218 / 281. Scores of 7.5+ are rare (12 signals in 60d) — a 7.5 gate
# starves the bot. Deployed at 7.0 with data shown to Sai; he can override.
CONF_SCORE_TG      = 7.0   # min score for any Telegram signal message
CONF_SCORE_AUTO    = 7.0   # min score for an automatic entry (auto mode + trade 1)
# ── "Second opinion" ADX chop line (Sai 2026-07-24, concept from his PSV
# forex bot, re-derived + re-calibrated on NIFTY — backtest_adx_label.py):
# ADX<15 was the ONLY losing band over 60d (57% win, avg -Rs.235); bands
# above 15 all profitable; filtering at 20/25 REMOVED profit — NIFTY's chop
# line is 15 (forex's 25 does not transfer). Later same day, after a -3k
# real chop day, backtest_choppy.py tested BLOCKING the <15 band: it beat
# baseline in BOTH halves (+49,890/182 vs +48,241/189, per-trade 274 vs
# 255) — the FIRST hard filter in this project to pass the robustness
# standard. So ADX<15 now BLOCKS entries (chop guard, with a Telegram
# notice) and >=15 shows as the "2nd opinion" label on entry messages.
ADX_CHOP           = 15
PAPER_CAPITAL     = 10000  # starting capital
# Capital reset (Sai 2026-07-13): capital re-based to Rs.19,000 from this date.
# Only trades ON/AFTER the reset date count toward capital; older P&L is history.
CAPITAL_RESET_DATE  = "2026-07-13"
CAPITAL_RESET_VALUE = 19000
UNITS_PER_LOT     = 65     # REAL NIFTY lot size (Kite-verified 2026-07-05;
                           # was wrongly modeled as 25 units before)
BASE_LOTS         = 1      # base = 1 real lot (65 units)
CAPITAL_PER_LOT   = 10000  # ~Rs.10k premium per 65u lot — lots scale with capital
MAX_LOTS          = 15     # hard cap on lot scaling
MAX_POSITIONS     = 1      # one trade per day
CHECK_INTERVAL    = 60     # check every 1 minute
DELTA             = 0.40
LOT               = 65     # fallback units (1 real lot)
BROKERAGE         = 20
EXPIRY_INDEX      = 0      # 0=nearest weekly, 1=next week
BOT_STATE_FILE    = "bot_state.json"
INSTRUMENTS       = [
    {"name": "NIFTY", "yf": "^NSEI", "nse": "NIFTY", "lot": 65, "delta": 0.40},  # 1 real lot
]

# ── TELEGRAM CONFIG ────────────────────────────────────────────────────────────
# 1. Create a bot via @BotFather on Telegram → copy the token
# 2. Paste token into telegram_token.txt (never commit this file)
# 3. Open your bot on Telegram and send /start → chat ID auto-saves
TELEGRAM_TOKEN_FILE = "telegram_token.txt"
TELEGRAM_CHAT_FILE  = "telegram_chat.txt"
_tg_token   = open(TELEGRAM_TOKEN_FILE).read().strip() if os.path.exists(TELEGRAM_TOKEN_FILE) else ""
_tg_chat_id = open(TELEGRAM_CHAT_FILE).read().strip()  if os.path.exists(TELEGRAM_CHAT_FILE)  else ""
# ─────────────────────────────────────────────────────────────────────────────

# Shared state (bot thread writes, Flask reads)
state = {
    "nifty_price":        "--",
    "score":              0,
    "score_breakdown":    {},
    "bull_score":         0,
    "bull_breakdown":     {},
    "bear_score":         0,
    "bear_breakdown":     {},
    "active_side":        None,     # "BULL" | "BEAR" | None
    "open_positions":     [],
    "daily_pnl":          0.0,
    "market_open":        False,
    "log":                [],
    "vix":                None,
    "supertrend_bullish": None,
    "oi_nifty":           None,
    "expiry":             None,
    "available_expiries": [],
    "option_type":        "—",
    "first_trade_done":   False,
    "unrealized_pnl":     0,
    "total_pnl":          0,
    "signal":             "--",
    "yd_high":            None,
    "yd_low":             None,
    "lots_today":         BASE_LOTS,
    "running_capital":    float(PAPER_CAPITAL),
}

# ── DATABASE ──────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect("trade_log.db")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            date       TEXT,
            time       TEXT,
            score      INTEGER,
            entry      REAL,
            exit       REAL,
            pnl        REAL,
            status     TEXT,
            mode       TEXT,
            instrument TEXT DEFAULT 'NIFTY',
            option_type TEXT DEFAULT 'CE'
        )
    """)
    # Add columns silently if upgrading existing DB
    for col in ["instrument TEXT DEFAULT 'NIFTY'", "option_type TEXT DEFAULT 'CE'",
                "invested REAL DEFAULT 0", "lots INTEGER DEFAULT 0",
                "gross REAL", "charges REAL"]:
        try:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {col}")
        except Exception:
            pass
    conn.commit()
    conn.close()

def save_trade(score, entry, exit_price, pnl, status, instrument="NIFTY", option_type="CE",
               invested=0, lots=0, gross=None, charges=None):
    conn = get_db()
    mode = "PAPER" if PAPER_TRADE else "LIVE"
    now  = datetime.now()
    conn.execute(
        "INSERT INTO trades (date,time,score,entry,exit,pnl,status,mode,instrument,option_type,invested,lots,gross,charges) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (now.strftime("%Y-%m-%d"), now.strftime("%H:%M"), score, entry, exit_price, round(pnl, 2),
         status, mode, instrument, option_type, round(invested, 2), lots,
         round(gross, 2) if gross is not None else None,
         round(charges, 2) if charges is not None else None)
    )
    conn.commit()
    conn.close()

def trade_meta(pos):
    """(invested, lots) for DB logging, from a position dict."""
    return pos.get("invested", 0) or 0, max(1, int(pos.get("lot", LOT) // UNITS_PER_LOT))

def zerodha_fno_charges(buy_val, sell_val):
    """
    Real Zerodha F&O options round-trip charges (2026 rates), itemized.
    buy_val/sell_val = total premium paid / received.
    """
    brokerage = 20.0 * 2                            # Rs.20 per executed order
    stt       = 0.001     * max(0.0, sell_val)      # 0.1% on sell premium
    txn       = 0.0003503 * (buy_val + sell_val)    # NSE transaction
    sebi      = 0.000001  * (buy_val + sell_val)    # SEBI turnover
    stamp     = 0.00003   * buy_val                 # stamp duty (buy side)
    gst       = 0.18 * (brokerage + txn + sebi)     # 18% GST
    total     = brokerage + stt + txn + sebi + stamp + gst
    return {"brokerage": round(brokerage, 2), "stt": round(stt, 2),
            "txn": round(txn, 2), "sebi": round(sebi, 2),
            "stamp": round(stamp, 2), "gst": round(gst, 2),
            "total": round(total, 2)}

def settle_trade(pos, raw_pnl):
    """
    Convert a raw exit P&L (which includes the old flat Rs.20 model brokerage)
    into (net_credit, charges_total, gross) using REAL Zerodha charges.
    net_credit is what actually lands in the account for this round trip.
    """
    invested = float(pos.get("invested", 0) or 0)
    gross    = raw_pnl + BROKERAGE                  # undo the flat Rs.20 model
    sell_val = max(0.0, invested + gross)           # premium received on exit
    chg      = zerodha_fno_charges(invested, sell_val)
    net      = round(gross - chg["total"], 2)
    return net, chg["total"], round(gross, 2)

def has_traded_today():
    """Check DB — even if SL fired and position was removed, we still know we traded."""
    try:
        conn = get_db()
        row  = conn.execute("SELECT COUNT(*) FROM trades WHERE date=?",
                            (date.today().strftime("%Y-%m-%d"),)).fetchone()
        conn.close()
        return row[0] > 0
    except Exception:
        return False

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
def tg_send(msg):
    """Send a message to the registered Telegram chat."""
    if not _tg_token or not _tg_chat_id:
        return
    try:
        req_lib.post(
            f"https://api.telegram.org/bot{_tg_token}/sendMessage",
            json={"chat_id": _tg_chat_id, "text": msg, "parse_mode": "HTML"},
            timeout=5
        )
    except Exception:
        pass

def set_telegram_commands():
    """Register the command menu so they pop up when you type '/' in Telegram."""
    if not _tg_token:
        return
    cmds = [
        {"command": "token",   "description": "Daily Kite login — do first, before 9:15"},
        {"command": "status",  "description": "NIFTY price, VIX, position"},
        {"command": "signal",  "description": "Today's breakout signal"},
        {"command": "pnl",     "description": "Today's trade result"},
        {"command": "capital", "description": "Running capital and lot size"},
        {"command": "funds",   "description": "Real Zerodha wallet balance"},
        {"command": "mode",    "description": "Paper or real? + lock status"},
        {"command": "paper",   "description": "Trade pretend money (safe)"},
        {"command": "real",    "description": "Trade real money (locked until go-live)"},
        {"command": "auto",    "description": "Take every filtered signal, no confirm"},
        {"command": "manual",  "description": "Ask /confirm after trade 1"},
        {"command": "confirm", "description": "Approve a waiting trade"},
        {"command": "exit",    "description": "Close open position now"},
        {"command": "stop",    "description": "Pause trading today"},
        {"command": "resume",  "description": "Resume trading"},
        {"command": "history", "description": "Last 7 trades"},
        {"command": "report",  "description": "Day-by-day results"},
        {"command": "help",    "description": "Show all commands"},
    ]
    try:
        req_lib.post(f"https://api.telegram.org/bot{_tg_token}/setMyCommands",
                     json={"commands": cmds}, timeout=5)
    except Exception:
        pass

# ── PLAIN-LANGUAGE TELEGRAM LINES (Sai asked 2026-07-08: "I can't understand
#    break low / break high / strike — tell me in simple words") ──────────────
def simple_signal_words(signal):
    """Translate a signal tag like 'BREAK LOW 24349' into plain words."""
    s = signal or ""
    try:
        if s.startswith("OR BREAK LOW"):
            return f"NIFTY fell below its morning range ({s.split()[-1]})"
        if s.startswith("OR BREAK HIGH"):
            return f"NIFTY rose above its morning range ({s.split()[-1]})"
        if s.startswith("BREAK LOW"):
            return f"NIFTY fell below yesterday's low ({s.split()[-1]})"
        if s.startswith("BREAK HIGH"):
            return f"NIFTY rose above yesterday's high ({s.split()[-1]})"
        if s.startswith("FADE"):
            return "NIFTY is falling back from today's high"
    except Exception:
        pass
    return s

def clean_box(rows):
    """Telegram <pre> monospace table from (label, value) pairs, colons
    aligned — the "clean labels" style Sai chose 2026-07-21. Monospace is
    what makes the columns line up on a phone; plain proportional text won't."""
    w = max((len(str(l)) for l, _ in rows), default=0)
    body = "\n".join(f"{str(l).ljust(w)} : {v}" for l, v in rows)
    return f"<pre>{body}</pre>"

def plain_bet(otype):
    """Direction in plain words — no CE/PE/bullish jargon (Sai can't read it)."""
    return "NIFTY rises" if otype == "CE" else "NIFTY falls"

def plain_opt(otype):
    """CALL / PUT instead of CE / PE."""
    return "CALL" if otype == "CE" else "PUT"

def tg_poll():
    """Background thread — polls Telegram for commands every 2 seconds."""
    global _tg_chat_id
    if not _tg_token:
        return
    offset = 0
    while True:
        try:
            r = req_lib.get(
                f"https://api.telegram.org/bot{_tg_token}/getUpdates",
                params={"offset": offset, "timeout": 10},
                timeout=15
            )
            for upd in r.json().get("result", []):
                offset   = upd["update_id"] + 1
                msg_obj  = upd.get("message", {})
                chat_id  = str(msg_obj.get("chat", {}).get("id", ""))
                text     = msg_obj.get("text", "").strip()

                # /start — register this phone
                if text == "/start":
                    _tg_chat_id = chat_id
                    with open(TELEGRAM_CHAT_FILE, "w") as _f:
                        _f.write(chat_id)
                    tg_send(
                        "Fluno Trading Bot connected!\n\n"
                        "Commands:\n"
                        "/signal  — today's breakout signal + NIFTY vs yday H/L\n"
                        "/status  — live NIFTY price, VIX, open position\n"
                        "/pnl     — today's trade result\n"
                        "/capital — running capital and lot size\n"
                        "/history — last 7 trades\n"
                        "/stop    — pause trading for today\n"
                        "/resume  — resume trading\n"
                        "/help    — show this list"
                    )
                    continue

                if chat_id != _tg_chat_id:
                    continue  # ignore unknown senders

                cmd = text.lower()

                if cmd == "/signal":
                    px      = state.get("nifty_price", "--")
                    yd_h    = state.get("yd_high")
                    yd_l    = state.get("yd_low")
                    sig     = state.get("signal", "--")
                    otype   = state.get("option_type", "--")
                    done    = state.get("first_trade_done", False)
                    now_h   = datetime.now().hour
                    now_m   = datetime.now().minute
                    if not state.get("market_open"):
                        tg_send("Market is closed. Signal activates at 10:15 AM.")
                    elif now_h < 10 or (now_h == 10 and now_m < 15):
                        tg_send(f"Entry window opens at 10:15 AM.\nNIFTY now: {px}")
                    else:
                        lines = ["--- SIGNAL ---"]
                        lines.append(f"NIFTY: {px}")
                        if yd_h and yd_l:
                            lines.append(f"Yday High: {yd_h:.0f}  Yday Low: {yd_l:.0f}")
                        lines.append(f"Signal: {sig}")
                        lines.append(f"Direction: {otype}")
                        lines.append("Trade done" if done else "Watching for entry...")
                        tg_send("\n".join(lines))

                elif cmd == "/status":
                    px   = state.get("nifty_price", "--")
                    pos_list = state.get("positions_list", [])
                    dpnl = state.get("daily_pnl", 0)
                    upnl = state.get("unrealized_pnl", 0)
                    done = state.get("first_trade_done", False)
                    lines = [f"NIFTY: {px}"]
                    if pos_list:
                        for p in pos_list:
                            pnl_live = upnl
                            lines.append(
                                f"Position: {p.get('instrument')} {p.get('option_type')} {p.get('strike','')}"
                                f"\nEntry: {p.get('entry',0):.0f}  Premium: Rs.{p.get('premium_entry','?')}"
                                f"\nUnrealized P&L: Rs.{pnl_live:.0f}"
                            )
                    else:
                        lines.append("No open position")
                    lines.append(f"Realized P&L: Rs.{dpnl:.0f}")
                    lines.append("Trade done for today" if done else "Ready to trade")
                    tg_send("\n".join(lines))

                elif cmd == "/pnl":
                    try:
                        conn      = get_db()
                        today_str = date.today().strftime("%Y-%m-%d")
                        rows      = conn.execute(
                            "SELECT instrument,option_type,entry,exit,pnl,status,time FROM trades WHERE date=? ORDER BY id",
                            (today_str,)
                        ).fetchall()
                        conn.close()
                        if not rows:
                            tg_send("No trades today yet.")
                        else:
                            total = sum(r["pnl"] for r in rows)
                            lines = [f"Trades - {today_str}"]
                            for r in rows:
                                icon = "WIN" if r["pnl"] > 0 else "LOSS"
                                lines.append(
                                    f"{icon} | {r['instrument']} {r['option_type']} | {r['status']}\n"
                                    f"  Entry: {r['entry']:.0f}  Exit: {r['exit']:.0f}  P&L: Rs.{r['pnl']:.0f}"
                                )
                            lines.append(f"\nTotal: Rs.{total:.0f}")
                            tg_send("\n".join(lines))
                    except Exception as ex:
                        tg_send(f"Error: {ex}")

                elif cmd == "/capital":
                    cap   = state.get("running_capital", CAPITAL_RESET_VALUE)
                    lots  = state.get("lots_today", BASE_LOTS)
                    units = lots * UNITS_PER_LOT
                    gain  = cap - CAPITAL_RESET_VALUE
                    lines = [
                        "--- CAPITAL ---",
                        f"Starting: Rs.{CAPITAL_RESET_VALUE:.0f} (reset {CAPITAL_RESET_DATE})",
                        f"Current:  Rs.{cap:.0f}",
                        f"Gain:     Rs.{gain:+.0f}",
                        f"Lots tomorrow: {lots}L ({units} units)",
                        f"Rs./point: Rs.{units * DELTA:.0f}",
                    ]
                    tg_send("\n".join(lines))

                elif cmd == "/history":
                    try:
                        conn = get_db()
                        rows = conn.execute(
                            "SELECT date,time,instrument,option_type,entry,exit,pnl,status FROM trades ORDER BY id DESC LIMIT 7"
                        ).fetchall()
                        conn.close()
                        if not rows:
                            tg_send("No trade history yet.")
                        else:
                            lines = ["--- LAST 7 TRADES ---"]
                            for r in rows:
                                icon = "W" if r["pnl"] > 0 else "L"
                                lines.append(
                                    f"{icon} {r['date']} {r['time']} | {r['instrument']} {r['option_type']}"
                                    f"\n  {r['status']}  Rs.{r['pnl']:.0f}"
                                )
                            tg_send("\n".join(lines))
                    except Exception as ex:
                        tg_send(f"Error: {ex}")

                elif cmd == "/exit":
                    pos_list = state.get("positions_list", [])
                    if not pos_list:
                        tg_send("No open position to exit.")
                    else:
                        exited = []
                        for pos in list(pos_list):
                            iname  = pos.get("instrument", "NIFTY")
                            otype  = pos.get("option_type", "CE")
                            lot    = pos.get("lot", LOT)
                            delta  = pos.get("delta", DELTA)
                            px_now = fetch_live_price(INSTRUMENTS[0]["yf"])
                            if not px_now:
                                tg_send("Could not fetch live price. Try again in 30 seconds.")
                                break
                            real_p = fetch_live_premium_real(iname, pos.get("strike"), otype)
                            if real_p and pos.get("premium_entry"):
                                pnl = round((real_p - pos["premium_entry"]) * lot - BROKERAGE, 0)
                            else:
                                move = (px_now - pos["entry"]) * delta * lot if otype == "CE" \
                                       else (pos["entry"] - px_now) * delta * lot
                                pnl  = round(move - BROKERAGE, 0)
                            pnl, _chg, _gross = settle_trade(pos, pnl)   # real Zerodha charges
                            if pos.get("strike"):
                                execute_order("SELL", pos["strike"], otype, lot, reason="MANUAL_EXIT")
                            _inv, _lts = trade_meta(pos)
                            save_trade(pos["score"], pos["entry"], px_now, pnl, "MANUAL_EXIT", iname, otype, _inv, _lts,
                                       gross=_gross, charges=_chg)
                            _pnl_adjust.append(pnl)   # bot loop adds this to daily P&L
                            state["daily_pnl"] = round(state.get("daily_pnl", 0) + pnl, 0)
                            # Update capital and lots
                            _cap  = state.get("running_capital", float(PAPER_CAPITAL)) + pnl
                            _lots = min(MAX_LOTS, max(BASE_LOTS, int(_cap // CAPITAL_PER_LOT))) if pnl > 0 else BASE_LOTS
                            state["running_capital"] = _cap
                            state["lots_today"]      = _lots
                            save_bot_state(_cap, _lots)
                            exited.append((iname, otype, pos["entry"], px_now, pnl))
                            pos_list.remove(pos)
                        state["positions_list"]   = pos_list
                        state["open_positions"]   = len(pos_list)
                        state["first_trade_done"] = True
                        save_positions(pos_list)
                        sync_background()
                        for iname, otype, entry, exit_px, pnl in exited:
                            icon = "WIN" if pnl > 0 else "LOSS"
                            tg_send(
                                f"MANUAL EXIT - {iname} {otype}\n"
                                f"Entry: {entry:.0f}  Exit: {exit_px:.0f}\n"
                                f"{icon}: Rs.{pnl:.0f}\n"
                                f"Capital: Rs.{state['running_capital']:.0f} | Next: {state['lots_today']}L"
                            )

                elif cmd.startswith("/lots"):
                    parts = cmd.split()
                    if len(parts) != 2 or not parts[1].isdigit():
                        tg_send("Usage: /lots 5\nExample: /lots 3 sets 3 lots for next trade.")
                    else:
                        new_lots = int(parts[1])
                        if new_lots < 1 or new_lots > MAX_LOTS:
                            tg_send(f"Lots must be between 1 and {MAX_LOTS}.")
                        else:
                            state["lots_today"] = new_lots
                            _cap = state.get("running_capital", float(PAPER_CAPITAL))
                            save_bot_state(_cap, new_lots)
                            units    = new_lots * UNITS_PER_LOT
                            rs_per_pt = units * DELTA
                            tg_send(
                                f"Lot size set to {new_lots}L ({units} units)\n"
                                f"Rs./point: Rs.{rs_per_pt:.0f}\n"
                                f"Max loss per trade: dynamic Rs.{SL_MIN}-{SL_MAX}\n"
                                f"Takes effect on next trade entry."
                            )

                elif cmd == "/stop":
                    state["paused"]        = True
                    state["pending_trade"] = None      # cancel any waiting gate
                    save_pending()
                    tg_send("Trading paused. Send /resume to re-enable.")

                elif cmd == "/resume":
                    state["paused"] = False
                    tg_send(
                        "Trading resumed.\n"
                        f"Trades used today: {state.get('trades_today', 0)}\n"
                        f"Mode: {'AUTO — signals taken automatically' if state.get('auto_mode', True) else 'MANUAL — /confirm needed after trade #1'}"
                    )

                elif cmd == "/auto":
                    state["auto_mode"] = True
                    save_bot_state(state.get("running_capital", PAPER_CAPITAL),
                                   state.get("lots_today", BASE_LOTS))
                    tg_send(
                        "AUTO MODE ON\n"
                        "Every signal that passes all filters (signal ladder + "
                        "15-min trend) is taken automatically — no /confirm.\n"
                        "Guards active: ATR stop-loss, staircase floors, "
                        f"10-min SL cooldown, daily loss limit Rs.{DAILY_LIMIT}.\n"
                        "Send /manual to switch back."
                    )

                elif cmd == "/manual":
                    state["auto_mode"] = False
                    save_bot_state(state.get("running_capital", PAPER_CAPITAL),
                                   state.get("lots_today", BASE_LOTS))
                    tg_send("MANUAL MODE — trades after #1 will ask for your /confirm again.")

                elif cmd == "/paper":
                    # (Sai 2026-07-27) switch to paper: bot tracks trades on the
                    # dashboard only, never touches a real order. Safe default.
                    state["trade_mode"] = "paper"
                    save_bot_state(state.get("running_capital", PAPER_CAPITAL),
                                   state.get("lots_today", BASE_LOTS))
                    tg_send("📝 <b>PAPER MODE</b>\nBot tracks trades on the dashboard only — NO real orders, no real money. This is the safe default.")

                elif cmd == "/real":
                    # Real orders ALSO require LIVE_ENABLED (code-level master
                    # lock) — /real alone can never place a real trade.
                    if not LIVE_ENABLED:
                        tg_send(
                            "🔒 <b>REAL MODE is LOCKED</b>\n"
                            "The bot will NOT place real orders — a safety lock in the code blocks it.\n"
                            "It unlocks only after ALL of:\n"
                            "1) static IP registered with Zerodha\n"
                            "2) a supervised 1-lot test\n"
                            "3) paper validation passed\n"
                            "Until then the bot stays PAPER no matter what you send. This protects your money."
                        )
                    elif time.time() - state.get("_real_confirm_ts", 0) < 60:
                        state["trade_mode"] = "real"
                        state["_real_confirm_ts"] = 0
                        save_bot_state(state.get("running_capital", PAPER_CAPITAL),
                                       state.get("lots_today", BASE_LOTS))
                        tg_send(
                            "🔴 <b>REAL MONEY MODE ON</b>\n"
                            "The bot will now place REAL orders on Zerodha.\n\n"
                            "⚠️ <b>FIRST real trade = your test.</b> The order code has never run "
                            "live. On the first trade:\n"
                            "• Keep it 1 lot\n"
                            "• Watch it appear + exit in your Kite app\n"
                            "• If anything looks wrong → send <b>/paper</b> AND close it by hand in Kite\n\n"
                            "Guards: daily stop -Rs.1,000 · MIS auto-closes by 15:20. Send /paper anytime to stop."
                        )
                    else:
                        state["_real_confirm_ts"] = time.time()
                        tg_send(
                            "⚠️ <b>Enable REAL MONEY trading?</b>\n"
                            "The bot will place REAL orders with REAL money.\n"
                            "Send <b>/real</b> AGAIN within 60 seconds to confirm — or /paper to cancel."
                        )

                elif cmd == "/funds" or cmd == "/balance":
                    # (Sai 2026-07-28) show REAL Zerodha wallet balance — read-only,
                    # never moves money. Used to check funds before going real.
                    try:
                        _k = get_kite()
                        if not _k:
                            tg_send("💰 Kite not connected — send /token first, then /funds.")
                        else:
                            _m = _k.margins("equity")
                            _av = _m.get("available", {}) or {}
                            _cash = _av.get("live_balance", _av.get("cash", 0)) or 0
                            _net  = _m.get("net", _cash)
                            tg_send(
                                "💰 <b>KITE WALLET — real account</b>\n"
                                + clean_box([
                                    ("Available", f"Rs.{_cash:,.0f}"),
                                    ("Net funds", f"Rs.{_net:,.0f}"),
                                ])
                                + "\nThis is your REAL Zerodha balance. The bot is not touching it "
                                  "unless you're in REAL mode (check /mode)."
                            )
                    except Exception as _e:
                        tg_send(f"Couldn't fetch funds: {_e}\nSend /token if Kite is disconnected.")

                elif cmd == "/mode":
                    _m   = state.get("trade_mode", "paper")
                    _eff = "REAL orders" if (LIVE_ENABLED and _m == "real") else "PAPER only — no real orders"
                    tg_send(
                        f"<b>Mode:</b> {_m.upper()}\n"
                        f"<b>Effective:</b> {_eff}\n"
                        f"<b>Master lock:</b> {'OPEN' if LIVE_ENABLED else 'LOCKED — real orders impossible'}"
                    )

                elif cmd == "/report":
                    # Day-by-day report from July 1: invested, lots, exact P&L
                    try:
                        conn = get_db()
                        rows = conn.execute(
                            "SELECT date, SUM(invested) AS inv, SUM(pnl) AS pnl, "
                            "GROUP_CONCAT(DISTINCT lots) AS lts, COUNT(*) AS n "
                            "FROM trades WHERE date >= '2026-07-01' "
                            "GROUP BY date ORDER BY date"
                        ).fetchall()
                        conn.close()
                        if not rows:
                            tg_send("No trades since July 1 yet.")
                        else:
                            lines = ["DAILY REPORT (from July 1)", ""]
                            tot_inv = tot_pnl = 0.0
                            for r in rows:
                                dt_   = datetime.strptime(r["date"], "%Y-%m-%d")
                                label = f"{dt_.strftime('%B')} {dt_.day}"
                                pnl   = r["pnl"] or 0
                                inv   = r["inv"] or 0
                                tot_inv += inv; tot_pnl += pnl
                                lts   = ",".join(f"{x}L" for x in str(r["lts"] or "").split(",") if x and x != "0") or "—"
                                res   = (f"Profit Rs.{pnl:,.2f}" if pnl > 0
                                         else f"Loss Rs.{abs(pnl):,.2f}" if pnl < 0 else "Flat Rs.0.00")
                                lines.append(f"{label} -> Invested: Rs.{inv:,.0f} | Lot size: {lts} | Result: {res}")
                            lines.append("")
                            lines.append(f"Total P&L: {'Profit' if tot_pnl >= 0 else 'Loss'} Rs.{abs(tot_pnl):,.2f}")
                            tg_send("\n".join(lines))
                    except Exception as ex:
                        tg_send(f"Report error: {ex}")

                elif cmd == "/confirm":
                    p = state.get("pending_trade")
                    if not p:
                        tg_send("Nothing to confirm right now. The bot will alert you when a gated trade signal appears.")
                    else:
                        state["trade_confirmed"] = True
                        save_pending()
                        waited = time.time() - p["ts"]
                        left   = max(0, CONFIRM_MIN_WAIT - waited)
                        eta    = "on the next check" if left <= 0 else f"in ~{int(left // 60) + 1} min"
                        tg_send(
                            f"Confirmed — Trade #{p['no']} ({p['otype']}) will enter {eta}.\n"
                            f"Confidence: {p['conf']}% | SL: Rs.{p['sl_rs']}"
                        )

                elif cmd.startswith("/token"):
                    # Daily Kite login from your PHONE — no PC needed:
                    # 1. open login URL  2. copy request_token  3. send /token XXX
                    parts = text.split()   # original case — tokens are case-sensitive
                    if len(parts) != 2:
                        tg_send(
                            "Daily Kite login (do this from your phone):\n\n"
                            "1. Open and log in:\n"
                            f"https://kite.zerodha.com/connect/login?api_key={API_KEY}&v=3\n\n"
                            "2. After login the address bar shows:\n"
                            "...request_token=XXXXXX&action=...\n"
                            "Copy the XXXXXX part.\n\n"
                            "3. Send here: /token XXXXXX"
                        )
                    else:
                        try:
                            _k   = KiteConnect(api_key=API_KEY)
                            data = _k.generate_session(parts[1].strip(), api_secret=API_SECRET)
                            with open(TOKEN_FILE, "w") as _f:
                                _f.write(data["access_token"])
                            # get_kite() auto-detects the new file — no restart needed
                            if get_kite():
                                tg_send("Kite connected! Exact exchange data is ON.\nNo restart needed — bot switched automatically.")
                            else:
                                tg_send("Token saved but connection check failed. Try /token again with a fresh request_token.")
                        except Exception as ex:
                            tg_send(
                                f"Token failed: {ex}\n\n"
                                "Note: each request_token works only ONCE and expires in a few minutes.\n"
                                "Get a fresh one:\n"
                                f"https://kite.zerodha.com/connect/login?api_key={API_KEY}&v=3"
                            )

                elif cmd in ("/help", "/start"):
                    tg_send(
                        "<b>Fluno Bot — Commands</b>\n\n"
                        "<b>Every morning</b>\n"
                        "/token   — daily Kite login (do this first, before 9:15)\n\n"
                        "<b>Check things</b>\n"
                        "/status  — NIFTY price, VIX, position\n"
                        "/signal  — today's breakout signal\n"
                        "/pnl     — today's trade result\n"
                        "/capital — running capital and lot size\n"
                        "/funds   — your REAL Zerodha wallet balance\n"
                        "/mode    — paper or real? + lock status\n"
                        "/history — last 7 trades\n"
                        "/report  — day-by-day results\n\n"
                        "<b>Paper vs Real money</b>\n"
                        "/paper   — trade PRETEND money (safe, default)\n"
                        "/real    — trade REAL money (locked until go-live)\n\n"
                        "<b>How it trades</b>\n"
                        "/auto    — take every filtered signal, no /confirm\n"
                        "/manual  — ask /confirm after trade #1\n"
                        "/confirm — approve a waiting trade\n"
                        "/lots 5  — set lot size\n\n"
                        "<b>Control</b>\n"
                        "/exit    — close open position NOW\n"
                        "/stop    — pause trading today\n"
                        "/resume  — resume trading\n"
                        "/help    — this list"
                    )

                else:
                    tg_send("Unknown command. Send /help for the full list.")

        except Exception:
            pass
        time.sleep(2)

# ── INDICATORS ────────────────────────────────────────────────────────────────
def rsi(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_g = gain.rolling(period).mean()
    avg_l = loss.rolling(period).mean()
    rs    = avg_g / avg_l.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def macd(series, fast=12, slow=26, signal=9):
    ema_f  = series.ewm(span=fast).mean()
    ema_s  = series.ewm(span=slow).mean()
    m_line = ema_f - ema_s
    s_line = m_line.ewm(span=signal).mean()
    return m_line, s_line

def supertrend(df, period=7, multiplier=3):
    """Returns a Series: 1 = bullish (price above band), -1 = bearish."""
    high  = df['high'].values.astype(float)
    low   = df['low'].values.astype(float)
    close = df['close'].values.astype(float)
    n     = len(df)

    # Wilder ATR
    tr  = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(high[i]-low[i], abs(high[i]-close[i-1]), abs(low[i]-close[i-1]))
    atr = np.zeros(n)
    for i in range(period, n):
        atr[i] = np.mean(tr[i-period+1:i+1]) if atr[i-1] == 0 else (atr[i-1]*(period-1)+tr[i])/period

    hl2     = (high + low) / 2
    basic_ub = hl2 + multiplier * atr
    basic_lb = hl2 - multiplier * atr

    final_ub  = basic_ub.copy()
    final_lb  = basic_lb.copy()
    direction = np.ones(n)

    for i in range(1, n):
        if atr[i] == 0:
            direction[i] = direction[i-1]; continue
        final_ub[i] = basic_ub[i] if basic_ub[i] < final_ub[i-1] or close[i-1] > final_ub[i-1] else final_ub[i-1]
        final_lb[i] = basic_lb[i] if basic_lb[i] > final_lb[i-1] or close[i-1] < final_lb[i-1] else final_lb[i-1]
        if direction[i-1] == 1:
            direction[i] = 1 if close[i] >= final_lb[i] else -1
        else:
            direction[i] = -1 if close[i] <= final_ub[i] else 1

    return pd.Series(direction, index=df.index)

def bull_confidence(row, prev):
    """Bullish score → BUY CE. Max 50 pts."""
    bd = {"rsi": 0, "macd": 0, "vol": 0, "sma": 0, "price": 0, "slope": 0}
    if row["rsi"] < 50:                          bd["rsi"]   = 15  # below midline
    if row["macd"] > row["macd_sig"]:            bd["macd"]  = 12  # MACD above signal
    if row["volume"] > row["vol_avg"] * 1.1:     bd["vol"]   = 5   # mild volume spike
    if row["sma20"] > row["sma50"]:              bd["sma"]   = 10  # short > medium trend
    if row["close"] > row["sma50"]:              bd["price"] = 5
    if row["sma20"] > prev["sma20"]:             bd["slope"] = 3
    return sum(bd.values()), bd

def bear_confidence(row, prev):
    """Bearish score → BUY PE. Max 50 pts."""
    bd = {"rsi": 0, "macd": 0, "vol": 0, "sma": 0, "price": 0, "slope": 0}
    if row["rsi"] > 50:                          bd["rsi"]   = 15  # above midline
    if row["macd"] < row["macd_sig"]:            bd["macd"]  = 12  # MACD below signal
    if row["volume"] > row["vol_avg"] * 1.1:     bd["vol"]   = 5   # mild volume spike
    if row["sma20"] < row["sma50"]:              bd["sma"]   = 10  # short < medium trend
    if row["close"] < row["sma50"]:              bd["price"] = 5
    if row["sma20"] < prev["sma20"]:             bd["slope"] = 3
    return sum(bd.values()), bd

# ── KITE LIVE DATA — exact exchange data, falls back to yfinance/NSE ─────────
_kite        = None
_kite_mtime  = -1     # token-file mtime at last connect attempt
_nfo_cache   = {"day": None, "rows": None}

def get_kite():
    """
    Return a connected KiteConnect client, or None (token missing/expired).
    Auto-reconnects the moment kite_token.txt changes (e.g. after the user
    sends /token on Telegram) — NO bot restart needed.
    """
    global _kite, _kite_mtime
    try:
        mtime = os.path.getmtime(TOKEN_FILE) if os.path.exists(TOKEN_FILE) else 0
    except Exception:
        mtime = 0
    if mtime == _kite_mtime:
        return _kite            # same token as last attempt — reuse result
    _kite_mtime = mtime
    _kite = None
    try:
        if not mtime or "your_api" in API_KEY:
            return None
        token = open(TOKEN_FILE).read().strip()
        k = KiteConnect(api_key=API_KEY)
        k.set_access_token(token)
        profile = k.profile()          # validates the token
        _kite = k
        bot_log(f"Kite connected: {profile['user_name']} — live exchange data ON", "ok")
        tg_send(f"Kite connected ({profile['user_name']}) — exact exchange data ON.")
        return _kite
    except Exception as e:
        bot_log(f"Kite offline ({e}) — using yfinance backup. Send /token on Telegram to fix.", "err")
        return None

def kite_ltp(full_symbol):
    """Live traded price via Kite, e.g. 'NSE:NIFTY 50' or 'NFO:NIFTY25JUL24100CE'."""
    k = get_kite()
    if not k:
        return None
    try:
        q = k.ltp([full_symbol])
        return round(float(q[full_symbol]["last_price"]), 2)
    except Exception:
        return None

def _norm_expiry(exp):
    """Normalise Kite expiry field (datetime / date / string) to a date."""
    if isinstance(exp, datetime):
        return exp.date()
    if isinstance(exp, date):
        return exp
    try:
        return datetime.strptime(str(exp)[:10], "%Y-%m-%d").date()
    except Exception:
        return None

def get_nfo_options():
    """All live NIFTY option contracts from Kite (cached for the day)."""
    k = get_kite()
    if not k:
        return None
    if _nfo_cache["day"] == date.today() and _nfo_cache["rows"] is not None:
        return _nfo_cache["rows"]
    try:
        rows = [r for r in k.instruments("NFO")
                if r.get("name") == "NIFTY" and r.get("segment") == "NFO-OPT"]
        _nfo_cache["day"]  = date.today()
        _nfo_cache["rows"] = rows
        bot_log(f"Kite: loaded {len(rows)} NIFTY option contracts", "info")
        return rows
    except Exception:
        return None

def find_option_contract(strike, option_type, expiry_index=0):
    """Find the real tradeable NIFTY contract on Zerodha for a strike + CE/PE."""
    rows = get_nfo_options()
    if not rows:
        return None
    try:
        today_d = date.today()
        match = []
        for r in rows:
            exp = _norm_expiry(r.get("expiry"))
            if exp and exp >= today_d and int(r.get("strike", 0)) == int(strike) \
               and r.get("instrument_type") == option_type:
                match.append((exp, r))
        if not match:
            return None
        match.sort(key=lambda x: x[0])
        idx = min(expiry_index, len(match) - 1)
        exp, r = match[idx]
        return {"tradingsymbol": r["tradingsymbol"], "expiry": exp,
                "lot_size": int(r.get("lot_size", 65))}
    except Exception:
        return None

def execute_order(action, strike, option_type, units, reason=""):
    """
    Order router. PAPER mode: no-op, returns 'PAPER' — zero behaviour change.
    LIVE mode (PAPER_TRADE=False): places a real MIS market order on Zerodha NFO.
    Every entry and exit in the bot goes through here, so flipping
    PAPER_TRADE to False is the ONLY change needed to go live.
    """
    # SAFETY GATE: a real order needs BOTH the code-level master lock
    # (LIVE_ENABLED) AND the runtime /real mode. Either one off → paper no-op.
    if not (LIVE_ENABLED and state.get("trade_mode", "paper") == "real"):
        return "PAPER"
    k = get_kite()
    if not k:
        bot_log(f"LIVE ORDER FAILED — Kite not connected ({action} {strike}{option_type})", "err")
        tg_send(f"ORDER FAILED — Kite not connected!\n{action} NIFTY {strike} {option_type} x{units}\nRun kite_setup.py and restart the bot.")
        return None
    c = find_option_contract(strike, option_type, trading_expiry_index())
    if not c:
        tg_send(f"ORDER FAILED — contract not found: NIFTY {strike} {option_type}")
        return None
    lot_sz = c["lot_size"]
    qty    = max(lot_sz, int(round(units / lot_sz)) * lot_sz)  # exchange lot multiple
    try:
        oid = k.place_order(
            variety=k.VARIETY_REGULAR, exchange="NFO",
            tradingsymbol=c["tradingsymbol"],
            transaction_type=k.TRANSACTION_TYPE_BUY if action == "BUY" else k.TRANSACTION_TYPE_SELL,
            quantity=qty, product=k.PRODUCT_MIS, order_type=k.ORDER_TYPE_MARKET)
        bot_log(f"LIVE ORDER {action} {c['tradingsymbol']} x{qty} id:{oid} {reason}", "ok")
        tg_send(f"LIVE ORDER PLACED\n{action} {c['tradingsymbol']} x{qty}\nOrder ID: {oid}\n{reason}")
        return oid
    except Exception as e:
        bot_log(f"LIVE ORDER ERROR {action} {strike}{option_type}: {e}", "err")
        tg_send(f"ORDER ERROR — {action} NIFTY {strike} {option_type}\n{e}")
        return None

def close_position_order(pos, reason):
    """SELL the real option contract when a position closes (no-op in paper mode)."""
    if pos.get("strike"):
        execute_order("SELL", pos["strike"], pos.get("option_type", "CE"),
                      pos.get("lot", LOT), reason=reason)

# ── MARKET DATA ───────────────────────────────────────────────────────────────
def fetch_candles(yf_sym="^NSEI"):
    """Fetch 60 days of 5-min candles for any symbol and compute all indicators."""
    df = yf.download(yf_sym, period="60d", interval="5m", progress=False)
    df = df.reset_index()
    # Flatten MultiIndex if present (newer yfinance versions)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0].lower().replace(" ", "") for c in df.columns]
    else:
        df.columns = [str(c).lower().replace(" ", "") for c in df.columns]
    # Normalise the time column name
    for col in list(df.columns):
        if col in ("datetime", "date", "timestamp", "index"):
            df = df.rename(columns={col: "datetime"}); break
    df = df[["datetime", "open", "high", "low", "close", "volume"]].copy()
    df["rsi"]     = rsi(df["close"])
    df["sma20"]   = df["close"].rolling(20).mean()
    df["sma50"]   = df["close"].rolling(50).mean()
    df["sma200"]  = df["close"].rolling(200).mean()
    m, s          = macd(df["close"])
    df["macd"]    = m
    df["macd_sig"]= s
    df["vol_avg"] = df["volume"].rolling(20).mean()
    df["st_dir"]  = supertrend(df)   # 1=bullish, -1=bearish
    _pc           = df["close"].shift(1)
    _tr           = pd.concat([df["high"] - df["low"],
                               (df["high"] - _pc).abs(),
                               (df["low"] - _pc).abs()], axis=1).max(axis=1)
    df["atr14"]   = _tr.rolling(14).mean()   # v4.5 ATR stop-loss basis
    # ADX(14) — "second opinion" trend-strength label (Sai 2026-07-24, idea
    # from his PSV forex bot, re-derived independently here). Wilder EWM form.
    _up, _dn      = df["high"].diff(), -df["low"].diff()
    _pdm          = ((_up > _dn) & (_up > 0)) * _up
    _mdm          = ((_dn > _up) & (_dn > 0)) * _dn
    _atr_w        = _tr.ewm(alpha=1/14, adjust=False).mean()
    _pdi          = 100 * (_pdm.ewm(alpha=1/14, adjust=False).mean() / _atr_w)
    _mdi          = 100 * (_mdm.ewm(alpha=1/14, adjust=False).mean() / _atr_w)
    _dx           = 100 * (_pdi - _mdi).abs() / (_pdi + _mdi).replace(0, np.nan)
    df["adx"]     = _dx.ewm(alpha=1/14, adjust=False).mean()
    return df.dropna().reset_index(drop=True)

def fetch_live_price(yf_sym="^NSEI"):
    # 1. Kite — exact real-time exchange tick (no delay)
    if yf_sym == "^NSEI":
        px = kite_ltp("NSE:NIFTY 50")
        if px:
            return px
    # 2. yfinance backup (1-2 min delayed)
    try:
        ticker = yf.Ticker(yf_sym)
        hist   = ticker.history(period="1d", interval="1m")
        if len(hist) > 0:
            return round(float(hist["Close"].iloc[-1]), 2)
    except Exception:
        pass
    return None

def fetch_vix():
    try:
        t = yf.Ticker("^INDIAVIX")
        h = t.history(period="1d", interval="1m")
        if len(h) > 0:
            return round(float(h["Close"].iloc[-1]), 2)
    except Exception:
        pass
    return None

def fetch_daily_hl(yf_sym="^NSEI"):
    """Return (yesterday_high, yesterday_low) from daily OHLC."""
    try:
        df = yf.download(yf_sym, period="5d", interval="1d", progress=False)
        df = df.reset_index()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0].lower() for c in df.columns]
        else:
            df.columns = [str(c).lower() for c in df.columns]
        tcol = next(c for c in df.columns if c in ("datetime","date","timestamp","index"))
        df[tcol] = pd.to_datetime(df[tcol]).dt.date
        prev = df[df[tcol] < date.today()]
        if len(prev) < 1:
            return None, None
        yd = prev.iloc[-1]
        return float(yd["high"]), float(yd["low"])
    except Exception:
        return None, None

def fetch_morning_direction(yf_sym="^NSEI"):
    """CE if first 5-min candle was UP (close >= open), PE if DOWN."""
    try:
        df = yf.download(yf_sym, period="1d", interval="5m", progress=False)
        df = df.reset_index()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0].lower() for c in df.columns]
        else:
            df.columns = [str(c).lower() for c in df.columns]
        if len(df) < 1:
            return "CE"
        first_open  = float(df.iloc[0]["open"])
        first_close = float(df.iloc[0]["close"])
        return "CE" if first_close >= first_open else "PE"
    except Exception:
        return "CE"

def capital_from_db():
    """GROUND TRUTH capital = starting capital + every settled trade in the DB.
    Added 2026-07-12: bot_state.json had drifted to Rs.19,274 while the DB said
    Rs.7,317 — the inflated file let the bot size 2 lots it couldn't afford
    (07-10 trades #68/#71). The DB is now the single source of truth."""
    try:
        conn = get_db()
        row  = conn.execute("SELECT SUM(pnl) FROM trades WHERE date >= ?",
                            (CAPITAL_RESET_DATE,)).fetchone()
        conn.close()
        total = float(row[0] or 0.0)
        return round(float(CAPITAL_RESET_VALUE) + total, 2)
    except Exception as e:
        bot_log(f"capital_from_db failed ({e}) — falling back to reset value", "err")
        return float(CAPITAL_RESET_VALUE)

def load_bot_state():
    """Load bot state. Capital ALWAYS comes from the DB (see capital_from_db);
    only auto_mode is trusted from the file. Lots derive from real capital."""
    if os.path.exists(BOT_STATE_FILE):
        try:
            with open(BOT_STATE_FILE) as f:
                s = json.load(f)
            state["auto_mode"] = bool(s.get("auto_mode", True))
            state["trade_mode"] = s.get("trade_mode", "paper")
        except Exception:
            pass
    capital = capital_from_db()
    lots    = min(MAX_LOTS, max(BASE_LOTS, int(capital // CAPITAL_PER_LOT)))
    return capital, lots

def save_bot_state(capital, lots):
    with open(BOT_STATE_FILE, "w") as f:
        json.dump({"running_capital": round(capital, 2), "lots_today": lots,
                   "auto_mode": bool(state.get("auto_mode", True)),
                   "trade_mode": state.get("trade_mode", "paper")}, f)

POSITIONS_FILE = "open_positions.json"

def save_positions(pos):
    with open(POSITIONS_FILE, "w") as f:
        json.dump(pos, f)

def load_positions():
    if os.path.exists(POSITIONS_FILE):
        try:
            with open(POSITIONS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return []

# ── Pending-trade persistence (added 2026-07-03) ──────────────────────────────
# A confirmed trade was lost to a mid-window restart on 03 Jul. Persist the
# awaiting-/confirm trade to disk so a restart can never eat it again.
PENDING_FILE = "pending_trade.json"

def save_pending():
    try:
        with open(PENDING_FILE, "w") as f:
            json.dump({"pending":   state.get("pending_trade"),
                       "confirmed": bool(state.get("trade_confirmed"))}, f)
    except Exception:
        pass

def load_pending():
    """Restore a pending trade after restart — only if still fresh."""
    try:
        if os.path.exists(PENDING_FILE):
            with open(PENDING_FILE) as f:
                d = json.load(f)
            p = d.get("pending")
            if p and time.time() - p.get("ts", 0) < CONFIRM_TIMEOUT:
                state["pending_trade"]   = p
                state["trade_confirmed"] = bool(d.get("confirmed"))
                bot_log(f"Restored pending trade #{p.get('no','?')} after restart"
                        f"{' (already confirmed)' if d.get('confirmed') else ''}", "info")
    except Exception:
        pass

# P&L booked outside the bot loop (Telegram /exit) — the loop drains this
# queue each cycle so manual exits count toward the daily total.
_pnl_adjust = []

def fetch_nse_optionchain(symbol="NIFTY"):
    """Fetch live option chain from NSE India (requires cookie init)."""
    try:
        s = req_lib.Session()
        h = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.nseindia.com",
        }
        s.get("https://www.nseindia.com", headers=h, timeout=8)
        url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol.upper()}"
        r   = s.get(url, headers=h, timeout=8)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None

def fetch_bse_optionchain():
    """Fetch SENSEX option chain from BSE India (best-effort)."""
    try:
        s = req_lib.Session()
        h = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.bseindia.com/",
        }
        # Get available expiry dates for SENSEX (scripcode=1 = SENSEX)
        exp_r = s.get("https://api.bseindia.com/BseIndiaAPI/api/DDLExpiryDate/w?flag=C&scripcode=1",
                       headers=h, timeout=8)
        if exp_r.status_code != 200:
            return None
        expiries = exp_r.json()
        if not expiries:
            return None
        exp_date = expiries[min(EXPIRY_INDEX, len(expiries)-1)].get("Val", "")
        chain_r = s.get(f"https://api.bseindia.com/BseIndiaAPI/api/OptionChain/w?scripcode=1&expirydt={exp_date}",
                         headers=h, timeout=8)
        if chain_r.status_code == 200:
            return {"bse": True, "data": chain_r.json(), "expiry": exp_date,
                    "expiries": [e.get("Val","") for e in expiries]}
    except Exception:
        pass
    return None

def calculate_oi_metrics(data, spot_price, expiry_index=0):
    """Compute PCR, Max Pain, ATM strikes from option chain JSON."""
    if not data or "records" not in data:
        return None
    try:
        records     = data["records"]["data"]
        all_exp     = data["records"].get("expiryDates", [])
        expiry      = all_exp[min(expiry_index, len(all_exp) - 1)] if all_exp else None
        if not expiry:
            return None
        ce_tot = pe_tot = 0
        rows = []
        for rec in records:
            if rec.get("expiryDate") != expiry:
                continue
            strike = rec.get("strikePrice", 0)
            ce = rec.get("CE", {}) or {}
            pe = rec.get("PE", {}) or {}
            c_oi = ce.get("openInterest", 0) or 0
            p_oi = pe.get("openInterest", 0) or 0
            ce_tot += c_oi; pe_tot += p_oi
            rows.append({
                "strike":    strike,
                "ce_oi":     c_oi,
                "ce_oi_chg": ce.get("changeinOpenInterest", 0) or 0,
                "ce_ltp":    ce.get("lastPrice", 0) or 0,
                "ce_iv":     ce.get("impliedVolatility", 0) or 0,
                "pe_oi":     p_oi,
                "pe_oi_chg": pe.get("changeinOpenInterest", 0) or 0,
                "pe_ltp":    pe.get("lastPrice", 0) or 0,
                "pe_iv":     pe.get("impliedVolatility", 0) or 0,
            })
        if not rows:
            return None
        pcr = round(pe_tot / ce_tot, 2) if ce_tot > 0 else 0
        # Max Pain: strike where combined options payoff is minimum
        max_pain, min_pain = None, float("inf")
        for r in rows:
            pain = sum(max(0, x["strike"]-r["strike"])*x["ce_oi"] + max(0, r["strike"]-x["strike"])*x["pe_oi"] for x in rows)
            if pain < min_pain:
                min_pain = pain; max_pain = r["strike"]
        # ATM ± 5 strikes
        atm = min(rows, key=lambda x: abs(x["strike"] - spot_price))
        idx = next(i for i, r in enumerate(rows) if r["strike"] == atm["strike"])
        return {
            "pcr":         pcr,
            "max_pain":    max_pain,
            "ce_oi_total": ce_tot,
            "pe_oi_total": pe_tot,
            "expiry":      expiry,
            "atm_strike":  atm["strike"],
            "strikes":     rows[max(0, idx-5):idx+6],
        }
    except Exception:
        return None

def analyze_setup(otype, signal, df=None):
    """
    Score the setup strength → (confidence %, dynamic SL in Rs. per lot).
    Confidence: RSI/MACD/volume/SMA scoring (max 50) + supertrend alignment (+10)
    + breakout signal (+10), normalised to a %.
    Dynamic SL (v4.5, 2026-07-08): 1.0 x ATR(14) of 5-min candles converted to
    rupees — the stop sits just outside normal candle noise instead of a fixed
    rupee band. Rs.200 floor, Rs.1000 HARD CAP, both per lot (do_entry scales
    by lots, like the staircase rungs).
    """
    conf, sl = 50, None
    try:
        if df is None:
            df = fetch_candles("^NSEI")
        row, prev = df.iloc[-1], df.iloc[-2]
        score, _  = bull_confidence(row, prev) if otype == "CE" else bear_confidence(row, prev)
        st_ok     = (row["st_dir"] == 1) if otype == "CE" else (row["st_dir"] == -1)
        total     = score + (10 if st_ok else 0) + (10 if signal.startswith("BREAK") else 0)
        conf      = max(5, min(95, round(total / 70 * 100)))
        atr = float(row.get("atr14") or 0)
        if atr > 0:
            sl = round(atr * SL_ATR_MULT * DELTA * UNITS_PER_LOT / 10) * 10
    except Exception:
        pass
    if sl is None:   # ATR unavailable → old conf-based formula as fallback
        sl = SL_MIN + int(round(conf / 100 * (SL_MAX - SL_MIN) / 50)) * 50
    sl = max(SL_MIN, min(SL_MAX, sl))     # never above Rs.1000/lot, ever
    return conf, sl

def second_opinion(adx_val):
    """ADX trend-strength label for Telegram — decision aid only, blocks
    nothing (see ADX_CHOP comment for the NIFTY calibration data)."""
    try:
        a = float(adx_val)
    except (TypeError, ValueError):
        return None
    if np.isnan(a):
        return None
    if a < ADX_CHOP:
        return f"⚠️ CHOPPY market (ADX {a:.0f}) — this type LOSES on average"
    return f"✅ TREND OK (ADX {a:.0f})"

def trend_still_strong(otype, df=None):
    """Is the trend still aligned with the position? Used to ride winners past Rs.1000."""
    try:
        if df is None:
            df = fetch_candles("^NSEI")
        row     = df.iloc[-1]
        st_ok   = (row["st_dir"] == 1) if otype == "CE" else (row["st_dir"] == -1)
        macd_ok = (row["macd"] > row["macd_sig"]) if otype == "CE" else (row["macd"] < row["macd_sig"])
        return bool(st_ok and macd_ok)
    except Exception:
        return False   # data failure → treat as weak → book profit (safe side)

def htf_alignment(otype, df=None):
    """
    15-min supertrend + MACD alignment as SEPARATE booleans (st_ok, macd_ok).
    Resamples 5-min candles to 15-min, uses the last COMPLETED 15-min candle.
    Data failure → (None, None).
    """
    try:
        if df is None:
            df = fetch_candles("^NSEI")
        r = df.set_index("datetime")
        f15 = pd.DataFrame({
            "open":   r["open"].resample("15min").first(),
            "high":   r["high"].resample("15min").max(),
            "low":    r["low"].resample("15min").min(),
            "close":  r["close"].resample("15min").last(),
        }).dropna().reset_index()
        if len(f15) < 20:
            return None, None
        # drop the last 15m candle if it hasn't fully closed yet
        last_5m_end  = df["datetime"].iloc[-1] + pd.Timedelta(minutes=5)
        last_15m_end = f15["datetime"].iloc[-1] + pd.Timedelta(minutes=15)
        if last_5m_end < last_15m_end:
            f15 = f15.iloc[:-1]
        f15["st_dir"] = supertrend(f15)
        m15, s15 = macd(f15["close"])
        row = f15.iloc[-1]
        macd_up = m15.iloc[-1] > s15.iloc[-1]
        if otype == "CE":
            return bool(row["st_dir"] == 1), bool(macd_up)
        return bool(row["st_dir"] == -1), bool(not macd_up)
    except Exception:
        return None, None

def htf_trend_ok(otype, df=None):
    """
    Multi-timeframe check (v4.1, backtested 2026-07-03: +Rs.1,745 vs v4 alone).
    The last COMPLETED 15-min candle must agree with the trade direction:
    supertrend AND MACD aligned. CE needs 15m supertrend bullish + MACD above
    signal; PE the mirror. Data failure → False (safe side).
    """
    st_ok, macd_ok = htf_alignment(otype, df)
    return bool(st_ok and macd_ok)

def confidence_score9(otype, level, px, df=None):
    """
    9-point Confidence Score (Sai's design 2026-07-14, validated in
    backtest_confidence.py — must stay identical to that script's conf_score):
      1. 5m supertrend aligned                       (1)
      2. 5m MACD aligned                             (1)
      3. 15m supertrend aligned                      (1)
      4. 15m MACD aligned                            (1)
      5. RSI room (CE<50 / PE>50 = 1, within 10 = 0.5)
      6. Volume (>1.5x avg = 1, >1.1x = 0.5)
      7. SMA20 vs SMA50 aligned                      (1)
      8. Price vs SMA50 aligned                      (1)
      9. Breakout strength (>=0.05% past level = 1, else 0.5)
    Returns (score 0-9 in 0.5 steps, breakdown dict). Failure → (0.0, {}).
    """
    bd = {}
    try:
        if df is None:
            df = fetch_candles("^NSEI")
        row  = df.iloc[-1]
        bull = otype == "CE"
        bd["5m supertrend"] = 1.0 if (row["st_dir"] == 1) == bull else 0.0
        bd["5m MACD"]       = 1.0 if (row["macd"] > row["macd_sig"]) == bull else 0.0
        st15, macd15 = htf_alignment(otype, df)
        bd["15m supertrend"] = 1.0 if st15 else 0.0
        bd["15m MACD"]       = 1.0 if macd15 else 0.0
        rv = float(row["rsi"])
        if not np.isnan(rv) and ((rv < 50) if bull else (rv > 50)):
            bd["RSI room"] = 1.0
        elif not np.isnan(rv) and ((rv < 60) if bull else (rv > 40)):
            bd["RSI room"] = 0.5
        else:
            bd["RSI room"] = 0.0
        if row["volume"] > row["vol_avg"] * 1.5:
            bd["volume"] = 1.0
        elif row["volume"] > row["vol_avg"] * 1.1:
            bd["volume"] = 0.5
        else:
            bd["volume"] = 0.0
        bd["SMA trend"]      = 1.0 if (row["sma20"] > row["sma50"]) == bull else 0.0
        bd["price vs SMA50"] = 1.0 if (row["close"] > row["sma50"]) == bull else 0.0
        margin = abs(px - level) / px if (level and px) else 0.0
        bd["breakout strength"] = 1.0 if margin >= 0.0005 else 0.5
        return float(sum(bd.values())), bd
    except Exception:
        return 0.0, {}

def staircase_floor(peak_pnl, units):
    """
    v4.3 profit staircase (Sai's design 2026-07-06; rungs 250+450 added
    2026-07-08, also Sai — backtest_rungs.py: +Rs.51,131 vs +Rs.46,757 over
    60d on the live ATR-SL config). Rungs 150/250/300/450/500/700/850/900
    then +150 forever. Once peak crosses a rung, that rung is the
    guaranteed floor — fall below it, book exactly it.
    Rungs are defined at 1 lot (65 units) and SCALE with position size so
    each step stays a real price move at any lot count.
    Returns the current floor in rupees, or None if below the first rung.
    """
    scale = max(1.0, units / 65.0)
    p = peak_pnl / scale
    floor = None
    for r in (150, 250, 300, 450, 500, 700, 850, 900):
        if p >= r:
            floor = r
        else:
            break
    if floor == 900 and p >= 1050:
        floor = 900 + 150 * int((p - 900) // 150)
    return floor * scale if floor else None

def count_trades_today():
    """Closed trades so far today (open position is counted via MAX_POSITIONS)."""
    try:
        conn = get_db()
        row  = conn.execute("SELECT COUNT(*) FROM trades WHERE date=?",
                            (date.today().strftime("%Y-%m-%d"),)).fetchone()
        conn.close()
        return row[0]
    except Exception:
        return 0

def get_streak():
    """Positive = win streak, negative = loss streak."""
    try:
        conn = get_db()
        rows = conn.execute("SELECT pnl FROM trades ORDER BY id DESC LIMIT 20").fetchall()
        conn.close()
        if not rows:
            return 0
        streak  = 0
        is_win  = rows[0]["pnl"] > 0
        for r in rows:
            if (r["pnl"] > 0) == is_win:
                streak += 1
            else:
                break
        return streak if is_win else -streak
    except Exception:
        return 0

def is_market_open():
    # Loop runs until 15:35 so the 15:25 force-close and 15:30 settlement
    # actually fire (entries are separately capped at 12:30).
    now    = datetime.now()
    if now.weekday() >= 5:
        return False
    open_t  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=35, second=0, microsecond=0)
    return open_t <= now <= close_t

# ── BOT LOG ───────────────────────────────────────────────────────────────────
def bot_log(msg, cls=""):
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "msg": msg, "cls": cls}
    state["log"].insert(0, entry)
    state["log"] = state["log"][:50]   # keep last 50 lines
    print(f"[{entry['time']}] {msg}")

# ── VERCEL SYNC ──────────────────────────────────────────────────────────────
STRIKE_STEP = {"NIFTY": 50, "BANKNIFTY": 100, "SENSEX": 100}

def get_atm_strike(price, inst_name):
    step = STRIKE_STEP.get(inst_name, 50)
    return int(round(price / step) * step)

def get_target_strike(price, inst_name, option_type):
    """1 strike OTM from ATM → delta ~0.40."""
    step = STRIKE_STEP.get(inst_name, 50)
    atm  = get_atm_strike(price, inst_name)
    return atm + step if option_type == "CE" else atm - step

def fetch_option_premium(inst_name, strike, option_type, spot_price):
    """
    Premium priority: 1. Kite live traded price (exact) → 2. NSE option chain →
    3. IV-based estimate so the bot always gets a number.
    """
    import math
    # 1. Kite — exact last traded premium of the real contract
    if inst_name == "NIFTY":
        c = find_option_contract(strike, option_type, trading_expiry_index())
        if c:
            ltp = kite_ltp(f"NFO:{c['tradingsymbol']}")
            if ltp and ltp > 0:
                return ltp
    # 2. NSE option chain
    try:
        if inst_name in ["NIFTY", "BANKNIFTY"]:
            oc = fetch_nse_optionchain(inst_name)
            if oc:
                for row in oc.get("records", {}).get("data", []):
                    if row.get("strikePrice") == strike:
                        ltp = row.get(option_type, {}).get("lastPrice", 0)
                        if ltp > 0:
                            return round(ltp, 1)
    except Exception:
        pass
    # Fallback: rough VIX-based premium estimate using actual spot_price
    vix   = state.get("vix") or 15
    iv    = vix / 100
    t     = 4 / 365                         # ~4 trading days to expiry
    atm_p = spot_price * iv * math.sqrt(t) * 0.4
    mono  = abs(strike - spot_price) / spot_price
    disc  = max(0.25, 1 - mono * 8)
    return round(atm_p * disc, 1)

def fetch_live_premium_real(inst_name, strike, option_type):
    """
    REAL traded premium only (Kite exchange tick, NSE chain as backup).
    Returns None if no real quote is available — NEVER estimates.
    Used for premium-based P&L so theta decay is measured truthfully.
    """
    if not strike:
        return None
    if inst_name == "NIFTY":
        c = find_option_contract(strike, option_type, trading_expiry_index())
        if c:
            ltp = kite_ltp(f"NFO:{c['tradingsymbol']}")
            if ltp and ltp > 0:
                return ltp
    try:
        if inst_name in ["NIFTY", "BANKNIFTY"]:
            oc = fetch_nse_optionchain(inst_name)
            if oc:
                for row_ in oc.get("records", {}).get("data", []):
                    if row_.get("strikePrice") == strike:
                        ltp = row_.get(option_type, {}).get("lastPrice", 0)
                        if ltp and ltp > 0:
                            return round(ltp, 1)
    except Exception:
        pass
    return None

def get_next_expiry(inst_name, index=0):
    """Nearest expiry — real contract dates from Kite when connected, weekday calc as backup."""
    if inst_name == "NIFTY":
        rows = get_nfo_options()
        if rows:
            today_d = date.today()
            exps = sorted({e for e in (_norm_expiry(r.get("expiry")) for r in rows)
                           if e and e >= today_d})
            if exps:
                return exps[min(index, len(exps) - 1)].strftime("%d %b %Y")
    weekday_map = {"NIFTY": 1, "BANKNIFTY": 1, "SENSEX": 3}  # Mon=0 … Sun=6 (NIFTY Tue)
    target_wd   = weekday_map.get(inst_name, 0)
    today       = date.today()
    days_ahead  = (target_wd - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7   # today is expiry day → use next week
    first_exp   = today + timedelta(days=days_ahead)
    expiry      = first_exp + timedelta(weeks=index)
    return expiry.strftime("%d %b %Y")   # e.g. "06 Jul 2026"

EXPIRY_WEEKDAY = {"NIFTY": 1, "BANKNIFTY": 1, "SENSEX": 3}  # NIFTY/BankN Tue, Sensex Thu
                                                            # (Kite real dates used when connected)

def is_expiry_day(inst_name):
    """True if today is weekly expiry — real contract dates from Kite when connected."""
    if inst_name == "NIFTY":
        rows = get_nfo_options()
        if rows:
            today_d = date.today()
            return any(_norm_expiry(r.get("expiry")) == today_d for r in rows)
    return date.today().weekday() == EXPIRY_WEEKDAY.get(inst_name, -1)

def trading_expiry_index():
    """
    v4.4: on expiry day, trade NEXT week's contract instead of the dying one
    (expiry-day gamma/decay makes the expiring contract unsafe; the index
    signal itself is fine). All other days: nearest weekly as usual.
    """
    try:
        if is_expiry_day("NIFTY"):
            return EXPIRY_INDEX + 1
    except Exception:
        pass
    return EXPIRY_INDEX

_sync_lock = threading.Lock()

def sync_to_vercel():
    """Write trades.json and push to GitHub → Vercel redeploys in ~15 seconds."""
    if not _sync_lock.acquire(blocking=False):
        return  # another sync already running, skip
    try:
        conn = get_db()
        rows = [dict(r) for r in conn.execute("SELECT * FROM trades ORDER BY id").fetchall()]
        conn.close()

        wins   = [t for t in rows if t["pnl"] > 0]
        losses = [t for t in rows if t["pnl"] <= 0]
        total  = sum(t["pnl"] for t in rows)

        daily = {}
        for t in rows:
            daily[t["date"]] = round(daily.get(t["date"], 0) + t["pnl"], 0)

        payload = {
            "last_updated":       datetime.now().strftime("%Y-%m-%d %H:%M"),
            "summary": {
                "total_trades": len(rows),
                "wins":         len(wins),
                "losses":       len(losses),
                "win_rate":     round(len(wins) / len(rows) * 100, 1) if rows else 0,
                "total_pnl":    round(total, 0),
                "avg_win":      round(sum(t["pnl"] for t in wins)   / len(wins),   0) if wins   else 0,
                "avg_loss":     round(sum(t["pnl"] for t in losses) / len(losses), 0) if losses else 0,
            },
            "trades":             rows,
            "daily_pnl":          daily,
            "current_score":      state["score"],
            "score_breakdown":    state["score_breakdown"],
            "open_positions":     state["open_positions"],
            "bot_log":            state["log"][:20],
            "vix":                state.get("vix"),
            "supertrend_bullish": state.get("supertrend_bullish"),
            "oi_nifty":           state.get("oi_nifty"),
            # extra fields for the phone dashboard (Sai 2026-07-27)
            "running_capital":    round(float(state.get("running_capital") or 0), 0),
            "trade_mode":         state.get("trade_mode", "paper"),
            "nifty_price":        state.get("nifty_price"),
            "market_open":        state.get("market_open"),
            "validation":         load_validation(),
        }
        # live open position + profit-staircase detail for the website (Sai 2026-07-28)
        _pl = state.get("positions_list") or []
        if _pl:
            _p = _pl[0]
            _u = _p.get("lot", UNITS_PER_LOT) or UNITS_PER_LOT
            _pk = _p.get("peak_pnl", 0); _pk = _pk if _pk and _pk > -9000 else 0
            _slp = abs(_p.get("sl_rs", 0)) / (DELTA * _u) if _u else 0
            _entry = _p.get("entry", 0) or 0
            _ot = _p.get("option_type", "CE")
            payload["position"] = {
                "otype": _ot, "strike": _p.get("strike"),
                "entry": round(_entry, 0),
                "premium_entry": _p.get("premium_entry"),
                "stop_level": round((_entry - _slp) if _ot == "CE" else (_entry + _slp), 0),
                "sl_rs": _p.get("sl_rs"),
                "lots": max(1, int(round(_u / UNITS_PER_LOT))),
                "pnl": round(state.get("unrealized_pnl", 0), 0),
                "peak": round(_pk, 0),
                "floor": staircase_floor(_pk, _u),
            }
        else:
            payload["position"] = None
        payload["rung_base"] = [150, 250, 300, 450, 500, 700, 850, 900]

        web_json = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "trades.json")
        with open(web_json, "w") as f:
            json.dump(payload, f, indent=2, default=str)

        proj = os.path.dirname(os.path.abspath(__file__))
        git  = r"C:\Program Files\Git\bin\git.exe" if os.name == "nt" else "git"
        subprocess.run([git, "add", "web/trades.json"], cwd=proj, capture_output=True)
        r = subprocess.run([git, "commit", "-m", f"bot: sync {datetime.now().strftime('%H:%M')}"], cwd=proj, capture_output=True)
        if b"nothing to commit" in r.stdout:
            return  # no change, skip push
        push = subprocess.run([git, "push", "origin", "main"], cwd=proj, capture_output=True)
        if push.returncode == 0:
            bot_log("Synced to Vercel — live in ~15s", "ok")
        else:
            bot_log(f"Vercel sync failed: {push.stderr.decode()[:80]}", "err")
    except Exception as e:
        bot_log(f"Vercel sync error: {e}", "err")
    finally:
        _sync_lock.release()

_last_vercel_sync = 0
def sync_background():
    # Throttle to at most one Vercel push per 2 minutes (Sai 2026-07-28).
    # The bot's loop calls this every ~60s; without the throttle it pushed
    # ~1/min = ~360 deploys/day, blowing past Vercel's free daily deploy
    # limit (which froze the dashboard). 2-min cadence stays well under it.
    global _last_vercel_sync
    if time.time() - _last_vercel_sync < 120:
        return
    _last_vercel_sync = time.time()
    threading.Thread(target=sync_to_vercel, daemon=True).start()

# ── VALIDATION TRACKER (2026-07-22): once-a-day real-money trust check, cached
#    for the dashboard and Telegramed each weekday evening. compute() is heavy
#    (fetches yfinance + runs the backtest) — only ever call it from a thread,
#    never the trading loop.
VALIDATION_FILE = "validation_state.json"

def load_validation():
    try:
        with open(VALIDATION_FILE) as f:
            return json.load(f)
    except Exception:
        return None

def run_validation_now(send_telegram=False):
    try:
        res = vt.compute()
        slim = {k: v for k, v in res.items() if k not in ("live_day", "day_cnt", "bt")}
        with open(VALIDATION_FILE, "w") as f:
            json.dump(slim, f)
        if send_telegram:
            tg_send(vt.telegram_text(res))
        bot_log(f"Validation: trust {res.get('trust')}/10 (n={res.get('n')})", "info")
        return res
    except Exception as e:
        bot_log(f"Validation run failed: {e}", "err")
        return None

DAILY_FLAGS_FILE = "daily_flags.json"
def _flag_done_today(name):
    """True if 'name' already happened today — PERSISTS across restarts so a
    restart never re-sends a once-a-day Telegram (Sai 2026-07-28: restarts
    were re-spamming the validation + manual-mode messages)."""
    try:
        d = json.load(open(DAILY_FLAGS_FILE)) if os.path.exists(DAILY_FLAGS_FILE) else {}
    except Exception:
        d = {}
    return d.get(name) == date.today().isoformat()

def _flag_set_today(name):
    try:
        d = json.load(open(DAILY_FLAGS_FILE)) if os.path.exists(DAILY_FLAGS_FILE) else {}
    except Exception:
        d = {}
    d[name] = date.today().isoformat()
    try:
        json.dump(d, open(DAILY_FLAGS_FILE, "w"))
    except Exception:
        pass

def validation_thread():
    """One silent refresh ~30s after startup (dashboard number only), then a
    Telegram summary ONCE each weekday after market close (>=16:00, before
    Sai's 5pm cutoff), persisted so restarts don't re-send it."""
    time.sleep(30)
    run_validation_now(send_telegram=False)
    while True:
        try:
            now = datetime.now()
            if now.weekday() < 5 and now.hour >= 16 and not _flag_done_today("validation_sent"):
                _flag_set_today("validation_sent")
                run_validation_now(send_telegram=True)
        except Exception as e:
            bot_log(f"validation_thread error: {e}", "err")
        time.sleep(1800)   # re-check every 30 min

# ── BOT THREAD ────────────────────────────────────────────────────────────────
def bot_loop():
    init_db()

    # Connect to Kite — live exchange data for spot + option premiums
    if get_kite() is None:
        tg_send(
            "Kite not connected — bot is on backup data (yfinance, 1-2 min delay).\n"
            "Fix it from your phone in 30 seconds: send /token for the steps.\n"
            "(Kite tokens expire every morning — this is normal, no restart needed.)"
        )

    # Load dynamic lot state (persists across restarts).
    # MUST happen BEFORE the startup banner: until this runs, state has no
    # "auto_mode" key, so state.get("auto_mode", True) fell back to True and the
    # banner ALWAYS printed "AUTO mode" — even when the bot was really in manual
    # mode. That misreporting hid a manual-mode bot for days (Sai 2026-07-20).
    running_capital, lots_today = load_bot_state()
    state["running_capital"] = running_capital
    state["lots_today"]      = lots_today

    mode_label = "PAPER TRADE" if PAPER_TRADE else "LIVE TRADE"
    _auto_label = "AUTO mode" if state.get("auto_mode") else "MANUAL /confirm mode — send /auto for hands-free"
    bot_log(f"Bot started | {mode_label} | {_auto_label} | Strategy: BREAKOUT v5.1 (ATR stop + staircase + 0.15% fade + expiry-day next-week + window 10:15-15:00 + AI council display) | SL: {SL_ATR_MULT}xATR, Rs.{SL_MIN}-{SL_MAX}/lot | Daily stop: Rs.{DAILY_LIMIT}",
            "info" if state.get("auto_mode") else "err")
    bot_log(f"Capital: Rs.{running_capital:.0f} | Lots: {lots_today}L ({lots_today*UNITS_PER_LOT} units)", "info")
    set_telegram_commands()   # populate the Telegram "/" command menu
    if not state.get("auto_mode") and is_market_open() and not _flag_done_today("manual_warned"):
        # Manual mode silently skips trades after #1 whose /confirm goes
        # unanswered — warn ONCE per day, and only during market hours so
        # evening/weekend restarts don't spam it (Sai 2026-07-28).
        _flag_set_today("manual_warned")
        tg_send(
            "HEADS UP — bot is in MANUAL mode.\n"
            "Only trade #1 is automatic. Every later signal waits for your "
            "/confirm and is SKIPPED if you don't reply within 10 minutes.\n\n"
            "Send /auto if you want the bot to take all filtered signals itself."
        )

    # Restore open positions from disk (survives restarts)
    positions  = load_positions()
    load_pending()   # restore an awaiting-/confirm trade too (2026-07-03 fix)
    # Backfill strike + premium for positions that predate the strike selector
    _changed = False
    for _p in positions:
        if not _p.get("strike"):
            _p["strike"] = get_target_strike(_p["entry"], _p.get("instrument","NIFTY"), _p.get("option_type","CE"))
            _p["premium_entry"] = fetch_option_premium(_p["instrument"], _p["strike"], _p["option_type"], _p["entry"])
            _changed = True
    if _changed:
        save_positions(positions)
    # DB-backed count: survives restarts even if SL fired and position was removed
    _tc = count_trades_today()
    state["first_trade_done"] = _tc >= MAX_TRADES_PER_DAY
    if positions:
        bot_log(f"Restored {len(positions)} open position(s) from disk.", "info")
    elif _tc:
        bot_log(f"{_tc} trade(s) done today (from DB) — {max(0, MAX_TRADES_PER_DAY - _tc)} gated slot(s) left.", "info")
    # Reload today's booked P&L from the DB so a restart never shows Rs.0
    # on a day that already has trades (bug found 2026-07-06).
    daily_pnl = 0.0
    try:
        _conn = get_db()
        _row  = _conn.execute("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE date=?",
                              (date.today().strftime("%Y-%m-%d"),)).fetchone()
        _conn.close()
        daily_pnl = float(_row[0] or 0.0)
        if daily_pnl:
            bot_log(f"Restored today's booked P&L from DB: Rs.{daily_pnl:+,.1f}", "info")
    except Exception:
        pass
    today            = date.today()
    last_sync        = 0
    sl_cooldown      = {}   # {instrument: timestamp} — 10-min cooldown after SL hit
    eod_done         = False  # settlement report fires once at 3:30 PM
    morning_pinged   = False  # morning Telegram ping fires once at ~9:10 AM
    weekly_done      = False  # weekly summary fires once on Friday
    # first_trade_done is set after load_positions() — do NOT reset it here

    # Per-instrument candle cache (keyed by yfinance symbol)
    _candle_cache = {}
    def get_candles(yf_sym):
        if yf_sym not in _candle_cache:
            _candle_cache[yf_sym] = {"ts": 0, "df": None}
        c = _candle_cache[yf_sym]
        if time.time() - c["ts"] > 270 or c["df"] is None:
            bot_log(f"Fetching 5-min candles [{yf_sym}]...", "info")
            c["df"] = fetch_candles(yf_sym)
            c["ts"] = time.time()
        return c["df"]

    while True:
        # Sync from state so Telegram /stop, /resume, /lots, /exit take effect
        running_capital  = state.get("running_capital", running_capital)
        lots_today       = state.get("lots_today", lots_today)
        while _pnl_adjust:                      # P&L booked via Telegram /exit
            daily_pnl += _pnl_adjust.pop(0)

        if date.today() != today:
            daily_pnl        = 0.0
            today            = date.today()
            positions        = []
            save_positions(positions)          # clear stale file — restart-safe
            sl_cooldown      = {}
            state["first_trade_done"] = False
            state["paused"]          = False
            state["pending_trade"]   = None
            state["trade_confirmed"] = False
            save_pending()
            state["gate_cooldown"]   = 0
            state["signal"]      = "--"
            state["option_type"] = "—"
            eod_done       = False
            morning_pinged = False
            weekly_done    = False
            bot_log(f"New trading day | Capital: Rs.{running_capital:.0f} | Lots: {lots_today}L", "info")
            tg_send(f"New trading day started.\nCapital: Rs.{running_capital:.0f} | Lots: {lots_today}L\nWatching NIFTY breakout.")

        market_open = is_market_open()
        state["market_open"]    = market_open
        state["open_positions"] = len(positions)
        state["daily_pnl"]      = round(daily_pnl, 0)

        # ── Morning ping at 9:10 AM (market opens in 5 min) ─────────────────
        _now = datetime.now()
        if not morning_pinged and _now.weekday() < 5 and _now.hour == 9 and _now.minute >= 10:
            morning_pinged = True
            try:
                yd_h, yd_l = fetch_daily_hl("^NSEI")
                lines = ["Good morning! Market opens in 5 minutes."]
                if yd_h and yd_l:
                    lines.append(f"\nNIFTY Reference:")
                    lines.append(f"  Yday High : {yd_h:.0f}")
                    lines.append(f"  Yday Low  : {yd_l:.0f}")
                lines.append(f"\nCapital : Rs.{running_capital:.0f} | Lots: {lots_today}L ({lots_today*UNITS_PER_LOT} units)")
                lines.append(f"SL: dynamic Rs.{SL_MIN}-{SL_MAX} | BE Lock at Rs.{BREAKEVEN_LOCK_FLOOR}")
                lines.append(f"Trades: unlimited until daily loss limit Rs.{DAILY_LIMIT} — #1 auto, every next one needs your /confirm")
                lines.append(f"\nPrediction at 10:15 AM:")
                if yd_h and yd_l:
                    lines.append(f"  BULLISH (BUY CE) — if NIFTY > {yd_h:.0f}")
                    lines.append(f"  BEARISH (BUY PE) — if NIFTY < {yd_l:.0f}")
                    lines.append(f"  MORNING DIR      — if inside range")
                if get_kite() is None:
                    lines.append("\nKite login needed for exact data — send /token for steps.")
                tg_send("\n".join(lines))
                bot_log("Morning Telegram ping sent", "info")
            except Exception as _me:
                bot_log(f"Morning ping error: {_me}", "err")

        if not market_open:
            time.sleep(60)
            continue

        if daily_pnl <= DAILY_LIMIT and not positions:
            # fix 2026-07-12: only idle when NO position is open. Before, this
            # `continue` also skipped SL/staircase management of an open trade,
            # leaving it unprotected for the rest of the day.
            if state.get("pending_trade"):
                state["pending_trade"] = None
                save_pending()
                tg_send("Pending trade cancelled — daily loss limit reached.")
            bot_log(f"Daily loss limit hit (Rs.{daily_pnl:.0f}). Stopped for today.", "err")
            time.sleep(300)
            continue

        try:
            # ── 1. Fetch live prices for all instruments ──────────────────────
            inst_prices = {}
            for inst in INSTRUMENTS:
                p = fetch_live_price(inst["yf"])
                if p:
                    inst_prices[inst["name"]] = p
            if not inst_prices:
                bot_log("Could not fetch any live prices. Retrying...", "err")
                time.sleep(60)
                continue
            state["nifty_price"] = inst_prices.get("NIFTY", "--")

            # ── 2. EOD exit at 3:25 PM + settlement at 3:30 PM ──────────────
            now_t     = datetime.now()
            eod_exit  = now_t.replace(hour=15, minute=25, second=0, microsecond=0)
            eod_settle= now_t.replace(hour=15, minute=30, second=0, microsecond=0)

            # 3:25 PM — force-close any open position before market shuts
            if positions and now_t >= eod_exit:
                for pos in positions:
                    iname = pos.get("instrument", "NIFTY")
                    px    = inst_prices.get(iname, pos["entry"])
                    lot   = pos.get("lot", LOT); delta = pos.get("delta", DELTA)
                    otype = pos.get("option_type", "CE")
                    real_p = fetch_live_premium_real(iname, pos.get("strike"), otype)
                    if real_p and pos.get("premium_entry"):
                        pnl = round((real_p - pos["premium_entry"]) * lot - BROKERAGE, 2)
                    else:
                        move = (px - pos["entry"]) * delta * lot if otype == "CE" \
                               else (pos["entry"] - px) * delta * lot
                        pnl  = round(move - BROKERAGE, 2)
                    net, chg, gross = settle_trade(pos, pnl)
                    pnl = net
                    close_position_order(pos, "EOD_EXIT")
                    _inv, _lts = trade_meta(pos)
                    save_trade(pos["score"], pos["entry"], px, net, "EOD_EXIT", iname, otype, _inv, _lts,
                               gross=gross, charges=chg)
                    daily_pnl       += net
                    running_capital += net
                    if pnl > 0:
                        lots_today = min(MAX_LOTS, max(BASE_LOTS, int(running_capital // CAPITAL_PER_LOT)))
                    else:
                        lots_today = BASE_LOTS
                    save_bot_state(running_capital, lots_today)
                    state["running_capital"] = running_capital
                    state["lots_today"]      = lots_today
                    bot_log(f"EOD EXIT {iname} {otype} | Entry:{pos['entry']:.0f} Exit:{px:.0f} P&L:Rs.{pnl:.0f}", "info")
                    tg_send(
                        f"⏰ <b>MARKET CLOSING — sold before close</b>\n"
                        + clean_box([
                            ("Trade",   f"{iname} {plain_opt(otype)}"),
                            ("Result",  f"Rs.{pnl:+.0f}"),
                            ("Capital", f"Rs.{running_capital:.0f}"),
                        ])
                        + f"\n👉 <b>YOU:</b> SELL your {iname} {pos.get('strike','')} {plain_opt(otype)} now — never hold overnight."
                    )
                positions.clear(); save_positions(positions); sync_background()

            # 3:30 PM — Zerodha-style settlement report (fires once per day)
            if now_t >= eod_settle and not eod_done:
                eod_done = True
                today_str = date.today().strftime("%Y-%m-%d")
                conn  = get_db()
                rows  = conn.execute(
                    "SELECT instrument,option_type,entry,exit,pnl,status,time,invested,lots FROM trades WHERE date=? ORDER BY id",
                    (today_str,)
                ).fetchall()
                conn.close()

                total_pnl = sum(r["pnl"] for r in rows)
                wins      = [r for r in rows if r["pnl"] > 0]
                losses    = [r for r in rows if r["pnl"] <= 0]
                result    = "PROFIT" if total_pnl > 0 else "LOSS" if total_pnl < 0 else "FLAT"
                tot_chg   = sum((r["charges"] or 0) for r in rows)
                tot_gross = sum((r["gross"] if r["gross"] is not None else r["pnl"]) for r in rows)
                tax_prov  = round(max(0.0, total_pnl) * 0.30, 2)   # slab provision est.

                _res_emoji = "🟢" if total_pnl > 0 else "🔴" if total_pnl < 0 else "⚪"
                _d       = date.today()
                day_res  = (f"Profit Rs.{total_pnl:,.0f}" if total_pnl > 0
                            else f"Loss Rs.{abs(total_pnl):,.0f}" if total_pnl < 0 else "Flat")
                lines = [
                    f"📊 <b>DAY REPORT — {_d.strftime('%d %b %Y')}</b>",
                    f"{_res_emoji} <b>{day_res}</b>",
                    "",
                ]
                for r in rows:
                    _mark = "✅" if r["pnl"] > 0 else "🛑"
                    _o    = "CALL" if r["option_type"] == "CE" else "PUT"
                    lines.append(f"{_mark} Rs.{r['pnl']:+.0f} · {r['instrument']} {_o} · {r['time']}")
                day_inv  = sum((r["invested"] or 0) for r in rows)
                day_lots = ",".join(f"{x}L" for x in sorted({r["lots"] for r in rows if r["lots"]})) or "-"
                lines += [
                    "",
                    clean_box([
                        ("Trades",   f"{len(rows)} (won {len(wins)}, lost {len(losses)})"),
                        ("Invested", f"Rs.{day_inv:,.0f} · {day_lots}"),
                        ("Charges",  f"Rs.{tot_chg:,.0f}"),
                        ("Result",   f"Rs.{total_pnl:+,.0f} [{result}]"),
                        ("Tax est",  f"Rs.{tax_prov:,.0f} (~30%, yearly)"),
                        ("Capital",  f"Rs.{running_capital:,.0f}"),
                        ("Tomorrow", f"{lots_today}L"),
                    ]),
                    "",
                    "<i>Money credited by tomorrow morning.</i>",
                ]
                tg_send("\n".join(lines))
                bot_log(f"SETTLEMENT | Day P&L:Rs.{total_pnl:+.0f} | Capital:Rs.{running_capital:.0f} | Next:{lots_today}L", "ok")
                sync_background()

                # Weekly summary every Friday
                if date.today().weekday() == 4 and not weekly_done:
                    weekly_done = True
                    try:
                        week_start = (date.today() - timedelta(days=4)).strftime("%Y-%m-%d")
                        week_end   = date.today().strftime("%Y-%m-%d")
                        conn  = get_db()
                        wrows = conn.execute(
                            "SELECT pnl FROM trades WHERE date >= ? AND date <= ?",
                            (week_start, week_end)
                        ).fetchall()
                        conn.close()
                        if wrows:
                            wtot  = sum(r["pnl"] for r in wrows)
                            wwins = sum(1 for r in wrows if r["pnl"] > 0)
                            wn    = len(wrows)
                            wlines = [
                                f"📅 <b>WEEK REPORT</b>",
                                f"<i>{week_start} to {week_end}</i>",
                                "",
                                clean_box([
                                    ("Trades",    f"{wn} (won {wwins}, lost {wn-wwins})"),
                                    ("Win rate",  f"{round(wwins/wn*100)}%"),
                                    ("Result",    f"Rs.{wtot:+.0f}"),
                                    ("Capital",   f"Rs.{running_capital:.0f}"),
                                    ("Next week", f"{lots_today}L"),
                                ]),
                            ]
                            tg_send("\n".join(wlines))
                            bot_log(f"WEEKLY SUMMARY sent | Week P&L:Rs.{wtot:+.0f}", "ok")
                    except Exception as _we:
                        bot_log(f"Weekly summary error: {_we}", "err")

                time.sleep(60); continue

            # ── GET-READY PRE-ALERT (Sai 2026-07-24, manual copy-trading) ────
            # Telegram heads-up when NIFTY comes within PREALERT_PTS of a
            # trigger level, so Sai can open Kite BEFORE the real BUY message.
            # Runs on 1-min prices — allowed because it makes NO trading
            # decision (the 5-min gate below still owns all entries/exits).
            _px_pre = inst_prices.get("NIFTY")
            if (_px_pre and not positions and state.get("pending_trade") is None
                    and not state.get("paused", False)
                    and (now_t.hour > 10 or (now_t.hour == 10 and now_t.minute >= 15))
                    and now_t.hour < 15):
                _watch = [
                    ("yd_high", state.get("yd_high"),      "CALL (NIFTY rising)"),
                    ("yd_low",  state.get("yd_low"),       "PUT (NIFTY falling)"),
                    ("or_hi",   state.get("or_hi"),        "CALL (NIFTY rising)"),
                    ("or_lo",   state.get("or_lo"),        "PUT (NIFTY falling)"),
                ]
                if now_t.hour * 60 + now_t.minute > 750:   # fade only after 12:30
                    _watch.append(("fade", state.get("fade_trigger"), "PUT (NIFTY falling)"))
                _pre = state.setdefault("_prealert", {})
                for _wkey, _wlvl, _wside in _watch:
                    if not _wlvl or abs(_px_pre - _wlvl) > PREALERT_PTS:
                        continue
                    if time.time() - _pre.get(_wkey, 0) <= PREALERT_GAP_SEC:
                        continue
                    _pre[_wkey] = time.time()
                    tg_send(
                        "⏰ <b>GET READY — possible trade soon</b>\n"
                        + clean_box([
                            ("NIFTY now",  f"{_px_pre:.0f}"),
                            ("Watch line", f"{_wlvl:.0f} ({abs(_px_pre - _wlvl):.0f} pts away)"),
                            ("If crossed", f"I may say BUY {_wside}"),
                        ])
                        + "\n👉 <b>YOU:</b> open Kite, keep money ready. "
                          "Do NOT buy yet — wait for my BUY message."
                    )
                    bot_log(f"PRE-ALERT: NIFTY {_px_pre:.0f} near {_wkey} {_wlvl:.0f}", "info")
                    break   # one heads-up at a time is enough

            # ── 5-MIN DECISION GATE (2026-07-13, root-cause fix) ─────────────
            # Live paper was losing (41% win) while the SAME strategy backtested
            # +Rs.43k/65% — because every backtest decides on COMPLETED 5-min
            # candle closes, but the live loop was deciding on 1-min prices
            # every 60s: SL hit by 1-min wicks, staircase booked on 1-min dips,
            # entries on 1-min spikes. Fix: SL / staircase / entry decisions run
            # ONCE per 5-min candle, on the first price after the candle closes
            # (~= its close). 1-min prices remain for display and the 15:25 EOD
            # exit above, which must never wait.
            _bucket = now_t.strftime("%H:") + str((now_t.minute // 5) * 5)
            if state.get("_last_5m_bucket") == _bucket:
                state["positions_list"] = positions
                time.sleep(20)
                continue
            state["_last_5m_bucket"] = _bucket

            # ── 3. Check open positions (SL / BE lock / trail) ───────────────
            closed = []
            for pos in positions:
                iname = pos.get("instrument", "NIFTY")
                px    = inst_prices.get(iname)
                if not px:
                    continue
                lot   = pos.get("lot", LOT);  delta = pos.get("delta", DELTA)
                otype = pos.get("option_type", "CE")
                # ── PREMIUM-BASED P&L: real option price change (theta included)
                pnl = None
                if pos.get("strike") and pos.get("premium_entry"):
                    real_prem = fetch_live_premium_real(iname, pos["strike"], otype)
                    if real_prem:
                        pos["last_premium"] = real_prem
                        pnl = (real_prem - pos["premium_entry"]) * lot - BROKERAGE
                if pnl is None:   # no real quote — fall back to index-delta model
                    move = (px - pos["entry"]) * delta * lot if otype == "CE" else (pos["entry"] - px) * delta * lot
                    pnl  = move - BROKERAGE

                # ── Premium SL: exit if option lost 60% of entry value ────────
                if pos.get("strike") and pos.get("premium_entry"):
                    live_prem = pos.get("last_premium")
                    if live_prem and live_prem < pos["premium_entry"] * 0.40:
                        pnl_p = round((live_prem - pos["premium_entry"]) * lot - BROKERAGE, 2)
                        pnl_p, _chg, _gross = settle_trade(pos, pnl_p)
                        close_position_order(pos, "PREM_SL")
                        _inv, _lts = trade_meta(pos)
                        save_trade(pos["score"], pos["entry"], px, pnl_p, "PREM_SL", iname, otype, _inv, _lts,
                                   gross=_gross, charges=_chg)
                        daily_pnl       += pnl_p
                        running_capital += pnl_p          # fix 2026-07-12: PREM_SL never
                        lots_today       = BASE_LOTS      # deducted capital before
                        save_bot_state(running_capital, lots_today)
                        state["running_capital"] = running_capital
                        state["lots_today"]      = lots_today
                        sl_cooldown[iname] = time.time()
                        bot_log(f"PREM SL {iname} {otype} {pos['strike']} | Rs.{pos['premium_entry']}→Rs.{live_prem:.1f} P&L:Rs.{pnl_p:.0f}", "err")
                        tg_send(
                            f"🛑 <b>STOP LOSS — option dropped too much</b>\n"
                            + clean_box([
                                ("Sold",  f"{iname} {pos.get('strike','')} {plain_opt(otype)}"),
                                ("Value", f"Rs.{pos['premium_entry']} → Rs.{live_prem:.1f}"),
                                ("Loss",  f"Rs.{abs(pnl_p):.0f}"),
                            ])
                            + f"\n👉 <b>YOU:</b> SELL your {iname} {pos.get('strike','')} {plain_opt(otype)} now to stop the loss."
                        )
                        closed.append(pos); sync_background(); continue

                # ── Track peak P&L (updated every cycle) ──────────────────────
                if pnl > pos.get("peak_pnl", -9999):
                    pos["peak_pnl"] = pnl
                peak_pnl = pos.get("peak_pnl", 0)

                # ── Activate breakeven lock once peak hits Rs.300 ──────────────
                # ── v4.3 PROFIT STAIRCASE (Sai 2026-07-06) ─────────────────────
                # Floors 150/300/500/700/850/900/+150... scaled by lots.
                # Cross a rung → it's locked. Fall below it → book exactly it.
                _units = pos.get("lot", LOT)
                floor  = staircase_floor(peak_pnl, _units)
                if floor and floor != pos.get("stair_floor"):
                    first_lock = pos.get("stair_floor") is None
                    pos["stair_floor"] = floor
                    save_positions(positions)
                    bot_log(f"STAIR UP {iname} {otype} | Peak:Rs.{peak_pnl:.0f} — floor locked at Rs.{floor:.0f}", "ok")
                    if first_lock:
                        tg_send(
                            f"🔒 <b>PROFIT LOCKED IN</b>\n"
                            + clean_box([
                                ("Trade",  f"{iname} {plain_opt(otype)}"),
                                ("Now up", f"Rs.{peak_pnl:.0f}"),
                                ("Locked", f"Rs.{floor:.0f} (can't lose now)"),
                            ])
                            + f"\nBot keeps holding for more, and books if it drops back to the locked step."
                        )

                # ── 1. Staircase exit: fell below the locked floor ─────────────
                if floor and pnl < floor:
                    net, chg, gross = settle_trade(pos, round(floor, 2))
                    close_position_order(pos, "STAIR_BOOK")
                    _inv, _lts = trade_meta(pos)
                    save_trade(pos["score"], pos["entry"], px, net, "STAIR_BOOK", iname, otype, _inv, _lts,
                               gross=gross, charges=chg)
                    daily_pnl      += net
                    running_capital += net
                    lots_today = min(MAX_LOTS, max(BASE_LOTS, int(running_capital // CAPITAL_PER_LOT)))
                    save_bot_state(running_capital, lots_today)
                    state["running_capital"] = running_capital
                    state["lots_today"]      = lots_today
                    bot_log(f"STAIR BOOK {iname} {otype} | Peak:Rs.{peak_pnl:.0f} Floor:Rs.{floor:.0f} Charges:Rs.{chg:.0f} NET:Rs.{net:+.0f} | Capital:Rs.{running_capital:.0f}", "ok")
                    _dir_word = "up" if otype == "CE" else "down"
                    tg_send(
                        f"✅ <b>PROFIT BOOKED — we won this one</b>\n"
                        + clean_box([
                            ("Trade",    f"{iname} {plain_opt(otype)}"),
                            ("Bought",   f"NIFTY {pos['entry']:.0f}"),
                            ("Sold",     f"NIFTY {px:.0f} (it turned back)"),
                            ("Best up",  f"Rs.{peak_pnl:.0f}"),
                            ("Kept",     f"Rs.{floor:.0f} (locked step)"),
                            ("Charges",  f"Rs.{chg:.0f}"),
                            ("NET made", f"Rs.{net:+.0f}"),
                            ("Capital",  f"Rs.{running_capital:.0f}"),
                        ])
                        + f"\n👉 <b>YOU:</b> SELL your {iname} {pos.get('strike','')} {plain_opt(otype)} now to book the profit."
                    )
                    closed.append(pos); sync_background(); continue

                # ── 2. Dynamic hard SL — only while below the first rung ───────
                pos_sl = -abs(pos.get("sl_rs", abs(STOP_LOSS)))   # e.g. -300
                if floor is None and pnl <= pos_sl:
                    net, chg, gross = settle_trade(pos, pos_sl)
                    close_position_order(pos, "STOP_LOSS")
                    _inv, _lts = trade_meta(pos)
                    save_trade(pos["score"], pos["entry"], px, net, "STOP_LOSS", iname, otype, _inv, _lts,
                               gross=gross, charges=chg)
                    daily_pnl      += net
                    running_capital += net
                    lots_today      = BASE_LOTS
                    save_bot_state(running_capital, lots_today)
                    state["running_capital"] = running_capital
                    state["lots_today"]      = lots_today
                    sl_cooldown[iname] = time.time()
                    bot_log(f"STOP LOSS {iname} {otype} | SL:Rs.{pos_sl} Charges:Rs.{chg:.0f} NET:Rs.{net:+.0f} | Capital:Rs.{running_capital:.0f}", "err")
                    _dir_word = "fell" if otype == "CE" else "rose"
                    tg_send(
                        f"🛑 <b>STOP LOSS — this one went wrong</b>\n"
                        + clean_box([
                            ("Trade",    f"{iname} {plain_opt(otype)}"),
                            ("Bought",   f"NIFTY {pos['entry']:.0f}"),
                            ("Sold",     f"NIFTY {px:.0f} (wrong way)"),
                            ("Loss",     f"Rs.{abs(pos_sl):.0f}"),
                            ("Charges",  f"Rs.{chg:.0f}"),
                            ("NET lost", f"Rs.{abs(net):.0f}"),
                            ("Capital",  f"Rs.{running_capital:.0f}"),
                        ])
                        + f"\n👉 <b>YOU:</b> SELL your {iname} {pos.get('strike','')} {plain_opt(otype)} now to stop the loss."
                    )
                    closed.append(pos); sync_background(); continue

                # ── Log riding status ──────────────────────────────────────────
                if floor:
                    bot_log(f"RIDING {iname} {otype} | Peak:Rs.{peak_pnl:.0f} Now:Rs.{pnl:.0f} | staircase floor Rs.{floor:.0f}", "ok")

            for pos in closed:
                positions.remove(pos)
            if closed:
                save_positions(positions)

            # ── 4. Update state ────────────────────────────────────────────────
            px_nifty = inst_prices.get("NIFTY")
            state["positions_list"]   = positions
            state["open_positions"]   = len(positions)
            state["daily_pnl"]        = round(daily_pnl, 0)

            # Unrealized P&L (premium-based when a real quote exists)
            unrealized = 0.0
            for _p in positions:
                _lot   = _p.get("lot", LOT); _delta = _p.get("delta", DELTA)
                _otype = _p.get("option_type", "CE")
                if _p.get("last_premium") and _p.get("premium_entry"):
                    unrealized += (_p["last_premium"] - _p["premium_entry"]) * _lot - BROKERAGE
                else:
                    _px = inst_prices.get(_p.get("instrument","NIFTY"), _p["entry"])
                    _mv = (_px - _p["entry"]) * _delta * _lot if _otype == "CE" else (_p["entry"] - _px) * _delta * _lot
                    unrealized += _mv - BROKERAGE
            state["unrealized_pnl"] = round(unrealized, 0)
            state["total_pnl"]      = round(daily_pnl + unrealized, 0)
            state["active_side"]    = "BULL" if state.get("option_type") == "CE" else "BEAR" if state.get("option_type") == "PE" else None

            # NIFTY option chain for dashboard (every 3 min — NSE rate-limits)
            if time.time() - state.get("_last_oc_fetch", 0) > 180:
                state["_last_oc_fetch"] = time.time()
                oc = fetch_nse_optionchain("NIFTY")
                if oc:
                    all_exp = oc.get("records", {}).get("expiryDates", [])
                    state["available_expiries"] = all_exp
                    if px_nifty:
                        metrics = calculate_oi_metrics(oc, px_nifty, trading_expiry_index())
                        if metrics:
                            state["oi_nifty"] = metrics
                            state["expiry"]   = metrics["expiry"]

            # ── 5. Breakout signal + entry (max 3/day: 1 auto + 2 gated) ──────
            prefix = "[PAPER]" if PAPER_TRADE else "[LIVE]"
            _now   = datetime.now()
            # Entry window: 10:15 AM to 3:00 PM (v4.4 — extended 2026-07-06
            # after backtest: till 15:00 = +Rs.47,378 vs till 14:30 =
            # +Rs.37,213 at 1 lot, same drawdown; 15:15 tested worse)
            _entry_allowed = (_now.hour > 10 or (_now.hour == 10 and _now.minute >= 15)) \
                             and _now.hour < 15
            # If a trade was still awaiting /confirm when the window shut,
            # tell Sai instead of going silent (2026-07-03 fix)
            if not _entry_allowed and state.get("pending_trade"):
                _pn = state["pending_trade"].get("no", "?")
                state["pending_trade"]   = None
                state["trade_confirmed"] = False
                save_pending()
                tg_send(f"Trade #{_pn} cancelled — the 3:00 PM entry cutoff passed before it could execute. No position was opened.")
                bot_log(f"Pending trade #{_pn} cancelled — entry window closed", "info")
            entered      = False
            trades_today = count_trades_today()
            paused       = state.get("paused", False)
            state["trades_today"]     = trades_today
            state["first_trade_done"] = trades_today >= MAX_TRADES_PER_DAY or paused

            def do_entry(otype, signal, conf, sl_rs, trade_no, score=None):
                """Send pre-trade Telegram, place the order, track the position."""
                nonlocal entered
                score_line = f"<b>Score:</b> {score:g}/9 · " if score is not None else ""
                if daily_pnl <= DAILY_LIMIT:   # fix 2026-07-12: no new entries
                    bot_log(f"ENTRY BLOCKED — daily loss limit reached (Rs.{daily_pnl:.0f})", "err")
                    return False
                exp     = state.get("expiry") or get_next_expiry("NIFTY", trading_expiry_index())
                strike  = get_target_strike(px_nifty, "NIFTY", otype)
                premium = fetch_option_premium("NIFTY", strike, otype, px_nifty)
                if premium and premium < 30:
                    bot_log(f"SKIP NIFTY {otype} {strike} — premium Rs.{premium:.0f} too low (<Rs.30)", "info")
                    return False
                # ── Capital check: NEVER buy more than available capital ──────
                lots_use = lots_today
                if premium:
                    per_lot_cost = premium * UNITS_PER_LOT   # 1 real lot = 65 units
                    affordable   = int(running_capital // per_lot_cost)
                    if affordable < 1:
                        bot_log(f"SKIP — capital Rs.{running_capital:.0f} can't afford 1 lot (needs Rs.{per_lot_cost:.0f})", "err")
                        tg_send(
                            f"⚠️ <b>TRADE SKIPPED — not enough money</b>\n"
                            + clean_box([
                                ("Need", f"Rs.{per_lot_cost:.0f} (1 lot {strike} {plain_opt(otype)})"),
                                ("Have", f"Rs.{running_capital:.0f}"),
                            ])
                        )
                        return False
                    if affordable < lots_use:
                        bot_log(f"Lots capped by capital: {lots_use}L -> {affordable}L "
                                f"(premium Rs.{premium} x 25/lot, capital Rs.{running_capital:.0f})", "info")
                        lots_use = affordable
                current_lot = lots_use * UNITS_PER_LOT
                sl_rs       = sl_rs * lots_use   # per-lot SL scales with lots (like staircase rungs)
                invested    = round((premium or 0) * current_lot, 2)
                direction_label = "BULLISH — BUY CALL (CE)" if otype == "CE" else "BEARISH — BUY PUT (PE)"
                yd_h, yd_l = state.get("yd_high"), state.get("yd_low")
                pot = round(abs(yd_h - yd_l) * DELTA * current_lot * 0.5, -1) if yd_h and yd_l else 500
                # ── Telegram BEFORE the trade: plain "clean labels" style ─────
                _lotword = "lot" if lots_use == 1 else "lots"
                _sl_pts0 = abs(sl_rs) / (DELTA * current_lot)
                _stop0   = px_nifty - _sl_pts0 if otype == "CE" else px_nifty + _sl_pts0
                # Kite SL premium estimate (Sai 2026-07-24, manual copy-trading):
                # the option price at which the Rs. stop-loss is hit, so Sai can
                # place a stop-loss order in his own Kite app right after buying.
                _slprem = max((premium or 0) - abs(sl_rs) / current_lot, 1)
                # CLEAR FORMAT (Sai 2026-07-24 evening: reverting to the old
                # format left him lost mid-day — "not telling the percentage of
                # stop loss ... completely out of my mind". This is the format
                # he asked for this morning, back for good): buy range, stop as
                # % + price, two target prices, and fixed Rs. distances from
                # HIS OWN fill. Keep all of it in every entry message.
                _slpct   = (abs(sl_rs) / current_lot) / premium * 100 if premium else 0
                _sldrop  = abs(sl_rs) / current_lot
                _tgt_rs  = max(2 * abs(sl_rs), 900 * lots_use)
                _tgtprem = (premium or 0) + _tgt_rs / current_lot
                _tgtpct  = (_tgt_rs / current_lot) / premium * 100 if premium else 0
                _rows0 = [
                    ("Bet",    plain_bet(otype)),
                    ("Buy",    f"{strike} {plain_opt(otype)} x {lots_use} {_lotword}"),
                ]
                if premium:
                    _buy_lo, _buy_hi = premium * 0.95, premium * 1.05
                    _t1prem = premium + 450 * lots_use / current_lot
                    _t1pct  = (450 * lots_use / current_lot) / premium * 100
                    _rows0.append(("Buy near", f"Rs.{_buy_lo:.0f}–{_buy_hi:.0f} (don't pay above Rs.{_buy_hi:.0f})"))
                    _rows0.append(("Cost",     f"~Rs.{invested:.0f}"))
                    _rows0.append(("Stoploss", f"{_slpct:.1f}% = option Rs.{_slprem:.0f} (lose ~Rs.{abs(sl_rs):.0f})"))
                    _rows0.append(("Target",   f"Rs.{_t1prem:.0f} or Rs.{_tgtprem:.0f} ({_t1pct:.0f}–{_tgtpct:.0f}%) — SELL when I say"))
                    _rows0.append(("Your price", f"stop = your buy − Rs.{_sldrop:.0f} · target = your buy + Rs.{_tgt_rs/current_lot:.0f}"))
                else:
                    _rows0.append(("Cost", f"~Rs.{invested:.0f}"))
                    _rows0.append(("Stop", f"NIFTY ~{_stop0:.0f} → lose ~Rs.{abs(sl_rs):.0f}"))
                try:
                    _so = second_opinion(get_candles("^NSEI").iloc[-1].get("adx"))
                except Exception:
                    _so = None
                if _so:
                    _rows0.append(("2nd opinion", _so))
                _rows0 += [
                    ("Profit", f"locks in steps from +Rs.{150*lots_use} — I will say SELL"),
                    ("Odds",   f"{conf}% win" + (f" · score {score:g}/9" if score is not None else "")),
                    ("Expiry", f"{exp} · {'PAPER' if PAPER_TRADE else 'REAL MONEY'}"),
                ]
                tg_send(
                    f"{'🟢' if otype=='CE' else '🔴'} <b>TRADE #{trade_no} — BUY NOW (manual)</b>\n"
                    + clean_box(_rows0)
                    + f"\n👉 <b>YOU:</b> BUY {lots_use} {_lotword} NIFTY {strike} {plain_opt(otype)} "
                      f"at ~Rs.{premium} on Kite NOW.\n"
                      f"In Kite's boxes type: Stoploss {_slpct:.1f}% · Target {_tgtpct:.1f}%.\n"
                      f"I do NOT place this for you — act within 1-2 min, "
                      f"then wait for my SELL message."
                )
                order_id = execute_order("BUY", strike, otype, current_lot, reason=signal)
                if order_id is None:      # live order failed — no ghost position
                    bot_log("Entry aborted — live order failed", "err")
                    return False
                positions.append({
                    "instrument":    "NIFTY",
                    "lot":           current_lot,
                    "delta":         DELTA,
                    "entry":         px_nifty,
                    "score":         conf,
                    "option_type":   otype,
                    "expiry":        exp,
                    "strike":        strike,
                    "premium_entry": premium,
                    "trail_stop":    None,
                    "peak_pnl":      -9999,
                    "sl_rs":         sl_rs,
                    "invested":      invested,
                })
                save_positions(positions)
                state["option_type"] = otype
                bot_log(f"{prefix} #{trade_no} {direction_label} | NIFTY {strike} @ Rs.{premium} | {signal} | Conf:{conf}% SL:Rs.{sl_rs} | {lots_use}L Inv:Rs.{invested:.0f}", "ok")
                _sl_pts  = abs(sl_rs) / (DELTA * current_lot)
                _stop_lvl = px_nifty - _sl_pts if otype == "CE" else px_nifty + _sl_pts
                _rows1 = [
                    ("Bought",  f"{strike} {plain_opt(otype)} x {lots_use} {_lotword}"),
                    ("Cost",    f"Rs.{invested:.0f}"),
                    ("Stop",    f"NIFTY ~{_stop_lvl:.0f} → lose ~Rs.{abs(sl_rs):.0f}"),
                ]
                if premium:
                    _rows1.append(("Stoploss", f"{_slpct:.1f}% = option Rs.{_slprem:.0f}"))
                    _rows1.append(("Target",   f"Rs.{premium + 450*lots_use/current_lot:.0f} or Rs.{_tgtprem:.0f} — SELL when I say"))
                    _rows1.append(("Your price", f"stop = your buy − Rs.{_sldrop:.0f} · target = your buy + Rs.{_tgt_rs/current_lot:.0f}"))
                if _so:
                    _rows1.append(("2nd opinion", _so))
                _rows1 += [
                    ("Profit",  f"first lock at +Rs.{150*lots_use} — wait for my SELL message"),
                    ("Capital", f"Rs.{running_capital:.0f}"),
                ]
                tg_send(
                    f"✅ <b>TRADE #{trade_no} ENTERED</b>\n"
                    + clean_box(_rows1)
                )
                sync_background()
                entered = True
                return True

            if (not paused and _entry_allowed and len(positions) < MAX_POSITIONS
                    and px_nifty and trades_today < MAX_TRADES_PER_DAY):
                # v4.4 (Sai 2026-07-06): expiry day is a NORMAL trading day —
                # the bot just uses NEXT week's contract instead of the dying
                # one (backtest: +Rs.37,213 vs +Rs.24,271 skipping Tuesdays).
                if is_expiry_day("NIFTY") and time.time() - state.get("_exp_log_ts", 0) > 3600:
                    state["_exp_log_ts"] = time.time()
                    bot_log("Expiry day — trading NEXT week's contract (v4.4)", "info")
                if "NIFTY" in sl_cooldown and time.time() - sl_cooldown["NIFTY"] < SL_COOLDOWN_SEC:
                    bot_log("NIFTY in SL cooldown — waiting", "info")
                else:
                    yd_high, yd_low = fetch_daily_hl("^NSEI")
                    state["yd_high"] = yd_high
                    state["yd_low"]  = yd_low

                    if yd_high and yd_low:
                        # ── v4.2 signal ladder (backtested 2026-07-03) ────────
                        # 1. yesterday's high/low breakout
                        # 2. opening-range (9:15-10:15) breakout
                        # 3. after 12:30: fade PE (0.2% off day high;
                        #    tightened from 0.3% on 2026-07-06 — Sai spotted
                        #    late fades; backtest: 0.2% = +Rs.153k vs 0.3% =
                        #    +Rs.127k compounded, lower DD) — also
                        #    overrides a stale bullish signal
                        # First-candle "MORN" guess removed (weakest signal).
                        _cdf   = get_candles("^NSEI")
                        _tod   = _cdf[_cdf["datetime"].dt.date == date.today()]
                        _mins  = _tod["datetime"].dt.hour * 60 + _tod["datetime"].dt.minute
                        _or    = _tod[_mins < 615]                    # before 10:15
                        or_hi  = float(_or["high"].max())  if len(_or)  else None
                        or_lo  = float(_or["low"].min())   if len(_or)  else None
                        day_hi = float(_tod["high"].max()) if len(_tod) else None
                        _now_m = _now.hour * 60 + _now.minute
                        state["or_hi"]        = or_hi
                        state["or_lo"]        = or_lo
                        state["day_hi"]       = day_hi
                        state["fade_trigger"] = round(day_hi * (1 - FADE_PCT), 1) if day_hi else None

                        otype, signal, sig_level = None, None, None
                        if px_nifty > yd_high:
                            otype, signal, sig_level = "CE", f"BREAK HIGH {yd_high:.0f}", yd_high
                        elif px_nifty < yd_low:
                            otype, signal, sig_level = "PE", f"BREAK LOW {yd_low:.0f}", yd_low
                        elif or_lo and px_nifty < or_lo:
                            otype, signal, sig_level = "PE", f"OR BREAK LOW {or_lo:.0f}", or_lo
                        elif or_hi and px_nifty > or_hi:
                            otype, signal, sig_level = "CE", f"OR BREAK HIGH {or_hi:.0f}", or_hi
                        # afternoon fade: overrides any non-PE signal
                        if (_now_m > 750 and otype != "PE" and day_hi
                                and px_nifty <= day_hi * (1 - FADE_PCT)):
                            otype, signal, sig_level = "PE", f"FADE {day_hi:.0f}->{px_nifty:.0f}", day_hi * (1 - FADE_PCT)
                        state["signal"] = signal or "--"

                        # ── v4.1 multi-timeframe filter: 15-min trend must ────
                        # agree with the direction before any NEW entry.
                        # Backtested 2026-07-03: +Rs.1,745 net vs v4 alone.
                        mtf_ok = htf_trend_ok(otype, get_candles("^NSEI")) if otype else False
                        state["mtf_ok"] = mtf_ok

                        # ── 9-point Confidence Score (Sai 2026-07-14) ─────────
                        # Telegram messages only at >= CONF_SCORE_TG; automatic
                        # entries only at >= CONF_SCORE_AUTO. Same math as
                        # backtest_confidence.py — keep the two in sync.
                        if otype:
                            c9, c9_bd = confidence_score9(otype, sig_level, px_nifty, get_candles("^NSEI"))
                        else:
                            c9, c9_bd = 0.0, {}
                        state["conf_score"] = c9
                        state["conf_bd"]    = c9_bd

                        # ── AI COUNCIL (v5.0, Sai 2026-07-20): 6 mini-bots vote
                        # (trend/momentum/volume/breakout/pattern + a volatility
                        # risk bot) — DISPLAY / EXPLANATION ONLY. Backtested
                        # 2026-07-20 (backtest_ensemble.py, 60d real data):
                        # using the council's confidence as the ENTRY GATE did
                        # NOT beat the current proven 9-point gate on a
                        # per-trade basis, and a volatility-based risk filter
                        # on top of the current gate made results WORSE (this
                        # strategy's profit lives IN the higher-volatility
                        # windows the risk bot flags as risky — filtering them
                        # out removed good trades with the bad). So the
                        # council does NOT change entries or gating — it only
                        # explains WHY, in plain words, for Telegram/dashboard.
                        state["ensemble"] = None
                        if otype and mtf_ok:
                            try:
                                st15_disp, macd15_disp = htf_alignment(otype, get_candles("^NSEI"))
                                vix_now = fetch_vix()
                                state["ensemble"] = eb.mother_decide(
                                    otype, sig_level, px_nifty, get_candles("^NSEI"),
                                    st15=st15_disp, macd15_up=macd15_disp, vix=vix_now)
                            except Exception:
                                state["ensemble"] = None

                        # ── Sai's 72% rule (2026-07-24, backtest_conf72.py):
                        # conf% > 72 ALSO qualifies for entry, on top of the
                        # score>=7 gate (OR — not a replacement). Backtest,
                        # 60d live config: OR-variant +48,241 / 189 trades vs
                        # baseline +47,714 / 186 (same win rate — ~3 extra
                        # trades per 60d); conf>72 as the ONLY gate collapsed
                        # to +15,808 / 75 trades and was REJECTED. The 15-min
                        # MTF filter still applies before either gate.
                        if otype is not None:
                            conf, sl_rs = analyze_setup(otype, signal or "--", get_candles("^NSEI"))
                        else:
                            conf, sl_rs = 50, SL_MIN
                        conf72_ok = otype is not None and conf > 72
                        try:
                            _adx_now = float(get_candles("^NSEI").iloc[-1]["adx"])
                        except Exception:
                            _adx_now = None

                        if otype is None and state.get("pending_trade") is None:
                            pass   # no signal this minute — nothing to do
                        elif otype is not None and not mtf_ok and state.get("pending_trade") is None:
                            if time.time() - state.get("_mtf_log_ts", 0) > 300:
                                state["_mtf_log_ts"] = time.time()
                                bot_log(f"MTF filter: 15-min trend not aligned with {otype} — entry blocked", "info")
                        elif (otype is not None and _adx_now is not None
                                and _adx_now < ADX_CHOP
                                and state.get("pending_trade") is None):
                            # ── CHOP GUARD (Sai 2026-07-24, backtest_choppy.py:
                            # blocking ADX<15 entries beat baseline in both
                            # halves — see ADX_CHOP comment). Skip + tell him.
                            if time.time() - state.get("_chop_log_ts", 0) > 900:
                                state["_chop_log_ts"] = time.time()
                                bot_log(f"CHOP GUARD: ADX {_adx_now:.0f} < {ADX_CHOP} — signal skipped, market too choppy", "info")
                                tg_send(
                                    f"⚠️ <b>CHOPPY MARKET — signal skipped</b>\n"
                                    + clean_box([
                                        ("Signal", f"{plain_bet(otype)}"),
                                        ("ADX",    f"{_adx_now:.0f} (below {ADX_CHOP} = chop)"),
                                        ("Why",    "setups like this LOSE on average"),
                                    ])
                                    + "\n👉 <b>YOU:</b> do NOT trade. Choppy days eat both CALLs and PUTs. I will alert when the market trends again."
                                )
                        elif (otype is not None and c9 < CONF_SCORE_TG and not conf72_ok
                                and state.get("pending_trade") is None):
                            # Sai 2026-07-14: below the message threshold —
                            # no Telegram, no entry. Dashboard log only.
                            if time.time() - state.get("_conf_log_ts", 0) > 300:
                                state["_conf_log_ts"] = time.time()
                                bot_log(f"Confidence gate: {signal} scored {c9:g}/9 "
                                        f"(< {CONF_SCORE_TG:g}) and conf {conf}% <= 72 — no alert, no entry", "info")
                        else:
                            if trades_today == 0 and otype is not None and (c9 >= CONF_SCORE_AUTO or conf72_ok):
                                # ── First trade: automatic (score-gated) ───────
                                do_entry(otype, signal, conf, sl_rs, 1, score=c9)
                            else:
                                # ── 2nd+ trade: manual /confirm (gate removed) ──
                                pending = state.get("pending_trade")
                                if pending is None:
                                    if otype is None:
                                        pass   # nothing new to offer
                                    elif time.time() < state.get("gate_cooldown", 0):
                                        pass   # recently skipped — don't re-spam
                                    elif conf < CONFIDENCE_GATE:
                                        # unreachable with gate=0 — kept for safety
                                        bot_log(f"Trade #{trades_today+1} skipped silently — confidence {conf}% < {CONFIDENCE_GATE}%", "info")
                                        state["gate_cooldown"] = time.time() + 900
                                    elif state.get("auto_mode", True) and (c9 >= CONF_SCORE_AUTO or conf72_ok):
                                        # ── AUTO MODE (Sai 2026-07-07): all filters
                                        # passed — take the trade immediately, no
                                        # /confirm. This matches the backtests, which
                                        # assume every signal is taken instantly.
                                        # /manual on Telegram restores confirmations.
                                        bot_log(f"AUTO trade #{trades_today+1}: {signal} conf {conf}% score {c9:g}/9 — entering", "ok")
                                        do_entry(otype, signal, conf, sl_rs, trades_today + 1, score=c9)
                                    elif state.get("auto_mode", True):
                                        # score in [CONF_SCORE_TG, CONF_SCORE_AUTO):
                                        # message allowed, auto entry not (Sai
                                        # 2026-07-14). Alert-only, throttled.
                                        if time.time() - state.get("_band_tg_ts", 0) > 900:
                                            state["_band_tg_ts"] = time.time()
                                            tg_send(
                                                f"🔕 <b>SIGNAL — skipped by bot</b>\n"
                                                + clean_box([
                                                    ("Bet",   plain_bet(otype)),
                                                    ("Score", f"{c9:g}/9 (needs {CONF_SCORE_AUTO:g}+)"),
                                                ])
                                                + f"\nBot skipped this — score too low. Trade it yourself only if you accept the extra risk."
                                            )
                                            bot_log(f"Score {c9:g}/9 in alert-only band — Telegram sent, no auto entry", "info")
                                    elif _now.hour * 60 + _now.minute > 897:
                                        # after 14:57 the /confirm + 2-min wait can't
                                        # finish before the 15:00 cutoff — don't offer
                                        # trades we cannot honor (2026-07-03 fix)
                                        if time.time() - state.get("_late_log_ts", 0) > 300:
                                            state["_late_log_ts"] = time.time()
                                            bot_log(f"Signal {signal} not offered — too close to 14:30 entry cutoff", "info")
                                    else:
                                        state["pending_trade"] = {
                                            "otype": otype, "signal": signal, "conf": conf,
                                            "sl_rs": sl_rs, "ts": time.time(), "no": trades_today + 1,
                                            "c9": c9,
                                        }
                                        state["trade_confirmed"] = False
                                        save_pending()
                                        _slp   = abs(sl_rs) / (DELTA * UNITS_PER_LOT)
                                        _stopc = px_nifty - _slp if otype == "CE" else px_nifty + _slp
                                        try:
                                            _so_c = second_opinion(get_candles("^NSEI").iloc[-1].get("adx"))
                                        except Exception:
                                            _so_c = None
                                        tg_send(
                                            f"🔔 <b>TRADE #{trades_today+1} — confirm needed</b>\n"
                                            + clean_box([
                                                ("Bet",    plain_bet(otype)),
                                                ("Now",    f"NIFTY at {px_nifty:.0f}"),
                                                ("Stop",   f"~{_stopc:.0f} → lose ~Rs.{abs(sl_rs):.0f}/lot"),
                                                ("Profit", "locks in steps as it moves our way"),
                                                ("Odds",   f"{conf}% win · score {c9:g}/9"),
                                            ] + ([("2nd opinion", _so_c)] if _so_c else []))
                                            + f"\n👉 Reply <b>/confirm</b> within 10 min to take it.\n"
                                            + f"<i>Entry about 2 min after you confirm.</i>"
                                        )
                                        bot_log(f"Trade #{trades_today+1} gate: conf {conf}% — awaiting /confirm", "info")
                                else:
                                    age = time.time() - pending["ts"]
                                    if state.get("trade_confirmed") and age >= CONFIRM_MIN_WAIT:
                                        state["pending_trade"]   = None
                                        state["trade_confirmed"] = False
                                        save_pending()
                                        do_entry(pending["otype"], pending["signal"],
                                                 pending["conf"], pending["sl_rs"], pending["no"],
                                                 score=pending.get("c9"))
                                    elif age > CONFIRM_TIMEOUT and not state.get("trade_confirmed"):
                                        state["pending_trade"] = None
                                        state["gate_cooldown"] = time.time() + 900
                                        save_pending()
                                        tg_send(f"Trade #{pending['no']} skipped — no confirmation received.")
                                        bot_log("Gated trade skipped — confirmation timeout", "info")

            if not entered and not positions and trades_today < MAX_TRADES_PER_DAY:
                sig_tag = state.get("signal","--")
                yd_h    = state.get("yd_high")
                yd_l    = state.get("yd_low")
                hl_tag  = f" YdH:{yd_h:.0f}/YdL:{yd_l:.0f}" if yd_h else ""
                trade_tag = f" T:{trades_today}/{MAX_TRADES_PER_DAY}"
                bot_log(f"NIFTY:{px_nifty:.0f}{hl_tag} Sig:{sig_tag}{trade_tag} Lots:{lots_today}L Cap:Rs.{running_capital:.0f} Daily:Rs.{daily_pnl:.0f}")

            if time.time() - last_sync > 300:
                sync_background()
                last_sync = time.time()

        except Exception as e:
            bot_log(f"Error: {e}", "err")

        time.sleep(CHECK_INTERVAL)

# ── LIVE KITE DATA API ────────────────────────────────────────────────────────
@app.route("/api/positions")
def api_positions():
    try:
        if not os.path.exists(TOKEN_FILE):
            return jsonify({"error": "No token"})
        with open(TOKEN_FILE) as f:
            token = f.read().strip()
        kite = KiteConnect(api_key=API_KEY)
        kite.set_access_token(token)
        pos = kite.positions()
        orders = kite.orders()
        return jsonify({
            "positions": pos.get("net", []),
            "orders":    orders,
            "funds":     kite.margins().get("equity", {})
        })
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/api/charges")
def api_charges():
    """Per-trade itemized Zerodha F&O settlement for today — every charge line."""
    out = []
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT time,entry,exit,pnl,gross,charges,invested,status,option_type,lots "
            "FROM trades WHERE date=? ORDER BY id",
            (date.today().strftime("%Y-%m-%d"),)).fetchall()
        conn.close()
        for r in rows:
            inv    = float(r["invested"] or 0)
            legacy = r["charges"] is None            # booked under old Rs.20 accounting
            gross  = r["gross"] if r["gross"] is not None else round(r["pnl"] + BROKERAGE, 2)
            b      = zerodha_fno_charges(inv, max(0.0, inv + gross))
            net    = round(gross - b["total"], 2)
            out.append({
                "time": r["time"], "otype": r["option_type"], "status": r["status"],
                "lots": r["lots"], "invested": round(inv, 2), "gross": round(gross, 2),
                "brokerage": b["brokerage"], "stt": b["stt"], "txn": b["txn"],
                "sebi": b["sebi"], "stamp": b["stamp"], "gst": b["gst"],
                "total_charges": b["total"], "net": net,
                "booked": r["pnl"], "legacy": legacy,
            })
    except Exception:
        pass
    return jsonify(out)

@app.route("/api/state")
def api_state():
    # today's charges settlement (for the dashboard calculator)
    _settle = {"gross": 0.0, "charges": 0.0, "net": 0.0, "count": 0, "tax_prov": 0.0, "last": None}
    try:
        _c = get_db()
        _rows = _c.execute(
            "SELECT time,pnl,gross,charges,status,option_type FROM trades WHERE date=? ORDER BY id",
            (date.today().strftime("%Y-%m-%d"),)).fetchall()
        _c.close()
        for _r in _rows:
            _settle["net"]     += _r["pnl"]
            _settle["gross"]   += _r["gross"] if _r["gross"] is not None else _r["pnl"]
            _settle["charges"] += _r["charges"] or 0
        _settle["count"]    = len(_rows)
        _settle["tax_prov"] = round(max(0.0, _settle["net"]) * 0.30, 2)
        if _rows:
            _lr = _rows[-1]
            _settle["last"] = {"time": _lr["time"], "pnl": _lr["pnl"],
                               "gross": _lr["gross"], "charges": _lr["charges"],
                               "status": _lr["status"], "otype": _lr["option_type"]}
        for k in ("gross", "charges", "net"):
            _settle[k] = round(_settle[k], 2)
    except Exception:
        pass
    return jsonify({
        "settlement":         _settle,
        "nifty_price":        state["nifty_price"],
        "score":              state["score"],
        "score_breakdown":    state["score_breakdown"],
        "bull_score":         state.get("bull_score", 0),
        "bull_breakdown":     state.get("bull_breakdown", {}),
        "bear_score":         state.get("bear_score", 0),
        "bear_breakdown":     state.get("bear_breakdown", {}),
        "active_side":        state.get("active_side"),
        "open_positions":     state["open_positions"],
        "daily_pnl":          state["daily_pnl"],
        "market_open":        state["market_open"],
        "log":                state["log"][:30],
        "paper_trade":        PAPER_TRADE,
        "trade_mode":         state.get("trade_mode", "paper"),
        "live_enabled":       LIVE_ENABLED,
        "vix":                state.get("vix"),
        "supertrend_bullish": state.get("supertrend_bullish"),
        "expiry":             state.get("expiry"),
        "available_expiries": state.get("available_expiries", []),
        "expiry_index":       EXPIRY_INDEX,
        "option_type":        state.get("option_type", "—"),
        "inst_scores":        state.get("inst_scores", {}),
        "paper_capital":      PAPER_CAPITAL,
        "threshold":          0,
        "unrealized_pnl":     state.get("unrealized_pnl", 0),
        "total_pnl":          state.get("total_pnl", 0),
        "first_trade_done":   state.get("first_trade_done", False),
        "signal":             state.get("signal", "--"),
        "yd_high":            state.get("yd_high"),
        "yd_low":             state.get("yd_low"),
        "lots_today":         state.get("lots_today", BASE_LOTS),
        "running_capital":    state.get("running_capital", float(PAPER_CAPITAL)),
        "streak":             get_streak(),
        "trades_today":       state.get("trades_today", 0),
        "max_trades":         MAX_TRADES_PER_DAY,
        "pending_trade":      bool(state.get("pending_trade")),
        "auto_mode":          state.get("auto_mode", True),
        "validation":         load_validation(),
        "or_hi":              state.get("or_hi"),
        "or_lo":              state.get("or_lo"),
        "day_hi":             state.get("day_hi"),
        "fade_trigger":       state.get("fade_trigger"),
        "mtf_ok":             state.get("mtf_ok"),
        "conf_score":         state.get("conf_score", 0.0),
        "conf_bd":            state.get("conf_bd", {}),
        "conf_score_tg":      CONF_SCORE_TG,
        "conf_score_auto":    CONF_SCORE_AUTO,
        "units_per_lot":      UNITS_PER_LOT,
        "daily_limit":        DAILY_LIMIT,
        "ensemble":           state.get("ensemble"),
    })

@app.route("/api/set_expiry", methods=["POST"])
def api_set_expiry():
    global EXPIRY_INDEX
    from flask import request
    data = request.get_json(force=True) or {}
    idx  = int(data.get("index", 0))
    EXPIRY_INDEX = max(0, idx)
    return jsonify({"ok": True, "expiry_index": EXPIRY_INDEX})

@app.route("/api/optionchain/<symbol>")
def api_optionchain(symbol):
    data = fetch_nse_optionchain(symbol.upper())
    if not data:
        return jsonify({"error": "NSE option chain unavailable — market may be closed or NSE rate-limited."})
    spot = state.get("nifty_price")
    if isinstance(spot, (int, float)):
        metrics = calculate_oi_metrics(data, spot)
        if metrics:
            return jsonify(metrics)
    return jsonify({"error": "Could not calculate metrics"})

@app.route("/api/vix")
def api_vix():
    v = fetch_vix()
    return jsonify({"vix": v})

@app.route("/api/intraday")
def api_intraday():
    """Today's 5-min candles + position levels for ALL 3 instruments."""
    result = {}
    positions_list = state.get("positions_list", [])
    inst_scores    = state.get("inst_scores", {})

    for inst in INSTRUMENTS:
        try:
            df = yf.download(inst["yf"], period="1d", interval="5m", progress=False)
            df = df.reset_index()
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0].lower().replace(" ", "") for c in df.columns]
            else:
                df.columns = [str(c).lower().replace(" ", "") for c in df.columns]
            tcol   = next((c for c in df.columns if c in ("datetime", "date", "timestamp")), df.columns[0])
            times  = df[tcol].dt.strftime("%H:%M").tolist()
            closes = [round(float(v), 2) for v in df["close"].tolist()]
            live_px = inst_scores.get(inst["name"], {}).get("price", closes[-1] if closes else 0)

            mult = inst["delta"] * inst["lot"]
            pos_data = []
            for pos in positions_list:
                if pos.get("instrument", "NIFTY") != inst["name"]:
                    continue
                entry  = pos.get("entry")
                if not entry:
                    continue   # skip corrupt position
                # Use per-position lot/delta if stored, else fall back to instrument defaults
                p_lot   = pos.get("lot",   inst["lot"])
                p_delta = pos.get("delta", inst["delta"])
                p_mult  = p_delta * p_lot
                trail  = pos.get("trail_stop")
                otype  = pos.get("option_type", "CE")
                p_sl   = -abs(pos.get("sl_rs", abs(STOP_LOSS)))   # per-trade dynamic SL
                if otype == "CE":
                    sl_p = round(entry + (p_sl + BROKERAGE) / p_mult, 1)
                    tp_p = None   # no fixed TP — let winners run
                    tr_p = round(entry + (trail + BROKERAGE) / p_mult, 1) if trail is not None else None
                else:
                    sl_p = round(entry - (p_sl + BROKERAGE) / p_mult, 1)
                    tp_p = None   # no fixed TP — let winners run
                    tr_p = round(entry - (trail + BROKERAGE) / p_mult, 1) if trail is not None else None
                strike        = pos.get("strike")
                premium_entry = pos.get("premium_entry")
                # Fetch live premium for all instruments (NSE for NIFTY/BANKNIFTY, VIX estimate for SENSEX)
                live_premium = None
                if strike:
                    live_premium = fetch_option_premium(pos.get("instrument", inst["name"]), strike, otype, live_px)
                pos_data.append({
                    "entry":         round(entry, 1),
                    "sl_price":      sl_p,
                    "tp_price":      tp_p,
                    "trail_price":   tr_p,
                    "trail_stop_pnl": trail,
                    "score":         pos.get("score", 0),
                    "option_type":   otype,
                    "live_price":    live_px,
                    "expiry":        pos.get("expiry") or get_next_expiry(pos.get("instrument","NIFTY"), trading_expiry_index()),
                    "strike":        strike,
                    "premium_entry": premium_entry,
                    "live_premium":  live_premium,
                    "peak_pnl":      pos.get("peak_pnl", None),
                    "stair_floor":   pos.get("stair_floor"),
                    "lot":           p_lot,
                    "sl_rs":         abs(p_sl),
                    "conf":          pos.get("score", 0),
                })
            result[inst["name"]] = {"times": times, "closes": closes,
                                     "live_price": live_px, "positions": pos_data}
        except Exception as e:
            result[inst["name"]] = {"times": [], "closes": [], "live_price": 0,
                                     "positions": [], "error": str(e)}
    # Today's executed trades — drawn as entry/exit markers on the chart
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT time, entry, exit, pnl, option_type, status FROM trades WHERE date=? ORDER BY id",
            (date.today().strftime("%Y-%m-%d"),)).fetchall()
        conn.close()
        result["today_trades"] = [dict(r) for r in rows]
    except Exception:
        result["today_trades"] = []
    return jsonify(result)

@app.route("/api/practice")
def api_practice():
    """Compare actual today vs v5.1 clean simulation."""
    try:
        conn = get_db()
        actual_trades = conn.execute(
            "SELECT time, entry, exit, pnl, status FROM trades WHERE date=? ORDER BY id",
            (date.today().strftime("%Y-%m-%d"),)).fetchall()
        conn.close()
        actual_net = sum(t["pnl"] for t in actual_trades)
        actual_count = len(actual_trades)

        # Practice: what v5.0 would have done (pre-computed from backtest)
        # For now, hardcode today's replay results — can be made dynamic later
        practice_net = 10898.01
        practice_count = 9
        practice_trades = [
            {"time": "10:25", "otype": "PE", "entry": 24251, "exit": 24244, "status": "STAIR", "pnl": 455},
            {"time": "10:35", "otype": "PE", "entry": 24244, "exit": 24283, "status": "SL", "pnl": -663},
            {"time": "12:15", "otype": "PE", "entry": 24278, "exit": 24230, "status": "STAIR", "pnl": 1304},
            {"time": "12:40", "otype": "PE", "entry": 24230, "exit": 24205, "status": "STAIR", "pnl": 855},
            {"time": "13:00", "otype": "PE", "entry": 24205, "exit": 24225, "status": "SL", "pnl": -513},
            {"time": "13:35", "otype": "PE", "entry": 24217, "exit": 24214, "status": "STAIR", "pnl": 106},
            {"time": "14:15", "otype": "PE", "entry": 24214, "exit": 23910, "status": "STAIR", "pnl": 8045},
            {"time": "14:30", "otype": "PE", "entry": 23910, "exit": 23883, "status": "STAIR", "pnl": 855},
            {"time": "14:40", "otype": "PE", "entry": 23883, "exit": 23863, "status": "STAIR", "pnl": 455},
        ]

        combined_net = actual_net + practice_net
        return jsonify({
            "actual": {
                "net": round(actual_net, 2),
                "count": actual_count,
                "trades": [dict(t) for t in actual_trades]
            },
            "practice_v45": {
                "net": practice_net,
                "count": practice_count,
                "trades": practice_trades
            },
            "combined": {
                "net": round(combined_net, 2),
                "label": "Actual + Practice v5.1"
            },
            "improvement": round(practice_net - actual_net, 2),
            "label": "v5.1 Clean All Day (ATR 1.5x SL + Dense Rungs + Daily Limit -1000 + 30min cooldown)"
        })
    except Exception as e:
        return jsonify({"error": str(e)})

# ── FLASK ROUTES ──────────────────────────────────────────────────────────────
@app.route("/")
def dashboard():
    conn   = get_db()
    trades = conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT 50").fetchall()
    trades = [dict(t) for t in trades]
    conn.close()

    # Stats
    total_trades = len(trades)
    wins         = sum(1 for t in trades if t["pnl"] > 0)
    win_rate     = round(wins / total_trades * 100, 1) if total_trades > 0 else 0

    # Daily P&L from trades table (today)
    today_str  = date.today().strftime("%Y-%m-%d")
    today_pnl  = sum(t["pnl"] for t in trades if t["date"] == today_str)

    # Calendar for current month
    now         = datetime.now()
    cal_month   = now.strftime("%B %Y")
    first_day   = date(now.year, now.month, 1)
    days_in_month = calendar.monthrange(now.year, now.month)[1]

    # Daily P&L per day from trades
    conn      = get_db()
    daily_rows = conn.execute(
        "SELECT date, SUM(pnl) as total FROM trades GROUP BY date"
    ).fetchall()
    conn.close()
    daily_map = {r["date"]: round(r["total"], 0) for r in daily_rows}

    # Build calendar cells
    cal_cells  = []
    start_dow  = first_day.weekday()   # 0=Mon
    for _ in range(start_dow):
        cal_cells.append({"type": "empty"})
    for day in range(1, days_in_month + 1):
        d       = date(now.year, now.month, day)
        d_str   = d.strftime("%Y-%m-%d")
        is_today = (d == date.today())
        is_we    = d.weekday() >= 5
        if is_we:
            cal_cells.append({"type": "holiday", "day": day, "today": is_today})
        elif d_str in daily_map:
            pnl  = daily_map[d_str]
            kind = "profit" if pnl >= 0 else "loss"
            cal_cells.append({"type": kind, "day": day, "pnl": int(pnl), "today": is_today})
        elif d > date.today():
            cal_cells.append({"type": "future", "day": day, "today": False})
        else:
            cal_cells.append({"type": "holiday", "day": day, "today": is_today})

    return render_template("index.html",
        nifty_price    = state["nifty_price"],
        score          = state["score"],
        open_positions = state["open_positions"],
        daily_pnl      = int(state["daily_pnl"]),
        market_open    = state["market_open"],
        log_entries    = state["log"],
        trades         = trades,
        total_trades   = total_trades,
        win_rate       = win_rate,
        calendar       = cal_cells,
        cal_month      = cal_month,
    )

# ── START ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    threading.Thread(target=bot_loop, daemon=True).start()
    threading.Thread(target=tg_poll,  daemon=True).start()
    threading.Thread(target=validation_thread, daemon=True).start()
    tg_ready = "✅ Token loaded" if _tg_token else "⚠️  No token (create telegram_token.txt)"
    print("\n" + "="*50)
    print("  Fluno Trading Bot is running!")
    print("  Open your browser at: http://localhost:5000")
    print(f"  Telegram: {tg_ready}")
    print("="*50 + "\n")
    app.run(host="127.0.0.1", port=5000, debug=False)
