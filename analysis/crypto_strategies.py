"""Crypto strategies — multiple complementary setups for 4h timeframe.

Each strategy returns a signal dict with full exit profile baked in:
    {
        symbol, strategy, side='BUY',
        entry_price, stop_price, target_price,
        max_hold_bars,
        exit_rule,         # str: 'stop_target_time' | 'sma50_break' | 'rsi_overbought_70' | 'rsi_overbought_80'
        reason,
    }

The loop dispatches each coin through ALL strategies and collects every
signal that fires. Multiple strategies CAN fire on the same coin — first
intent wins (executor refuses duplicates).
"""

from __future__ import annotations

import logging

import pandas as pd

from analysis.crypto_data import load_cached
from analysis.indicators import attach

# Minimum margin above SMA50 to call something a real "above-trend" entry.
# Without this, ADA/PEPE-style paper-thin breakouts (entered at +0.0% margin)
# get wicked by routine noise within minutes. 1% buffer eliminates that whole
# class of fake-breakout trades while still allowing most real breakouts to fire.
SMA50_MIN_MARGIN_PCT = 1.0

log = logging.getLogger("crypto_strategies")


def _btc_trend_ok(interval: str = "4h") -> bool:
    """BTC must be above its SMA50 to allow long entries on alts."""
    df = load_cached("BTCUSDT", interval)
    if df is None or df.empty or len(df) < 50:
        return False
    d = attach(df)
    last = d.iloc[-1]
    if pd.isna(last["sma50"]):
        return False
    return float(last["Close"]) > float(last["sma50"])


# ---------- Strategy 1: Breakout (existing, conservative) ----------

def crypto_breakout_4h(df: pd.DataFrame, symbol: str) -> dict | None:
    """20-candle breakout — requires FRESH breakout, not extended chase.

    Filters:
      - Close > prior 20-bar high (just barely — we want fresh breakouts)
      - But NOT more than 5% above prior high (don't chase extended moves)
      - Volume > 1.5x avg (real conviction)
      - Close > SMA50 (uptrend)
      - RSI(14) < 72 (not already overbought; tightened from 75 after the
        2026-05-08 STRK trade entered at RSI=74 and immediately faded —
        late-chase risk with no upside symmetry. Hardcoded by request, not
        configurable via settings: this is a no-touch guardrail.)
      - 24h close-to-close < 25% (not parabolic blow-off)
    """
    if df is None or df.empty or len(df) < 60:
        return None
    d = attach(df).dropna(subset=["high20", "atr14", "sma50", "volratio", "rsi14"])
    if len(d) < 7:
        return None
    last = d.iloc[-1]
    prior_high = float(d["high20"].iloc[-2])
    close = float(last["Close"])
    rsi = float(last["rsi14"])
    sma50 = float(last["sma50"])
    vr = float(last["volratio"])
    chg_24h = (close - float(d["Close"].iloc[-7])) / float(d["Close"].iloc[-7]) * 100

    # Required: above prior high, but only by <=5% (catch fresh breakouts, not chases)
    gap = (close - prior_high) / prior_high * 100
    sma50_margin_pct = (close - sma50) / sma50 * 100 if sma50 > 0 else -999
    if not (
        0 < gap <= 5
        and vr > 1.5
        and sma50_margin_pct >= SMA50_MIN_MARGIN_PCT
        and rsi < 72
        and chg_24h < 25
    ):
        return None

    atr = float(last["atr14"])
    return {
        "date": d.index[-1], "symbol": symbol, "strategy": "breakout_4h", "side": "BUY",
        "entry_price": close,
        "stop_price": close - 2.5 * atr,
        "target_price": close + 4.0 * atr,
        "max_hold_bars": 24,
        "exit_rule": "sma50_break",
        "reason": f"fresh breakout +{gap:.1f}% over prior high, vol={vr:.1f}x, RSI={rsi:.0f}",
    }


# ---------- Strategy 2: Momentum surge (catches APE/NFP-style moves) ----------

def momentum_surge_4h(df: pd.DataFrame, symbol: str) -> dict | None:
    """Volume + price-velocity trigger — catches breakouts BEFORE they clear 20-bar high.

    Fires when:
      - 24h volume > 3 × avg (massive interest)
      - 24h close-to-close > +5% (strong momentum)
      - RSI(14) < 75 (not chasing absolute top)
      - Above SMA50 (in trend)
    """
    if df is None or df.empty or len(df) < 60:
        return None
    d = attach(df).dropna(subset=["sma50", "rsi14", "volratio", "atr14"])
    if len(d) < 7:  # need 6 bars (24h) of history
        return None
    last = d.iloc[-1]
    close = float(last["Close"])
    sma50 = float(last["sma50"])
    rsi = float(last["rsi14"])

    # Use 24h windows (6 × 4h candles)
    chg_24h_pct = (close - float(d["Close"].iloc[-7])) / float(d["Close"].iloc[-7]) * 100
    vol_24h = float(d["Volume"].tail(6).sum())
    vol_avg_24h = float(d["Volume"].tail(120).sum()) / 20  # avg of 20 prior 24h windows
    vol_ratio_24h = vol_24h / vol_avg_24h if vol_avg_24h else 0

    # Cap 24h % at 20% — above that is chasing a parabola, EV turns negative
    sma50_margin_pct = (close - sma50) / sma50 * 100 if sma50 > 0 else -999

    # Trend-direction check: 24h % can be +19% even when the last 8h have been
    # a steady bleed (today's AIUSDT — pumped +30% then crashed -22% off high
    # before bot bought). Require the most recent close to be the HIGHEST of
    # the last 3 closes — confirms the surge is still going UP right now,
    # not reversing post-pump.
    c_now = close
    c_prev = float(d["Close"].iloc[-2])
    c_prev2 = float(d["Close"].iloc[-3])
    trend_intact = c_now > c_prev and c_now > c_prev2

    if not (
        vol_ratio_24h > 3.0
        and 5 < chg_24h_pct < 20
        and rsi < 75
        and sma50_margin_pct >= SMA50_MIN_MARGIN_PCT
        and trend_intact
    ):
        return None

    atr = float(last["atr14"])
    # Tight risk: 5% stop or 2×ATR (whichever closer to entry — we're entering hot, want quick exit if wrong)
    stop = max(close * 0.95, close - 2.0 * atr)
    target = close * 1.08  # +8% target — momentum trades take quick profit

    return {
        "date": d.index[-1], "symbol": symbol, "strategy": "momentum_surge", "side": "BUY",
        "entry_price": close,
        "stop_price": stop,
        "target_price": target,
        "max_hold_bars": 12,  # 2 days — momentum dies fast
        "exit_rule": "rsi_overbought_80",
        "reason": f"24h vol={vol_ratio_24h:.1f}x, +{chg_24h_pct:.1f}%, RSI={rsi:.0f}",
    }


# ---------- Strategy 3: Pullback in uptrend (buy the dip) ----------

def pullback_uptrend_4h(df: pd.DataFrame, symbol: str) -> dict | None:
    """Buy a REAL pullback in a confirmed uptrend (not just sideways chop).

    Fires when:
      - Close > SMA50 AND SMA50 rising (clear uptrend)
      - Recent 20-bar high was made within last 10 bars (active uptrend, not stale)
      - Close is 5-15% BELOW that recent high (genuine pullback, not just sideways)
      - RSI(14) between 35-50 (cooled off, room to bounce)
      - Price within 2% of EMA20 (at short-term support)
      - Stop distance < 8% (tight enough to be worth it)
    """
    if df is None or df.empty or len(df) < 60:
        return None
    d = attach(df).dropna(subset=["sma50", "ema20", "rsi14", "low5", "atr14"])
    if len(d) < 11:
        return None
    last = d.iloc[-1]
    close = float(last["Close"])
    sma50 = float(last["sma50"])
    sma50_5_ago = float(d["sma50"].iloc[-6])
    ema20 = float(last["ema20"])
    rsi = float(last["rsi14"])
    low5 = float(last["low5"])

    # Recent high must be from active price action (within last 10 bars)
    recent_window = d["High"].tail(10)
    recent_high = float(recent_window.max())
    bars_since_high = 9 - int(recent_window.argmax())  # 0 = current bar, 9 = 10 bars ago
    pullback_pct = (recent_high - close) / recent_high * 100

    sma50_rising = sma50 > sma50_5_ago
    near_ema20 = abs(close - ema20) / ema20 < 0.02
    sma50_margin_pct = (close - sma50) / sma50 * 100 if sma50 > 0 else -999

    if not (
        sma50_margin_pct >= SMA50_MIN_MARGIN_PCT and sma50_rising
        and bars_since_high <= 6  # high made recently
        and 5 <= pullback_pct <= 15  # real pullback, not sideways or crash
        and 35 <= rsi <= 50
        and near_ema20
    ):
        return None

    atr = float(last["atr14"])
    stop = min(low5 * 0.99, close - 1.5 * atr)
    target = recent_high  # back to recent high = continuation

    # Sanity checks: reasonable risk + reward
    risk_pct = (close - stop) / close * 100
    reward_pct = (target - close) / close * 100
    if risk_pct > 8 or reward_pct < 2 or reward_pct / risk_pct < 1.0:
        return None

    return {
        "date": d.index[-1], "symbol": symbol, "strategy": "pullback_uptrend", "side": "BUY",
        "entry_price": close,
        "stop_price": stop,
        "target_price": target,
        "max_hold_bars": 18,
        "exit_rule": "sma50_break",
        "reason": f"pullback {pullback_pct:.1f}% from {bars_since_high}bar high, RSI={rsi:.0f}",
    }


# ---------- Strategy 4: Oversold mean reversion (buy capitulation in macro uptrend) ----------

def oversold_meanrev_4h(df: pd.DataFrame, symbol: str) -> dict | None:
    """RSI(2) extreme oversold while still above SMA200 — short-term bounce play.

    Fires when:
      - RSI(2) < 5 (severely oversold)
      - Close > SMA200 (still in long-term uptrend)
      - Above SMA50 wouldn't apply here — we're buying a dip BELOW SMA50 sometimes

    NOTE: requires 200 bars (~33 days) of history.
    """
    if df is None or df.empty or len(df) < 200:
        return None
    d = attach(df).dropna(subset=["rsi2", "sma200", "low5", "atr14"])
    if d.empty:
        return None
    last = d.iloc[-1]
    close = float(last["Close"])
    rsi2 = float(last["rsi2"])
    sma200 = float(last["sma200"])

    if not (rsi2 < 5 and close > sma200):
        return None

    atr = float(last["atr14"])
    stop = max(close * 0.95, close - 1.5 * atr)
    target = close * 1.07
    return {
        "date": d.index[-1], "symbol": symbol, "strategy": "oversold_meanrev", "side": "BUY",
        "entry_price": close,
        "stop_price": stop,
        "target_price": target,
        "max_hold_bars": 6,  # very fast — bounce happens quickly or thesis is wrong
        "exit_rule": "rsi_overbought_70",
        "reason": f"RSI2={rsi2:.1f}<5 (oversold), >SMA200 (macro uptrend)",
    }


# ---------- Strategy 5: 1h breakout — fresh entries on shorter timeframe ----------

def crypto_breakout_1h(df: pd.DataFrame, symbol: str) -> dict | None:
    """20-bar breakout on 1h timeframe — catches FRESH breakouts within 1 hour.

    Uses 1h candles (passed in `df`) but requires the 4h SMA50 trend filter
    is already verified by the BTC regime check at the scan level.

    Filters:
      - Close > prior 20 1h-bars high (~last 20 hours)
      - Gap above prior high <= 3% (very fresh, our 15-min scan should catch this)
      - Volume > 1.5x avg
      - Close > SMA50 (1h SMA50 = ~2 days of trend)
      - RSI(14) < 75 (not overbought)
      - 24h close-to-close < 25% (not parabolic)
    """
    if df is None or df.empty or len(df) < 60:
        return None
    d = attach(df).dropna(subset=["high20", "atr14", "sma50", "volratio", "rsi14"])
    if len(d) < 25:  # need 24 hours = 24 bars for 24h chg
        return None
    last = d.iloc[-1]
    prior_high = float(d["high20"].iloc[-2])
    close = float(last["Close"])
    rsi = float(last["rsi14"])
    sma50 = float(last["sma50"])
    vr = float(last["volratio"])
    chg_24h = (close - float(d["Close"].iloc[-25])) / float(d["Close"].iloc[-25]) * 100

    gap = (close - prior_high) / prior_high * 100
    sma50_margin_pct = (close - sma50) / sma50 * 100 if sma50 > 0 else -999
    if not (
        0 < gap <= 3              # very fresh breakout only
        and vr > 1.5
        and sma50_margin_pct >= SMA50_MIN_MARGIN_PCT
        and rsi < 75
        and chg_24h < 25
    ):
        return None

    # Fixed-percent stops/targets — ATR-based was too tight for low-vol majors
    # like BNB (2-bar ATR=$5 = 0.7% stop → wicked by random noise). 5%/8% gives
    # the trade room to breathe through normal pullbacks while keeping R:R ~1.6.
    return {
        "date": d.index[-1], "symbol": symbol, "strategy": "breakout_1h", "side": "BUY",
        "entry_price": close,
        "stop_price": close * 0.95,         # -5%
        "target_price": close * 1.08,       # +8%
        "max_hold_bars": 12,                 # 12h max — short timeframe = quick exit
        "exit_rule": "sma50_break",
        "reason": f"1h fresh breakout +{gap:.1f}% over prior high, vol={vr:.1f}x, RSI={rsi:.0f}",
    }


def support_bounce_1h(df: pd.DataFrame, symbol: str) -> dict | None:
    """Bull-flag pattern: rising coin pulls back to a support level, tests it
    multiple times, then bounces with conviction. Catches the SAGA-style
    continuation where breakout/momentum strategies miss it.

    Filters:
      - Recent rise: 12h change >= +8% (real prior leg)
      - Support detected: 3+ wick lows clustered within ±1.5% tolerance
        across the last 12 1h-bars
      - Consolidation: range of last 6 bars within ±4% (tight coil)
      - Bounce: latest closed 1h bar is GREEN with volume >= 1.5x 20-bar avg
      - Not overbought: RSI 50-72 (room to run, not exhausted)
      - Above SMA50 (longer trend intact)

    Stop: 3% below the support cluster low
    Target: recent swing high (the leg origin we're bouncing back toward)
    Hold: 18 bars (~18h)
    """
    if df is None or df.empty or len(df) < 30:
        return None
    d = attach(df)
    last = d.iloc[-1]
    close = float(last["Close"])
    open_ = float(last["Open"])

    # 1) Recent rise — need a leg up to retrace from
    if len(d) < 13:
        return None
    close_12h_ago = float(d.iloc[-13]["Close"])
    if close_12h_ago <= 0:
        return None
    rise_12h_pct = (close - close_12h_ago) / close_12h_ago * 100
    if rise_12h_pct < 8.0:
        return None

    # 2) Above SMA50 — trend still intact
    if pd.isna(last.get("sma50")) or close <= float(last["sma50"]):
        return None

    # 3) Support cluster — find lowest low in last 12 bars and count touches
    window = d.iloc[-12:]
    lows = window["Low"].astype(float).tolist()
    support_low = min(lows)
    tolerance = 0.015  # ±1.5%
    touches = sum(1 for lo in lows if abs(lo - support_low) / support_low <= tolerance)
    if touches < 3:
        return None

    # 4) Consolidation — range of last 6 bars compressed
    last_6 = d.iloc[-6:]
    high6 = float(last_6["High"].max())
    low6 = float(last_6["Low"].min())
    range_pct = (high6 - low6) / low6 * 100 if low6 > 0 else 999
    if range_pct > 5.0:
        return None  # too wide = not yet consolidating

    # 5) Bounce signal — latest closed bar GREEN with volume
    if close <= open_:
        return None
    if pd.notna(last.get("vol_sma20")) and float(last["vol_sma20"]) > 0:
        vol_ratio = float(last["Volume"]) / float(last["vol_sma20"])
        if vol_ratio < 1.5:
            return None
    else:
        return None

    # 6) RSI gate — not too cold (no real momentum) or too hot (chase territory)
    if pd.isna(last.get("rsi14")):
        return None
    rsi = float(last["rsi14"])
    if rsi < 50 or rsi > 72:
        return None

    # Levels
    stop = support_low * 0.97  # 3% below support cluster
    # Target = swing high of the prior leg (highest high in last 24 bars,
    # capped at +8% if too far away to be realistic in 18h hold)
    swing_high = float(d["High"].iloc[-24:].max())
    target = min(swing_high, close * 1.08)
    if target <= close * 1.02:
        return None  # not enough upside to be worth the risk

    return {
        "date": d.index[-1], "symbol": symbol, "strategy": "support_bounce", "side": "BUY",
        "entry_price": close, "stop_price": stop, "target_price": target,
        "max_hold_bars": 18, "exit_rule": "stop_target_time",
        "reason": f"support ${support_low:.5f} held {touches}x, +{rise_12h_pct:.1f}%/12h, RSI={rsi:.0f}",
    }


# Registry: each strategy declares its native timeframe.
# scan_crypto() loads the right kline data per strategy.
STRATEGIES_BY_TIMEFRAME = {
    "4h": {
        "breakout_4h": crypto_breakout_4h,
        "momentum_surge": momentum_surge_4h,
        "pullback_uptrend": pullback_uptrend_4h,
        "oversold_meanrev": oversold_meanrev_4h,
    },
    "1h": {
        "breakout_1h": crypto_breakout_1h,
        "support_bounce": support_bounce_1h,
    },
}

# Backward-compat flat dict (kept for any old callers)
STRATEGIES = {
    name: fn
    for tf_strats in STRATEGIES_BY_TIMEFRAME.values()
    for name, fn in tf_strats.items()
}


def _sane_rr(sig: dict) -> bool:
    """Reject signals with crazy stop distances or unfavorable R:R."""
    e, s, t = sig["entry_price"], sig["stop_price"], sig["target_price"]
    if s >= e or t <= e:
        return False
    risk_pct = (e - s) / e * 100
    reward_pct = (t - e) / e * 100
    if risk_pct > 10:  # never risk more than 10% of position on stop
        return False
    if reward_pct < 2:  # need at least 2% upside
        return False
    if reward_pct / risk_pct < 1.2:  # require at least 1.2:1 R:R
        return False
    return True


# Strategy quality ordering — when multiple signals fire, prioritize these first
STRATEGY_PRIORITY = {
    "breakout_4h": 1,         # confirmed 4h breakout = highest quality
    "breakout_1h": 2,         # fresh 1h breakout = also good
    "momentum_surge": 3,       # volume + momentum
    "pullback_uptrend": 4,    # buying dips
    "oversold_meanrev": 5,    # contrarian = lowest priority
}


def scan_crypto(coins: list[str], interval: str = "4h") -> tuple[list[dict], list[dict]]:
    """Scan all coins through all strategies across ALL timeframes.

    The `interval` arg is now legacy (kept for backward compat) — actual timeframes
    are read from STRATEGIES_BY_TIMEFRAME registry. Each strategy gets the kline
    data matching its declared timeframe.

    Signals sorted by quality (strategy priority, then R:R desc).
    Executor refuses duplicates by symbol, so best signal per symbol wins.
    """
    signals: list[dict] = []
    blocked: list[dict] = []

    # Regime check uses 4h BTC trend regardless of strategy timeframe
    if not _btc_trend_ok("4h"):
        blocked.append({"symbol": "ALL", "reason": "BTC below SMA50 — regime off, no longs"})
        return signals, blocked

    for sym in coins:
        coin_fired = False
        for tf, strats in STRATEGIES_BY_TIMEFRAME.items():
            df = load_cached(sym, tf)
            if df is None or df.empty:
                continue
            for strat_name, fn in strats.items():
                try:
                    sig = fn(df, sym)
                    if sig and _sane_rr(sig):
                        signals.append(sig)
                        coin_fired = True
                    elif sig:
                        blocked.append({"symbol": sym, "reason": f"{strat_name}: rejected by R:R sanity"})
                except Exception as e:
                    log.warning("%s/%s error: %s", sym, strat_name, e)
        if not coin_fired:
            blocked.append({"symbol": sym, "reason": "no setup matched on any timeframe"})

    def sort_key(s):
        rr = (s["target_price"] - s["entry_price"]) / (s["entry_price"] - s["stop_price"])
        return (STRATEGY_PRIORITY.get(s["strategy"], 99), -rr, s["symbol"])
    signals.sort(key=sort_key)

    return signals, blocked
