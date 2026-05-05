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

# (connect_seconds, read_seconds) — passed to every Binance HTTP call so a
# wedged socket (lost internet, DNS hiccup, Binance outage) can't park a
# Flask worker thread forever. Without this, python-binance defaults to NO
# timeout and hangs indefinitely; the dashboard then sticks on "loading…"
# until the Flask process is restarted.
_BINANCE_TIMEOUT = (5, 10)
_BINANCE_REQUEST_PARAMS = {"timeout": _BINANCE_TIMEOUT}


def _binance_client(key: str | None = None, secret: str | None = None):
    """Construct a python-binance Client with our standard request timeouts."""
    from binance.client import Client
    return Client(key, secret, requests_params=_BINANCE_REQUEST_PARAMS)


DEFAULTS = {
    "crypto_kill_switch": "off",
    "crypto_trading_mode": "paper",
    "crypto_max_position_usd": "50",
    "crypto_max_concurrent": "2",
    "crypto_drawdown_halt_pct": "15",
    "crypto_min_balance_usd": "100",
    # Partial profit-take defaults (so missing settings don't silently → 0,
    # which would make new_stop=entry instead of entry+buffer%)
    "crypto_partial_take_enabled": "on",
    "crypto_partial_take_trigger_pct": "4.0",
    "crypto_partial_take_fraction": "0.5",
    "crypto_breakeven_buffer_pct": "1.0",
    "crypto_fee_rate_per_side": "0.001",
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


def _set_setting(key: str, value: str) -> None:
    from webapp.models import Setting, db
    row = Setting.query.get(key)
    if row:
        row.value = value
    else:
        db.session.add(Setting(key=key, value=value))
    db.session.commit()


def update_day_start_and_check_halt(current_value: float) -> dict:
    """Snapshot today's start-of-day account value and auto-halt on daily drawdown.

    Replaces the older lifetime-peak halt. Each MYT day is its own threshold:
    a -15% day trips, but the next morning resets the snapshot and starts fresh.

    Behavior:
      - On the first call of a new MYT day, snapshot current_value as the
        day's "start" (stored in crypto_day_start_value_usd + crypto_day_start_date).
      - If the user disabled it (crypto_drawdown_halt_enabled != "on"), update
        the snapshot but never halt.
      - If (start - current) / start * 100 >= crypto_drawdown_halt_pct AND the
        kill switch is currently OFF, auto-flip kill switch to ON, write a
        CryptoRun audit row (kind='drawdown_halt'), and stash a one-shot notice
        in crypto_drawdown_notice for the Settings page to display once.

    Returns: {day_start, drawdown_pct, halt_triggered, enabled}
    """
    if current_value is None or current_value <= 0:
        return {"day_start": 0.0, "drawdown_pct": 0.0,
                "halt_triggered": False, "enabled": False}

    # Compute today's date in MYT (where the user lives — matches Settings UI)
    from datetime import timezone, timedelta
    MYT = timezone(timedelta(hours=8))
    today_myt = datetime.utcnow().replace(tzinfo=timezone.utc).astimezone(MYT).date()
    today_str = today_myt.isoformat()

    snap_date = _get_setting("crypto_day_start_date") or ""
    try:
        snap_value = float(_get_setting("crypto_day_start_value_usd") or 0)
    except (TypeError, ValueError):
        snap_value = 0.0

    # First call of the day (or first call ever) — set today's baseline.
    if snap_date != today_str or snap_value <= 0:
        _set_setting("crypto_day_start_date", today_str)
        _set_setting("crypto_day_start_value_usd", f"{current_value:.4f}")
        snap_value = current_value
        return {"day_start": snap_value, "drawdown_pct": 0.0,
                "halt_triggered": False, "enabled": True}

    enabled = (_get_setting("crypto_drawdown_halt_enabled") or "on").lower() == "on"
    drawdown_pct = (snap_value - current_value) / snap_value * 100.0 if snap_value > 0 else 0.0
    halt_pct = _f("crypto_drawdown_halt_pct")

    halt_triggered = False
    if (enabled
            and drawdown_pct >= halt_pct
            and _get_setting("crypto_kill_switch") != "on"):
        from webapp.models import CryptoRun, db
        ts = datetime.utcnow()
        msg = (
            f"auto-halted (daily): drawdown -{drawdown_pct:.2f}% "
            f"(day start ${snap_value:.2f} → current ${current_value:.2f}, "
            f"threshold {halt_pct:.0f}%)"
        )
        _set_setting("crypto_kill_switch", "on")
        _set_setting("crypto_drawdown_notice", f"{ts.isoformat()}|{msg}")
        run = CryptoRun(kind="drawdown_halt", status="ok",
                        started_at=ts, ended_at=ts, summary=msg)
        db.session.add(run)
        db.session.commit()
        log.warning("DAILY DRAWDOWN HALT — kill switch auto-flipped: %s", msg)
        halt_triggered = True

    return {"day_start": snap_value, "drawdown_pct": drawdown_pct,
            "halt_triggered": halt_triggered, "enabled": enabled}


# Backwards-compat shim for any caller still using the old name.
update_peak_and_check_halt = update_day_start_and_check_halt


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
    """Parse stop/target/max_hold/exit_rule from a position's notes string.

    Also parses partial-take state (set after a partial profit-take fires):
      partial_done   = True if partial sell already happened on this position
      original_stop  = the stop level BEFORE the breakeven-move adjustment
                       (preserved so dashboards can show "stop moved $X → $Y")
    """
    out = {
        "stop": None, "target": None, "max_hold": 24, "exit_rule": "stop_target_time",
        "partial_done": False, "original_stop": None,
    }
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
        elif token == "partial_done=1":
            out["partial_done"] = True
        elif token.startswith("original_stop=$"):
            try: out["original_stop"] = float(token.replace("original_stop=$", ""))
            except ValueError: pass
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
        # Attach the remaining net qty (post-partial-sells) to the BUY object
        # so callers can compute correct unrealized P&L. last_buy.qty is the
        # ORIGINAL buy qty; _remaining_qty is what's actually still open.
        last_buy._remaining_qty = qty
        result.append(last_buy)
    return result


# Backward-compat alias
def _open_paper_positions() -> list:
    return _open_positions(is_paper=True)


def _check_guardrails(intent: dict, mode: str, client=None) -> tuple[bool, str]:
    """Return (ok, reason). Any failure aborts execution."""
    if _get_setting("crypto_kill_switch") == "on":
        return False, "kill switch is ON"

    # Per-strategy disable — user can switch off underperformers in current
    # regime (e.g., turn off breakout_4h during a choppy bear market) without
    # halting the whole bot. Stored as CSV: "breakout_4h,momentum_surge".
    disabled_csv = _get_setting("crypto_disabled_strategies") or ""
    if disabled_csv:
        disabled = {s.strip() for s in disabled_csv.split(",") if s.strip()}
        strat = intent.get("strategy", "")
        if strat in disabled:
            return False, f"strategy '{strat}' is disabled"

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
            key, secret = get_binance_creds()
            if not key or not secret:
                result["mode"] = "skipped"
                result["reason"] = "live mode set but no API keys configured"
                return result
            client = _binance_client(key, secret)
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

def execute_sell(position, current_price: float, exit_reason: str,
                 qty_override: float | None = None) -> dict:
    """Close an open position. Mode (paper/live) inferred from the position itself.

    Args:
        position: a CryptoTrade row (the original BUY)
        current_price: latest market price (from cached klines)
        exit_reason: human-readable reason ('stop hit', 'target', 'time stop', etc.)
        qty_override: if provided, sell exactly this much (used by partial sells).
                      Otherwise: PAPER computes remaining qty from FIFO history;
                      LIVE caps to the actual free balance reported by Binance.

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

    entry_price = float(position.price)

    # Determine qty to sell.
    # PAPER: must compute net remaining (because paper has no real balance to query).
    #        After a partial sell earlier, position.qty is stale (still shows original);
    #        actual remaining = original − sum of partial sells already done.
    # LIVE:  the existing min(qty_to_sell, free) below caps to actual remaining,
    #        so we don't need to pre-compute. But if qty_override is given (partial),
    #        we want to sell exactly that.
    if qty_override is not None:
        qty_to_sell = float(qty_override)
    elif is_paper:
        # Sum all paper SELLs of this symbol that happened AFTER this BUY
        prior_sells = (CryptoTrade.query
                       .filter_by(symbol=position.symbol, side="SELL", is_paper=True)
                       .filter(CryptoTrade.executed_at > position.executed_at)
                       .filter(CryptoTrade.strategy != "manual_liquidation")
                       .all())
        already_sold = sum(float(s.qty) for s in prior_sells)
        qty_to_sell = max(0.0, float(position.qty) - already_sold)
    else:
        qty_to_sell = float(position.qty)  # LIVE will cap by free balance below

    if qty_to_sell <= 0:
        result["reason"] = "nothing left to sell (already fully closed)"
        return result

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
        key, secret = get_binance_creds()
        if not key or not secret:
            result["reason"] = "live exit needed but no API keys"
            return result
        client = _binance_client(key, secret)

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


def execute_partial_sell(position, current_price: float, fraction: float) -> dict:
    """Sell a fraction of an open position; first call also moves the stop.

    Auto-trigger fires this once (gated by `partial_done=1` in notes) when
    price reaches `entry × (1 + crypto_partial_take_trigger_pct/100)` — locks
    in profit and tightens the stop to entry+buffer%.

    Manual button can fire this repeatedly (ladder-out). On calls AFTER the
    first one, the sell still happens but the stop is NOT touched again —
    the user already chose where to set the stop on partial #1; subsequent
    discretionary partials are pure size reduction.

    Sell qty = `fraction × REMAINING_qty` (NOT × original qty), so a 50%
    ladder progressively halves the remaining position rather than trying
    to sell 50% of the original (which would close the whole thing on call 2).
    """
    from webapp.models import db, CryptoTrade

    if fraction <= 0 or fraction >= 1:
        return {"executed": False, "mode": "paper" if position.is_paper else "live",
                "reason": f"invalid partial fraction {fraction} (must be 0 < f < 1)",
                "trade_id": None, "fill_price": None, "fill_qty": None}

    # Compute REMAINING qty (post-prior-sells). Same logic for paper and live.
    prior_sells = (CryptoTrade.query
                   .filter_by(symbol=position.symbol, side="SELL", is_paper=position.is_paper)
                   .filter(CryptoTrade.executed_at > position.executed_at)
                   .filter(CryptoTrade.strategy != "manual_liquidation")
                   .all())
    already_sold = sum(float(s.qty) for s in prior_sells)
    remaining = max(0.0, float(position.qty) - already_sold)
    if remaining <= 0:
        return {"executed": False, "mode": "paper" if position.is_paper else "live",
                "reason": "nothing left to partial-sell (position already closed)",
                "trade_id": None, "fill_price": None, "fill_qty": None}

    qty_to_sell = remaining * fraction
    # Use the existing meta to detect whether this is the first partial. If yes,
    # the post-sell block tightens the stop and sets partial_done=1; if no, the
    # sell happens but notes are left alone (auto-trigger remains gated).
    meta_before = parse_entry_notes(position.notes)
    is_first_partial = not meta_before["partial_done"]
    label_n = "" if is_first_partial else f" ladder #{1 + sum(1 for s in prior_sells if 'partial profit take' in (s.notes or ''))}"
    label = f"partial profit take ({int(fraction * 100)}%){label_n}"
    res = execute_sell(position, current_price, label, qty_override=qty_to_sell)
    if not res["executed"]:
        return res  # don't update notes if the sell didn't go through

    # Subsequent ladder calls: skip the stop-move / notes rewrite. Notes already
    # carry partial_done=1 + tightened stop + preserved original_stop from call #1.
    if not is_first_partial:
        log.info("LADDER PARTIAL %s sold %.6f @ $%.6f (stop unchanged)",
                 position.symbol, qty_to_sell, current_price)
        return res

    # Compute the new stop level (entry + small breakeven buffer)
    try:
        buffer_pct = _f("crypto_breakeven_buffer_pct") or 1.0  # 0 → use sensible default
    except Exception:
        buffer_pct = 1.0
    meta = parse_entry_notes(position.notes)
    entry = float(position.price)
    original_stop = meta["stop"] if meta["stop"] else entry * 0.95
    new_stop = entry * (1 + buffer_pct / 100.0)
    target_str = _fmt_price(meta["target"]) if meta["target"] else _fmt_price(entry * 1.05)

    # Preserve the ORIGINAL entry-reason text (the human-readable last token —
    # things like "fresh breakout +4.2%, vol=1.6x, RSI=67"). The journal reads
    # the last token of notes as the entry reason; if we rebuild without it,
    # the journal would display "original_stop=$X" as the entry reason.
    entry_reason_text = ""
    if position.notes:
        for token in position.notes.split("·"):
            token = token.strip()
            # Skip our own structured tokens; keep anything that doesn't look like one
            if (token and not token.startswith(("stop=$", "target=$", "max_hold=", "exit=",
                                                 "original_stop=$", "PARTIAL DONE", "PAPER ENTRY",
                                                 "LIVE ENTRY"))
                and token != "partial_done=1"):
                entry_reason_text = token  # last matching one wins (the actual reason)

    # Reconstruct the position notes with new stop + flags. Order matters:
    # original_stop comes BEFORE the entry-reason text so the entry-reason is
    # the last token (which is what the journal extracts).
    parts = [
        "PARTIAL DONE",
        f"stop=${_fmt_price(new_stop)}",
        f"target=${target_str}",
        f"max_hold={meta['max_hold']}",
        f"exit={meta['exit_rule']}",
        "partial_done=1",
        f"original_stop=${_fmt_price(original_stop)}",
    ]
    if entry_reason_text:
        parts.append(entry_reason_text)
    position.notes = " · ".join(parts)
    db.session.commit()

    log.info(
        "PARTIAL SELL %s sold %.6f @ $%.6f (%s); stop moved $%.6f → $%.6f, target unchanged $%s",
        position.symbol, qty_to_sell, current_price, label,
        original_stop, new_stop, target_str,
    )
    return res
