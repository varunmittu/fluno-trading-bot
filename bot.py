"""
Fluno Trading Bot — NIFTY Options
Paper trade mode is ON by default. Set PAPER_TRADE = False only after validation.
Run: python bot.py
"""

import subprocess, sys
subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "kiteconnect", "pandas", "numpy", "yfinance"], stdout=subprocess.DEVNULL)

import sqlite3, time, os
from datetime import datetime, date, timedelta
import pandas as pd
import numpy as np
import yfinance as yf
from kiteconnect import KiteConnect

# ── CONFIG ────────────────────────────────────────────────────────────────────
API_KEY        = "your_api_key_here"  # paste from developers.kite.trade
TOKEN_FILE     = "kite_token.txt"
PAPER_TRADE    = True          # Set False only after paper trading validates strategy
THRESHOLD      = 55
STOP_LOSS      = -150          # Rs. per trade
TAKE_PROFIT    = 1200          # Rs. per trade
DAILY_LIMIT    = -500          # Rs. per day — bot stops if hit
MAX_POSITIONS  = 2
CHECK_INTERVAL = 300           # seconds (5 min)
NIFTY_TOKEN    = 256265        # NSE:NIFTY 50 instrument token
DELTA          = 0.40          # Options delta approximation
LOT            = 25            # NIFTY lot size
BROKERAGE      = 20            # Rs. per trade
# ─────────────────────────────────────────────────────────────────────────────

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

# ── DATABASE ──────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect("trade_log.db")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            date      TEXT,
            time      TEXT,
            score     INTEGER,
            entry     REAL,
            exit      REAL,
            pnl       REAL,
            status    TEXT,
            mode      TEXT
        )
    """)
    conn.commit()
    return conn

def save_trade(conn, score, entry, exit_price, pnl, status):
    now = datetime.now()
    mode = "PAPER" if PAPER_TRADE else "LIVE"
    conn.execute(
        "INSERT INTO trades (date, time, score, entry, exit, pnl, status, mode) VALUES (?,?,?,?,?,?,?,?)",
        (now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"), score, entry, exit_price, pnl, status, mode)
    )
    conn.commit()

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

def confidence_score(row, prev):
    score = 0
    if row['rsi'] < 45:                                              score += 20
    if prev['macd'] < prev['macd_sig'] and row['macd'] > row['macd_sig']: score += 15
    if row['volume'] > row['vol_avg'] * 1.2:                         score += 5
    if row['sma20'] > row['sma50'] > row['sma200']:                  score += 20
    if row['close'] > row['sma50']:                                  score += 10
    if row['sma20'] > prev['sma20']:                                 score += 5
    return score

# ── MARKET DATA (yfinance — free, no Kite subscription needed) ────────────────
def fetch_candles():
    df = yf.download("^NSEI", period="3y", interval="1d", progress=False)
    df = df.reset_index()
    df.columns = ['datetime', 'open', 'high', 'low', 'close', 'volume']
    df['rsi']     = rsi(df['close'])
    df['sma20']   = df['close'].rolling(20).mean()
    df['sma50']   = df['close'].rolling(50).mean()
    df['sma200']  = df['close'].rolling(200).mean()
    m, s          = macd(df['close'])
    df['macd']    = m
    df['macd_sig']= s
    df['vol_avg'] = df['volume'].rolling(20).mean()
    return df.dropna().reset_index(drop=True)

def live_price():
    ticker = yf.Ticker("^NSEI")
    hist   = ticker.history(period="1d", interval="1m")
    if len(hist) == 0:
        return None
    return float(hist['Close'].iloc[-1])

# ── MARKET HOURS ──────────────────────────────────────────────────────────────
def is_market_open():
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    open_t  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=25, second=0, microsecond=0)
    return open_t <= now <= close_t

# ── MAIN LOOP ─────────────────────────────────────────────────────────────────
def main():
    if not os.path.exists(TOKEN_FILE):
        print("ERROR: kite_token.txt not found. Run kite_setup.py first.")
        sys.exit(1)

    with open(TOKEN_FILE) as f:
        access_token = f.read().strip()

    kite = KiteConnect(api_key=API_KEY)
    kite.set_access_token(access_token)

    try:
        profile = kite.profile()
        log(f"Connected as {profile['user_name']}")
    except Exception as e:
        print(f"ERROR: Could not connect — {e}")
        print("Your access token may have expired. Run kite_setup.py again.")
        sys.exit(1)

    conn = init_db()
    mode_label = "PAPER TRADE" if PAPER_TRADE else "LIVE TRADE"
    log(f"Bot started | Mode: {mode_label} | Threshold: {THRESHOLD} | Stop loss: Rs.{STOP_LOSS}")

    positions  = []   # list of {entry_price, score}
    daily_pnl  = 0.0
    today      = date.today()

    while True:
        # Reset daily P&L at start of new day
        if date.today() != today:
            daily_pnl = 0.0
            today     = date.today()
            positions = []
            log("New trading day — P&L reset.")

        if not is_market_open():
            log("Market closed. Waiting...")
            time.sleep(60)
            continue

        if daily_pnl <= DAILY_LIMIT:
            log(f"Daily loss limit hit (Rs.{daily_pnl:.0f}). Bot stopped for today.")
            time.sleep(300)
            continue

        try:
            df    = fetch_candles()
            price = live_price()
            if price is None:
                log("Could not fetch live price. Retrying...")
                time.sleep(60)
                continue

            # Check existing positions for stop-loss / take-profit
            closed = []
            for pos in positions:
                move = (price - pos['entry']) * DELTA * LOT
                pnl  = move - BROKERAGE
                if pnl <= STOP_LOSS:
                    pnl = STOP_LOSS
                    save_trade(conn, pos['score'], pos['entry'], price, pnl, "STOP_LOSS")
                    daily_pnl += pnl
                    log(f"STOP LOSS hit | Entry: {pos['entry']:.0f} | Exit: {price:.0f} | P&L: Rs.{pnl:.0f} | Daily: Rs.{daily_pnl:.0f}")
                    closed.append(pos)
                elif pnl >= TAKE_PROFIT:
                    pnl = TAKE_PROFIT
                    save_trade(conn, pos['score'], pos['entry'], price, pnl, "TAKE_PROFIT")
                    daily_pnl += pnl
                    log(f"TAKE PROFIT hit | Entry: {pos['entry']:.0f} | Exit: {price:.0f} | P&L: Rs.{pnl:.0f} | Daily: Rs.{daily_pnl:.0f}")
                    closed.append(pos)
            for pos in closed:
                positions.remove(pos)

            # Check for new entry signal
            if len(positions) < MAX_POSITIONS:
                row  = df.iloc[-1]
                prev = df.iloc[-2]
                score = confidence_score(row, prev)
                log(f"NIFTY: {price:.0f} | Score: {score} | Open positions: {len(positions)}")

                if score >= THRESHOLD:
                    positions.append({'entry': price, 'score': score})
                    action = "[PAPER] BUY CE" if PAPER_TRADE else "BUY CE"
                    log(f"{action} | NIFTY: {price:.0f} | Score: {score} | Positions: {len(positions)}/{MAX_POSITIONS}")
            else:
                log(f"NIFTY: {price:.0f} | Max positions reached ({MAX_POSITIONS})")

        except Exception as e:
            log(f"Error: {e}")

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
