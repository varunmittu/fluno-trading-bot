"""
ENSEMBLE COUNCIL (v5.0, built 2026-07-20, Sai's request: "6 mini bots analyse
market/chart, mother bot decides buy call or put").

Six specialist mini-bots, each looking at a different angle of the SAME 5-min
candle data the live bot already fetches (fetch_candles() in app.py already
computes rsi/sma20/sma50/sma200/macd/macd_sig/vol_avg/st_dir/atr14 per row —
no new data source needed for bots 1-4/6). Mini-bot 5 (volatility/risk) also
uses India VIX, which the bot already fetches via fetch_vix() but never used
in trading decisions before now.

HONEST NOTE for Sai: there is no live news-headline feed wired in (would need
a paid/free news API key — happy to add one if you get a key from
newsapi.org or similar). "News bot" here is the VOLATILITY bot: it reads
India VIX (the market's real-time fear gauge — this moves BECAUSE of news/
global events, so it's a legitimate stand-in) plus how wide the candles are
right now vs normal. It doesn't pick a side; its job is purely to turn DOWN
confidence when conditions look choppy/dangerous, which is the "reduce risk
of losing money" part of the ask.

MOTHER BOT: every mini-bot always returns a vote (CE or PE) + a confidence
0.0-1.0 for that vote (mirrors how bull_confidence/bear_confidence already
work in app.py). breakout_bot's vote is fixed by the existing, PROVEN yd-hi/
yd-lo/opening-range/fade ladder from app.py (v4.2-v4.5, backtested many
times) — the ensemble does NOT replace that trigger, it adds 5 more
independent opinions ON TOP of it, weighted, to size up confidence better
and catch cases where the breakout looks good on price alone but everything
else disagrees (the "reduce losses" case).

WEIGHTS are hand-set defaults, tunable via ensemble_weights.json (written by
backtest_ensemble.py's grid search — that's the "AI" here: a data-fitted
weighted-vote model, not a black box. Every number is explainable to Sai.)
"""
import json
import os

import numpy as np
import pandas as pd

WEIGHTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ensemble_weights.json")

DEFAULT_WEIGHTS = {
    "breakout":   1.6,   # the proven trigger — highest trust
    "trend":      1.2,
    "momentum":   1.0,
    "volume":     0.6,
    "pattern":    0.5,   # candlestick reads are noisy on an index — low weight
}


def load_weights():
    try:
        with open(WEIGHTS_FILE) as f:
            w = json.load(f)
        return {**DEFAULT_WEIGHTS, **w}
    except Exception:
        return dict(DEFAULT_WEIGHTS)


def save_weights(w):
    with open(WEIGHTS_FILE, "w") as f:
        json.dump(w, f, indent=2)


# ─────────────────────────────────────────────────────────────────────────
# MINI-BOT 1: TREND BOT
# 5m supertrend + 15m supertrend + SMA20-vs-50 + price-vs-SMA200 (long-term
# context — computed by fetch_candles but never used by any entry logic
# before now).
# ─────────────────────────────────────────────────────────────────────────
def trend_bot(row, st15, macd15_up):
    # NOTE: pandas/numpy comparisons return numpy.bool_, and numpy.bool_(True)
    # is NOT `is True` in Python (different object/type) — always bool(cond)
    # a condition before branching on it, never compare with `is`.
    checks_ce = checks_pe = 0.0
    total = 0.0
    def tick(cond, weight=1.0):
        nonlocal checks_ce, checks_pe, total
        total += weight
        if bool(cond): checks_ce += weight
        else:          checks_pe += weight

    tick(row["st_dir"] == 1)
    if st15 is not None:
        tick(st15)
    tick(row["sma20"] > row["sma50"])
    if not np.isnan(row.get("sma200", np.nan)):
        tick(row["close"] > row["sma200"], weight=0.5)   # long-term context, lighter weight

    if total == 0:
        return "CE", 0.0, "no data"
    if checks_ce >= checks_pe:
        conf = checks_ce / total
        return "CE", round(conf, 3), f"{checks_ce:g}/{total:g} trend checks bullish"
    conf = checks_pe / total
    return "PE", round(conf, 3), f"{checks_pe:g}/{total:g} trend checks bearish"


# ─────────────────────────────────────────────────────────────────────────
# MINI-BOT 2: MOMENTUM BOT
# MACD level + MACD histogram SLOPE (accelerating/decelerating — new, the
# old 9-point score only checked level) + RSI level + RSI slope.
# ─────────────────────────────────────────────────────────────────────────
def momentum_bot(df):
    row, prev = df.iloc[-1], df.iloc[-2]
    hist      = row["macd"] - row["macd_sig"]
    hist_prev = prev["macd"] - prev["macd_sig"]
    rsi_now, rsi_prev = row["rsi"], prev["rsi"]

    ce = pe = total = 0.0
    def tick(cond, weight=1.0):
        nonlocal ce, pe, total
        total += weight
        if bool(cond): ce += weight
        else:          pe += weight

    tick(row["macd"] > row["macd_sig"])          # MACD level
    tick(hist > hist_prev, weight=0.7)            # MACD momentum accelerating up
    if not np.isnan(rsi_now):
        tick(rsi_now < 50)                        # RSI has room to run up (CE) / down (PE)
        if not np.isnan(rsi_prev):
            tick(rsi_now > rsi_prev, weight=0.5)  # RSI rising = bullish momentum building

    if total == 0:
        return "CE", 0.0, "no data"
    if ce >= pe:
        return "CE", round(ce / total, 3), f"MACD/RSI momentum bullish ({ce:g}/{total:g})"
    return "PE", round(pe / total, 3), f"MACD/RSI momentum bearish ({pe:g}/{total:g})"


# ─────────────────────────────────────────────────────────────────────────
# MINI-BOT 3: VOLUME BOT
# Volume spike size AND whether the spike candle itself was green or red —
# the old score only checked spike size, not direction.
# ─────────────────────────────────────────────────────────────────────────
def volume_bot(row):
    if np.isnan(row.get("vol_avg", np.nan)) or row["vol_avg"] == 0:
        return "CE", 0.0, "no volume data"
    ratio = row["volume"] / row["vol_avg"]
    green = row["close"] >= row["open"]
    vote = "CE" if green else "PE"
    if ratio > 1.5:
        conf = 0.9
    elif ratio > 1.1:
        conf = 0.5
    else:
        conf = 0.15    # quiet volume = low-conviction vote either way
    return vote, conf, f"vol {ratio:.1f}x avg, {'green' if green else 'red'} candle"


# ─────────────────────────────────────────────────────────────────────────
# MINI-BOT 4: BREAKOUT BOT
# Wraps the EXISTING proven v4.2-v4.5 ladder (yd hi/lo, opening-range,
# afternoon fade). Direction is decided by app.py's ladder BEFORE this is
# called (unchanged) — this bot only grades how strong the breakout is,
# continuously instead of the old binary 0.5/1.0 step.
# ─────────────────────────────────────────────────────────────────────────
def breakout_bot(otype, px, level):
    if not level or not px:
        return otype, 0.0, "no level"
    margin_pct = abs(px - level) / px
    # 0% margin -> 0.5 conf (just crossed), 0.15%+ margin -> 1.0 conf (clean break)
    conf = 0.5 + min(0.5, margin_pct / 0.0015 * 0.5)
    return otype, round(conf, 3), f"{margin_pct*100:.3f}% past level {level:.0f}"


# ─────────────────────────────────────────────────────────────────────────
# MINI-BOT 5: VOLATILITY / RISK BOT — not directional. Turns confidence DOWN
# in choppy/dangerous conditions. This is the loss-reduction mechanism.
# ─────────────────────────────────────────────────────────────────────────
def volatility_bot(df, vix=None):
    row = df.iloc[-1]
    atr_now = row.get("atr14", np.nan)
    atr_hist = df["atr14"].tail(40)
    mult = 1.0
    reasons = []

    if not np.isnan(atr_now) and len(atr_hist.dropna()) >= 10:
        atr_pct_rank = (atr_hist < atr_now).mean()   # 0-1, where this ATR ranks recently
        if atr_pct_rank > 0.90:
            mult *= 0.75
            reasons.append(f"ATR in top 10% of last 40 candles (rank {atr_pct_rank:.2f}) — choppy/crash risk")
        elif atr_pct_rank > 0.75:
            mult *= 0.90
            reasons.append(f"ATR elevated (rank {atr_pct_rank:.2f})")

    if vix is not None:
        if vix >= 22:
            mult *= 0.80
            reasons.append(f"VIX {vix:.1f} — elevated market fear")
        elif vix >= 18:
            mult *= 0.92
            reasons.append(f"VIX {vix:.1f} — mildly elevated")

    if not reasons:
        reasons.append("normal conditions")
    return round(max(0.5, min(1.0, mult)), 3), "; ".join(reasons)


# ─────────────────────────────────────────────────────────────────────────
# MINI-BOT 6: PATTERN BOT — candlestick chart-pattern reader ("analyse the
# chart" per Sai's ask). Low weight by design: single/double-candle patterns
# on an index 5-min chart are noisy; this is a confluence signal, not a
# trigger.
# ─────────────────────────────────────────────────────────────────────────
def pattern_bot(df):
    if len(df) < 3:
        return "CE", 0.0, "not enough candles"
    c0, c1 = df.iloc[-1], df.iloc[-2]     # current, previous
    body0 = abs(c0["close"] - c0["open"])
    rng0  = c0["high"] - c0["low"]
    upper_wick = c0["high"] - max(c0["close"], c0["open"])
    lower_wick = min(c0["close"], c0["open"]) - c0["low"]
    body1 = abs(c1["close"] - c1["open"])
    red1  = c1["close"] < c1["open"]
    green1 = c1["close"] > c1["open"]
    green0 = c0["close"] > c0["open"]
    red0   = c0["close"] < c0["open"]

    # Bullish engulfing
    if red1 and green0 and c0["close"] >= c1["open"] and c0["open"] <= c1["close"] and body0 > body1:
        return "CE", 0.75, "bullish engulfing"
    # Bearish engulfing
    if green1 and red0 and c0["close"] <= c1["open"] and c0["open"] >= c1["close"] and body0 > body1:
        return "PE", 0.75, "bearish engulfing"
    # Hammer (bullish reversal): long lower wick, small body in upper half
    if rng0 > 0 and lower_wick >= 2 * body0 and upper_wick < body0:
        return "CE", 0.55, "hammer"
    # Shooting star (bearish reversal): long upper wick, small body in lower half
    if rng0 > 0 and upper_wick >= 2 * body0 and lower_wick < body0:
        return "PE", 0.55, "shooting star"
    # Doji: indecision, near-zero conviction
    if rng0 > 0 and body0 <= rng0 * 0.1:
        return ("CE" if green0 else "PE"), 0.1, "doji (indecision)"
    # No pattern — weak read from plain candle color
    return ("CE" if green0 else "PE"), 0.2, "no clear pattern, plain candle color"


# ─────────────────────────────────────────────────────────────────────────
# MOTHER BOT — combines all 6 votes into one final decision.
# ─────────────────────────────────────────────────────────────────────────
def mother_decide(otype, level, px, df, st15=None, macd15_up=None, vix=None, weights=None):
    """
    otype/level = candidate direction + trigger level from app.py's existing
    breakout ladder (unchanged trigger logic). Returns a dict:
      {
        "otype": "CE"/"PE" (mirrors input — mother bot does not flip the
                 trigger direction, it grades confidence in it),
        "confidence_pct": 0-100,
        "risk_mult": volatility bot's dampening factor,
        "votes": {bot_name: {"vote":.., "conf":.., "reason":..}},
        "agree_count": how many of the 5 directional bots agree with otype,
      }
    """
    if weights is None:
        weights = load_weights()
    row = df.iloc[-1]

    votes = {}
    votes["trend"]    = dict(zip(("vote", "conf", "reason"), trend_bot(row, st15, macd15_up)))
    votes["momentum"] = dict(zip(("vote", "conf", "reason"), momentum_bot(df)))
    votes["volume"]   = dict(zip(("vote", "conf", "reason"), volume_bot(row)))
    votes["breakout"] = dict(zip(("vote", "conf", "reason"), breakout_bot(otype, px, level)))
    votes["pattern"]  = dict(zip(("vote", "conf", "reason"), pattern_bot(df)))
    risk_mult, risk_reason = volatility_bot(df, vix)

    weighted_sum, weight_total, agree_count = 0.0, 0.0, 0
    for name, v in votes.items():
        w = weights.get(name, 1.0)
        weight_total += w
        sign = 1.0 if v["vote"] == otype else -1.0
        if sign > 0:
            agree_count += 1
        weighted_sum += w * v["conf"] * sign

    normalized = (weighted_sum / weight_total + 1) / 2 if weight_total else 0.5   # -> 0..1
    confidence_pct = round(max(0.0, min(100.0, normalized * 100 * risk_mult)), 1)

    return {
        "otype": otype,
        "confidence_pct": confidence_pct,
        "risk_mult": risk_mult,
        "risk_reason": risk_reason,
        "votes": votes,
        "agree_count": agree_count,
    }


def format_votes_plain(result):
    """Plain-language one-liner per bot, for Telegram (Sai's style: simple words)."""
    lines = []
    names = {"breakout": "Chart breakout bot", "trend": "Trend bot",
             "momentum": "Momentum bot", "volume": "Volume bot", "pattern": "Pattern bot"}
    for key, label in names.items():
        v = result["votes"][key]
        side = "CALL (up)" if v["vote"] == "CE" else "PUT (down)"
        lines.append(f"  {label}: {side}, {v['conf']*100:.0f}% sure — {v['reason']}")
    lines.append(f"  Risk bot: confidence x{result['risk_mult']:.2f} — {result['risk_reason']}")
    return "\n".join(lines)
