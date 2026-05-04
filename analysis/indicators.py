"""Technical indicators — thin wrappers on the `ta` library + a few helpers.

All functions take a price DataFrame (OHLCV with a DatetimeIndex) and return
a Series aligned to the same index. NaN-filled at the start (no look-ahead).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import ta


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    return ta.momentum.RSIIndicator(close=close, window=period, fillna=False).rsi()


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return ta.volatility.AverageTrueRange(
        high=df["High"], low=df["Low"], close=df["Close"], window=period, fillna=False,
    ).average_true_range()


def ema(close: pd.Series, period: int) -> pd.Series:
    return close.ewm(span=period, adjust=False).mean()


def sma(close: pd.Series, period: int) -> pd.Series:
    return close.rolling(period).mean()


def rolling_high(close: pd.Series, period: int) -> pd.Series:
    return close.rolling(period).max()


def rolling_low(close: pd.Series, period: int) -> pd.Series:
    return close.rolling(period).min()


def avg_volume(volume: pd.Series, period: int) -> pd.Series:
    return volume.rolling(period).mean()


def attach(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of df with common indicators added as new columns."""
    out = df.copy()
    out["rsi2"] = rsi(out["Close"], 2)
    out["rsi14"] = rsi(out["Close"], 14)
    out["atr14"] = atr(out, 14)
    out["ema20"] = ema(out["Close"], 20)
    out["sma50"] = sma(out["Close"], 50)
    out["sma200"] = sma(out["Close"], 200)
    out["high20"] = rolling_high(out["Close"], 20)
    out["low5"] = rolling_low(out["Close"], 5)
    out["volavg20"] = avg_volume(out["Volume"], 20)
    out["volratio"] = out["Volume"] / out["volavg20"]
    return out
