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

app = Flask(__name__)

# ── CONFIG ────────────────────────────────────────────────────────────────────
API_KEY           = "your_api_key_here"  # paste from developers.kite.trade
TOKEN_FILE        = "kite_token.txt"
PAPER_TRADE       = True
STOP_LOSS             = -150   # hard SL — only fires BEFORE breakeven lock
BIG_TRAIL_START       = 1000   # let winners run — trail activates here
BIG_TRAIL_DROP        = 200    # close if profit drops Rs.200 from peak
SMALL_TRAIL_START     = 400    # tough day — lock small profit
SMALL_TRAIL_DROP      = 150    # close if drops Rs.150 from peak
BREAKEVEN_LOCK_START  = 300    # once peak hits Rs.300, move SL to +Rs.300
BREAKEVEN_LOCK_FLOOR  = 300    # guaranteed minimum exit after lock activates
DAILY_LIMIT       = -300   # safety net (1 trade/day so rarely hit)
PAPER_CAPITAL     = 10000  # starting capital
BASE_LOTS         = 3      # base = 3 lots (75 NIFTY units)
CAPITAL_PER_LOT   = PAPER_CAPITAL / BASE_LOTS  # Rs.3333 per lot
MAX_LOTS          = 15     # hard cap on lot scaling
MAX_POSITIONS     = 1      # one trade per day
CHECK_INTERVAL    = 60     # check every 1 minute
DELTA             = 0.40
LOT               = 75     # fallback units (3 lots × 25)
BROKERAGE         = 20
EXPIRY_INDEX      = 0      # 0=nearest weekly, 1=next week
BOT_STATE_FILE    = "bot_state.json"
INSTRUMENTS       = [
    {"name": "NIFTY", "yf": "^NSEI", "nse": "NIFTY", "lot": 75, "delta": 0.40},  # 3 lots
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
    for col in ["instrument TEXT DEFAULT 'NIFTY'", "option_type TEXT DEFAULT 'CE'"]:
        try:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {col}")
        except Exception:
            pass
    conn.commit()
    conn.close()

def save_trade(score, entry, exit_price, pnl, status, instrument="NIFTY", option_type="CE"):
    conn = get_db()
    mode = "PAPER" if PAPER_TRADE else "LIVE"
    now  = datetime.now()
    conn.execute(
        "INSERT INTO trades (date,time,score,entry,exit,pnl,status,mode,instrument,option_type) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (now.strftime("%Y-%m-%d"), now.strftime("%H:%M"), score, entry, exit_price, round(pnl, 2), status, mode, instrument, option_type)
    )
    conn.commit()
    conn.close()

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
                    vix  = state.get("vix")
                    pos_list = state.get("positions_list", [])
                    dpnl = state.get("daily_pnl", 0)
                    upnl = state.get("unrealized_pnl", 0)
                    done = state.get("first_trade_done", False)
                    lines = [f"NIFTY: {px}"]
                    if vix:
                        lines.append(f"VIX: {vix:.1f}  {'HIGH - caution' if vix > 20 else 'OK'}")
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
                    cap   = state.get("running_capital", PAPER_CAPITAL)
                    lots  = state.get("lots_today", BASE_LOTS)
                    units = lots * 25
                    gain  = cap - PAPER_CAPITAL
                    lines = [
                        "--- CAPITAL ---",
                        f"Starting: Rs.{PAPER_CAPITAL:.0f}",
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
                            move   = (px_now - pos["entry"]) * delta * lot if otype == "CE" \
                                     else (pos["entry"] - px_now) * delta * lot
                            pnl    = round(move - BROKERAGE, 0)
                            save_trade(pos["score"], pos["entry"], px_now, pnl, "MANUAL_EXIT", iname, otype)
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
                            units    = new_lots * 25
                            rs_per_pt = units * DELTA
                            tg_send(
                                f"Lot size set to {new_lots}L ({units} units)\n"
                                f"Rs./point: Rs.{rs_per_pt:.0f}\n"
                                f"Max loss per trade: Rs.{abs(STOP_LOSS)}\n"
                                f"Takes effect on next trade entry."
                            )

                elif cmd == "/stop":
                    state["first_trade_done"] = True
                    tg_send("Trading paused for today. Send /resume to re-enable.")

                elif cmd == "/resume":
                    state["first_trade_done"] = False
                    tg_send("Trading resumed. Bot will enter next valid signal.")

                elif cmd in ("/help", "/start"):
                    tg_send(
                        "Fluno Bot Commands:\n\n"
                        "/signal     — today's breakout signal\n"
                        "/status     — NIFTY price, VIX, position\n"
                        "/pnl        — today's trade result\n"
                        "/capital    — running capital and lot size\n"
                        "/history    — last 7 trades\n"
                        "/exit       — close open position NOW at market price\n"
                        "/lots 5     — set lot size (e.g. /lots 5 = 5 lots)\n"
                        "/stop       — pause trading today\n"
                        "/resume     — resume trading\n"
                        "/help       — this list"
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
    return df.dropna().reset_index(drop=True)

def fetch_live_price(yf_sym="^NSEI"):
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

def load_bot_state():
    """Load running capital and today's lot size from disk."""
    if os.path.exists(BOT_STATE_FILE):
        try:
            with open(BOT_STATE_FILE) as f:
                s = json.load(f)
            return float(s.get("running_capital", PAPER_CAPITAL)), int(s.get("lots_today", BASE_LOTS))
        except Exception:
            pass
    return float(PAPER_CAPITAL), BASE_LOTS

def save_bot_state(capital, lots):
    with open(BOT_STATE_FILE, "w") as f:
        json.dump({"running_capital": round(capital, 2), "lots_today": lots}, f)

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
    now    = datetime.now()
    if now.weekday() >= 5:
        return False
    open_t  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=25, second=0, microsecond=0)
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
    Try NSE option chain for actual LTP.
    Falls back to a VIX-based ATM estimate so the bot always gets a number.
    """
    import math
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

def get_next_expiry(inst_name, index=0):
    """Calculate nearest expiry: NIFTY=Thursday, BANKNIFTY=Wednesday, SENSEX=Friday."""
    weekday_map = {"NIFTY": 3, "BANKNIFTY": 2, "SENSEX": 4}  # Mon=0 … Sun=6
    target_wd   = weekday_map.get(inst_name, 3)
    today       = date.today()
    days_ahead  = (target_wd - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7   # today is expiry day → use next week
    first_exp   = today + timedelta(days=days_ahead)
    expiry      = first_exp + timedelta(weeks=index)
    return expiry.strftime("%d %b %Y")   # e.g. "03 Jul 2026"

EXPIRY_WEEKDAY = {"NIFTY": 3, "BANKNIFTY": 2, "SENSEX": 4}  # Thu, Wed, Fri

def is_expiry_day(inst_name):
    """True if today is weekly expiry for this instrument — theta risk too high for buying."""
    return date.today().weekday() == EXPIRY_WEEKDAY.get(inst_name, -1)

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
        }

        web_json = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "trades.json")
        with open(web_json, "w") as f:
            json.dump(payload, f, indent=2, default=str)

        proj = os.path.dirname(os.path.abspath(__file__))
        git  = r"C:\Program Files\Git\bin\git.exe"
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

def sync_background():
    threading.Thread(target=sync_to_vercel, daemon=True).start()

# ── BOT THREAD ────────────────────────────────────────────────────────────────
def bot_loop():
    init_db()

    # Connect to Kite (just for identity — data comes from yfinance)
    try:
        if os.path.exists(TOKEN_FILE):
            with open(TOKEN_FILE) as f:
                token = f.read().strip()
            kite = KiteConnect(api_key=API_KEY)
            kite.set_access_token(token)
            profile = kite.profile()
            bot_log(f"Connected as {profile['user_name']}", "ok")
        else:
            bot_log("kite_token.txt not found — run kite_setup.py", "err")
    except Exception as e:
        bot_log(f"Kite connection failed: {e}", "err")

    mode_label = "PAPER TRADE" if PAPER_TRADE else "LIVE TRADE"
    bot_log(f"Bot started | {mode_label} | Strategy: BREAKOUT | SL=Rs.{abs(STOP_LOSS)}", "info")

    # Load dynamic lot state (persists across restarts)
    running_capital, lots_today = load_bot_state()
    state["running_capital"] = running_capital
    state["lots_today"]      = lots_today
    bot_log(f"Capital: Rs.{running_capital:.0f} | Lots: {lots_today}L ({lots_today*25} units)", "info")

    # Restore open positions from disk (survives restarts)
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

    positions  = load_positions()
    # Backfill strike + premium for positions that predate the strike selector
    _changed = False
    for _p in positions:
        if not _p.get("strike"):
            _p["strike"] = get_target_strike(_p["entry"], _p.get("instrument","NIFTY"), _p.get("option_type","CE"))
            _p["premium_entry"] = fetch_option_premium(_p["instrument"], _p["strike"], _p["option_type"], _p["entry"])
            _changed = True
    if _changed:
        import json as _json
        with open(POSITIONS_FILE, "w") as _f:
            _json.dump(positions, _f)
    # DB-backed flag: survives restarts even if SL fired and position was removed
    first_trade_done = has_traded_today() or bool(positions)
    state["first_trade_done"] = first_trade_done
    if positions:
        bot_log(f"Restored {len(positions)} open position(s) from disk.", "info")
    elif first_trade_done:
        bot_log("Already traded today (from DB) — won't enter again.", "info")
    daily_pnl        = 0.0
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
        # Sync from state so Telegram /stop and /resume take effect next iteration
        first_trade_done = state.get("first_trade_done", first_trade_done)

        if date.today() != today:
            daily_pnl        = 0.0
            today            = date.today()
            positions        = []
            sl_cooldown      = {}
            first_trade_done = False
            state["first_trade_done"] = False
            state["signal"] = "--"
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
        if not morning_pinged and _now.hour == 9 and _now.minute >= 10:
            morning_pinged = True
            try:
                yd_h, yd_l = fetch_daily_hl("^NSEI")
                lines = ["Good morning! Market opens in 5 minutes."]
                if yd_h and yd_l:
                    lines.append(f"\nNIFTY Reference:")
                    lines.append(f"  Yday High : {yd_h:.0f}")
                    lines.append(f"  Yday Low  : {yd_l:.0f}")
                lines.append(f"\nCapital : Rs.{running_capital:.0f} | Lots: {lots_today}L ({lots_today*25} units)")
                lines.append(f"SL: Rs.{abs(STOP_LOSS)} | BE Lock at Rs.{BREAKEVEN_LOCK_FLOOR}")
                lines.append(f"\nPrediction at 10:15 AM:")
                if yd_h and yd_l:
                    lines.append(f"  BULLISH (BUY CE) — if NIFTY > {yd_h:.0f}")
                    lines.append(f"  BEARISH (BUY PE) — if NIFTY < {yd_l:.0f}")
                    lines.append(f"  MORNING DIR      — if inside range")
                tg_send("\n".join(lines))
                bot_log("Morning Telegram ping sent", "info")
            except Exception as _me:
                bot_log(f"Morning ping error: {_me}", "err")

        if not market_open:
            time.sleep(60)
            continue

        if daily_pnl <= DAILY_LIMIT:
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
                    move  = (px - pos["entry"]) * delta * lot if otype == "CE" \
                            else (pos["entry"] - px) * delta * lot
                    pnl   = round(move - BROKERAGE, 2)
                    save_trade(pos["score"], pos["entry"], px, pnl, "EOD_EXIT", iname, otype)
                    daily_pnl       += pnl
                    running_capital += pnl
                    if pnl > 0:
                        lots_today = min(MAX_LOTS, max(BASE_LOTS, int(running_capital // CAPITAL_PER_LOT)))
                    else:
                        lots_today = BASE_LOTS
                    save_bot_state(running_capital, lots_today)
                    state["running_capital"] = running_capital
                    state["lots_today"]      = lots_today
                    bot_log(f"EOD EXIT {iname} {otype} | Entry:{pos['entry']:.0f} Exit:{px:.0f} P&L:Rs.{pnl:.0f}", "info")
                positions.clear(); save_positions(positions); sync_background()

            # 3:30 PM — Zerodha-style settlement report (fires once per day)
            if now_t >= eod_settle and not eod_done:
                eod_done = True
                today_str = date.today().strftime("%Y-%m-%d")
                conn  = get_db()
                rows  = conn.execute(
                    "SELECT instrument,option_type,entry,exit,pnl,status,time FROM trades WHERE date=? ORDER BY id",
                    (today_str,)
                ).fetchall()
                conn.close()

                total_pnl = sum(r["pnl"] for r in rows)
                wins      = [r for r in rows if r["pnl"] > 0]
                losses    = [r for r in rows if r["pnl"] <= 0]
                result    = "PROFIT" if total_pnl > 0 else "LOSS" if total_pnl < 0 else "FLAT"

                lines = [
                    "=" * 30,
                    f"SETTLEMENT REPORT — {today_str}",
                    "=" * 30,
                ]
                for r in rows:
                    tag = "WIN " if r["pnl"] > 0 else "LOSS"
                    lines.append(
                        f"{tag} | {r['instrument']} {r['option_type']} | {r['time']}\n"
                        f"     Entry:{r['entry']:.0f}  Exit:{r['exit']:.0f}  "
                        f"Status:{r['status']}  P&L:Rs.{r['pnl']:.0f}"
                    )
                lines += [
                    "-" * 30,
                    f"Trades    : {len(rows)}  (W:{len(wins)} L:{len(losses)})",
                    f"Day P&L   : Rs.{total_pnl:+.0f}  [{result}]",
                    f"Capital   : Rs.{running_capital:.0f}",
                    f"Tomorrow  : {lots_today}L ({lots_today*25} units)",
                    "=" * 30,
                    "Settlement complete. T+1 credit by tomorrow morning.",
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
                                "=" * 28,
                                "WEEKLY SUMMARY",
                                f"{week_start} to {week_end}",
                                "=" * 28,
                                f"Trades   : {wn}  (W:{wwins} L:{wn-wwins})",
                                f"Win rate : {round(wwins/wn*100)}%",
                                f"Week P&L : Rs.{wtot:+.0f}",
                                f"Capital  : Rs.{running_capital:.0f}",
                                f"Next week: {lots_today}L ({lots_today*25} units)",
                                "=" * 28,
                            ]
                            tg_send("\n".join(wlines))
                            bot_log(f"WEEKLY SUMMARY sent | Week P&L:Rs.{wtot:+.0f}", "ok")
                    except Exception as _we:
                        bot_log(f"Weekly summary error: {_we}", "err")

                time.sleep(60); continue

            # ── 3. Check open positions (SL / BE lock / trail) ───────────────
            closed = []
            for pos in positions:
                iname = pos.get("instrument", "NIFTY")
                px    = inst_prices.get(iname)
                if not px:
                    continue
                lot   = pos.get("lot", LOT);  delta = pos.get("delta", DELTA)
                otype = pos.get("option_type", "CE")
                move  = (px - pos["entry"]) * delta * lot if otype == "CE" else (pos["entry"] - px) * delta * lot
                pnl   = move - BROKERAGE

                # ── Premium SL: exit if option lost 60% of entry value ────────
                if pos.get("strike") and pos.get("premium_entry"):
                    live_prem = fetch_option_premium(iname, pos["strike"], otype, px)
                    if live_prem and live_prem < pos["premium_entry"] * 0.40:
                        pnl_p = round((live_prem - pos["premium_entry"]) * lot - BROKERAGE, 2)
                        save_trade(pos["score"], pos["entry"], px, pnl_p, "PREM_SL", iname, otype)
                        daily_pnl += pnl_p
                        sl_cooldown[iname] = time.time()
                        bot_log(f"PREM SL {iname} {otype} {pos['strike']} | Rs.{pos['premium_entry']}→Rs.{live_prem:.1f} P&L:Rs.{pnl_p:.0f}", "err")
                        tg_send(f"PREMIUM SL — {iname} {otype}\nStrike: {pos.get('strike','')}\nPremium: Rs.{pos['premium_entry']} → Rs.{live_prem:.1f}\nP&L: Rs.{pnl_p:.0f}")
                        closed.append(pos); sync_background(); continue

                # ── Track peak P&L (updated every cycle) ──────────────────────
                if pnl > pos.get("peak_pnl", -9999):
                    pos["peak_pnl"] = pnl
                peak_pnl = pos.get("peak_pnl", 0)

                # ── Activate breakeven lock once peak hits Rs.300 ──────────────
                if peak_pnl >= BREAKEVEN_LOCK_START and not pos.get("be_locked"):
                    pos["be_locked"] = True
                    save_positions(positions)
                    bot_log(f"BE LOCK ON {iname} {otype} | Peak:Rs.{peak_pnl:.0f} — SL floor now Rs.{BREAKEVEN_LOCK_FLOOR}", "ok")
                    tg_send(
                        f"BE LOCK ACTIVE — {iname} {otype}\n"
                        f"Peak: Rs.{peak_pnl:.0f}\n"
                        f"SL floor raised to Rs.{BREAKEVEN_LOCK_FLOOR} — you cannot lose now!\n"
                        f"Still running... waiting for Rs.{BIG_TRAIL_START} to activate big trail."
                    )

                # ── 1. Breakeven lock exit (pnl drops below Rs.300 after lock) ─
                if pos.get("be_locked") and pnl < BREAKEVEN_LOCK_FLOOR:
                    exit_pnl = BREAKEVEN_LOCK_FLOOR   # guaranteed floor
                    save_trade(pos["score"], pos["entry"], px, exit_pnl, "BE_LOCK", iname, otype)
                    daily_pnl      += exit_pnl
                    running_capital += exit_pnl
                    lots_today = min(MAX_LOTS, max(BASE_LOTS, int(running_capital // CAPITAL_PER_LOT)))
                    save_bot_state(running_capital, lots_today)
                    state["running_capital"] = running_capital
                    state["lots_today"]      = lots_today
                    bot_log(f"BE LOCK EXIT {iname} {otype} | Peak:Rs.{peak_pnl:.0f} Booked:Rs.{exit_pnl} | Capital:Rs.{running_capital:.0f} Lots->{lots_today}L", "ok")
                    tg_send(
                        f"BE LOCK EXIT — {iname} {otype}\n"
                        f"Peak: Rs.{peak_pnl:.0f}  Booked: Rs.{exit_pnl} (guaranteed floor)\n"
                        f"Capital: Rs.{running_capital:.0f} | Next: {lots_today}L"
                    )
                    closed.append(pos); sync_background(); continue

                # ── 2. Hard SL — only fires before BE lock activates ───────────
                if not pos.get("be_locked") and pnl <= STOP_LOSS:
                    save_trade(pos["score"], pos["entry"], px, STOP_LOSS, "STOP_LOSS", iname, otype)
                    daily_pnl      += STOP_LOSS
                    running_capital += STOP_LOSS
                    lots_today      = BASE_LOTS
                    save_bot_state(running_capital, lots_today)
                    state["running_capital"] = running_capital
                    state["lots_today"]      = lots_today
                    sl_cooldown[iname] = time.time()
                    bot_log(f"STOP LOSS {iname} {otype} | Entry:{pos['entry']:.0f} Exit:{px:.0f} | Capital:Rs.{running_capital:.0f} Lots->{lots_today}L", "err")
                    tg_send(
                        f"STOP LOSS — {iname} {otype}\n"
                        f"Entry: {pos['entry']:.0f}  Exit: {px:.0f}\n"
                        f"Loss: Rs.{STOP_LOSS}\n"
                        f"Capital: Rs.{running_capital:.0f} | Back to {lots_today}L tomorrow"
                    )
                    closed.append(pos); sync_background(); continue

                # ── 3. Big trail: let winners run (exits at peak - Rs.200) ──────
                if peak_pnl >= BIG_TRAIL_START and pnl <= peak_pnl - BIG_TRAIL_DROP:
                    exit_pnl = round(pnl, 2)
                    save_trade(pos["score"], pos["entry"], px, exit_pnl, "TRAIL_EXIT", iname, otype)
                    daily_pnl      += exit_pnl
                    running_capital += exit_pnl
                    lots_today = min(MAX_LOTS, max(BASE_LOTS, int(running_capital // CAPITAL_PER_LOT)))
                    save_bot_state(running_capital, lots_today)
                    state["running_capital"] = running_capital
                    state["lots_today"]      = lots_today
                    bot_log(f"TRAIL EXIT {iname} {otype} | Peak:Rs.{peak_pnl:.0f} Booked:Rs.{exit_pnl:.0f} | Capital:Rs.{running_capital:.0f} Lots->{lots_today}L", "ok")
                    tg_send(
                        f"TRAIL EXIT — {iname} {otype}\n"
                        f"Peak: Rs.{peak_pnl:.0f}  Booked: Rs.{exit_pnl:.0f}\n"
                        f"Capital: Rs.{running_capital:.0f} | Next: {lots_today}L"
                    )
                    closed.append(pos); sync_background(); continue

                # ── 4. Small trail: range/tough days (floor = Rs.300 if locked) ─
                if peak_pnl >= SMALL_TRAIL_START and pnl <= peak_pnl - SMALL_TRAIL_DROP:
                    raw_pnl  = round(pnl, 2)
                    exit_pnl = max(BREAKEVEN_LOCK_FLOOR, raw_pnl) if pos.get("be_locked") else raw_pnl
                    status   = "PROFIT_LOCK" if exit_pnl > 0 else "STOP_LOSS"
                    save_trade(pos["score"], pos["entry"], px, exit_pnl, status, iname, otype)
                    daily_pnl      += exit_pnl
                    running_capital += exit_pnl
                    if exit_pnl > 0:
                        lots_today = min(MAX_LOTS, max(BASE_LOTS, int(running_capital // CAPITAL_PER_LOT)))
                    else:
                        lots_today = BASE_LOTS
                    save_bot_state(running_capital, lots_today)
                    state["running_capital"] = running_capital
                    state["lots_today"]      = lots_today
                    bot_log(f"PROFIT LOCK {iname} {otype} | Peak:Rs.{peak_pnl:.0f} Booked:Rs.{exit_pnl:.0f} | Capital:Rs.{running_capital:.0f}", "ok")
                    tg_send(
                        f"PROFIT LOCK — {iname} {otype}\n"
                        f"Peak: Rs.{peak_pnl:.0f}  Booked: Rs.{exit_pnl:.0f}\n"
                        f"Capital: Rs.{running_capital:.0f} | Next: {lots_today}L"
                    )
                    closed.append(pos); sync_background(); continue

                # ── Log riding status ──────────────────────────────────────────
                be_tag = " | BE LOCKED (floor Rs.300)" if pos.get("be_locked") else ""
                if peak_pnl >= BIG_TRAIL_START:
                    bot_log(f"RIDING {iname} {otype} | Peak:Rs.{peak_pnl:.0f} Now:Rs.{pnl:.0f} (big trail active){be_tag}", "ok")
                elif peak_pnl >= SMALL_TRAIL_START:
                    bot_log(f"RIDING {iname} {otype} | Peak:Rs.{peak_pnl:.0f} Now:Rs.{pnl:.0f} (small trail){be_tag}", "ok")
                elif pos.get("be_locked"):
                    bot_log(f"RIDING {iname} {otype} | Peak:Rs.{peak_pnl:.0f} Now:Rs.{pnl:.0f}{be_tag}", "ok")

            for pos in closed:
                positions.remove(pos)
            if closed:
                save_positions(positions)

            # ── 4. Update state ────────────────────────────────────────────────
            px_nifty = inst_prices.get("NIFTY")
            state["positions_list"]   = positions
            state["open_positions"]   = len(positions)
            state["daily_pnl"]        = round(daily_pnl, 0)
            state["first_trade_done"] = first_trade_done

            # Unrealized P&L
            unrealized = 0.0
            for _p in positions:
                _px    = inst_prices.get(_p.get("instrument","NIFTY"), _p["entry"])
                _lot   = _p.get("lot", LOT); _delta = _p.get("delta", DELTA)
                _otype = _p.get("option_type", "CE")
                _mv    = (_px - _p["entry"]) * _delta * _lot if _otype == "CE" else (_p["entry"] - _px) * _delta * _lot
                unrealized += _mv - BROKERAGE
            state["unrealized_pnl"] = round(unrealized, 0)
            state["total_pnl"]      = round(daily_pnl + unrealized, 0)
            state["active_side"]    = "BULL" if state.get("option_type") == "CE" else "BEAR" if state.get("option_type") == "PE" else None

            # NIFTY option chain for dashboard
            oc = fetch_nse_optionchain("NIFTY")
            if oc:
                all_exp = oc.get("records", {}).get("expiryDates", [])
                state["available_expiries"] = all_exp
                if px_nifty:
                    metrics = calculate_oi_metrics(oc, px_nifty, EXPIRY_INDEX)
                    if metrics:
                        state["oi_nifty"] = metrics
                        state["expiry"]   = metrics["expiry"]

            # ── 5. Breakout signal + entry ────────────────────────────────────
            prefix = "[PAPER]" if PAPER_TRADE else "[LIVE]"
            _now   = datetime.now()
            # Entry window: 10:15 AM to 12:30 PM (avoid late entries)
            _entry_allowed = (_now.hour > 10 or (_now.hour == 10 and _now.minute >= 15)) \
                             and (_now.hour < 12 or (_now.hour == 12 and _now.minute <= 30))
            entered = False

            if not first_trade_done and _entry_allowed and len(positions) < MAX_POSITIONS and px_nifty:
                if is_expiry_day("NIFTY"):
                    bot_log("SKIP — NIFTY expiry day (Thursday)", "info")
                elif "NIFTY" in sl_cooldown and time.time() - sl_cooldown["NIFTY"] < 600:
                    bot_log("NIFTY in SL cooldown — waiting", "info")
                else:
                    yd_high, yd_low = fetch_daily_hl("^NSEI")
                    state["yd_high"] = yd_high
                    state["yd_low"]  = yd_low

                    if yd_high and yd_low:
                        # Breakout signal
                        if px_nifty > yd_high:
                            otype  = "CE"
                            signal = f"BREAK HIGH {yd_high:.0f}"
                        elif px_nifty < yd_low:
                            otype  = "PE"
                            signal = f"BREAK LOW {yd_low:.0f}"
                        else:
                            otype  = fetch_morning_direction("^NSEI")
                            signal = f"MORN {'UP' if otype=='CE' else 'DN'} ({px_nifty:.0f})"

                        state["signal"] = signal
                        inst        = INSTRUMENTS[0]
                        current_lot = lots_today * 25   # 3 lots = 75 units
                        exp         = state.get("expiry") or get_next_expiry("NIFTY", EXPIRY_INDEX)
                        strike      = get_target_strike(px_nifty, "NIFTY", otype)
                        premium     = fetch_option_premium("NIFTY", strike, otype, px_nifty)

                        if premium and premium < 30:
                            bot_log(f"SKIP NIFTY {otype} {strike} — premium Rs.{premium:.0f} too low (<Rs.30)", "info")
                        else:
                            positions.append({
                                "instrument":    "NIFTY",
                                "lot":           current_lot,
                                "delta":         DELTA,
                                "entry":         px_nifty,
                                "score":         0,
                                "option_type":   otype,
                                "expiry":        exp,
                                "strike":        strike,
                                "premium_entry": premium,
                                "trail_stop":    None,
                                "peak_pnl":      -9999,
                            })
                            save_positions(positions)
                            first_trade_done = True
                            state["first_trade_done"] = True
                            state["option_type"]      = otype
                            direction_label = "BULLISH — BUY CALL (CE)" if otype == "CE" else "BEARISH — BUY PUT (PE)"
                            bot_log(f"{prefix} {direction_label} | NIFTY {strike} @ Rs.{premium} | {signal} | {lots_today}L ({current_lot}u) | Expiry:{exp}", "ok")
                            tg_send(
                                f"TRADE ENTERED — NIFTY {otype}\n"
                                f"Direction : {direction_label}\n"
                                f"Signal    : {signal}\n"
                                f"Strike    : {strike}  Premium: Rs.{premium}\n"
                                f"Lots      : {lots_today}L ({current_lot} units)  Expiry: {exp}\n"
                                f"SL        : Rs.{abs(STOP_LOSS)} | BE Lock at Rs.{BREAKEVEN_LOCK_FLOOR}\n"
                                f"Capital   : Rs.{running_capital:.0f}\n"
                                f"Mode      : {'PAPER' if PAPER_TRADE else 'LIVE'}"
                            )
                            sync_background()
                            entered = True

            if not entered and not first_trade_done:
                sig_tag = state.get("signal","--")
                yd_h    = state.get("yd_high")
                yd_l    = state.get("yd_low")
                hl_tag  = f" YdH:{yd_h:.0f}/YdL:{yd_l:.0f}" if yd_h else ""
                bot_log(f"NIFTY:{px_nifty:.0f}{hl_tag} Sig:{sig_tag} Lots:{lots_today}L Cap:Rs.{running_capital:.0f} Daily:Rs.{daily_pnl:.0f}")

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

@app.route("/api/state")
def api_state():
    return jsonify({
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
                if otype == "CE":
                    sl_p = round(entry + (STOP_LOSS + BROKERAGE) / p_mult, 1)
                    tp_p = None   # no fixed TP — let winners run
                    tr_p = round(entry + (trail + BROKERAGE) / p_mult, 1) if trail is not None else None
                else:
                    sl_p = round(entry - (STOP_LOSS + BROKERAGE) / p_mult, 1)
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
                    "expiry":        pos.get("expiry") or get_next_expiry(pos.get("instrument","NIFTY"), EXPIRY_INDEX),
                    "strike":        strike,
                    "premium_entry": premium_entry,
                    "live_premium":  live_premium,
                    "peak_pnl":      pos.get("peak_pnl", None),
                    "be_locked":     pos.get("be_locked", False),
                    "lot":           p_lot,
                })
            result[inst["name"]] = {"times": times, "closes": closes,
                                     "live_price": live_px, "positions": pos_data}
        except Exception as e:
            result[inst["name"]] = {"times": [], "closes": [], "live_price": 0,
                                     "positions": [], "error": str(e)}
    return jsonify(result)

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
    tg_ready = "✅ Token loaded" if _tg_token else "⚠️  No token (create telegram_token.txt)"
    print("\n" + "="*50)
    print("  Fluno Trading Bot is running!")
    print("  Open your browser at: http://localhost:5000")
    print(f"  Telegram: {tg_ready}")
    print("="*50 + "\n")
    app.run(host="127.0.0.1", port=5000, debug=False)
