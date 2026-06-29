"""
Real backtest using yfinance historical data.
Tests the trading strategy on actual NIFTY data.
Run: python backtest_real.py
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta

print("Installing dependencies...")
import subprocess
import sys
subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "yfinance"])

import yfinance as yf

# ============ 1. FETCH NIFTY DATA ============
print("\nFetching NIFTY 50 historical data (3 years)...")
nifty = yf.download("^NSEI", period="3y", interval="1d", progress=False)

print(f"Loaded {len(nifty)} candles")
nifty = nifty.reset_index()
nifty.columns = ['datetime', 'open', 'high', 'low', 'close', 'volume']
print(nifty.head())

# ============ 2. INDICATORS ============
def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def macd(series, fast=12, slow=26, signal=9):
    ema_fast = series.ewm(span=fast).mean()
    ema_slow = series.ewm(span=slow).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal).mean()
    return macd_line, signal_line

nifty['rsi'] = rsi(nifty['close'])
nifty['sma20'] = nifty['close'].rolling(20).mean()
nifty['sma50'] = nifty['close'].rolling(50).mean()
nifty['sma200'] = nifty['close'].rolling(200).mean()
macd_line, signal_line = macd(nifty['close'])
nifty['macd'] = macd_line
nifty['macd_signal'] = signal_line
nifty['vol_avg'] = nifty['volume'].rolling(20).mean()

# ============ 3. CONFIDENCE SCORE ============
def confidence_score(row, prev_row):
    """Calculate confidence score (0-100)"""
    score = 0
    
    # Technical: 40 pts max
    if row['rsi'] < 45:
        score += 20
    if prev_row['macd'] < prev_row['macd_signal'] and row['macd'] > row['macd_signal']:
        score += 15
    if row['volume'] > row['vol_avg'] * 1.2:
        score += 5
    
    # Trend: 35 pts max
    if row['sma20'] > row['sma50'] > row['sma200']:
        score += 20
    if row['close'] > row['sma50']:
        score += 10
    if row['sma20'] > prev_row['sma20']:
        score += 5
    
    return score

# ============ 4. SIMULATE TRADES ============
CONFIDENCE_THRESHOLD = 55
STOP_LOSS = -150
TAKE_PROFIT = 1200
DAILY_LOSS_LIMIT = -500

trades = []

for i in range(1, len(nifty)):
    row = nifty.iloc[i]
    prev_row = nifty.iloc[i-1]
    
    if pd.isna(row['sma200']):
        continue
    
    score = confidence_score(row, prev_row)
    
    if score >= CONFIDENCE_THRESHOLD:
        entry_price = row['close']
        pnl = 0
        
        # Simulate order with Delta approximation
        DELTA = 0.40  # Options move ~40% of underlying
        LOT = 25
        
        # Walk forward to find exit
        for j in range(i + 1, min(i + 100, len(nifty))):
            move_pts = (nifty.iloc[j]['close'] - entry_price)
            pnl = move_pts * DELTA * LOT
            
            if pnl <= STOP_LOSS:
                pnl = STOP_LOSS
                break
            if pnl >= TAKE_PROFIT:
                pnl = TAKE_PROFIT
                break
        
        pnl -= 20  # Brokerage
        trades.append({
            'date': row['datetime'],
            'score': score,
            'entry': entry_price,
            'pnl': pnl
        })

# ============ 5. RESULTS ============
trades_df = pd.DataFrame(trades)

print("\n" + "="*60)
print("BACKTEST RESULTS — REAL NIFTY DATA")
print("="*60)

if len(trades_df) == 0:
    print("No trades triggered.")
else:
    wins = (trades_df['pnl'] > 0).sum()
    losses = (trades_df['pnl'] <= 0).sum()
    win_rate = (wins / len(trades_df)) * 100 if len(trades_df) > 0 else 0
    
    print(f"Total trades:           {len(trades_df)}")
    print(f"Winning trades:         {wins}")
    print(f"Losing trades:          {losses}")
    print(f"Win rate:               {win_rate:.1f}%")
    print(f"Total P&L:              Rs.{trades_df['pnl'].sum():.0f}")
    print(f"Avg P&L per trade:      Rs.{trades_df['pnl'].mean():.0f}")
    print(f"Avg win:                Rs.{trades_df[trades_df['pnl']>0]['pnl'].mean():.0f}")
    print(f"Avg loss:               Rs.{trades_df[trades_df['pnl']<=0]['pnl'].mean():.0f}")
    
    if win_rate >= 65:
        print("\n[GOOD] Strategy shows GOOD win rate (65%+). Proceed to paper trading.")
    elif win_rate >= 55:
        print("\n[OK]  Strategy shows MODERATE win rate (55-65%). Monitor closely.")
    else:
        print("\n[BAD] Strategy shows LOW win rate (<55%). Needs adjustment before live trading.")

print("="*60)
