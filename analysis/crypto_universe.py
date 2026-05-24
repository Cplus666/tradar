"""Dynamic universe selector — pulls hot/high-volume coins from Binance each scan.

Replaces the static watchlist with a daily-refreshed universe:
  - Top N USDT pairs by 24h quote volume (where the real money moves)
  - Plus top M USDT pairs by absolute 24h % change (where momentum lives)
  - Always include core majors (BTC, ETH) regardless of rank
  - Filter out junk: stablecoins, leveraged tokens, low-volume noise

Public API:
    get_dynamic_universe(top_volume=30, top_movers=10, min_volume_usd=10_000_000)
        -> list[dict]   # each: {symbol, price, change_pct_24h, quote_volume, source}
"""

from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger("crypto_universe")

# Always include these regardless of rank
ALWAYS_INCLUDE = {"BTCUSDT", "ETHUSDT"}

# Filter rules
STABLECOIN_BASES = {
    "USDC", "USDT", "BUSD", "FDUSD", "DAI", "TUSD", "USDP", "PAX", "USDD",
    "EUR", "GBP", "AUD", "TRY", "BRL", "JPY", "RUB", "USD1", "U", "RLUSD",
    "USDE", "GUSD", "EURI", "PYUSD",
}
# Leveraged tokens have specific suffixes
LEVERAGED_SUFFIXES = re.compile(r"(UP|DOWN|BULL|BEAR|3L|3S|5L|5S)USDT$")
# Wrapped/derivative tokens — track underlying instead
WRAPPED_PATTERN = re.compile(r"^(W|ST)?(BTC|ETH)USDT$")  # WBTC, STETH, etc.


def _is_tradeable(sym: str, base: str | None = None) -> bool:
    """Filter out junk symbols we don't want to trade."""
    if not sym.endswith("USDT"):
        return False
    # Non-ASCII symbol (e.g. 币安人生USDT) — rare experimental tokens, skip
    if not sym.isascii():
        return False
    if LEVERAGED_SUFFIXES.search(sym):
        return False
    if WRAPPED_PATTERN.match(sym) and sym not in ALWAYS_INCLUDE:
        return False
    base = base or sym[:-4]  # strip "USDT"
    if base in STABLECOIN_BASES:
        return False
    return True


def get_dynamic_universe(
    top_volume: int = 30,
    top_movers: int = 10,
    min_volume_usd: float = 10_000_000,
) -> list[dict[str, Any]]:
    """Return a deduped, filtered list of coins to scan this round.

    Each entry: {symbol, price, change_pct_24h, quote_volume, source}
    `source`: which selector picked it ("volume", "mover", "always", or several joined)
    """
    from binance.client import Client
    client = Client()  # public endpoint, no keys needed
    all_24h = client.get_ticker()
    usdt = [t for t in all_24h if _is_tradeable(t["symbol"])]

    # 1) Apply hard volume floor
    usdt = [t for t in usdt if float(t["quoteVolume"]) >= min_volume_usd]

    # 1.5) Filter out symbols that aren't actively TRADING on Binance
    #      (BREAK = suspended, HALT = emergency stop, etc.). Without this,
    #      suspended symbols like UTKUSDT keep appearing in scans, wasting
    #      every loop's CPU + Binance API calls. Single batch lookup via
    #      get_exchange_info — cheap.
    try:
        exch = client.get_exchange_info()
        tradeable_status = {s["symbol"] for s in exch["symbols"]
                            if s.get("status") == "TRADING"}
        before = len(usdt)
        usdt = [t for t in usdt if t["symbol"] in tradeable_status]
        dropped = before - len(usdt)
        if dropped > 0:
            log.info("dropped %d non-TRADING symbols (BREAK/HALT/etc)", dropped)
    except Exception as e:
        log.warning("exchange status filter failed (allowing all): %s", e)

    log.info("after volume + symbol filters: %d candidates", len(usdt))

    # 2) Top by volume
    by_vol = sorted(usdt, key=lambda t: float(t["quoteVolume"]), reverse=True)[:top_volume]
    vol_set = {t["symbol"] for t in by_vol}

    # 3) Top by absolute 24h % move (positive only — we're long-only)
    movers_pool = [t for t in usdt if float(t["priceChangePercent"]) > 0]
    by_move = sorted(movers_pool, key=lambda t: float(t["priceChangePercent"]), reverse=True)[:top_movers]
    move_set = {t["symbol"] for t in by_move}

    # 4) Always-include
    by_always = [t for t in usdt if t["symbol"] in ALWAYS_INCLUDE]
    always_set = {t["symbol"] for t in by_always}

    # 5) Merge with source tags
    seen: dict[str, dict] = {}
    for t in by_vol + by_move + by_always:
        sym = t["symbol"]
        if sym in seen:
            continue
        sources = []
        if sym in vol_set:
            sources.append("volume")
        if sym in move_set:
            sources.append("mover")
        if sym in always_set:
            sources.append("always")
        seen[sym] = {
            "symbol": sym,
            "price": float(t["lastPrice"]),
            "change_pct_24h": float(t["priceChangePercent"]),
            "quote_volume": float(t["quoteVolume"]),
            "source": "+".join(sources),
        }

    out = list(seen.values())
    log.info(
        "universe: %d coins (%d by volume, %d by mover, %d always-include)",
        len(out), len(vol_set), len(move_set), len(always_set),
    )
    return out
