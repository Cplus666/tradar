"""Binance account → CryptoHolding sync.

Read-only. Pulls balances via get_account(), prices via get_symbol_ticker(),
and persists rows to the crypto_holdings table. Records a CryptoRun for audit.

Public API:
    sync_holdings(api_key, api_secret) -> dict summary
"""

from __future__ import annotations

import logging
import traceback
from datetime import datetime
from typing import Any

log = logging.getLogger("binance_sync")

# Treat <$1 USD positions as dust (won't clutter the dashboard).
DUST_USD_THRESHOLD = 1.0


def persist_holdings_from_data(balances: list, tickers: dict[str, float]) -> dict:
    """Write CryptoHolding rows from PRE-FETCHED data — no API calls, no audit log.

    Called from /api/dashboard so the holdings page stays in sync with whatever
    the dashboard just fetched live.

    Args:
        balances: list of dicts with 'asset', 'free', 'locked' (from get_account()['balances'])
        tickers: dict mapping symbol → price (from get_all_tickers())
    """
    from webapp.models import CryptoHolding, db

    real_count = 0; dust_count = 0; total_value_usd = 0.0
    nonzero = [b for b in balances if float(b["free"]) + float(b["locked"]) > 0]
    CryptoHolding.query.delete()
    for b in nonzero:
        asset = b["asset"]
        free = float(b["free"])
        locked = float(b["locked"])
        total = free + locked
        # Price lookup: stablecoins = $1, otherwise direct USDT pair, otherwise via BTC
        if asset in ("USDT", "USDC", "BUSD", "FDUSD", "DAI", "TUSD", "USDP"):
            price_usd = 1.0
        elif f"{asset}USDT" in tickers:
            price_usd = tickers[f"{asset}USDT"]
        elif f"{asset}BTC" in tickers and "BTCUSDT" in tickers:
            price_usd = tickers[f"{asset}BTC"] * tickers["BTCUSDT"]
        else:
            price_usd = None
        value_usd = total * price_usd if price_usd is not None else None
        is_dust = (value_usd is None) or (value_usd < DUST_USD_THRESHOLD)
        note = "no-price (delisted?)" if value_usd is None else ("dust" if is_dust else None)
        db.session.add(CryptoHolding(
            asset=asset, free=free, locked=locked,
            last_price_usd=price_usd, value_usd=value_usd,
            fetched_at=datetime.utcnow(), notes=note,
        ))
        if is_dust:
            dust_count += 1
        else:
            real_count += 1
            if value_usd:
                total_value_usd += value_usd
    db.session.commit()
    return {"real_count": real_count, "dust_count": dust_count, "total_value_usd": total_value_usd}


def _get_price_usd(client, asset: str) -> float | None:
    """Get current USD price for an asset.

    Tries direct USDT pair first, then BTC->USDT path. Returns None if not priceable.
    Stablecoins return ~1.0.
    """
    if asset in ("USDT", "USDC", "BUSD", "FDUSD", "DAI", "TUSD", "USDP"):
        return 1.0
    # Direct USDT pair
    try:
        t = client.get_symbol_ticker(symbol=f"{asset}USDT")
        return float(t["price"])
    except Exception:
        pass
    # Via BTC
    try:
        t1 = client.get_symbol_ticker(symbol=f"{asset}BTC")
        t2 = client.get_symbol_ticker(symbol="BTCUSDT")
        return float(t1["price"]) * float(t2["price"])
    except Exception:
        pass
    return None


def sync_holdings(api_key: str, api_secret: str) -> dict[str, Any]:
    """Pull balances + prices from Binance, replace crypto_holdings rows.

    Returns a summary dict. Caller is responsible for app context.
    """
    from binance.client import Client
    from binance.exceptions import BinanceAPIException
    from webapp.models import CryptoHolding, CryptoRun, db

    started = datetime.utcnow()
    run = CryptoRun(kind="sync", status="running", started_at=started)
    db.session.add(run)
    db.session.commit()

    summary: dict[str, Any] = {
        "ok": False, "real_count": 0, "dust_count": 0,
        "total_value_usd": 0.0, "errors": [],
    }
    log_lines: list[str] = []

    try:
        client = Client(api_key, api_secret)
        account = client.get_account()
        balances = [
            b for b in account.get("balances", [])
            if float(b["free"]) + float(b["locked"]) > 0
        ]
        log_lines.append(f"fetched {len(balances)} non-zero balances")

        # Wipe + re-populate. Holdings are a snapshot, not a journal.
        CryptoHolding.query.delete()

        for b in balances:
            try:
                asset = b["asset"]
                free = float(b["free"])
                locked = float(b["locked"])
                total = free + locked
                price_usd = _get_price_usd(client, asset)
                if price_usd is None:
                    log_lines.append(f"  {asset}: NO PRICE (qty={total:.8f}) — skipped from value")
                    value_usd = None
                    summary["errors"].append(f"{asset}: no price")
                else:
                    value_usd = total * price_usd
                # Unpriceable assets are treated as dust so they don't clutter the main view.
                is_dust = (value_usd is None) or (value_usd < DUST_USD_THRESHOLD)
                note = "no-price (delisted?)" if value_usd is None else ("dust" if is_dust else None)
                db.session.add(CryptoHolding(
                    asset=asset, free=free, locked=locked,
                    last_price_usd=price_usd, value_usd=value_usd,
                    fetched_at=datetime.utcnow(), notes=note,
                ))
                if is_dust:
                    summary["dust_count"] += 1
                else:
                    summary["real_count"] += 1
                    if value_usd:
                        summary["total_value_usd"] += value_usd
                    log_lines.append(
                        f"  {asset}: qty={total:.8f}  price=${price_usd:.6f}  value=${value_usd:.4f}"
                        + (" [LOCKED]" if locked > 0 else "")
                    )
            except Exception as e:
                log_lines.append(f"  {b.get('asset','?')}: skipped due to error: {e}")
                summary["errors"].append(f"{b.get('asset','?')}: {e}")

        db.session.commit()
        summary["ok"] = True
        run.status = "ok"
        run.summary = (
            f"synced {summary['real_count']} real + {summary['dust_count']} dust assets, "
            f"total value ${summary['total_value_usd']:.2f}"
        )
        log.info(run.summary)
    except BinanceAPIException as e:
        run.status = "error"
        run.error = f"BinanceAPIException: {e.message} (code {e.code})"
        run.summary = f"failed: {e.message}"
        summary["errors"].append(run.error)
        log.exception("binance sync failed")
    except Exception as e:
        run.status = "error"
        run.error = f"{e}\n{traceback.format_exc()}"
        run.summary = f"failed: {e}"
        summary["errors"].append(str(e))
        log.exception("sync failed")
    finally:
        run.ended_at = datetime.utcnow()
        run.log_excerpt = "\n".join(log_lines)[-4000:]
        db.session.commit()

    return summary
