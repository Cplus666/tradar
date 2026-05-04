"""Crypto order executor — paper-first, live mode behind explicit toggle.

The single entry point is `execute_intent(intent)`. It runs ALL pre-trade
guardrails in order and refuses on any failure. In paper mode it simulates
a fill; in live mode it places a real Binance MARKET order, reconciles the
fill, and persists CryptoTrade(is_paper=False).

Settings read from the Setting table (crypto_*):
    crypto_kill_switch         on | off                  (default: off)
    crypto_trading_mode        paper | live              (default: paper)
    crypto_max_position_usd    USD per trade             (default: 50)
    crypto_max_concurrent      max open positions        (default: 2)
    crypto_drawdown_halt_pct   portfolio DD halt %       (default: 15)
    crypto_min_balance_usd     min USDT to enable live   (default: 100)
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

log = logging.getLogger("crypto_executor")

DEFAULTS = {
    "crypto_kill_switch": "off",
    "crypto_trading_mode": "paper",
    "crypto_max_position_usd": "50",
    "crypto_max_concurrent": "2",
    "crypto_drawdown_halt_pct": "15",
    "crypto_min_balance_usd": "100",
}


def _get_setting(key: str) -> str:
    from webapp.models import Setting
    row = Setting.query.get(key)
    if row and row.value is not None:
        return row.value
    return DEFAULTS.get(key, "")


def _f(key: str) -> float:
    try:
        return float(_get_setting(key))
    except (TypeError, ValueError):
        return float(DEFAULTS.get(key, "0"))


def _fmt_price(p: float) -> str:
    """Price-aware formatter — preserves precision for low-priced coins."""
    if p < 0.0001:
        return f"{p:.8f}"
    if p < 1:
        return f"{p:.6f}"
    return f"{p:.4f}"


def _build_entry_notes(intent: dict, prefix: str) -> str:
    """Build notes string with parseable key=value tokens for the exit checker."""
    return (
        f"{prefix} · stop=${_fmt_price(intent.get('stop_price', 0))}"
        f" · target=${_fmt_price(intent.get('target_price', 0))}"
        f" · max_hold={intent.get('max_hold_bars', 24)}"
        f" · exit={intent.get('exit_rule', 'stop_target_time')}"
        f" · {intent.get('reason', '')}"
    )


def _adjust_levels_to_fill(intent: dict, fill_price: float) -> dict:
    """Recompute stop/target relative to actual fill price, preserving planned %.

    Prevents the bug where a stale-data scan-time entry has stop > actual fill.
    The risk and reward percentages are preserved; only the absolute levels shift.
    """
    scan_entry = intent.get("entry_price", fill_price)
    scan_stop = intent.get("stop_price", scan_entry * 0.95)
    scan_target = intent.get("target_price", scan_entry * 1.05)
    if scan_entry <= 0:
        return intent
    risk_pct = (scan_entry - scan_stop) / scan_entry
    reward_pct = (scan_target - scan_entry) / scan_entry
    new = dict(intent)
    new["stop_price"] = fill_price * (1 - risk_pct)
    new["target_price"] = fill_price * (1 + reward_pct)
    new["entry_price"] = fill_price
    return new


def _quantity_to_step_string(qty: float, step_str: str) -> str:
    """Round quantity DOWN to lot step using Decimal — Binance rejects float garbage."""
    from decimal import Decimal
    step_d = Decimal(step_str).normalize()
    decimals = abs(step_d.as_tuple().exponent) if step_d.as_tuple().exponent < 0 else 0
    qty_d = (Decimal(str(qty)) // Decimal(step_str)) * Decimal(step_str)
    return format(qty_d, f".{decimals}f")


def parse_entry_notes(notes: str | None) -> dict:
    """Parse stop/target/max_hold/exit_rule from a position's notes string."""
    out = {"stop": None, "target": None, "max_hold": 24, "exit_rule": "stop_target_time"}
    if not notes:
        return out
    for token in notes.split("·"):
        token = token.strip()
        if token.startswith("stop=$"):
            try: out["stop"] = float(token.replace("stop=$", ""))
            except ValueError: pass
        elif token.startswith("target=$"):
            try: out["target"] = float(token.replace("target=$", ""))
            except ValueError: pass
        elif token.startswith("max_hold="):
            try: out["max_hold"] = int(token.replace("max_hold=", ""))
            except ValueError: pass
        elif token.startswith("exit="):
            out["exit_rule"] = token.replace("exit=", "").strip()
    return out


def _open_positions(is_paper: bool | None = None) -> list:
    """Return open positions, excluding dust remnants.

    A position counts as "open" only if remaining net qty × last buy price > $1 USD.
    Anything smaller is dust left over from rounding to lot step on the sell side.
    """
    from webapp.models import CryptoTrade
    q = CryptoTrade.query
    if is_paper is not None:
        q = q.filter_by(is_paper=is_paper)
    trades = q.order_by(CryptoTrade.executed_at).all()
    open_qty: dict[tuple[str, bool], float] = {}
    for t in trades:
        if t.strategy == "manual_liquidation":
            continue
        key = (t.symbol, t.is_paper)
        sign = 1 if t.side == "BUY" else -1
        open_qty[key] = open_qty.get(key, 0) + sign * t.qty
    result = []
    for (sym, paper), qty in open_qty.items():
        if qty <= 1e-9:
            continue
        last_buy = (
            CryptoTrade.query
            .filter_by(symbol=sym, side="BUY", is_paper=paper)
            .filter(CryptoTrade.strategy != "manual_liquidation")
            .order_by(CryptoTrade.executed_at.desc()).first()
        )
        if not last_buy:
            continue
        # Dust filter: ignore positions worth less than $1 (leftover rounding)
        residual_value = qty * float(last_buy.price)
        if residual_value < 1.0:
            continue
        result.append(last_buy)
    return result


# Backward-compat alias
def _open_paper_positions() -> list:
    return _open_positions(is_paper=True)


def _check_guardrails(intent: dict, mode: str, client=None) -> tuple[bool, str]:
    """Return (ok, reason). Any failure aborts execution."""
    if _get_setting("crypto_kill_switch") == "on":
        return False, "kill switch is ON"

    max_pos_usd = _f("crypto_max_position_usd")
    if intent.get("size_usd", 0) > max_pos_usd:
        return False, f"size ${intent['size_usd']:.2f} > max ${max_pos_usd:.2f}"

    # Check positions of THE SAME mode (paper checks paper; live checks live)
    is_paper_mode = (mode == "paper")
    max_concurrent = int(_f("crypto_max_concurrent"))
    open_positions = _open_positions(is_paper=is_paper_mode)
    if len(open_positions) >= max_concurrent:
        return False, f"max concurrent reached ({len(open_positions)}/{max_concurrent})"

    # Don't double up on the same symbol
    if any(p.symbol == intent["symbol"] for p in open_positions):
        return False, f"already have open position in {intent['symbol']}"

    if mode == "live":
        if client is None:
            return False, "live mode requires Binance client"
        # Min balance check
        try:
            acct = client.get_account()
            usdt = next((b for b in acct["balances"] if b["asset"] == "USDT"), None)
            free_usdt = float(usdt["free"]) if usdt else 0.0
        except Exception as e:
            return False, f"live balance check failed: {e}"
        min_bal = _f("crypto_min_balance_usd")
        if free_usdt < min_bal:
            return False, f"USDT free ${free_usdt:.2f} < min ${min_bal:.2f} (live disabled until funded)"
        if free_usdt < intent.get("size_usd", 0):
            return False, f"insufficient USDT (${free_usdt:.2f} < ${intent['size_usd']:.2f})"

    return True, "ok"


def execute_intent(intent: dict) -> dict:
    """Single entry point. Returns result dict with status + details.

    Result shape:
      {
        "executed":   bool,
        "mode":       "paper" | "live" | "skipped",
        "reason":     str,
        "trade_id":   int | None,
        "fill_price": float | None,
        "fill_qty":   float | None,
      }
    """
    from webapp.crypto.routes import get_binance_creds
    from webapp.models import CryptoTrade, db

    mode = _get_setting("crypto_trading_mode") or "paper"
    result = {
        "executed": False, "mode": mode, "reason": "",
        "trade_id": None, "fill_price": None, "fill_qty": None,
    }

    client = None
    if mode == "live":
        try:
            from binance.client import Client
            key, secret = get_binance_creds()
            if not key or not secret:
                result["mode"] = "skipped"
                result["reason"] = "live mode set but no API keys configured"
                return result
            client = Client(key, secret)
        except Exception as e:
            result["mode"] = "skipped"
            result["reason"] = f"client init failed: {e}"
            return result

    ok, reason = _check_guardrails(intent, mode, client)
    if not ok:
        result["mode"] = "skipped"
        result["reason"] = reason
        log.warning("intent skipped: %s — %s", intent.get("symbol"), reason)
        return result

    if mode == "paper":
        # Simulated fill at entry_price. Write the trade.
        qty = intent["size_usd"] / intent["entry_price"]
        trade = CryptoTrade(
            symbol=intent["symbol"], side="BUY", qty=qty, price=intent["entry_price"],
            quote_amount=intent["size_usd"], executed_at=datetime.utcnow(),
            status="filled", is_paper=True, strategy=intent.get("strategy", "unknown"),
            notes=_build_entry_notes(intent, prefix="PAPER"),
        )
        db.session.add(trade)
        db.session.commit()
        result.update({
            "executed": True, "reason": "paper fill at intent price",
            "trade_id": trade.id, "fill_price": intent["entry_price"], "fill_qty": qty,
        })
        log.info("PAPER BUY %s qty=%.8f @ $%.4f", intent["symbol"], qty, intent["entry_price"])
        return result

    # LIVE MODE — pre-flight: confirm symbol is actively trading on Binance.
    # Some symbols show "BREAK" / "HALT" / etc. and would fail with cryptic errors mid-trade.
    try:
        info = client.get_symbol_info(intent["symbol"])
        sym_status = info.get("status") if info else None
        if sym_status != "TRADING":
            result["mode"] = "skipped"
            result["reason"] = f"symbol status={sym_status} (not TRADING) — refusing to enter"
            log.warning("skipped %s due to status %s", intent["symbol"], sym_status)
            return result
    except Exception as e:
        result["mode"] = "skipped"
        result["reason"] = f"symbol info check failed: {e}"
        return result

    try:
        order = client.create_order(
            symbol=intent["symbol"], side="BUY", type="MARKET",
            quoteOrderQty=intent["size_usd"],
        )
    except Exception as e:
        result["reason"] = f"live order failed: {e}"
        log.exception("live order failed for %s", intent["symbol"])
        return result

    # Reconcile fill from order fills array
    fills = order.get("fills", [])
    if not fills:
        result["reason"] = "order placed but no fills reported"
        return result
    total_qty = sum(float(f["qty"]) for f in fills)
    total_quote = sum(float(f["qty"]) * float(f["price"]) for f in fills)
    avg_price = total_quote / total_qty if total_qty else 0.0

    # Re-anchor stop/target to the actual fill price (not scan-time entry_price).
    # This prevents inverted stops when the order fills at a meaningfully different price.
    adjusted_intent = _adjust_levels_to_fill(intent, avg_price)
    trade = CryptoTrade(
        symbol=intent["symbol"], side="BUY", qty=total_qty, price=avg_price,
        quote_amount=total_quote, executed_at=datetime.utcnow(),
        status="filled", is_paper=False, strategy=intent.get("strategy", "unknown"),
        binance_order_id=str(order.get("orderId", "")),
        notes=_build_entry_notes(adjusted_intent, prefix="LIVE"),
    )
    db.session.add(trade)
    db.session.commit()
    result.update({
        "executed": True, "reason": "live fill",
        "trade_id": trade.id, "fill_price": avg_price, "fill_qty": total_qty,
    })
    log.info("LIVE BUY %s qty=%.8f @ $%.4f (orderId=%s)",
             intent["symbol"], total_qty, avg_price, order.get("orderId"))
    return result


# ============================================================================
#  SELL execution — closes an open position via market order
# ============================================================================

def execute_sell(position, current_price: float, exit_reason: str) -> dict:
    """Close an open position. Mode (paper/live) inferred from the position itself.

    Args:
        position: a CryptoTrade row (the original BUY)
        current_price: latest market price (from cached klines)
        exit_reason: human-readable reason ('stop hit', 'target', 'time stop', etc.)

    Returns result dict similar to execute_intent.
    """
    from webapp.crypto.routes import get_binance_creds
    from webapp.models import CryptoTrade, db
    import math

    is_paper = position.is_paper
    mode_str = "paper" if is_paper else "live"
    result = {
        "executed": False, "mode": mode_str, "reason": "",
        "trade_id": None, "fill_price": None, "fill_qty": None,
    }

    qty_to_sell = float(position.qty)
    entry_price = float(position.price)

    if is_paper:
        # Simulated fill at current price
        sell = CryptoTrade(
            symbol=position.symbol, side="SELL", qty=qty_to_sell, price=current_price,
            quote_amount=qty_to_sell * current_price, executed_at=datetime.utcnow(),
            status="filled", is_paper=True, strategy=position.strategy,
            notes=f"PAPER EXIT · {exit_reason}",
        )
        db.session.add(sell)
        db.session.commit()
        pnl_pct = (current_price - entry_price) / entry_price * 100
        result.update({
            "executed": True,
            "reason": f"paper exit @ ${current_price:.6f} ({exit_reason}) — P&L {pnl_pct:+.2f}%",
            "trade_id": sell.id, "fill_price": current_price, "fill_qty": qty_to_sell,
        })
        log.info("PAPER SELL %s qty=%.8f @ $%.6f (%s) P&L=%+.2f%%",
                 position.symbol, qty_to_sell, current_price, exit_reason, pnl_pct)
        return result

    # LIVE SELL — real Binance market order
    try:
        from binance.client import Client
        key, secret = get_binance_creds()
        if not key or not secret:
            result["reason"] = "live exit needed but no API keys"
            return result
        client = Client(key, secret)

        # Use ACTUAL free balance (fees on the buy leave us slightly short of qty bought).
        # Round DOWN to lot step using Decimal — float math produces precision garbage.
        info = client.get_symbol_info(position.symbol)
        lot_filter = next(f for f in info["filters"] if f["filterType"] == "LOT_SIZE")
        step_str = lot_filter["stepSize"]
        acct = client.get_account()
        base_asset = position.symbol.replace("USDT", "")
        balance = next((b for b in acct["balances"] if b["asset"] == base_asset), None)
        free = float(balance["free"]) if balance else 0
        sell_target = min(qty_to_sell, free)
        if sell_target <= 0:
            result["reason"] = f"no balance to sell (asked {qty_to_sell}, free {free})"
            return result
        qty_str = _quantity_to_step_string(sell_target, step_str)
        if float(qty_str) <= 0:
            result["reason"] = f"qty rounds to zero (free {free}, step {step_str})"
            return result

        order = client.create_order(
            symbol=position.symbol, side="SELL", type="MARKET", quantity=qty_str,
        )
        fills = order.get("fills", [])
        if not fills:
            result["reason"] = "sell placed but no fills reported"
            return result
        total_qty = sum(float(f["qty"]) for f in fills)
        total_quote = sum(float(f["qty"]) * float(f["price"]) for f in fills)
        avg_price = total_quote / total_qty if total_qty else 0.0

        sell = CryptoTrade(
            symbol=position.symbol, side="SELL", qty=total_qty, price=avg_price,
            quote_amount=total_quote, executed_at=datetime.utcnow(),
            status="filled", is_paper=False, strategy=position.strategy,
            binance_order_id=str(order.get("orderId", "")),
            notes=f"LIVE EXIT · {exit_reason}",
        )
        db.session.add(sell)
        db.session.commit()
        pnl_pct = (avg_price - entry_price) / entry_price * 100
        result.update({
            "executed": True,
            "reason": f"live exit @ ${avg_price:.6f} ({exit_reason}) — P&L {pnl_pct:+.2f}%",
            "trade_id": sell.id, "fill_price": avg_price, "fill_qty": total_qty,
        })
        log.info("LIVE SELL %s qty=%.8f @ $%.6f (%s) P&L=%+.2f%%  orderId=%s",
                 position.symbol, total_qty, avg_price, exit_reason, pnl_pct, order.get("orderId"))
        return result
    except Exception as e:
        result["reason"] = f"live sell failed: {e}"
        log.exception("live sell failed for %s", position.symbol)
        return result
