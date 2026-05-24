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
    crypto_loss_halt_pct       daily loss halt %         (default: 5)
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
    "crypto_min_balance_usd": "100",
    # Starting capital — the principal used by all synth-day-start calculations.
    # AUTO-ADJUSTED at MYT-midnight rollover when net USDT deposits/withdrawals
    # are detected via Binance API. User can also set manually via Settings.
    # Default 200.29 = the original principal at first deploy. After deposits,
    # this grows; after withdrawals, it shrinks. Result: all-time P&L cleanly
    # separates contributions from trading P&L.
    "crypto_starting_capital_usd": "200.29",
    # Net USDT deposits/withdrawals during TODAY (since MYT midnight).
    # Updated by the user's "Refresh deposit/withdraw" button. Reset to 0 at
    # MYT midnight rollover — at that moment the past-24h fetch captures
    # yesterday's finalized total into today's snapshot row, so the so-far
    # counter starts fresh tracking new day's flows.
    "crypto_today_deposits_so_far_usd": "0",
    # Partial profit-take defaults (so missing settings don't silently → 0,
    # which would make new_stop=entry instead of entry+buffer%)
    "crypto_partial_take_enabled": "on",
    "crypto_partial_take_trigger_pct": "4.0",
    "crypto_partial_take_fraction": "0.5",
    "crypto_breakeven_buffer_pct": "1.0",
    # Lock-in fraction of partial gain on the runner's stop. After partial fires
    # at price P (gain G% from entry), new stop = entry × (1 + max(buffer, G×LF)/100).
    # 0.5 = "lock in half the gain"; 0 = old static buffer-only behavior.
    "crypto_partial_lock_fraction": "0.5",
    "crypto_fee_rate_per_side": "0.001",
    # Daily P&L halts — both auto-resume next MYT day. Different from the
    # catastrophic kill-switch (which is permanent until manually flipped).
    # Loss halt: at -X% from day-start, sell all open positions + block new
    # entries until midnight. Profit halt: same but at +X%, lock in gains.
    "crypto_loss_halt_enabled": "off",
    "crypto_loss_halt_pct": "5.0",
    "crypto_profit_halt_enabled": "off",
    "crypto_profit_halt_pct": "5.0",
    # Internal state — auto-cleared at MYT midnight rollover. Don't surface
    # these in Settings UI; they're managed by the halt logic.
    "crypto_today_loss_halted": "0",
    "crypto_today_profit_halted": "0",
    # Override flags — set by user "Resume trading today" button. They consume
    # today's halt slot: halt logic skips re-firing for the same kind today,
    # but the kind re-arms automatically at next MYT midnight.
    "crypto_today_loss_overridden": "0",
    "crypto_today_profit_overridden": "0",
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


def is_today_halted() -> bool:
    """True if today's auto-resume halt is active (loss or profit halt fired)."""
    return (_get_setting("crypto_today_loss_halted") == "1"
            or _get_setting("crypto_today_profit_halted") == "1")


def _starting_capital() -> float:
    """Returns the INITIAL principal — the bot's first deposit, before any
    subsequent deposits/withdrawals. Read from `crypto_starting_capital_usd`
    setting; fallback 200.29 if missing.

    NOT the "current principal." For current/historical principal at any
    specific date use `_principal_at_day(date_iso)` — that helper sums the
    signed deposits/withdrawals from `crypto_daily_snapshots` to give you
    the principal as it was at the start of that day.
    """
    try:
        v = float(_get_setting("crypto_starting_capital_usd") or "0")
        if v > 0:
            return v
    except (TypeError, ValueError):
        pass
    return 200.29


def _today_deposits_so_far() -> float:
    """Net deposits/withdrawals during TODAY (since MYT midnight). Updated by
    the 'Refresh deposit/withdraw' button. 0 by default; reset to 0 at MYT
    rollover."""
    try:
        return float(_get_setting("crypto_today_deposits_so_far_usd") or "0")
    except (TypeError, ValueError):
        return 0.0


def _principal_at_day(date_iso: str) -> float:
    """Principal in effect at the moment we're computing for `date_iso`.

    Three components:
      1. initial principal (from `crypto_starting_capital_usd`)
      2. Σ signed deposits from all snapshot rows with date <= date_iso
         (each row's deposits_during_day_usd was captured at that row's
          MYT-midnight rollover via past-24h fetch — so row date=X stores
          deposits during day X-1, and `<=` correctly sums all flows up to
          and including the prior calendar day)
      3. IF date_iso is today, also add `crypto_today_deposits_so_far_usd`
         — this is the user-refreshed mid-day deposit counter, so today's
         deposits propagate immediately after the user clicks Refresh.

    Without #3, today's deposits wouldn't reflect in the dashboard / synth /
    halt threshold until the next midnight rollover.
    """
    from webapp.models import CryptoDailySnapshot
    initial = _starting_capital()
    try:
        rows = (CryptoDailySnapshot.query
                .filter(CryptoDailySnapshot.date <= date_iso,
                        CryptoDailySnapshot.deposits_during_day_usd.isnot(None))
                .with_entities(CryptoDailySnapshot.deposits_during_day_usd)
                .all())
        cum_deposits = sum(float(r[0] or 0) for r in rows)
    except Exception:
        cum_deposits = 0.0
    # Add today's mid-day deposits if asking about today.
    from datetime import timezone, timedelta
    MYT_TZ = timezone(timedelta(hours=8))
    today_iso = (datetime.utcnow().replace(tzinfo=timezone.utc)
                 .astimezone(MYT_TZ).date().isoformat())
    today_so_far = _today_deposits_so_far() if date_iso == today_iso else 0.0
    return initial + cum_deposits + today_so_far


def _collect_ghost_init_data() -> list[dict]:
    """Snapshot live open positions + current ticker prices for ghost seeding.
    Called just before sell_all_open_positions so we capture prices while
    positions are still open. Returns [] on any failure (non-fatal)."""
    try:
        positions = _open_positions(is_paper=False)
        if not positions:
            return []
        tickers = {t["symbol"]: float(t["price"])
                   for t in _binance_client().get_all_tickers()}
        result = []
        for pos in positions:
            meta  = parse_entry_notes(pos.notes)
            strat = (pos.strategy or "").lower()
            bar_h = 1 if ("1h" in strat or "momentum" in strat or "oversold" in strat) else 4
            result.append({
                "symbol":        pos.symbol,
                "qty":           float(getattr(pos, "_remaining_qty", pos.qty)),
                "price":         tickers.get(pos.symbol, float(pos.price)),
                "stop":          meta.get("stop"),
                "target":        meta.get("target"),
                "strategy":      pos.strategy,
                "entered_at":    pos.executed_at,
                "max_hold_bars": meta.get("max_hold", 12),
                "bar_interval_h":bar_h,
                "partial_done":  bool(meta.get("partial_done")),
                "original_stop": meta.get("original_stop"),
            })
        return result
    except Exception as e:
        log.warning("ghost init data collection failed (non-fatal): %s", e)
        return []


def sell_all_open_positions(reason: str, mode_filter: bool | None = None) -> int:
    """Close every currently-open position at market. Returns # successful sells.

    Used by daily-P&L halts (profit/loss). Each sell goes through execute_sell
    with the standard exit_reason wiring so the journal/dashboard show why.
    """
    n = 0
    for pos in _open_positions(is_paper=mode_filter):
        try:
            # Need a current price — try ticker fetch via the bot's existing client
            try:
                client = _binance_client()
                cur = float(client.get_symbol_ticker(symbol=pos.symbol)["price"])
            except Exception:
                # Fallback: use last buy price (will fill at simulated/market in execute_sell)
                cur = float(pos.price)
            res = execute_sell(pos, cur, reason)
            if res.get("executed"):
                n += 1
                log.info("HALT-CLOSE %s: %s", pos.symbol, res.get("reason", reason))
            else:
                log.warning("HALT-CLOSE %s FAILED: %s", pos.symbol, res.get("reason", "?"))
        except Exception as e:
            log.exception("HALT-CLOSE error on %s: %s", pos.symbol, e)
    return n


def _is_paper_mode_setting() -> bool:
    """Read trading mode from settings — paper means no Binance deposit fetch."""
    return (_get_setting("crypto_trading_mode") or "paper") == "paper"


def _fetch_net_usdt_deposits_since_today_midnight() -> float | None:
    """Net USDT deposits − withdrawals from MYT 00:00 today to NOW.

    Used by the user's 'Refresh deposit/withdraw' button to update
    `crypto_today_deposits_so_far_usd` so today's same-day flows propagate
    into the synth/dashboard immediately. Returns None if either Binance
    API call fails (caller should not update the setting on None — keep
    the previous value). In paper mode, returns 0.0 — paper deposits don't
    show up on Binance."""
    if _is_paper_mode_setting():
        return 0.0
    try:
        from webapp.crypto.routes import get_binance_creds
        from datetime import timezone, timedelta
        key, secret = get_binance_creds()
        if not key or not secret:
            return None
        client = _binance_client(key, secret)
        MYT = timezone(timedelta(hours=8))
        today_midnight_myt = (datetime.utcnow().replace(tzinfo=timezone.utc)
                              .astimezone(MYT)
                              .replace(hour=0, minute=0, second=0, microsecond=0))
        start_ms = int(today_midnight_myt.astimezone(timezone.utc).timestamp() * 1000)
        end_ms = int(datetime.utcnow().timestamp() * 1000)
        deposits = 0.0
        withdrawals = 0.0
        try:
            for d in client.get_deposit_history(coin="USDT",
                                                startTime=start_ms,
                                                endTime=end_ms) or []:
                if d.get("status") == 1:
                    deposits += float(d.get("amount") or 0)
        except Exception as e:
            log.warning("today's deposit history fetch failed: %s", e)
            return None
        try:
            for w in client.get_withdraw_history(coin="USDT",
                                                 startTime=start_ms,
                                                 endTime=end_ms) or []:
                if w.get("status") == 6:
                    withdrawals += float(w.get("amount") or 0)
        except Exception as e:
            log.warning("today's withdraw history fetch failed: %s", e)
            return None
        # Add P2P (C2C) inflow/outflow — invisible to standard deposit API
        p2p = _fetch_net_usdt_p2p(client, start_ms, end_ms)
        if p2p is not None:
            deposits += p2p[0]
            withdrawals += p2p[1]
        # If P2P fetch failed: don't error — just miss P2P this tick (caller
        # treats absence of news as zero, which is the existing behavior).
        return deposits - withdrawals
    except Exception as e:
        log.warning("since-midnight deposit fetch failed: %s", e)
        return None


def _fetch_net_usdt_p2p(client, start_ms: int, end_ms: int) -> tuple[float, float] | None:
    """Fetch P2P (C2C) USDT activity in [start_ms, end_ms].

    P2P moves USDT between Binance internal accounts (P2P wallet ↔ Spot wallet)
    without an on-chain transaction, so it's invisible to get_deposit_history.
    But from a Spot-wallet perspective, BUY = USDT inflow (deposit-equivalent)
    and SELL = USDT outflow (withdrawal-equivalent).

    Returns (p2p_in_usdt, p2p_out_usdt), or None on API failure.
    Both values are zero (not None) if no P2P trades exist — that's a valid
    state, not a failure.
    """
    p2p_in = 0.0
    p2p_out = 0.0
    for trade_type, accumulator_key in [("BUY", "in"), ("SELL", "out")]:
        try:
            res = client.get_c2c_trade_history(
                tradeType=trade_type,
                startTimestamp=start_ms,
                endTimestamp=end_ms,
            ) or {}
        except Exception as e:
            log.warning("C2C %s history fetch failed: %s", trade_type, e)
            return None
        for t in (res.get("data") or []):
            if (t.get("asset") == "USDT"
                    and t.get("orderStatus") == "COMPLETED"):
                amt = float(t.get("amount") or 0)
                if accumulator_key == "in":
                    p2p_in += amt
                else:
                    p2p_out += amt
    return p2p_in, p2p_out


def _fetch_net_usdt_deposits_last_24h() -> float | None:
    """Net USDT deposits − withdrawals in the past 24h, via Binance API.

    Returns the float net amount, or None if the API call failed (caller
    should treat None as 'unknown' — store NULL in the snapshot, not 0.0).
    Only counts USDT directly; non-USDT deposits would need price conversion
    that's not worth the complexity for this use case. In paper mode,
    returns 0.0 — paper deposits don't show on Binance.
    """
    if _is_paper_mode_setting():
        return 0.0
    try:
        from webapp.crypto.routes import get_binance_creds
        key, secret = get_binance_creds()
        if not key or not secret:
            return None
        client = _binance_client(key, secret)
        end_ms = int(datetime.utcnow().timestamp() * 1000)
        start_ms = end_ms - (24 * 60 * 60 * 1000)
        deposits = 0.0
        withdrawals = 0.0
        try:
            for d in client.get_deposit_history(coin="USDT",
                                                startTime=start_ms,
                                                endTime=end_ms) or []:
                # status: 1 = success (deposit credited)
                if d.get("status") == 1:
                    deposits += float(d.get("amount") or 0)
        except Exception as e:
            log.warning("deposit history fetch failed: %s", e)
            return None
        try:
            for w in client.get_withdraw_history(coin="USDT",
                                                 startTime=start_ms,
                                                 endTime=end_ms) or []:
                # status: 6 = completed (withdrawal sent successfully)
                if w.get("status") == 6:
                    withdrawals += float(w.get("amount") or 0)
        except Exception as e:
            log.warning("withdraw history fetch failed: %s", e)
            # Got deposits but not withdrawals — still better than nothing,
            # but mark as unknown to avoid storing a misleading "deposits-only"
            # net figure.
            return None
        # Add P2P (C2C) — same logic as the since-midnight function
        p2p = _fetch_net_usdt_p2p(client, start_ms, end_ms)
        if p2p is not None:
            deposits += p2p[0]
            withdrawals += p2p[1]
        return deposits - withdrawals
    except Exception as e:
        log.warning("net deposit fetch failed: %s", e)
        return None


def _write_daily_snapshot(date_iso: str, total_value: float,
                          usdt_free: float | None = None,
                          open_value: float | None = None,
                          source: str = "synth",
                          overwrite: bool = False) -> None:
    """Insert/update a CryptoDailySnapshot row.

    Default behavior is INSERT-IF-MISSING (idempotent on date PK). Pass
    `overwrite=True` to update an existing row — used by the rollover code
    so today's row reflects the synth value even if a prior 'backfill' or
    older-format row was already there.

    `source` should be "synth" for rollover/recompute writes (the value is
    the synthesized day-start). Other sources ("backfill") are legacy.
    """
    from webapp.models import CryptoDailySnapshot, db
    existing = CryptoDailySnapshot.query.get(date_iso)
    if existing is not None:
        if not overwrite:
            return  # idempotent — keep existing row
        # On overwrite, do NOT refetch deposits. The deposits column has a
        # specific semantics ("captured at this row's rollover for past 24h
        # = the prior calendar day's deposits"). Refetching mid-day would
        # overwrite that with a time-shifted past-24h window, losing the
        # canonical value. Today's deposits will be captured at TOMORROW's
        # rollover and stored in tomorrow's row — that's by design.
        existing.total_value_usd = float(total_value)
        existing.source = source
        if usdt_free is not None:
            existing.usdt_free = usdt_free
        if open_value is not None:
            existing.open_value_usd = open_value
        db.session.commit()
        log.info("daily snapshot updated: %s @ $%.2f source=%s", date_iso, total_value, source)
        return
    deposits_24h = _fetch_net_usdt_deposits_last_24h()
    row = CryptoDailySnapshot(
        date=date_iso,
        total_value_usd=float(total_value),
        usdt_free=usdt_free,
        open_value_usd=open_value,
        deposits_during_day_usd=deposits_24h,
        source=source,
    )
    db.session.add(row)
    db.session.commit()
    log.info("daily snapshot saved: %s @ $%.2f (deposits24h=%s)",
             date_iso, total_value,
             f"${deposits_24h:.2f}" if deposits_24h is not None else "?")


def _compute_synth_day_start_today() -> float:
    """Compute the synthesized day-start value for today (MYT).

    Formula: principal_at_today + Σ (FIFO net P&L from closes BEFORE today MYT 00:00).
      where principal_at_today = initial + Σ deposits/withdrawals before today.

    Same denominator as dashboard today's-P&L card and daily ROI graph, so
    halt threshold matches what the user sees. Deposits/withdrawals propagate
    via _principal_at_day which sums the signed `deposits_during_day_usd`
    column from the snapshot table.

    Walks crypto_trades via FIFO with REMAINING-QTY tracking — handles partial
    sells correctly. Cheap (a few hundred trade rows, <50ms typically).
    """
    from webapp.models import CryptoTrade
    from datetime import timezone, timedelta
    MYT = timezone(timedelta(hours=8))
    now_myt = datetime.utcnow().replace(tzinfo=timezone.utc).astimezone(MYT)
    today_start_myt = now_myt.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_utc = today_start_myt.astimezone(timezone.utc).replace(tzinfo=None)

    try:
        fee_rate = float(_get_setting("crypto_fee_rate_per_side") or "0.001")
    except (TypeError, ValueError):
        fee_rate = 0.001

    trades = (CryptoTrade.query
              .filter(CryptoTrade.is_paper == False)
              .order_by(CryptoTrade.executed_at)
              .all())
    by_sym: dict = {}
    for t in trades:
        if t.strategy == "manual_liquidation":
            continue
        by_sym.setdefault(t.symbol, []).append(t)

    pre_today_realized = 0.0
    for _sym, ts in by_sym.items():
        buys: list = []  # [trade, remaining_qty]
        for t in ts:
            if t.side == "BUY":
                buys.append([t, float(t.qty)])
            elif t.side == "SELL" and buys:
                sell_qty_remaining = float(t.qty)
                while sell_qty_remaining > 1e-9 and buys:
                    buy, buy_remaining = buys[0]
                    consumed = min(buy_remaining, sell_qty_remaining)
                    buys[0][1] -= consumed
                    sell_qty_remaining -= consumed
                    _bq = float(buy.qty) or 1
                    _sq = float(t.qty) or 1
                    buy_value = float(buy.quote_amount or float(buy.price) * float(buy.qty)) * (consumed / _bq)
                    sell_value = float(t.quote_amount or float(t.price) * float(t.qty)) * (consumed / _sq)
                    gross = sell_value - buy_value
                    fee = (buy_value + sell_value) * fee_rate
                    pnl = gross - fee
                    if buys[0][1] <= 1e-9:
                        buys.pop(0)
                    if t.executed_at < today_start_utc:
                        pre_today_realized += pnl

    today_iso = today_start_myt.date().isoformat()
    return _principal_at_day(today_iso) + pre_today_realized


def _entry_basis_value(usdt_free: float | None = None) -> float:
    """Day-start value using ENTRY-PRICE basis for open positions:
        day_start = usdt_total + Σ(remaining_qty × entry_price) for live positions
    where usdt_total = free + locked. Locked USDT (sitting in pending limit BUY
    orders the user placed) is still real money — it'll either fill into a
    position or get returned to free on cancel. Counting only `free` made the
    day_start understate the account by the amount locked.

    This makes today_pnl = realized + unrealized reconcile cleanly because both
    sides use the same cost basis. Caller may pass usdt_free if already known
    (avoids an extra Binance API call); when caller passes it, the caller is
    responsible for using TOTAL (free + locked), not just free.

    Returns 0 if Binance unreachable.
    """
    if usdt_free is None:
        try:
            from webapp.crypto.routes import get_binance_creds
            key, secret = get_binance_creds()
            if not key or not secret:
                return 0.0
            client = _binance_client(key, secret)
            acct = client.get_account()
            usdt = next((b for b in acct["balances"] if b["asset"] == "USDT"), None)
            # Use free + locked: locked USDT in pending limit BUY orders is still
            # part of the account value (Binance shows it in "Est. Total Value").
            usdt_free = (float(usdt["free"]) + float(usdt["locked"])) if usdt else 0.0
        except Exception:
            return 0.0
    entry_basis = 0.0
    for p in _open_positions(is_paper=False):
        qty = float(getattr(p, "_remaining_qty", p.qty))
        entry_basis += qty * float(p.price)
    return usdt_free + entry_basis


def update_day_start_and_check_halt(current_value: float, usdt_free: float | None = None) -> dict:
    """Snapshot today's start-of-day value and fire BOTH loss and profit halts.

    Each MYT day is its own threshold; flags auto-clear at midnight rollover.

    Two halt mechanisms (separate, can stack):
      1. crypto_loss_halt_pct: soft auto-resume. At -X% from day-start, sell all
         positions + block new entries until next MYT midnight.
      2. crypto_profit_halt_pct: mirror of #1 at +X%, locks in gains.

    The catastrophic "drawdown_halt" was removed — its only function over
    crypto_loss_halt_pct was requiring a manual kill-switch reset, which
    contradicted the user's autonomous-operation goal. crypto_kill_switch
    is still respected for manual emergency-stop scenarios.

    Returns: {day_start, drawdown_pct, halt_triggered, enabled,
              today_pnl_pct, loss_halt_fired, profit_halt_fired}
    """
    if current_value is None or current_value <= 0:
        return {"day_start": 0.0, "drawdown_pct": 0.0,
                "halt_triggered": False, "enabled": False}

    from datetime import timezone, timedelta
    MYT = timezone(timedelta(hours=8))
    today_myt = datetime.utcnow().replace(tzinfo=timezone.utc).astimezone(MYT).date()
    today_str = today_myt.isoformat()

    snap_date = _get_setting("crypto_day_start_date") or ""
    try:
        snap_value = float(_get_setting("crypto_day_start_value_usd") or 0)
    except (TypeError, ValueError):
        snap_value = 0.0
    # Format marker: snap_value is "synth" (= principal + Σ realized pre-today)
    # since the dashboard-halt-reconciliation switchover. Missing/old marker →
    # snap_value still has old "midnight account value" semantics → force a
    # re-rollover NOW so the halt threshold matches what the dashboard shows
    # (instead of waiting until next MYT midnight to re-sync).
    snap_format = _get_setting("crypto_day_start_format") or ""

    # New MYT day → re-snapshot using ENTRY-BASIS value, not raw Binance balance.
    # day_start = USDT_at_midnight + Σ(open_qty × entry_price)
    # This makes today_pnl = realized + unrealized reconcile cleanly because the
    # same cost basis (entry price) is used on both sides. Raw Binance balance
    # at midnight bakes in unrealized gains/losses on overnight positions, which
    # then double-count when those positions are eventually sold.
    # Falls back to raw current_value if entry-basis can't be computed.
    if snap_date != today_str or snap_value <= 0:
        snap_for_day = _entry_basis_value(usdt_free=usdt_free)
        if snap_for_day <= 0:
            snap_for_day = current_value  # fallback: keep old behavior
        _set_setting("crypto_day_start_date", today_str)
        _set_setting("crypto_day_start_value_usd", f"{snap_for_day:.4f}")
        _set_setting("crypto_day_start_format", "entry_basis")
        _set_setting("crypto_today_loss_halted", "0")
        _set_setting("crypto_today_profit_halted", "0")
        _set_setting("crypto_today_loss_overridden", "0")
        _set_setting("crypto_today_profit_overridden", "0")
        # Reset today's mid-day deposit counter — yesterday's flows have
        # been finalized into today's snapshot row via past-24h fetch in
        # _write_daily_snapshot, so the so-far counter starts fresh.
        _set_setting("crypto_today_deposits_so_far_usd", "0")
        # Reset rotation counters at midnight
        _set_setting("crypto_rotation_count_today", "0")
        _set_setting("crypto_last_rotation_at", "")
        # Daily snapshot table stores the same value used by halt + dashboard
        # + ROI graph. Single source of truth.
        try:
            _write_daily_snapshot(today_str, snap_for_day, source="entry_basis", overwrite=True)
        except Exception as e:
            log.warning("daily snapshot write failed (non-fatal): %s", e)
        snap_value = snap_for_day
        return {"day_start": snap_value, "drawdown_pct": 0.0,
                "halt_triggered": False, "enabled": True,
                "today_pnl_pct": 0.0, "loss_halt_fired": False, "profit_halt_fired": False}

    today_pnl_pct = (current_value - snap_value) / snap_value * 100.0 if snap_value > 0 else 0.0
    drawdown_pct = -today_pnl_pct if today_pnl_pct < 0 else 0.0

    # NOTE: legacy "drawdown_halt_pct" was removed in favor of the new soft
    # halts below. crypto_kill_switch is still respected for manual emergency
    # stops, but no auto-flip on drawdown anymore.
    enabled = False  # kept for return-shape backward compat
    halt_triggered = False  # ditto

    # === NEW soft auto-resume halts (loss + profit, sell positions, resume tomorrow) ===
    loss_halt_fired = False
    profit_halt_fired = False

    # MIN P&L HALT — fires when today P&L drops to/below the floor (signed).
    # Floor = -5  → halts if P&L ≤ -5% (loss protection)
    # Floor = +6  → halts if P&L ≤ +6% (locks in gains above 6%)
    # One-shot migration: legacy values were stored positive (5 = "halt at -5%").
    # If we detect that pattern AND no migration flag, flip the sign in-place.
    if _get_setting("crypto_halt_pct_signed_v1") != "1":
        try:
            _legacy = float(_get_setting("crypto_loss_halt_pct") or "0")
            if _legacy > 0:
                _set_setting("crypto_loss_halt_pct", f"{-_legacy:.4f}")
                log.info("migrated crypto_loss_halt_pct: %.2f → %.2f (signed semantic)",
                         _legacy, -_legacy)
        except (TypeError, ValueError):
            pass
        _set_setting("crypto_halt_pct_signed_v1", "1")

    if (_get_setting("crypto_loss_halt_enabled") == "on"
            and _get_setting("crypto_today_loss_halted") != "1"
            and _get_setting("crypto_today_loss_overridden") != "1"):
        min_pnl_pct = _f("crypto_loss_halt_pct")  # signed; default migrated above
        if today_pnl_pct <= min_pnl_pct:
            from webapp.models import CryptoRun, db
            ts = datetime.utcnow()
            msg = (
                f"MIN HALT: today P&L {today_pnl_pct:+.2f}% (≤ floor {min_pnl_pct:+.2f}%) — "
                f"closing all positions, halting until next MYT day"
            )
            _set_setting("crypto_today_loss_halted", "1")
            _ghost_pre = _collect_ghost_init_data()
            n_closed = sell_all_open_positions(f"MIN HALT — today {today_pnl_pct:+.2f}%")
            run = CryptoRun(kind="loss_halt", status="ok",
                            started_at=ts, ended_at=ts,
                            summary=f"{msg} ({n_closed} positions closed)")
            db.session.add(run)
            db.session.commit()
            log.warning(msg)
            loss_halt_fired = True
            try:
                from analysis.crypto_ghost import start_ghost
                start_ghost(_ghost_pre, current_value)
            except Exception as _ge:
                log.warning("ghost start failed (non-fatal): %s", _ge)

    # MAX P&L HALT — fires when today P&L rises to/above the ceiling.
    # Ceiling stays positive-only (no use case for negative ceiling).
    if (not loss_halt_fired
            and _get_setting("crypto_profit_halt_enabled") == "on"
            and _get_setting("crypto_today_profit_halted") != "1"
            and _get_setting("crypto_today_profit_overridden") != "1"):
        profit_halt_pct = _f("crypto_profit_halt_pct")
        if today_pnl_pct >= profit_halt_pct:
            from webapp.models import CryptoRun, db
            ts = datetime.utcnow()
            msg = (
                f"MAX HALT: today P&L {today_pnl_pct:+.2f}% (≥ ceiling +{profit_halt_pct:.2f}%) — "
                f"locking in gains, halting until next MYT day"
            )
            _set_setting("crypto_today_profit_halted", "1")
            _ghost_pre = _collect_ghost_init_data()
            n_closed = sell_all_open_positions(f"MAX HALT — today {today_pnl_pct:+.2f}%")
            run = CryptoRun(kind="profit_halt", status="ok",
                            started_at=ts, ended_at=ts,
                            summary=f"{msg} ({n_closed} positions closed)")
            db.session.add(run)
            db.session.commit()
            log.warning(msg)
            profit_halt_fired = True
            try:
                from analysis.crypto_ghost import start_ghost
                start_ghost(_ghost_pre, current_value)
            except Exception as _ge:
                log.warning("ghost start failed (non-fatal): %s", _ge)

    return {"day_start": snap_value, "drawdown_pct": drawdown_pct,
            "halt_triggered": halt_triggered, "enabled": enabled,
            "today_pnl_pct": today_pnl_pct,
            "loss_halt_fired": loss_halt_fired,
            "profit_halt_fired": profit_halt_fired}


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
    """
    out = {
        "stop": None, "target": None, "max_hold": 24, "exit_rule": "stop_target_time",
        "partial_done": False, "original_stop": None,
        "trail_active": False, "trail_high": None,
        "peak_pnl_pct": 0.0,
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
        elif token == "trail_active=1":
            out["trail_active"] = True
        elif token.startswith("trail_high=$"):
            try: out["trail_high"] = float(token.replace("trail_high=$", ""))
            except ValueError: pass
        elif token.startswith("peak_pnl_pct="):
            try: out["peak_pnl_pct"] = float(token.replace("peak_pnl_pct=", ""))
            except ValueError: pass
    return out


def update_peak_pnl(position, current_price: float) -> float:
    """Track the highest unrealized P&L this position has reached. Used by the
    smart-stop trend check (a 'still-up' trend requires holding most of the
    peak gain — if we hit +10% then dropped to +1%, that's reversal, not just
    a wick). Returns the (possibly updated) peak."""
    from webapp.models import db
    entry = float(position.price)
    if entry <= 0:
        return 0.0
    cur_pnl_pct = (current_price - entry) / entry * 100
    meta = parse_entry_notes(position.notes)
    prior_peak = float(meta.get("peak_pnl_pct") or 0.0)
    new_peak = max(prior_peak, cur_pnl_pct)
    # Only write if it actually moved (avoid DB churn)
    if new_peak > prior_peak + 0.1:
        parts = []
        seen = False
        for tok in (position.notes or "").split("·"):
            s = tok.strip()
            if s.startswith("peak_pnl_pct="):
                parts.append(f"peak_pnl_pct={new_peak:.2f}")
                seen = True
            elif s:
                parts.append(s)
        if not seen:
            parts.append(f"peak_pnl_pct={new_peak:.2f}")
        position.notes = " · ".join(parts)
        db.session.commit()
    return new_peak


def execute_sell_limit(position, current_price: float, exit_reason: str,
                       limit_offset_pct: float = 0.3) -> dict:
    """Sell using a LIMIT order instead of MARKET. Limit price = current * (1 - offset%).

    Behavior:
      - Places GTC LIMIT SELL at limit price
      - Fills immediately if best-bid >= limit (normal case → minimal slippage)
      - Sits on book if price drops past limit (gap/crash → no fill yet)
      - Caller is expected to escalate to market sell after a timeout if unfilled

    Returns same shape as execute_sell, plus result['order_id'] for tracking."""
    from webapp.crypto.routes import get_binance_creds
    from webapp.models import CryptoTrade, db

    is_paper = position.is_paper
    result = {"executed": False, "mode": "paper" if is_paper else "live",
              "reason": "", "trade_id": None, "fill_price": None,
              "fill_qty": None, "order_id": None}

    if is_paper:
        # Paper: simulate as immediate fill at current price
        return execute_sell(position, current_price, exit_reason)

    try:
        key, secret = get_binance_creds()
        if not key or not secret:
            result["reason"] = "no API keys"
            return result
        client = _binance_client(key, secret)

        info = client.get_symbol_info(position.symbol)
        pf = next(f for f in info["filters"] if f["filterType"] == "PRICE_FILTER")
        lf = next(f for f in info["filters"] if f["filterType"] == "LOT_SIZE")
        tick_size, step_size = pf["tickSize"], lf["stepSize"]

        base_asset = position.symbol.replace("USDT", "")
        acct = client.get_account()
        bal = next((b for b in acct["balances"] if b["asset"] == base_asset), None)
        free = float(bal["free"]) if bal else 0.0
        if free <= 0:
            result["reason"] = f"no balance ({base_asset})"
            return result

        qty_str = _quantity_to_step_string(free, step_size)
        limit_price = current_price * (1 - limit_offset_pct / 100.0)
        limit_str = _round_to_tick(limit_price, tick_size)

        order = client.create_order(
            symbol=position.symbol, side="SELL", type="LIMIT",
            timeInForce="GTC", quantity=qty_str, price=limit_str,
        )
        oid = str(order.get("orderId", ""))
        status = (order.get("status") or "").upper()
        log.info("LIMIT SELL %s qty=%s @ $%s (offset %.2f%% from $%.6f) status=%s orderId=%s",
                 position.symbol, qty_str, limit_str, limit_offset_pct, current_price, status, oid)

        # If filled immediately, write the trade record now
        if status == "FILLED":
            fills = order.get("fills", [])
            total_qty = sum(float(f["qty"]) for f in fills)
            gross_quote = sum(float(f["qty"]) * float(f["price"]) for f in fills)
            fee_in_usdt = sum(
                float(f.get("commission", 0))
                for f in fills if f.get("commissionAsset") == "USDT"
            )
            net_quote = gross_quote - fee_in_usdt
            avg_price = gross_quote / total_qty if total_qty else 0.0
            sell = CryptoTrade(
                symbol=position.symbol, side="SELL", qty=total_qty, price=avg_price,
                quote_amount=net_quote, executed_at=datetime.utcnow(),
                status="filled", is_paper=False, strategy=position.strategy,
                binance_order_id=oid,
                notes=f"LIVE EXIT (LIMIT) · {exit_reason}",
            )
            db.session.add(sell)
            db.session.commit()
            result.update({"executed": True, "trade_id": sell.id,
                           "fill_price": avg_price, "fill_qty": total_qty,
                           "order_id": oid, "reason": f"limit fill @ ${avg_price:.6f}"})
            # Smart re-entry hook — use max(peak, final) pnl so partial winners
            # still qualify even if the final close was barely positive.
            try:
                entry_price = float(position.price)
                pnl_pct = (avg_price - entry_price) / entry_price * 100
                meta_for_peak = parse_entry_notes(position.notes)
                peak_pct = float(meta_for_peak.get("peak_pnl_pct") or 0.0)
                effective_pnl = max(peak_pct, pnl_pct)
                maybe_setup_reentry(position, avg_price, effective_pnl)
            except Exception as e:
                log.warning("reentry setup (limit) failed for %s: %s", position.symbol, e)
        else:
            # Order placed but not filled yet — caller tracks via order_id
            result.update({"executed": False, "order_id": oid,
                           "reason": f"limit placed @ ${limit_str} ({status})"})
        return result
    except Exception as e:
        result["reason"] = f"limit sell failed: {e}"
        log.exception("execute_sell_limit %s failed", position.symbol)
        return result


def _detect_surge(symbol: str) -> bool:
    """Detect a sudden price+volume surge over the last 15 minutes.
    Returns True if last-15min ROC >= surge_roc_15min_pct AND last-15min volume
    >= surge_vol_mult x avg per-15min volume of the previous 45 minutes.
    Single 1m-klines API call per invocation; caller should gate to rare events.
    Returns False unconditionally when crypto_surge_promote_enabled is off."""
    if (_get_setting("crypto_surge_promote_enabled") or "on").lower() != "on":
        return False
    try:
        roc_pct = float(_get_setting("crypto_surge_roc_15min_pct") or "2.5")
        vol_mult = float(_get_setting("crypto_surge_vol_mult") or "2.0")
    except (TypeError, ValueError):
        roc_pct, vol_mult = 2.5, 2.0
    try:
        from binance.client import Client
        klines = Client().get_klines(symbol=symbol, interval="1m", limit=60)
    except Exception as e:
        log.warning("surge detect %s: kline fetch failed: %s", symbol, e)
        return False
    if not klines or len(klines) < 60:
        return False
    try:
        first_close = float(klines[-16][4])
        last_close = float(klines[-1][4])
        if first_close <= 0:
            return False
        roc = (last_close - first_close) / first_close * 100.0
        if roc < roc_pct:
            return False
        last_15_vol = sum(float(k[5]) for k in klines[-15:])
        prev_45_vol = sum(float(k[5]) for k in klines[-60:-15])
        if prev_45_vol <= 0:
            return False
        prev_15_avg = prev_45_vol / 3.0
        return last_15_vol >= vol_mult * prev_15_avg
    except (ValueError, IndexError) as e:
        log.warning("surge detect %s: parse failed: %s", symbol, e)
        return False


def _compute_reentry_level(sell_price: float, original_entry: float,
                            current_price: float, df_1h=None) -> float | None:
    """Pick the best re-entry level: highest of several support candidates that
    is still BELOW current price. Returns None if no valid level found.

    Candidates considered:
      - sell_price          (psychological anchor — where we exited)
      - original_entry      (cost basis of the trade we just closed)
      - EMA20 (1h)          (dynamic short-term support)
      - low5 × 1.005        (recent swing low)
    The highest level that is still below current is the most natural pullback
    target — closer to current = more likely to be tested.
    """
    candidates = [sell_price, original_entry]
    if df_1h is not None and len(df_1h) >= 20:
        try:
            ema20 = float(df_1h["Close"].astype(float).tail(20).ewm(span=20).mean().iloc[-1])
            low5 = float(df_1h["Low"].astype(float).tail(5).min()) * 1.005
            candidates.extend([ema20, low5])
        except Exception:
            pass
    valid = [c for c in candidates if c is not None and 0 < c < current_price]
    if not valid:
        return None
    return max(valid)  # highest support below current = closest test


def _place_reentry_limit(symbol: str, limit_price: float, size_usd: float) -> str | None:
    """Place a GTC LIMIT BUY order at the computed re-entry level. Returns
    orderId on success, None on failure."""
    from webapp.crypto.routes import get_binance_creds
    try:
        key, secret = get_binance_creds()
        if not key or not secret:
            return None
        client = _binance_client(key, secret)

        info = client.get_symbol_info(symbol)
        if not info or info.get("status") != "TRADING":
            log.info("reentry: %s not TRADING — skip", symbol)
            return None

        # Capital check — need at least size_usd in free USDT
        acct = client.get_account()
        usdt = next((b for b in acct["balances"] if b["asset"] == "USDT"), None)
        free_usdt = float(usdt["free"]) if usdt else 0.0
        if free_usdt < size_usd:
            log.info("reentry: %s skipped — USDT free $%.2f < $%.2f",
                     symbol, free_usdt, size_usd)
            return None

        pf = next(f for f in info["filters"] if f["filterType"] == "PRICE_FILTER")
        lf = next(f for f in info["filters"] if f["filterType"] == "LOT_SIZE")
        tick_size, step_size = pf["tickSize"], lf["stepSize"]

        price_str = _round_to_tick(limit_price, tick_size)
        qty = size_usd / float(price_str)
        qty_str = _quantity_to_step_string(qty, step_size)
        if float(qty_str) <= 0:
            return None

        order = client.create_order(
            symbol=symbol, side="BUY", type="LIMIT",
            timeInForce="GTC", quantity=qty_str, price=price_str,
        )
        oid = str(order.get("orderId", ""))
        log.info("REENTRY LIMIT %s qty=%s @ $%s orderId=%s",
                 symbol, qty_str, price_str, oid)
        return oid or None
    except Exception as e:
        log.warning("reentry place %s failed: %s", symbol, e)
        return None


def maybe_setup_reentry(closed_position, sell_price: float, sell_pnl_pct: float) -> None:
    """After a profitable exit, evaluate whether to set up a re-entry limit
    order on Binance. Called from execute_sell (and other exit paths) after
    a SELL trade row is written.

    Conditions to fire:
      - sell_pnl_pct >= 2.0% (only meaningful winners)
      - macro intact (BTC > SMA50)
      - feature enabled in settings
      - no active re-entry already pending for this symbol
    """
    if (_get_setting("crypto_reentry_enabled") or "on").lower() != "on":
        return
    if sell_pnl_pct < 2.0:
        return  # not a meaningful winner
    if closed_position.is_paper:
        return  # paper mode skips
    symbol = closed_position.symbol

    # Skip if a re-entry order for this symbol is already pending
    import json
    pending_raw = _get_setting("crypto_reentry_orders") or "[]"
    try:
        pending = json.loads(pending_raw)
    except Exception:
        pending = []
    if any(p.get("symbol") == symbol for p in pending):
        log.info("reentry %s skipped — already have pending order", symbol)
        return

    # Macro check: BTC above SMA50 on 4h
    try:
        from analysis.crypto_data import load_cached
        from analysis.crypto_strategies import _btc_trend_ok
        btc_df = load_cached("BTCUSDT", "4h")
        if btc_df is None or btc_df.empty or not _btc_trend_ok(btc_df):
            log.info("reentry %s skipped — BTC regime not OK", symbol)
            return
    except Exception as e:
        log.warning("reentry %s: btc check failed: %s", symbol, e)
        return

    # Compute re-entry level
    try:
        from analysis.crypto_data import load_cached
        from binance.client import Client
        cur_price = float(Client().get_ticker(symbol=symbol)["lastPrice"])
        df_1h = load_cached(symbol, "1h")
        original_entry = float(closed_position.price)
        level = _compute_reentry_level(sell_price, original_entry, cur_price, df_1h)
        if level is None:
            log.info("reentry %s skipped — no valid level (cur $%.6f, sell $%.6f)",
                     symbol, cur_price, sell_price)
            return
    except Exception as e:
        log.warning("reentry %s: level compute failed: %s", symbol, e)
        return

    # Size: 75% of max_position_usd (smaller bet on second entry)
    try:
        max_pos = _f("crypto_max_position_usd")
    except Exception:
        max_pos = 60.0
    size_usd = max_pos * 0.75

    oid = _place_reentry_limit(symbol, level, size_usd)
    if not oid:
        return

    # Persist to watchlist for cancellation/fill tracking
    pending.append({
        "symbol": symbol,
        "order_id": oid,
        "limit_price": level,
        "sell_price": sell_price,
        "size_usd": size_usd,
        "placed_at": datetime.utcnow().isoformat(),
        "original_entry": float(closed_position.price),
        "original_strategy": closed_position.strategy,
    })
    _set_setting("crypto_reentry_orders", json.dumps(pending))
    log.info("reentry %s LIMIT placed at $%.6f (size $%.2f, orderId %s)",
             symbol, level, size_usd, oid)


def process_reentry_orders() -> int:
    """Called from the main loop. Polls each pending re-entry order:
      - If FILLED: writes a trade record so bot can manage it; removes from list
      - If CANCELLED externally: removes from list
      - If 24h elapsed, +5% new high since sell, -12% crash, or BTC regime broke:
        cancels on Binance, removes from list
    Returns number of changes made."""
    from webapp.models import CryptoTrade, db
    import json
    pending_raw = _get_setting("crypto_reentry_orders") or "[]"
    try:
        pending = json.loads(pending_raw)
    except Exception:
        pending = []
    if not pending:
        return 0

    from webapp.crypto.routes import get_binance_creds
    from binance.client import Client
    try:
        key, secret = get_binance_creds()
        if not key or not secret:
            return 0
        client = _binance_client(key, secret)
    except Exception:
        return 0

    pub = Client()
    new_pending = []
    changes = 0
    for p in pending:
        symbol = p["symbol"]
        oid = int(p["order_id"])
        try:
            order = client.get_order(symbol=symbol, orderId=oid)
            status = (order.get("status") or "").upper()

            if status == "FILLED":
                executed_qty = float(order.get("executedQty") or 0)
                cumm_quote   = float(order.get("cummulativeQuoteQty") or 0)
                if executed_qty > 0:
                    avg_price = cumm_quote / executed_qty
                    notes = (
                        f"LIVE · stop=${avg_price*0.95:.6g} · target=${avg_price*1.06:.6g} · "
                        f"max_hold=12 · exit=stop_target_time · reentry_pullback · "
                        f"orig_sell=${p['sell_price']} · orig_strategy={p.get('original_strategy', '?')}"
                    )
                    trade = CryptoTrade(
                        symbol=symbol, side="BUY", qty=executed_qty,
                        price=avg_price, quote_amount=cumm_quote,
                        executed_at=datetime.utcnow(), status="filled",
                        is_paper=False, strategy="reentry_pullback",
                        binance_order_id=str(oid), notes=notes,
                    )
                    db.session.add(trade)
                    db.session.commit()
                    log.info("REENTRY FILLED %s qty=%s @ $%.6f - bot now manages",
                             symbol, executed_qty, avg_price)
                    changes += 1
                continue  # don't re-add to pending

            if status in ("CANCELED", "EXPIRED", "REJECTED"):
                log.info("reentry %s order %s already %s — clearing tracker",
                         symbol, oid, status)
                changes += 1
                continue

            # Status NEW or PARTIALLY_FILLED — check cancel triggers
            from datetime import timedelta as _td
            placed_at = datetime.fromisoformat(p["placed_at"])
            elapsed_h = (datetime.utcnow() - placed_at).total_seconds() / 3600
            cur = float(pub.get_ticker(symbol=symbol)["lastPrice"])
            sell_px = float(p["sell_price"])

            should_cancel = False
            cancel_reason = ""
            if elapsed_h >= 24:
                should_cancel = True; cancel_reason = "24h elapsed"
            elif cur > sell_px * 1.05:
                should_cancel = True; cancel_reason = f"new high (cur ${cur} > sell ${sell_px} × 1.05)"
            # NOTE: 'deep crash' check removed — for a LIMIT BUY at $X, if price
            # falls past $X * 0.88, the order would have already filled at $X
            # on the way down. The check was dead code. Downside protection
            # comes from the position's stop loss (set when fill triggers
            # process_reentry_orders to write the trade row).
            else:
                # BTC regime check — only every ~10 cycles (cheap by hour)
                try:
                    from analysis.crypto_data import load_cached
                    from analysis.crypto_strategies import _btc_trend_ok
                    btc_df = load_cached("BTCUSDT", "4h")
                    if btc_df is not None and not _btc_trend_ok(btc_df):
                        should_cancel = True; cancel_reason = "BTC regime broke"
                except Exception:
                    pass

            if should_cancel:
                try:
                    client.cancel_order(symbol=symbol, orderId=oid)
                except Exception as e:
                    if "unknown order" not in str(e).lower():
                        log.warning("reentry cancel %s failed: %s", oid, e)
                log.info("REENTRY CANCEL %s order %s: %s", symbol, oid, cancel_reason)
                changes += 1
                continue

            # Keep watching
            new_pending.append(p)
        except Exception as e:
            log.warning("reentry process %s order %s: %s", symbol, oid, e)
            new_pending.append(p)

    if changes > 0:
        _set_setting("crypto_reentry_orders", json.dumps(new_pending))
    return changes


_SMART_LIMIT_STRATEGIES = {"momentum_surge", "breakout_4h", "breakout_1h"}


def _compute_smart_entry_level(symbol: str, current_price: float) -> float | None:
    """Pick a smart LIMIT BUY price for a momentum/breakout signal.

    Returns the HIGHEST of these candidates that is still BELOW current price:
      - EMA14 on 1h (short-term dynamic support)
      - lowest LOW of the last 6 1h-bars (recent support test)
      - current_price × 0.98 (-2% pullback floor)

    The highest below-current value is the most plausible pullback level
    price would test. Returns None if no valid level (e.g., current is already
    BELOW all candidates — coin is already dumping, don't try to catch).
    """
    from analysis.crypto_data import load_cached
    df_1h = load_cached(symbol, "1h")
    if df_1h is None or df_1h.empty or len(df_1h) < 14:
        return None
    try:
        closes = df_1h["Close"].astype(float)
        lows = df_1h["Low"].astype(float)
        ema14 = float(closes.tail(14).ewm(span=14).mean().iloc[-1])
        swing_low_6h = float(lows.tail(6).min()) * 1.003  # tiny buffer above the low
        pullback_2pct = current_price * 0.98
    except Exception as e:
        log.warning("smart entry %s: failed to compute candidates: %s", symbol, e)
        return None
    candidates = [c for c in (ema14, swing_low_6h, pullback_2pct)
                  if c is not None and 0 < c < current_price]
    if not candidates:
        return None
    return max(candidates)


def _place_smart_entry_limit(intent: dict, smart_price: float, size_usd: float) -> str | None:
    """Place a GTC LIMIT BUY order at the computed smart entry level."""
    from webapp.crypto.routes import get_binance_creds
    try:
        key, secret = get_binance_creds()
        if not key or not secret:
            return None
        client = _binance_client(key, secret)
        symbol = intent["symbol"]
        info = client.get_symbol_info(symbol)
        if not info or info.get("status") != "TRADING":
            return None
        # Capital check
        acct = client.get_account()
        usdt = next((b for b in acct["balances"] if b["asset"] == "USDT"), None)
        free_usdt = float(usdt["free"]) if usdt else 0.0
        if free_usdt < size_usd:
            log.info("smart entry %s: USDT free $%.2f < $%.2f", symbol, free_usdt, size_usd)
            return None
        pf = next(f for f in info["filters"] if f["filterType"] == "PRICE_FILTER")
        lf = next(f for f in info["filters"] if f["filterType"] == "LOT_SIZE")
        tick, step = pf["tickSize"], lf["stepSize"]
        price_str = _round_to_tick(smart_price, tick)
        qty = size_usd / float(price_str)
        qty_str = _quantity_to_step_string(qty, step)
        if float(qty_str) <= 0:
            return None
        order = client.create_order(
            symbol=symbol, side="BUY", type="LIMIT",
            timeInForce="GTC", quantity=qty_str, price=price_str,
        )
        return str(order.get("orderId", "")) or None
    except Exception as e:
        log.warning("smart entry place %s failed: %s", intent.get("symbol"), e)
        return None


def process_smart_entry_orders() -> int:
    """Poll pending smart-entry LIMIT BUY orders:
      - FILLED: write trade record + apply intent's stop/target
      - 30+ min elapsed: cancel
      - Price ran +3% past signal (we're chasing now): cancel
    Returns count of changes.
    """
    from webapp.models import CryptoTrade, db
    import json
    raw = _get_setting("crypto_smart_entry_orders") or "[]"
    try:
        pending = json.loads(raw)
    except Exception:
        pending = []
    if not pending:
        return 0
    from webapp.crypto.routes import get_binance_creds
    from binance.client import Client
    try:
        key, secret = get_binance_creds()
        if not key or not secret:
            return 0
        client = _binance_client(key, secret)
    except Exception:
        return 0
    pub = Client()
    new_pending = []
    changes = 0
    for p in pending:
        sym = p["symbol"]
        oid = int(p["order_id"])
        try:
            order = client.get_order(symbol=sym, orderId=oid)
            status = (order.get("status") or "").upper()

            if status == "FILLED":
                qty = float(order.get("executedQty") or 0)
                quote = float(order.get("cummulativeQuoteQty") or 0)
                if qty > 0:
                    avg_price = quote / qty
                    intent = p["intent"]
                    # Adjust stop/target based on actual fill (better than scan price)
                    fill_pct_below_scan = (intent["entry_price"] - avg_price) / intent["entry_price"]
                    stop = avg_price * (1 - (intent["entry_price"] - intent["stop_price"]) / intent["entry_price"])
                    target = avg_price * (1 + (intent["target_price"] - intent["entry_price"]) / intent["entry_price"])
                    notes = (
                        f"LIVE · stop=${stop:.6g} · target=${target:.6g} · "
                        f"max_hold={intent['max_hold_bars']} · exit={intent['exit_rule']} · "
                        f"{intent.get('reason','')} · smart_entry (limit @${p['limit_price']:.6g})"
                    )
                    trade = CryptoTrade(
                        symbol=sym, side="BUY", qty=qty, price=avg_price,
                        quote_amount=quote, executed_at=datetime.utcnow(),
                        status="filled", is_paper=False, strategy=intent["strategy"],
                        binance_order_id=str(oid), notes=notes,
                    )
                    db.session.add(trade)
                    db.session.commit()
                    log.info("SMART ENTRY FILLED %s qty=%s @ $%.6f", sym, qty, avg_price)
                    changes += 1
                continue

            if status in ("CANCELED", "EXPIRED", "REJECTED"):
                log.info("smart entry %s order %s %s — clearing", sym, oid, status)
                changes += 1
                continue

            # Still NEW — check cancel conditions
            placed_at = datetime.fromisoformat(p["placed_at"])
            elapsed_min = (datetime.utcnow() - placed_at).total_seconds() / 60
            cur = float(pub.get_ticker(symbol=sym)["lastPrice"])
            signal_price = float(p["signal_price"])

            cancel = False
            reason = ""
            if elapsed_min >= 30:
                cancel = True; reason = "30min elapsed"
            elif cur > signal_price * 1.03:
                cancel = True; reason = f"chase guard (cur ${cur} > signal ${signal_price} × 1.03)"

            if cancel:
                try:
                    client.cancel_order(symbol=sym, orderId=oid)
                except Exception as e:
                    if "unknown order" not in str(e).lower():
                        log.warning("smart entry cancel %s failed: %s", oid, e)
                log.info("SMART ENTRY CANCEL %s order %s: %s", sym, oid, reason)
                changes += 1
                continue
            new_pending.append(p)
        except Exception as e:
            log.warning("smart entry process %s order %s: %s", sym, oid, e)
            new_pending.append(p)

    if changes > 0:
        _set_setting("crypto_smart_entry_orders", json.dumps(new_pending))
    return changes


def _signal_still_valid(intent: dict) -> tuple[bool, str]:
    """Pre-flight freshness check just before live BUY.
    Signals are computed on the close of the previous bar, but execution may be
    delayed (regime gate, surge gate, kline lag). Between scan-time and now,
    price can have fallen back through SMA50 or far below the breakout level,
    turning a "fresh breakout" into a knife-catch (whipsaw).

    Re-fetches the latest 1h klines from Binance and confirms:
      1) Current price has not fallen more than freshness_max_drift_pct below
         the intent's entry_price (default 0.3%).
      2) If exit_rule is sma50_break, current close is still > SMA50 on 1h.

    Returns (ok, reason).
    """
    if (_get_setting("crypto_signal_freshness_enabled") or "on").lower() != "on":
        return True, "freshness check disabled"
    try:
        max_drift_pct = float(_get_setting("crypto_signal_max_drift_pct") or "0.3")
    except (TypeError, ValueError):
        max_drift_pct = 0.3
    symbol = intent.get("symbol")
    entry_price = float(intent.get("entry_price") or 0)
    if not symbol or entry_price <= 0:
        return True, "no symbol/entry to validate"
    try:
        from binance.client import Client
        # 60 1h-bars: enough for SMA50 + 10 bars of headroom.
        klines = Client().get_klines(symbol=symbol, interval="1h", limit=60)
    except Exception as e:
        log.warning("freshness %s: kline fetch failed: %s", symbol, e)
        return True, "kline fetch failed (allowing)"
    if not klines or len(klines) < 51:
        return True, "not enough bars (allowing)"
    try:
        closes = [float(k[4]) for k in klines]
        current = closes[-1]
        if current <= 0:
            return True, "bad price (allowing)"
        drift_pct = (current - entry_price) / entry_price * 100.0
        if drift_pct < -max_drift_pct:
            return False, (
                f"signal stale: price ${_fmt_price(current)} drifted {drift_pct:+.2f}% "
                f"from entry ${_fmt_price(entry_price)} (max {-max_drift_pct:.2f}%)"
            )
        # SMA50 re-check for breakout-style entries
        exit_rule = (intent.get("exit_rule") or "").lower()
        if exit_rule == "sma50_break":
            sma50 = sum(closes[-50:]) / 50.0
            if current <= sma50:
                return False, (
                    f"signal stale: close ${_fmt_price(current)} fell back below "
                    f"SMA50 ${_fmt_price(sma50)} before entry"
                )
        return True, "ok"
    except (ValueError, IndexError, ZeroDivisionError) as e:
        log.warning("freshness %s: parse failed: %s", symbol, e)
        return True, "parse failed (allowing)"


def _round_to_tick(price: float, tick_size: str) -> str:
    """Round a price DOWN to a valid multiple of Binance's tickSize. Decimal-safe.

    quantize() only adjusts decimal places — it doesn't snap to multiples. For
    DOGE with tick=0.00001, the price must be an integer multiple of 0.00001
    (so 0.11479 is valid, 0.11479950 is NOT, even though both have ≤8 decimals).
    """
    from decimal import Decimal, ROUND_DOWN
    tick = Decimal(tick_size)
    p = Decimal(str(price))
    snapped = (p / tick).to_integral_value(rounding=ROUND_DOWN) * tick
    # quantize to tick's decimal places (canonicalize formatting)
    snapped = snapped.quantize(tick)
    return format(snapped, "f")


def _cancel_trail_order(client, symbol: str, order_id: str) -> bool:
    """Cancel an existing trail stop-limit order. Returns True if cancelled or
    already gone (filled/cancelled previously). Returns False on hard failure."""
    if not order_id:
        return True
    try:
        client.cancel_order(symbol=symbol, orderId=int(order_id))
        log.info("trail: cancelled stop-limit order %s on %s", order_id, symbol)
        return True
    except Exception as e:
        # "Unknown order sent" = already filled or already cancelled — OK
        msg = str(e).lower()
        if "unknown order" in msg or "-2011" in msg:
            return True
        log.warning("trail: cancel order %s on %s failed: %s", order_id, symbol, e)
        return False


def _place_trail_stop_limit(position, trail_high: float) -> str | None:
    """Place a Binance STOP_LOSS_LIMIT order to handle the trail exit on the
    exchange (avoids market-order slippage during stop cascades).

      stopPrice  = trail_high × (1 - trail_pct/100)         (trigger)
      price      = stopPrice  × (1 - 0.5/100)               (0.5% below trigger)

    trail_pct is read from setting `crypto_surge_trail_pct` (default 3.0) — the
    same value the in-loop trail check uses, so on-exchange and in-loop levels
    stay in sync when the setting changes.

    Returns the new orderId (str) on success, or None on failure / no-op.
    Caller is responsible for cancelling any prior trail order first.
    """
    if position.is_paper:
        return None  # paper mode — no real order
    from webapp.crypto.routes import get_binance_creds
    try:
        key, secret = get_binance_creds()
        if not key or not secret:
            return None
        client = _binance_client(key, secret)

        symbol = position.symbol
        info = client.get_symbol_info(symbol)
        if not info or info.get("status") != "TRADING":
            return None
        price_filter = next(f for f in info["filters"] if f["filterType"] == "PRICE_FILTER")
        lot_filter   = next(f for f in info["filters"] if f["filterType"] == "LOT_SIZE")
        tick_size = price_filter["tickSize"]
        step_size = lot_filter["stepSize"]

        # Use actual held balance — partial-take / fee skim may have shrunk qty
        base_asset = symbol.replace("USDT", "")
        acct = client.get_account()
        bal = next((b for b in acct["balances"] if b["asset"] == base_asset), None)
        free = float(bal["free"]) if bal else 0.0
        if free <= 0:
            log.warning("trail: %s no free balance to protect", symbol)
            return None
        qty_str = _quantity_to_step_string(free, step_size)
        if float(qty_str) <= 0:
            return None

        try:
            trail_pct = float(_get_setting("crypto_surge_trail_pct") or "3.0")
        except (TypeError, ValueError):
            trail_pct = 3.0
        stop_mult  = 1 - trail_pct / 100.0
        limit_mult = stop_mult * 0.995   # 0.5% below trigger
        stop_price  = _round_to_tick(trail_high * stop_mult,  tick_size)
        limit_price = _round_to_tick(trail_high * limit_mult, tick_size)

        order = client.create_order(
            symbol=symbol,
            side="SELL",
            type="STOP_LOSS_LIMIT",
            timeInForce="GTC",
            quantity=qty_str,
            stopPrice=stop_price,
            price=limit_price,
        )
        oid = str(order.get("orderId", ""))
        log.info("trail: %s placed STOP_LIMIT qty=%s stop=$%s limit=$%s orderId=%s",
                 symbol, qty_str, stop_price, limit_price, oid)
        return oid or None
    except Exception as e:
        log.warning("trail: place stop-limit on %s failed: %s", position.symbol, e)
        return None


def reconcile_trail_order(position) -> bool:
    """If the on-exchange trail stop-limit has filled, write the SELL trade
    record into our DB and return True. Caller should then skip further
    processing of this position in this cycle. Returns False if the order is
    still open / pending / cancelled / not present (position remains live)."""
    from webapp.models import CryptoTrade, db
    if position.is_paper:
        return False
    # Extract trail_order_id from notes
    order_id = None
    for tok in (position.notes or "").split("·"):
        s = tok.strip()
        if s.startswith("trail_order_id="):
            order_id = s.split("=", 1)[1].strip()
            break
    if not order_id:
        return False
    from webapp.crypto.routes import get_binance_creds
    try:
        key, secret = get_binance_creds()
        if not key or not secret:
            return False
        client = _binance_client(key, secret)
        order = client.get_order(symbol=position.symbol, orderId=int(order_id))
    except Exception as e:
        log.warning("reconcile_trail %s order %s: query failed: %s",
                    position.symbol, order_id, e)
        return False
    status = (order.get("status") or "").upper()
    if status != "FILLED":
        return False
    # Order filled on exchange — reconcile into DB
    executed_qty = float(order.get("executedQty") or 0)
    cumm_quote   = float(order.get("cummulativeQuoteQty") or 0)
    if executed_qty <= 0:
        return False
    avg_price = cumm_quote / executed_qty
    # Subtract USDT commission (matches our updated execute_sell convention)
    # cummulativeQuoteQty already nets out commission for SELL fills paid in USDT.
    sell = CryptoTrade(
        symbol=position.symbol, side="SELL", qty=executed_qty, price=avg_price,
        quote_amount=cumm_quote, executed_at=datetime.utcnow(),
        status="filled", is_paper=False, strategy=position.strategy,
        binance_order_id=str(order.get("orderId", "")),
        notes=f"LIVE EXIT · trail stop-limit filled on exchange (orderId={order_id})",
    )
    db.session.add(sell)
    db.session.commit()
    entry_price = float(position.price)
    pnl_pct = (avg_price - entry_price) / entry_price * 100
    log.info("RECONCILED trail order %s on %s — SELL %s @ $%.6f (P&L %+.2f%%)",
             order_id, position.symbol, executed_qty, avg_price, pnl_pct)
    return True


def _set_trail_in_notes(position, high_water: float, activate: bool = False) -> None:
    """Update notes to set or refresh trail-mode state. Idempotent.

    Side effect (LIVE only): places/replaces a Binance STOP_LOSS_LIMIT order at
    trail_high × 0.97 (trigger) / × 0.965 (limit). Throttled — only replaces the
    on-exchange order if the new high is >= 0.5% above the prior trail_high.

    Also sets partial_done=1 when activating trail mode — partial-take would
    otherwise still fire at +4% and tighten the stop, defeating the trail
    purpose. In trail mode, the trail_stop IS the only exit (plus time-stop).
    """
    from webapp.models import db
    parts: list[str] = []
    seen_active = False
    seen_high = False
    seen_partial = False
    prior_trail_high: float | None = None
    prior_order_id: str | None = None
    if position.notes:
        for token in position.notes.split("·"):
            t = token.strip()
            if t == "trail_active=1":
                seen_active = True
                parts.append(t)
            elif t == "partial_done=1":
                seen_partial = True
                parts.append(t)
            elif t.startswith("trail_high=$"):
                seen_high = True
                try:
                    prior_trail_high = float(t.split("=$", 1)[1])
                except (ValueError, IndexError):
                    prior_trail_high = None
                parts.append(f"trail_high=${_fmt_price(high_water)}")
            elif t.startswith("trail_order_id="):
                try:
                    prior_order_id = t.split("=", 1)[1].strip()
                except IndexError:
                    prior_order_id = None
                # don't append; we may rewrite with a new ID below
            elif t:
                parts.append(t)
    activating_now = activate and not seen_active
    if activating_now:
        parts.append("trail_active=1")
    if not seen_high:
        parts.append(f"trail_high=${_fmt_price(high_water)}")
    # Block partial-take whenever trail is active (or being activated). Without
    # this, +4% partial would tighten the stop AND make the trail meaningless.
    if (activating_now or seen_active) and not seen_partial:
        parts.append("partial_done=1")

    # On-exchange order management:
    #  - place new order when first activating
    #  - place new order if trail is active but no order exists (backfill case)
    #  - replace when high moves up at least 0.5% (avoid hammering Binance)
    new_order_id = prior_order_id
    if not position.is_paper:
        needs_refresh = activating_now or (seen_active and not prior_order_id)
        if prior_trail_high and prior_trail_high > 0:
            uplift_pct = (high_water - prior_trail_high) / prior_trail_high * 100
            if uplift_pct >= 0.5:
                needs_refresh = True
        if needs_refresh:
            from webapp.crypto.routes import get_binance_creds
            try:
                key, secret = get_binance_creds()
                if key and secret and prior_order_id:
                    client = _binance_client(key, secret)
                    _cancel_trail_order(client, position.symbol, prior_order_id)
            except Exception as e:
                log.warning("trail: pre-place cancel sequence failed on %s: %s",
                            position.symbol, e)
            new_order_id = _place_trail_stop_limit(position, high_water) or new_order_id

    if new_order_id:
        parts.append(f"trail_order_id={new_order_id}")

    position.notes = " · ".join(parts)
    db.session.commit()


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
        # Dust filter: ignore positions worth less than $5 — Binance MIN_NOTIONAL
        # rejects sells under $5/$10 anyway, so tracking these leads to repeated
        # SELL FAILED warnings and clutters the dashboard with un-closeable dust.
        residual_value = qty * float(last_buy.price)
        if residual_value < 5.0:
            continue
        # Attach the remaining net qty (post-partial-sells) so callers can
        # compute correct unrealized P&L. last_buy.qty is the ORIGINAL buy
        # qty; _remaining_qty is what's actually still open.
        last_buy._remaining_qty = qty
        result.append(last_buy)
    return result


# Backward-compat alias
def _open_paper_positions() -> list:
    return _open_positions(is_paper=True)


def _last_losing_exit_at(symbol: str, is_paper: bool):
    """Timestamp of the most recent losing SELL on `symbol` for the given mode,
    or None if no losing exit on record. FIFO-paired against prior BUYs."""
    from webapp.models import CryptoTrade
    trades = (
        CryptoTrade.query
        .filter_by(symbol=symbol, is_paper=is_paper)
        .filter(CryptoTrade.strategy != "manual_liquidation")
        .order_by(CryptoTrade.executed_at)
        .all()
    )
    if not trades:
        return None
    last_sell = None
    for t in reversed(trades):
        if t.side == "SELL":
            last_sell = t
            break
    if last_sell is None:
        return None
    buys: list[list] = []
    def _consume(buys: list[list], qty: float) -> float:
        cost = 0.0
        sq = qty
        while sq > 1e-9 and buys:
            bq, bv = buys[0]
            consumed = min(bq, sq)
            cost += bv * (consumed / bq) if bq > 0 else 0.0
            buys[0][0] -= consumed
            buys[0][1] -= bv * (consumed / bq) if bq > 0 else 0.0
            sq -= consumed
            if buys[0][0] <= 1e-9:
                buys.pop(0)
        return cost
    for t in trades:
        if t.id == last_sell.id:
            break
        if t.side == "BUY":
            bv = float(t.quote_amount or float(t.price) * float(t.qty))
            buys.append([float(t.qty), bv])
        elif t.side == "SELL":
            _consume(buys, float(t.qty))
    cost = _consume(buys, float(last_sell.qty))
    sell_value = float(last_sell.quote_amount or float(last_sell.price) * float(last_sell.qty))
    if sell_value < cost:
        return last_sell.executed_at
    return None


def _rotation_eligible_signal(intent: dict) -> tuple[bool, str]:
    """Is this incoming signal high-conviction enough to justify rotation?
    Returns (yes, reason_or_score_string)."""
    try:
        min_vol = float(_get_setting("crypto_rotation_min_vol_ratio") or "5.0")
    except (TypeError, ValueError):
        min_vol = 5.0
    # The intent's "reason" field is typed like "24h vol=8.0x, +7.1%, RSI=66"
    reason = intent.get("reason", "")
    import re
    m_vol = re.search(r"vol=([\d.]+)x", reason)
    m_chg = re.search(r"([+-][\d.]+)%", reason)
    m_rsi = re.search(r"RSI=([\d]+)", reason)
    if not (m_vol and m_chg and m_rsi):
        return False, "could not parse signal strength"
    vol = float(m_vol.group(1))
    chg = float(m_chg.group(1))
    rsi = int(m_rsi.group(1))
    if vol < min_vol:
        return False, f"vol {vol:.1f}x < threshold {min_vol:.1f}x"
    if chg < 5.0:
        return False, f"24h change {chg:+.1f}% < 5%"
    if not (60 <= rsi <= 75):
        return False, f"RSI {rsi} outside 60-75"
    return True, f"vol={vol:.1f}x, +{chg:.1f}%, RSI={rsi}"


def _rotation_find_victim(is_paper: bool):
    """Pick the weakest open position eligible to be kicked. Returns the
    position object or None. Eligibility:
      - pnl_usd < -$0.50 (clearly losing)
      - held >= 60 minutes (gave it time)
      - not partial_done (don't kick post-partial runners)
      - not trail_active (don't kick surging trades)
    Picks the LOWEST pnl_pct among eligible."""
    from datetime import datetime as _dt, timedelta as _td
    open_pos = _open_positions(is_paper=is_paper)
    if not open_pos:
        return None
    # Need current prices for pnl calc
    try:
        client = _binance_client()
        prices = {t["symbol"]: float(t["price"]) for t in client.get_all_tickers()}
    except Exception:
        return None
    candidates = []
    for p in open_pos:
        meta = parse_entry_notes(p.notes)
        if meta.get("partial_done") or meta.get("trail_active"):
            continue
        held = (_dt.utcnow() - p.executed_at).total_seconds() / 60.0
        if held < 60:
            continue
        cur = prices.get(p.symbol)
        if cur is None:
            continue
        qty = float(getattr(p, "_remaining_qty", p.qty))
        entry = float(p.price)
        if entry <= 0:
            continue
        pnl_usd = (cur - entry) * qty
        pnl_pct = (cur - entry) / entry * 100
        if pnl_usd >= -0.50:
            continue
        candidates.append((pnl_pct, pnl_usd, p, cur))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])  # most negative first
    return candidates[0]  # (pnl_pct, pnl_usd, position, cur_price)


def _try_rotation(intent: dict, mode: str) -> tuple[bool, str]:
    """Attempt smart rotation: sell weakest losing position to make room for
    a high-conviction new signal. Returns (rotated, reason).
    Caller must have already confirmed max_concurrent blocked the entry."""
    from datetime import datetime as _dt, timedelta as _td
    if (_get_setting("crypto_rotation_enabled") or "off").lower() != "on":
        return False, "rotation disabled"
    # Halt active → don't rotate (bot already paused for a reason)
    if (_get_setting("crypto_today_loss_halted") == "1"
            or _get_setting("crypto_today_profit_halted") == "1"):
        return False, "halt active, skipping rotation"
    # Max rotations per day
    try:
        max_day = int(float(_get_setting("crypto_rotation_max_per_day") or "2"))
    except (TypeError, ValueError):
        max_day = 2
    try:
        count_today = int(_get_setting("crypto_rotation_count_today") or "0")
    except (TypeError, ValueError):
        count_today = 0
    if count_today >= max_day:
        return False, f"daily rotation cap reached ({count_today}/{max_day})"
    # 30-min cooldown between rotations
    last_iso = _get_setting("crypto_last_rotation_at") or ""
    if last_iso:
        try:
            last_dt = _dt.fromisoformat(last_iso)
            if (_dt.utcnow() - last_dt) < _td(minutes=30):
                mins_left = int(30 - (_dt.utcnow() - last_dt).total_seconds() / 60)
                return False, f"rotation cooldown ({mins_left}min left)"
        except ValueError:
            pass
    # Signal strength gate
    ok_sig, sig_info = _rotation_eligible_signal(intent)
    if not ok_sig:
        return False, f"signal not strong enough: {sig_info}"
    # Find victim
    is_paper = (mode == "paper")
    victim = _rotation_find_victim(is_paper)
    if victim is None:
        return False, "no kickable position (all profitable, recent, or protected)"
    pnl_pct, pnl_usd, pos, cur_price = victim
    log.info("ROTATION: kicking %s (pnl %+.2f%%, $%+.2f) for %s (%s)",
             pos.symbol, pnl_pct, pnl_usd, intent["symbol"], sig_info)
    # Execute SELL on victim with rotation marker
    reason = f"rotation: making room for {intent['symbol']}"
    res = execute_sell(pos, cur_price, reason)
    if not res.get("executed"):
        log.error("ROTATION ABORT: victim sell failed — %s", res.get("reason"))
        return False, f"victim sell failed: {res.get('reason')}"
    # Record rotation event
    _set_setting("crypto_rotation_count_today", str(count_today + 1))
    _set_setting("crypto_last_rotation_at", _dt.utcnow().isoformat())
    return True, f"kicked {pos.symbol} for {intent['symbol']}"


def _check_guardrails(intent: dict, mode: str, client=None, *, rotation_bypass: bool = False) -> tuple[bool, str]:
    """Return (ok, reason). Any failure aborts execution.
    rotation_bypass: when True, skip max_concurrent and loss_cooldown (used
    after a successful rotation sell freed a slot for this specific entry)."""
    if _get_setting("crypto_kill_switch") == "on":
        return False, "kill switch is ON"

    # Daily auto-resume halts: today's P&L breached loss/profit threshold;
    # all positions were sold, no new entries until next MYT midnight.
    if _get_setting("crypto_today_loss_halted") == "1":
        return False, "today's loss halt active — auto-resumes at next MYT midnight"
    if _get_setting("crypto_today_profit_halted") == "1":
        return False, "today's profit halt active — auto-resumes at next MYT midnight"

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
    if not rotation_bypass and len(open_positions) >= max_concurrent:
        return False, f"max concurrent reached ({len(open_positions)}/{max_concurrent})"

    # Don't double up on the same symbol
    if any(p.symbol == intent["symbol"] for p in open_positions):
        return False, f"already have open position in {intent['symbol']}"

    # Per-coin cooldown after a losing exit. The market regime that produced
    # the loss is usually still in effect for a few hours; re-entering the
    # same setup is the SAHARA bug (sold at stop, re-bought 11 min later).
    # Skipped when rotation_bypass is set — rotation already established the
    # new entry is high-conviction and should override cooldown on different coin.
    if not rotation_bypass:
        try:
            cooldown_h = float(_get_setting("crypto_loss_cooldown_hours") or "4")
        except (TypeError, ValueError):
            cooldown_h = 4.0
        if cooldown_h > 0:
            last_loss = _last_losing_exit_at(intent["symbol"], is_paper_mode)
            if last_loss is not None:
                from datetime import datetime as _dt, timedelta as _td
                elapsed = _dt.utcnow() - last_loss
                cool_td = _td(hours=cooldown_h)
                if elapsed < cool_td:
                    mins_left = int((cool_td - elapsed).total_seconds() / 60)
                    return False, f"cooldown after loss on {intent['symbol']} ({mins_left}min remaining)"

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
        # Smart rotation: if max_concurrent is the blocker, try to kick a weak
        # losing position for a high-conviction new signal (PENDLE-style miss).
        if reason.startswith("max concurrent reached"):
            rotated, rot_reason = _try_rotation(intent, mode)
            if rotated:
                log.info("ROTATION SUCCESS: %s — retrying entry on %s", rot_reason, intent.get("symbol"))
                # Retry guardrails with bypass — slot is now free, cooldown waived
                ok, reason = _check_guardrails(intent, mode, client, rotation_bypass=True)
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

    # Pre-flight freshness: signal was computed on a previous bar; if the market
    # moved against us between scan and now (regime/surge gating delay), the
    # breakout might already be invalidated. Skip to avoid whipsaw losses.
    fresh_ok, fresh_reason = _signal_still_valid(intent)
    if not fresh_ok:
        result["mode"] = "skipped"
        result["reason"] = fresh_reason
        log.warning("freshness skip %s: %s", intent["symbol"], fresh_reason)
        return result

    # Intra-bar dump filter: the strategy reads CLOSED bars only, so it can
    # miss a sharp reversal happening in the current bar (ALLO 21:00 1h bar
    # was -4% RED while the 4h-closed trend still looked bullish — strategy
    # fired anyway, bot caught the falling knife). Block ANY entry when the
    # latest 1h bar is currently > -2% (dumping).
    strat_check = (intent.get("strategy") or "").lower()
    if strat_check in _SMART_LIMIT_STRATEGIES:
        try:
            from binance.client import Client
            kl = Client().get_klines(symbol=intent["symbol"], interval="1h", limit=1)
            if kl:
                o = float(kl[0][1])
                c = float(kl[0][4])
                if o > 0:
                    bar_pct = (c - o) / o * 100
                    if bar_pct < -2.0:
                        result["mode"] = "skipped"
                        result["reason"] = (
                            f"intra-bar dump: current 1h bar {bar_pct:+.2f}% "
                            f"(open ${o:.6g} → close ${c:.6g}) — knife-catch guard"
                        )
                        log.warning("intra-bar dump skip %s: %s",
                                    intent["symbol"], result["reason"])
                        return result
        except Exception as e:
            log.warning("intra-bar dump check %s failed (allowing): %s",
                        intent["symbol"], e)

    # Strength-adaptive entry: for momentum/breakout signals, decide between
    # MARKET (catch the move) and LIMIT (wait for pullback) based on the
    # current 1h bar's strength. If the bar is strongly green (>=1.5%),
    # the move is real and we want to catch it — MARKET buy full size.
    # Otherwise place a smart-limit at pullback level. This handles the NEAR
    # case (strong trend, limit never fills) AND the ALLO case (dump bar,
    # blocked entirely by the earlier intra-bar dump filter).
    strat = (intent.get("strategy") or "").lower()
    smart_limit_on = (_get_setting("crypto_smart_entry_enabled") or "on").lower() == "on"
    if smart_limit_on and strat in _SMART_LIMIT_STRATEGIES:
        # Check latest 1h bar strength to decide market vs limit
        is_strong_continuation = False
        try:
            from binance.client import Client
            kl1h = Client().get_klines(symbol=intent["symbol"], interval="1h", limit=1)
            if kl1h:
                bar_o = float(kl1h[0][1])
                bar_c = float(kl1h[0][4])
                if bar_o > 0:
                    bar_pct = (bar_c - bar_o) / bar_o * 100
                    if bar_pct >= 1.5:
                        is_strong_continuation = True
                        log.info("STRONG CONTINUATION %s: 1h bar +%.2f%% — using MARKET buy",
                                 intent["symbol"], bar_pct)
        except Exception as e:
            log.warning("strength check %s failed (defaulting to limit): %s",
                        intent["symbol"], e)

        if is_strong_continuation:
            # Strong bar: skip smart-limit, fall through to MARKET buy below.
            # WIDEN STOP to -8%: market buy pays peak price, so the position
            # needs more breathing room for normal post-breakout pullbacks.
            # Without this, today's ME pattern: bot market-buys $0.1059 on
            # strong bar, normal pullback hits -5% stop ($0.1006) within hours.
            # With -8% stop ($0.0974), position has room to retest support
            # and recover.
            old_stop = intent.get("stop_price", 0)
            new_stop = intent["entry_price"] * 0.92  # -8% from scan price
            if new_stop < old_stop:  # only widen, never tighten
                intent["stop_price"] = new_stop
                log.info("AGGRESSIVE ENTRY %s: widened stop $%.6f → $%.6f (-8%%)",
                         intent["symbol"], old_stop, new_stop)
        else:
            # Weak/neutral bar: place smart-limit at pullback level
            try:
                from binance.client import Client
                cur_px = float(Client().get_ticker(symbol=intent["symbol"])["lastPrice"])
            except Exception:
                cur_px = float(intent["entry_price"])
            smart_level = _compute_smart_entry_level(intent["symbol"], cur_px)
            if smart_level is not None and smart_level < cur_px:
                oid = _place_smart_entry_limit(intent, smart_level, intent["size_usd"])
                if oid:
                    import json
                    raw = _get_setting("crypto_smart_entry_orders") or "[]"
                    try:
                        pending = json.loads(raw)
                    except Exception:
                        pending = []
                    # Drop any duplicate pending for same symbol
                    pending = [p for p in pending if p.get("symbol") != intent["symbol"]]
                    pending.append({
                        "symbol": intent["symbol"],
                        "order_id": oid,
                        "limit_price": smart_level,
                        "signal_price": cur_px,
                        "placed_at": datetime.utcnow().isoformat(),
                        "intent": {
                            "symbol": intent["symbol"],
                            "strategy": intent["strategy"],
                            "entry_price": intent["entry_price"],
                            "stop_price": intent["stop_price"],
                            "target_price": intent["target_price"],
                            "max_hold_bars": intent["max_hold_bars"],
                            "exit_rule": intent["exit_rule"],
                            "reason": intent.get("reason", ""),
                        },
                    })
                    _set_setting("crypto_smart_entry_orders", json.dumps(pending))
                    log.info("SMART ENTRY LIMIT %s @ $%.6f (signal $%.6f) orderId=%s",
                             intent["symbol"], smart_level, cur_px, oid)
                    result.update({"executed": False, "mode": "limit_placed",
                                   "reason": f"smart-limit placed @ ${smart_level:.6f}",
                                   "order_id": oid})
                    return result
                # else: place failed (capital/symbol/etc) — fall through to market

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
    gross_qty = sum(float(f["qty"]) for f in fills)
    total_quote = sum(float(f["qty"]) * float(f["price"]) for f in fills)
    avg_price = total_quote / gross_qty if gross_qty else 0.0

    # Subtract commission paid in the base asset — Binance skims the fee from
    # the purchased qty by default (e.g., DOGE buy gets 521 filled but 0.521
    # taken as fee → wallet shows 520.479). Use the actual `commission` /
    # `commissionAsset` fields from each fill rather than guessing.
    base_asset = intent["symbol"].replace("USDT", "")
    fee_in_base = sum(
        float(f.get("commission", 0))
        for f in fills if f.get("commissionAsset") == base_asset
    )
    total_qty = gross_qty - fee_in_base
    if fee_in_base > 0:
        log.info("BUY %s: gross=%.8f - fee=%.8f (%s) = net=%.8f",
                 intent["symbol"], gross_qty, fee_in_base, base_asset, total_qty)

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

    # Determine qty to sell. PAPER must compute net remaining (no balance to query);
    # LIVE caps via min(qty, free) below. qty_override (partial sells) trumps both.
    if qty_override is not None:
        qty_to_sell = float(qty_override)
    elif is_paper:
        prior_sells = (CryptoTrade.query
                       .filter_by(symbol=position.symbol, side="SELL", is_paper=True)
                       .filter(CryptoTrade.executed_at > position.executed_at)
                       .filter(CryptoTrade.strategy != "manual_liquidation")
                       .all())
        already_sold = sum(float(s.qty) for s in prior_sells)
        qty_to_sell = max(0.0, float(position.qty) - already_sold)
    else:
        qty_to_sell = float(position.qty)

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
        gross_quote = sum(float(f["qty"]) * float(f["price"]) for f in fills)
        # USDT commission on SELL is skimmed from the proceeds — record what we
        # actually received, not the gross.
        fee_in_usdt = sum(
            float(f.get("commission", 0))
            for f in fills if f.get("commissionAsset") == "USDT"
        )
        total_quote = gross_quote - fee_in_usdt
        avg_price = gross_quote / total_qty if total_qty else 0.0
        if fee_in_usdt > 0:
            log.info("SELL %s: gross_quote=%.4f - fee=%.4f USDT = net=%.4f",
                     position.symbol, gross_quote, fee_in_usdt, total_quote)

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
        # Set up smart re-entry limit if exit was profitable + macro OK.
        # Use max(peak_pnl, final_pnl) so partial-then-stopped trades still
        # qualify as winners (today's RAD: peak +5%, final +0.29% — without
        # this fix the partial profit gets ignored for re-entry decision).
        try:
            meta_for_peak = parse_entry_notes(position.notes)
            peak_pct = float(meta_for_peak.get("peak_pnl_pct") or 0.0)
            effective_pnl = max(peak_pct, pnl_pct)
            maybe_setup_reentry(position, avg_price, effective_pnl)
        except Exception as e:
            log.warning("reentry setup failed for %s: %s", position.symbol, e)
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
    meta_before = parse_entry_notes(position.notes)
    is_first_partial = not meta_before["partial_done"]
    label_n = "" if is_first_partial else f" ladder #{1 + sum(1 for s in prior_sells if 'partial profit take' in (s.notes or ''))}"
    label = f"partial profit take ({int(fraction * 100)}%){label_n}"
    res = execute_sell(position, current_price, label, qty_override=qty_to_sell)
    if not res["executed"]:
        return res

    if not is_first_partial:
        log.info("LADDER PARTIAL %s sold %.6f @ $%.6f (stop unchanged)",
                 position.symbol, qty_to_sell, current_price)
        return res

    # `_f()` already falls back to DEFAULTS when a setting is missing, so we
    # don't need an `or X.X` fallback (which would silently coerce a legitimate
    # 0.0 — the documented "static buffer-only" escape hatch — to the default).
    try:
        buffer_pct = _f("crypto_breakeven_buffer_pct")
    except Exception:
        buffer_pct = 1.0
    # NEW: lock in a fraction of the partial's gain on the runner.
    # Old behavior: stop moved to entry+buffer% (a static "breakeven+1%").
    #   Problem: if partial fired at +5%, runner's stop was still +1% → giving
    #   back 4 of the 5pp captured by partial. NOTUSDT 2026-05-07 case was
    #   partial at +3.6%, stop at +1%, runner stopped at +0.46% → kept only
    #   1pp of the 3.6pp move on the runner half.
    # New behavior: stop = entry × (1 + max(buffer, gain × lock_fraction)/100)
    #   So if partial fired at +5% with lock_fraction=0.5, stop = +2.5%.
    #   crypto_partial_lock_fraction=0 disables the lock and reverts to the
    #   static buffer (so the `or` fallback below would silently break that).
    try:
        lock_fraction = _f("crypto_partial_lock_fraction")
    except Exception:
        lock_fraction = 0.5
    meta = parse_entry_notes(position.notes)
    entry = float(position.price)
    original_stop = meta["stop"] if meta["stop"] else entry * 0.95
    partial_gain_pct = (current_price - entry) / entry * 100.0 if entry > 0 else 0.0
    lock_pct = max(buffer_pct, partial_gain_pct * lock_fraction)
    new_stop = entry * (1 + lock_pct / 100.0)
    target_str = _fmt_price(meta["target"]) if meta["target"] else _fmt_price(entry * 1.05)

    # Preserve the ORIGINAL entry-reason text (the human-readable last token —
    # things like "fresh breakout +4.2%, vol=1.6x, RSI=67"). The journal reads
    # the last token of notes as the entry reason; if we rebuild without it,
    # the journal would display "original_stop=$X" as the entry reason.
    entry_reason_text = ""
    if position.notes:
        for token in position.notes.split("·"):
            token = token.strip()
            if (token and not token.startswith(("stop=$", "target=$", "max_hold=", "exit=",
                                                 "original_stop=$", "PARTIAL DONE", "PAPER ENTRY",
                                                 "LIVE ENTRY"))
                and token != "partial_done=1"):
                entry_reason_text = token

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
