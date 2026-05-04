"""Binance kline (candle) fetcher + cache.

Caches OHLCV for crypto pairs to data/crypto/<symbol>.parquet.
Uses python-binance public endpoint (no API key needed for klines).

Public API:
    refresh_pair(symbol, interval='4h', lookback_days=180) -> pd.DataFrame
    refresh_all_pairs(interval='4h')                       -> dict[symbol, status]
    load_cached(symbol, interval='4h')                     -> pd.DataFrame
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
CRYPTO_CACHE_DIR = ROOT / "data" / "crypto"

log = logging.getLogger("crypto_data")

INTERVAL_MAP = {
    "1h": ("1h", 24 * 60),
    "4h": ("4h", 6 * 60),
    "1d": ("1d", 24 * 60),
}


def _cache_path(symbol: str, interval: str) -> Path:
    return CRYPTO_CACHE_DIR / f"{symbol.upper()}_{interval}.parquet"


def _klines_to_df(klines: list[list]) -> pd.DataFrame:
    """Convert raw Binance klines list to OHLCV DataFrame."""
    cols = [
        "open_time", "Open", "High", "Low", "Close", "Volume",
        "close_time", "quote_volume", "trades", "taker_buy_base",
        "taker_buy_quote", "ignore",
    ]
    df = pd.DataFrame(klines, columns=cols)
    df["date"] = pd.to_datetime(df["open_time"], unit="ms")
    df = df.set_index("date")
    for c in ["Open", "High", "Low", "Close", "Volume"]:
        df[c] = df[c].astype(float)
    return df[["Open", "High", "Low", "Close", "Volume"]]


def refresh_pair(symbol: str, interval: str = "4h", lookback_days: int = 180) -> pd.DataFrame:
    """Fetch + cache klines for one pair.

    Uses NO startTime so Binance returns the most-recent N candles (default ordering).
    With limit=1000 4h candles = ~166 days of history — enough for SMA200.
    Specifying startTime + limit returned the FIRST 1000 candles in the window,
    not the latest — producing 14-day-stale data.
    """
    from binance.client import Client
    if interval not in INTERVAL_MAP:
        raise ValueError(f"unsupported interval: {interval}")
    binance_interval, _ = INTERVAL_MAP[interval]
    client = Client()  # No keys needed for public klines endpoint
    klines = client.get_klines(symbol=symbol, interval=binance_interval, limit=1000)
    if not klines:
        log.warning("%s/%s: no klines returned", symbol, interval)
        return pd.DataFrame()
    df = _klines_to_df(klines)
    CRYPTO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(_cache_path(symbol, interval))
    log.info("%s/%s: cached %d candles (%s -> %s)", symbol, interval, len(df), df.index.min(), df.index.max())
    return df


def refresh_all_pairs(interval: str = "4h") -> dict[str, str]:
    """Refresh every active pair in crypto_coins. Returns symbol -> status string."""
    from webapp.models import CryptoCoin
    out: dict[str, str] = {}
    coins = CryptoCoin.query.filter_by(active=True).all()
    for c in coins:
        try:
            df = refresh_pair(c.symbol, interval)
            out[c.symbol] = f"ok ({len(df)} candles)" if not df.empty else "empty"
        except Exception as e:
            log.warning("%s: %s", c.symbol, e)
            out[c.symbol] = f"failed: {e}"
    return out


def load_cached(symbol: str, interval: str = "4h") -> pd.DataFrame | None:
    p = _cache_path(symbol, interval)
    if not p.exists():
        return None
    return pd.read_parquet(p)
