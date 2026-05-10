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


def update_day_start_and_check_halt(current_value: float) -> dict:
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

    # New MYT day → re-snapshot day_start + clear halt flags.
    #
    # PAPER mode: day_start = principal + Σ all-time realized P&L (excludes
    # unrealized; matches dashboard formula in _account_summary). At midnight
    # rollover, today_realized was 0 (rolled over by the date check above), so
    # this equals "yesterday's day_end + yesterday's realized" = next day's open.
    #
    # LIVE mode: day_start = current_value (real Binance balance, includes dust
    # + unrealized). Captures actual portfolio state — needed to catch deposits
    # that happen mid-rollover.
    if snap_date != today_str or snap_value <= 0:
        if _is_paper_mode_setting():
            # Compute principal + Σ all-time realized for paper-mode day_start.
            # Avoid circular import by inlining a minimal FIFO realized calc here.
            from webapp.models import CryptoTrade
            try:
                fee_rate_dsv = float(_get_setting("crypto_fee_rate_per_side") or "0.001")
            except (TypeError, ValueError):
                fee_rate_dsv = 0.001
            paper_trades = (CryptoTrade.query
                            .filter_by(is_paper=True)
                            .filter(CryptoTrade.strategy != "manual_liquidation")
                            .order_by(CryptoTrade.executed_at)
                            .all())
            by_sym_dsv = {}
            for t in paper_trades:
                by_sym_dsv.setdefault(t.symbol, []).append(t)
            all_realized = 0.0
            for sym, ts in by_sym_dsv.items():
                buys = []
                for t in ts:
                    if t.side == "BUY":
                        buys.append([t, float(t.qty)])
                    elif t.side == "SELL" and buys:
                        sell_qty = float(t.qty)
                        while sell_qty > 1e-9 and buys:
                            buy, br = buys[0]
                            consumed = min(br, sell_qty)
                            buys[0][1] -= consumed
                            sell_qty -= consumed
                            bq = float(buy.qty) or 1
                            sq = float(t.qty) or 1
                            bv = float(buy.quote_amount or float(buy.price)*float(buy.qty)) * (consumed/bq)
                            sv = float(t.quote_amount or float(t.price)*float(t.qty)) * (consumed/sq)
                            all_realized += (sv - bv) - (bv + sv) * fee_rate_dsv
                            if buys[0][1] <= 1e-9:
                                buys.pop(0)
            try:
                principal = float(_get_setting("crypto_starting_capital_usd") or "0")
            except (TypeError, ValueError):
                principal = 0.0
            day_start_to_save = principal + all_realized
        else:
            day_start_to_save = current_value
        _set_setting("crypto_day_start_date", today_str)
        _set_setting("crypto_day_start_value_usd", f"{day_start_to_save:.4f}")
        _set_setting("crypto_day_start_format", "synth" if _is_paper_mode_setting() else "live")
        _set_setting("crypto_today_loss_halted", "0")
        _set_setting("crypto_today_profit_halted", "0")
        _set_setting("crypto_today_loss_overridden", "0")
        _set_setting("crypto_today_profit_overridden", "0")
        # Reset today's mid-day deposit counter — yesterday's flows have
        # been finalized into today's snapshot row via past-24h fetch in
        # _write_daily_snapshot, so the so-far counter starts fresh.
        _set_setting("crypto_today_deposits_so_far_usd", "0")
        # Daily snapshot table stores SYNTH value (same number used by the
        # halt + dashboard + ROI graph). Single source of truth.
        try:
            _write_daily_snapshot(today_str, current_value, source="live", overwrite=True)
        except Exception as e:
            log.warning("daily snapshot write failed (non-fatal): %s", e)
        snap_value = current_value
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
        # Attach the remaining net qty (post-partial-sells) so callers can
        # compute correct unrealized P&L. last_buy.qty is the ORIGINAL buy
        # qty; _remaining_qty is what's actually still open.
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
