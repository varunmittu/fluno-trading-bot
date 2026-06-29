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
from datetime import datetime, date
import requests as req_lib
import pandas as pd
import numpy as np
import yfinance as yf
from kiteconnect import KiteConnect

app = Flask(__name__)

# ── CONFIG ────────────────────────────────────────────────────────────────────
API_KEY        = "your_api_key_here"  # paste from developers.kite.trade
TOKEN_FILE     = "kite_token.txt"
PAPER_TRADE    = True
THRESHOLD      = 55
STOP_LOSS      = -150
TAKE_PROFIT    = 1200
DAILY_LIMIT    = -500
MAX_POSITIONS  = 2
CHECK_INTERVAL = 300
DELTA          = 0.40
LOT            = 25
BROKERAGE      = 20
# ─────────────────────────────────────────────────────────────────────────────

# Shared state (bot thread writes, Flask reads)
state = {
    "nifty_price":       "--",
    "score":             0,
    "score_breakdown":   {},
    "open_positions":    [],
    "daily_pnl":         0.0,
    "market_open":       False,
    "log":               [],
    "vix":               None,
    "supertrend_bullish": None,
    "oi_nifty":          None,
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
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            date    TEXT,
            time    TEXT,
            score   INTEGER,
            entry   REAL,
            exit    REAL,
            pnl     REAL,
            status  TEXT,
            mode    TEXT
        )
    """)
    conn.commit()
    conn.close()

def save_trade(score, entry, exit_price, pnl, status):
    conn = get_db()
    mode = "PAPER" if PAPER_TRADE else "LIVE"
    now  = datetime.now()
    conn.execute(
        "INSERT INTO trades (date,time,score,entry,exit,pnl,status,mode) VALUES (?,?,?,?,?,?,?,?)",
        (now.strftime("%Y-%m-%d"), now.strftime("%H:%M"), score, entry, exit_price, round(pnl, 2), status, mode)
    )
    conn.commit()
    conn.close()

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

def confidence_score(row, prev):
    bd = {"rsi": 0, "macd": 0, "vol": 0, "sma": 0, "price": 0, "slope": 0}
    if row["rsi"] < 45:                                                    bd["rsi"]   = 20
    if prev["macd"] < prev["macd_sig"] and row["macd"] > row["macd_sig"]: bd["macd"]  = 15
    if row["volume"] > row["vol_avg"] * 1.2:                               bd["vol"]   = 5
    if row["sma20"] > row["sma50"] > row["sma200"]:                        bd["sma"]   = 20
    if row["close"] > row["sma50"]:                                         bd["price"] = 10
    if row["sma20"] > prev["sma20"]:                                        bd["slope"] = 5
    return sum(bd.values()), bd

# ── MARKET DATA ───────────────────────────────────────────────────────────────
def fetch_candles():
    df = yf.download("^NSEI", period="3y", interval="1d", progress=False)
    df = df.reset_index()
    df.columns = ["datetime","open","high","low","close","volume"]
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

def fetch_live_price():
    try:
        ticker = yf.Ticker("^NSEI")
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

def calculate_oi_metrics(data, spot_price):
    """Compute PCR, Max Pain, ATM strikes from option chain JSON."""
    if not data or "records" not in data:
        return None
    try:
        records = data["records"]["data"]
        expiry  = data["records"]["expiryDates"][0]
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
def sync_to_vercel():
    """Write trades.json and push to GitHub → Vercel redeploys in ~15 seconds."""
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
        subprocess.run(["git", "add", "web/trades.json"], cwd=proj, capture_output=True)
        r = subprocess.run(["git", "commit", "-m", f"bot: sync {datetime.now().strftime('%H:%M')}"], cwd=proj, capture_output=True)
        if b"nothing to commit" in r.stdout:
            return  # no change, skip push
        push = subprocess.run(["git", "push", "origin", "main"], cwd=proj, capture_output=True)
        if push.returncode == 0:
            bot_log("Synced to Vercel — live in ~15s", "ok")
        else:
            bot_log(f"Vercel sync failed: {push.stderr.decode()[:80]}", "err")
    except Exception as e:
        bot_log(f"Vercel sync error: {e}", "err")

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
    bot_log(f"Bot started | {mode_label} | Threshold: {THRESHOLD}", "info")

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
    if positions:
        bot_log(f"Restored {len(positions)} open position(s) from disk.", "info")
    daily_pnl  = 0.0
    today      = date.today()
    last_sync  = 0   # epoch time of last periodic sync

    # Cache daily candles — refresh once per day only
    _candle_cache = {"date": None, "df": None}
    def get_candles():
        today_str = date.today().isoformat()
        if _candle_cache["date"] != today_str or _candle_cache["df"] is None:
            bot_log("Fetching daily candles from Yahoo...", "info")
            _candle_cache["df"]   = fetch_candles()
            _candle_cache["date"] = today_str
        return _candle_cache["df"]

    while True:
        if date.today() != today:
            daily_pnl = 0.0
            today     = date.today()
            positions = []
            bot_log("New trading day — P&L reset.", "info")

        market_open = is_market_open()
        state["market_open"]     = market_open
        state["open_positions"]  = len(positions)
        state["daily_pnl"]       = round(daily_pnl, 0)

        if not market_open:
            bot_log("Market closed. Waiting...")
            time.sleep(60)
            continue

        if daily_pnl <= DAILY_LIMIT:
            bot_log(f"Daily loss limit hit (Rs.{daily_pnl:.0f}). Stopped for today.", "err")
            time.sleep(300)
            continue

        # EOD exit — force-close all positions at 3:20 PM
        now_t = datetime.now()
        eod   = now_t.replace(hour=15, minute=20, second=0, microsecond=0)
        if positions and now_t >= eod:
            price = fetch_live_price() or 0
            for pos in positions:
                move = (price - pos["entry"]) * DELTA * LOT
                pnl  = round(move - BROKERAGE, 2)
                save_trade(pos["score"], pos["entry"], price, pnl, "EOD_EXIT")
                daily_pnl += pnl
                bot_log(f"EOD EXIT | Entry:{pos['entry']:.0f} Exit:{price:.0f} P&L:Rs.{pnl:.0f}", "info")
            positions.clear()
            save_positions(positions)
            sync_background()
            time.sleep(60)
            continue

        try:
            price = fetch_live_price()
            if price is None:
                bot_log("Could not fetch live price. Retrying...", "err")
                time.sleep(60)
                continue

            state["nifty_price"] = price

            # Check open positions for stop-loss / take-profit / trailing stop
            closed = []
            for pos in positions:
                move = (price - pos["entry"]) * DELTA * LOT
                pnl  = move - BROKERAGE

                # Activate trailing stop once Rs.400 profit is reached
                if pnl >= 400 and pos.get("trail_stop") is None:
                    pos["trail_stop"] = 0      # lock in break-even
                    save_positions(positions)
                    bot_log(f"Trail stop ON — protecting breakeven | P&L:Rs.{pnl:.0f}", "info")
                elif pnl >= 700 and pos.get("trail_stop", -9999) < 200:
                    pos["trail_stop"] = 200    # lock in Rs.200 profit
                    save_positions(positions)
                    bot_log(f"Trail stop raised to +Rs.200 | P&L:Rs.{pnl:.0f}", "info")

                # Trail stop triggered
                if pos.get("trail_stop") is not None and pnl <= pos["trail_stop"]:
                    save_trade(pos["score"], pos["entry"], price, round(pnl, 2), "TRAIL_STOP")
                    daily_pnl += pnl
                    bot_log(f"TRAIL STOP | Entry:{pos['entry']:.0f} Exit:{price:.0f} P&L:Rs.{pnl:.0f}", "ok")
                    closed.append(pos); sync_background(); continue

                if pnl <= STOP_LOSS:
                    pnl = STOP_LOSS
                    save_trade(pos["score"], pos["entry"], price, pnl, "STOP_LOSS")
                    daily_pnl += pnl
                    bot_log(f"STOP LOSS | Entry:{pos['entry']:.0f} Exit:{price:.0f} P&L:Rs.{pnl:.0f}", "err")
                    closed.append(pos)
                    sync_background()
                elif pnl >= TAKE_PROFIT:
                    pnl = TAKE_PROFIT
                    save_trade(pos["score"], pos["entry"], price, pnl, "TAKE_PROFIT")
                    daily_pnl += pnl
                    bot_log(f"TAKE PROFIT | Entry:{pos['entry']:.0f} Exit:{price:.0f} P&L:Rs.{pnl:.0f}", "ok")
                    closed.append(pos)
                    sync_background()
            for pos in closed:
                positions.remove(pos)
            if closed:
                save_positions(positions)

            # Check for new entry (uses cached daily candles — only fetches once per day)
            df          = get_candles()
            row         = df.iloc[-1]
            prev        = df.iloc[-2]
            score, bkdn = confidence_score(row, prev)

            # Supertrend direction (1=bullish, -1=bearish)
            st_bullish = int(row["st_dir"]) == 1
            state["supertrend_bullish"] = st_bullish

            state["score"]           = score
            state["score_breakdown"] = bkdn
            state["open_positions"]  = len(positions)
            state["daily_pnl"]       = round(daily_pnl, 0)

            # VIX check — fetch once per loop, skip new entries if panic
            vix = fetch_vix()
            if vix:
                state["vix"] = vix

            # Option chain — fetch and cache for Vercel sync
            oc = fetch_nse_optionchain("NIFTY")
            if oc:
                metrics = calculate_oi_metrics(oc, price)
                if metrics:
                    state["oi_nifty"] = metrics

            # Entry decision: score + Supertrend + VIX filter
            if len(positions) < MAX_POSITIONS and score >= THRESHOLD:
                if state.get("vix") and state["vix"] > 25:
                    bot_log(f"VIX={state['vix']:.1f} — high volatility, NO new entry | Score:{score}/75", "err")
                elif not st_bullish:
                    bot_log(f"Score:{score}/75 but Supertrend BEARISH — skipping entry", "err")
                else:
                    positions.append({"entry": price, "score": score, "trail_stop": None})
                    save_positions(positions)
                    action = "[PAPER] BUY CE" if PAPER_TRADE else "BUY CE"
                    st_tag = "ST:Bullish"
                    bot_log(f"{action} | NIFTY:{price:.0f} Score:{score}/75 {st_tag} Pos:{len(positions)}/2", "ok")
                    sync_background()
            else:
                st_tag = "ST:Bull" if st_bullish else "ST:Bear"
                vix_tag = f" VIX:{state['vix']:.1f}" if state.get("vix") else ""
                bot_log(f"NIFTY:{price:.0f} Score:{score}/75 {st_tag}{vix_tag} Pos:{len(positions)}/2 Daily:Rs.{daily_pnl:.0f}")

            # Sync score/log to Vercel every 30 minutes even without a trade
            if time.time() - last_sync > 1800:
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
        "nifty_price":       state["nifty_price"],
        "score":             state["score"],
        "score_breakdown":   state["score_breakdown"],
        "open_positions":    state["open_positions"],
        "daily_pnl":         state["daily_pnl"],
        "market_open":       state["market_open"],
        "log":               state["log"][:30],
        "paper_trade":       PAPER_TRADE,
        "vix":               state.get("vix"),
        "supertrend_bullish":state.get("supertrend_bullish"),
    })

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
    t = threading.Thread(target=bot_loop, daemon=True)
    t.start()
    print("\n" + "="*50)
    print("  Fluno Trading Bot is running!")
    print("  Open your browser at: http://localhost:5000")
    print("="*50 + "\n")
    app.run(host="127.0.0.1", port=5000, debug=False)  # localhost only — not exposed to network
