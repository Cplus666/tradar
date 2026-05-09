"""Crypto workspace blueprint. URL prefix: /crypto."""

from __future__ import annotations

import json
from datetime import datetime

import pandas as pd
from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for

from webapp.models import CryptoCoin, CryptoHolding, CryptoRun, CryptoTrade, Setting, db

bp = Blueprint("crypto", __name__, url_prefix="/tradar", template_folder="templates")


@bp.app_template_filter("dt")
def fmt_dt(value):
    """UTC datetime → 'YYYY-MM-DD HH:MM MYT' display. DB stores naive UTC."""
    if not value:
        return ""
    from datetime import timezone, timedelta
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    myt = value.astimezone(timezone(timedelta(hours=8)))
    return myt.strftime("%Y-%m-%d %H:%M MYT")


KEY_SETTING = "binance_api_key"
SECRET_SETTING = "binance_api_secret"

# All strategies the bot can run. Keep in sync with analysis/crypto_strategies.py.
# The Settings page renders one toggle per name; per-strategy disable is enforced
# in analysis/crypto_executor.py:_check_guardrails.
STRATEGY_NAMES = (
    "breakout_4h",
    "breakout_1h",
    "momentum_surge",
    "pullback_uptrend",
    "oversold_meanrev",
)


def _setting(key: str, default: str = "") -> str:
    row = Setting.query.get(key)
    return row.value if row else default


# ---- Binance client helpers + dashboard cache ----
#
# Every Binance HTTP call gets a (connect=5s, read=10s) timeout — without
# this, python-binance hangs forever on a wedged socket (e.g. lost wifi),
# parking a Flask worker thread and freezing the dashboard at "loading…".
_BINANCE_REQUEST_PARAMS = {"timeout": (5, 10)}

# Last successful /api/dashboard payload — served (with stale=True) when a
# fresh Binance call fails so users see useful numbers instead of a hang.
_DASHBOARD_CACHE: dict = {"payload": None, "ts": None}


def _binance_client(key: str | None = None, secret: str | None = None):
    """python-binance Client wrapped with our standard request timeouts."""
    from binance.client import Client
    return Client(key, secret, requests_params=_BINANCE_REQUEST_PARAMS)


def _set_setting(key: str, value: str) -> None:
    row = Setting.query.get(key)
    if row:
        row.value = value
    else:
        db.session.add(Setting(key=key, value=value))
    db.session.commit()


def _delete_setting(key: str) -> None:
    row = Setting.query.get(key)
    if row:
        db.session.delete(row)
        db.session.commit()


def get_binance_creds() -> tuple[str | None, str | None]:
    """Return (api_key, api_secret) from Settings table or env vars (env wins)."""
    import os
    env_key = os.environ.get("BINANCE_API_KEY")
    env_secret = os.environ.get("BINANCE_API_SECRET")
    if env_key and env_secret:
        return env_key, env_secret
    db_key = _setting(KEY_SETTING)
    db_secret = _setting(SECRET_SETTING)
    return (db_key or None), (db_secret or None)


def _displayed_is_paper() -> bool:
    """Returns True if dashboard / journal / activity should show paper trades,
    False for live trades. Determined by the current trading mode setting.

    Without this helper, hardcoded `is_paper=False` filters everywhere caused
    paper-mode tradar to show empty dashboards (the bug fixed 2026-05-09)."""
    from analysis.crypto_executor import _is_paper_mode_setting
    return _is_paper_mode_setting()


def _has_binance_keys() -> bool:
    k, s = get_binance_creds()
    return bool(k and s)


def _mask(s: str | None) -> str:
    if not s:
        return ""
    if len(s) <= 8:
        return "••••"
    return f"{s[:4]}…{s[-4:]}"


def _account_summary(prefetched_tickers: dict | None = None) -> dict:
    """Total account value, today's P&L (realized + unrealized), win rate.

    Works in BOTH live and paper mode:
      - Live: pulls usdt_free from Binance get_account(); positions filtered is_paper=False
      - Paper: derives usdt_free from trade history (starting_capital - open_cost
        + realized_pnl); positions filtered is_paper=True; tickers from public
        get_all_tickers (no auth needed)

    Today = since 00:00 MYT today. Pass prefetched_tickers to avoid duplicate fetch.
    """
    from datetime import timezone, timedelta
    from analysis.crypto_executor import (
        _open_positions, parse_entry_notes, _is_paper_mode_setting,
        _principal_at_day,
    )
    from datetime import timezone as _tz_init, timedelta as _td_init

    is_paper = _is_paper_mode_setting()
    pos_filter = True if is_paper else False

    _init_today_iso = (datetime.utcnow().replace(tzinfo=_tz_init.utc)
                       .astimezone(_tz_init(_td_init(hours=8))).date().isoformat())
    out = {
        "account_value": None, "usdt_free": None,
        "today_realized": 0.0, "today_unrealized": 0.0, "today_total": 0.0,
        "today_wins": 0, "today_losses": 0, "today_win_rate": None,
        "open_count": 0, "open_value": 0.0,
        "starting_capital": _principal_at_day(_init_today_iso),
        "all_time_pnl": 0.0,
    }

    # Get tickers — always works without auth (public endpoint)
    client = None
    if prefetched_tickers is not None:
        all_tickers = prefetched_tickers
    else:
        try:
            client = _binance_client()  # no key needed for tickers
            all_tickers = {t["symbol"]: float(t["price"]) for t in client.get_all_tickers()}
        except Exception:
            all_tickers = {}

    # USDT free balance — different sources for live vs paper
    if is_paper:
        # Paper mode: synthesize cash from trade history.
        # usdt_free = starting_capital + Σ(sells − sell_fee) − Σ(buys + buy_fee)
        # In paper mode this CAN go negative (bot trades larger than starting
        # capital); don't clamp — account_value math depends on the true value
        # so that account_value = usdt_free + open_value reconciles with
        # starting_capital + realized + unrealized.
        from webapp.models import CryptoTrade
        trades = (CryptoTrade.query
                  .filter_by(is_paper=True)
                  .order_by(CryptoTrade.executed_at)
                  .all())
        try:
            fee_rate_init = float(_setting("crypto_fee_rate_per_side", "0.001"))
        except (TypeError, ValueError):
            fee_rate_init = 0.001
        cash = float(out["starting_capital"])
        for t in trades:
            quote = float(t.quote_amount or float(t.price) * float(t.qty))
            fee = quote * fee_rate_init
            if t.side == "BUY":
                cash -= (quote + fee)
            elif t.side == "SELL":
                cash += (quote - fee)
        out["usdt_free"] = cash  # may be negative; that's correct in paper mode
    else:
        # Live mode: pull from Binance
        try:
            key, secret = get_binance_creds()
            if not key or not secret:
                return out  # no keys + live mode = can't proceed
            client = _binance_client(key, secret)
            acct = client.get_account()
            usdt = next((b for b in acct["balances"] if b["asset"] == "USDT"), None)
            out["usdt_free"] = float(usdt["free"]) if usdt else 0.0
            # Sync holdings table — only meaningful in live mode
            try:
                from analysis.binance_sync import persist_holdings_from_data
                persist_holdings_from_data(acct.get("balances", []), all_tickers)
            except Exception:
                pass
        except Exception:
            return out

    open_pos = _open_positions(is_paper=pos_filter)
    open_value = 0.0
    today_unrealized = 0.0
    myt_now = datetime.utcnow().replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=8)))
    today_start_utc = (myt_now.replace(hour=0, minute=0, second=0, microsecond=0)).astimezone(timezone.utc).replace(tzinfo=None)

    # Fee rate — used to net both today's realized P&L and unrealized P&L
    # so dashboard numbers match journal + daily ROI chart conventions.
    try:
        fee_rate = float(_setting("crypto_fee_rate_per_side", "0.001"))
    except (TypeError, ValueError):
        fee_rate = 0.001

    for p in open_pos:
        cur = all_tickers.get(p.symbol)
        if cur is None:
            continue
        # Use REMAINING qty (post-partial-sells); falls back to original buy qty.
        qty = float(getattr(p, "_remaining_qty", p.qty))
        value = cur * qty
        open_value += value
        # Sum CURRENT unrealized P&L (vs entry, net of estimated round-trip
        # fees) for ALL open positions — regardless of when they were opened.
        # Matches the values shown on the dashboard position cards.
        buy_value = float(p.price) * qty
        sell_value = value
        gross = sell_value - buy_value
        fee = (buy_value + sell_value) * fee_rate
        today_unrealized += gross - fee
    out["open_count"] = len(open_pos)
    out["open_value"] = open_value
    out["account_value"] = out["usdt_free"] + open_value

    # NOTE: _account_summary used to call update_day_start_and_check_halt here,
    # but that triggered halt sells inside a function whose first job is to
    # build a display snapshot. The halt's sell_all_open_positions ran AFTER
    # we'd already computed open_value/today_unrealized/account_value above,
    # so the dashboard would briefly show liquidated positions as still open
    # (until the next 30s refresh). Halt firing now lives only in the loop
    # paths (run_crypto_loop every 15 min, run_fast_exit_check every 30s) so
    # _account_summary is a pure reader. day_start_value is overridden to the
    # synth value below (out["day_start_value"] = synth_day_start).
    out["day_start_value"] = 0.0
    out["drawdown_pct"] = 0.0
    out["today_pnl_pct"] = 0.0

    # Halt status — read AFTER update_day_start_and_check_halt so any halt fired
    # this tick is reflected in the flags the dashboard sees. The "_overridden"
    # flags let the dashboard show "manually overridden today" state instead of
    # just hiding the banner — so the user remembers the safety net is off.
    out["loss_halted_today"] = (_setting("crypto_today_loss_halted", "0") == "1")
    out["profit_halted_today"] = (_setting("crypto_today_profit_halted", "0") == "1")
    out["loss_halt_overridden"] = (_setting("crypto_today_loss_overridden", "0") == "1")
    out["profit_halt_overridden"] = (_setting("crypto_today_profit_overridden", "0") == "1")
    try:
        out["loss_halt_pct"] = float(_setting("crypto_loss_halt_pct", "5.0"))
    except (TypeError, ValueError):
        out["loss_halt_pct"] = 5.0
    try:
        out["profit_halt_pct"] = float(_setting("crypto_profit_halt_pct", "5.0"))
    except (TypeError, ValueError):
        out["profit_halt_pct"] = 5.0

    # Today's realized P&L: pair BUY-SELL where SELL closed today
    from webapp.models import CryptoTrade
    trades = CryptoTrade.query.filter(CryptoTrade.is_paper == _displayed_is_paper()).order_by(CryptoTrade.executed_at).all()
    by_sym: dict = {}
    for t in trades:
        if t.strategy == "manual_liquidation":
            continue
        by_sym.setdefault(t.symbol, []).append(t)
    today_realized = 0.0
    today_wins = today_losses = 0
    all_realized = 0.0
    # FIFO with REMAINING-QTY tracking — same as the journal builder so the
    # dashboard total reconciles exactly with what's visible in the journal.
    for sym, ts in by_sym.items():
        buys: list[list] = []  # [buy, remaining_qty]
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
                    all_realized += pnl
                    if t.executed_at >= today_start_utc:
                        today_realized += pnl
                        if pnl > 0: today_wins += 1
                        else: today_losses += 1
                    if buys[0][1] <= 1e-9:
                        buys.pop(0)
    # Today's P&L — designed to RECONCILE with account_value:
    #   today_realized   = trades closed today (sum of net P&L from FIFO)
    #   today_unrealized = current unrealized on ALL open positions (vs entry)
    #   today_total      = realized + unrealized
    #   day_start_value  = STARTING_CAPITAL + Σ realized P&L from closes BEFORE
    #                      today MYT 00:00 (synthesized from trade history, NOT
    #                      a snapshot of midnight account value)
    #
    # Why synthesize instead of snapshot? With this formula:
    #   account_value = day_start_value + today_total   (always reconciles ✓)
    # Because: account_value = principal + Σ all_realized + current_unrealized
    #                       = principal + (Σ realized_pre_today + today_realized)
    #                         + today_unrealized
    #                       = day_start_value + today_total
    #
    # The "midnight account-value snapshot" is still saved in
    # crypto_day_start_value_usd setting (used by halt logic) and
    # crypto_daily_snapshots table (kept for halt rollover bookkeeping +
    # deposit-correction work). We just don't use it as the today's-P&L
    # denominator anymore — synthesis is cleaner and reproducible from trades.
    out["today_realized"] = today_realized
    out["today_wins"] = today_wins
    out["today_losses"] = today_losses
    if today_wins + today_losses > 0:
        out["today_win_rate"] = today_wins / (today_wins + today_losses) * 100
    out["all_time_pnl"] = out["account_value"] - out["starting_capital"] if out["account_value"] else 0

    # Day-start derivation differs by mode:
    #   Live mode: trust the stored snapshot (set at midnight from real Binance
    #     balance) and derive today_unrealized = today_total − today_realized.
    #     This catches deposits/withdrawals during the day that show up as
    #     "unrealized" delta.
    #   Paper mode: stored snapshot is unreliable (no real Binance call), so
    #     use the per-position-card sum we already computed in the loop above.
    #     today_total = today_realized + today_unrealized; day_start derived
    #     from account_value − today_total (always reconciles).
    #
    # Without this split, paper mode read a stale stored_day_start from a
    # prior config and computed today_unrealized = -$6,200 while the actual
    # per-position sum was -$37 (off by ~167×). Fixed 2026-05-09.
    if is_paper:
        # Paper: use the loop-computed today_unrealized as truth
        out["today_unrealized"] = today_unrealized
        today_total = today_realized + today_unrealized
        out["today_total"] = today_total
        if out["account_value"] is not None:
            out["day_start_value"] = out["account_value"] - today_total
        else:
            out["day_start_value"] = out["starting_capital"]
    else:
        # Live: trust the stored Binance midnight snapshot
        stored_day_start = 0.0
        try:
            stored_day_start = float(_setting("crypto_day_start_value_usd", "0") or 0)
        except (TypeError, ValueError):
            stored_day_start = 0.0
        if stored_day_start <= 0:
            stored_day_start = out["starting_capital"] + (all_realized - today_realized)
        out["day_start_value"] = stored_day_start
        today_total = (out["account_value"] - stored_day_start) if out["account_value"] else 0.0
        out["today_total"] = today_total
        out["today_unrealized"] = today_total - today_realized

    # P&L percentages — divide by day_start_value (positive number)
    day_start_for_pct = out["day_start_value"]
    if day_start_for_pct and day_start_for_pct > 0 and out["account_value"] is not None:
        out["today_total_pct"] = today_total / day_start_for_pct * 100
        out["today_pnl_pct"] = today_total / day_start_for_pct * 100
        out["drawdown_pct"] = -out["today_pnl_pct"] if out["today_pnl_pct"] < 0 else 0.0
    else:
        out["today_total_pct"] = None
    out["all_time_pct"] = (out["all_time_pnl"] / out["starting_capital"] * 100) if out["starting_capital"] else None
    return out


def _open_position_cards(tickers: dict | None = None) -> list:
    """Position cards. If tickers=None, returns DB-only data with current=None.

    Caller passes tickers dict (symbol → price) for the live version.
    P&L is NET of estimated round-trip fees (matches journal + daily ROI chart).
    """
    from analysis.crypto_executor import _open_positions, parse_entry_notes
    try:
        fee_rate = float(_setting("crypto_fee_rate_per_side", "0.001"))
    except (TypeError, ValueError):
        fee_rate = 0.001
    out = []
    for p in _open_positions(is_paper=_displayed_is_paper()):
        meta = parse_entry_notes(p.notes)
        cur = (tickers or {}).get(p.symbol)
        if cur is None:
            cur = float(p.price)  # placeholder = entry price (gauge centers, P&L = 0)
        entry = float(p.price)
        stop = meta["stop"] or entry * 0.95
        target = meta["target"] or entry * 1.05
        held_h = (datetime.utcnow() - p.executed_at).total_seconds() / 3600
        max_hold_h = meta["max_hold"]  # 4h-bar count for breakout_4h, 1h-bar count for breakout_1h, etc.
        # Heuristic: if strategy contains "1h" or "momentum" or "oversold", bars are hours; if "4h", bars are *4 hours
        if "1h" in (p.strategy or "") or "momentum" in (p.strategy or "") or "oversold" in (p.strategy or ""):
            max_hold_hours = max_hold_h * 1
        else:
            max_hold_hours = max_hold_h * 4
        time_to_stop_h = max(0, max_hold_hours - held_h)
        # Position of price between stop and target (0 = at stop, 1 = at target)
        if target > stop:
            gauge = max(0.0, min(1.0, (cur - stop) / (target - stop)))
        else:
            gauge = 0.5
        # Net P&L — assumes you'll pay both buy + sell fees when this position closes.
        # Use REMAINING qty (handles positions that did partial profit-take). Without
        # this the dashboard P&L mismatches the journal: dashboard would compute on
        # original buy qty (e.g. 5.36 LINK) while journal correctly tracks 4.02 LINK.
        qty = float(getattr(p, "_remaining_qty", p.qty))
        buy_value  = entry * qty
        sell_value = cur * qty
        gross_pnl = sell_value - buy_value
        fee = (buy_value + sell_value) * fee_rate
        pnl_usd = gross_pnl - fee
        pnl_pct = (pnl_usd / buy_value * 100) if buy_value > 0 else 0.0
        out.append({
            "id": p.id, "symbol": p.symbol, "strategy": p.strategy,
            "entry": entry, "current": cur, "stop": stop, "target": target,
            "qty": qty, "notional": buy_value,
            "pnl_pct": pnl_pct, "pnl_usd": pnl_usd,
            "fee_usd": round(fee, 4),
            "gross_pnl_usd": round(gross_pnl, 4),
            "held_h": held_h, "time_to_stop_h": time_to_stop_h,
            "gauge_pct": gauge * 100,  # 0-100 for CSS width
            "risk_pct": (entry - stop) / entry * 100,
            "reward_pct": (target - entry) / entry * 100,
            # Partial-take state — let the dashboard show a "PARTIAL" badge
            # and the original (pre-move) stop, so users can see at a glance
            # which positions have already booked half their size.
            "partial_done": bool(meta.get("partial_done")),
            "original_stop": meta.get("original_stop"),
        })
    out.sort(key=lambda x: -x["pnl_pct"])  # winners first
    return out


def _strategy_breakdown() -> list:
    """Per-strategy stats: trades, win rate, avg P&L, total. NET of fees so the
    totals reconcile with the dashboard card, journal, and ROI graph (which all
    use the same fee_rate)."""
    from webapp.models import CryptoTrade
    try:
        fee_rate = float(_setting("crypto_fee_rate_per_side", "0.001"))
    except (TypeError, ValueError):
        fee_rate = 0.001
    trades = CryptoTrade.query.filter(CryptoTrade.is_paper == _displayed_is_paper()).order_by(CryptoTrade.executed_at).all()
    by_sym: dict = {}
    for t in trades:
        if t.strategy == "manual_liquidation":
            continue
        by_sym.setdefault(t.symbol, []).append(t)
    by_strat: dict = {}
    for sym, ts in by_sym.items():
        buys = []
        for t in ts:
            if t.side == "BUY":
                buys.append([t, float(t.qty)])
            elif t.side == "SELL" and buys:
                # FIFO with REMAINING-QTY tracking — partial sells consume only
                # the qty actually sold, not the whole buy.
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
                    pnl_usd = gross - fee
                    pnl_pct = (pnl_usd / buy_value * 100) if buy_value > 0 else 0.0
                    strat = buy.strategy or "unknown"
                    d = by_strat.setdefault(strat, {"wins": 0, "losses": 0, "win_pcts": [], "loss_pcts": [], "net_usd": 0.0})
                    d["net_usd"] += pnl_usd
                    if pnl_pct > 0:
                        d["wins"] += 1; d["win_pcts"].append(pnl_pct)
                    else:
                        d["losses"] += 1; d["loss_pcts"].append(pnl_pct)
                    if buys[0][1] <= 1e-9:
                        buys.pop(0)
    out = []
    for strat, d in by_strat.items():
        n = d["wins"] + d["losses"]
        out.append({
            "strategy": strat,
            "total": n,
            "wins": d["wins"], "losses": d["losses"],
            "win_rate": (d["wins"] / n * 100) if n else None,
            "avg_win_pct": (sum(d["win_pcts"]) / len(d["win_pcts"])) if d["win_pcts"] else None,
            "avg_loss_pct": (sum(d["loss_pcts"]) / len(d["loss_pcts"])) if d["loss_pcts"] else None,
            "net_usd": d["net_usd"],
        })
    out.sort(key=lambda x: -x["net_usd"])
    return out


def _equity_curve(days: int = 7) -> list:
    """Extract account value over time from sync run summaries.

    Sync run summaries look like: 'synced 1 real + 16 dust assets, total value $199.88'
    """
    from datetime import timedelta, timezone
    import re
    from webapp.models import CryptoRun
    cutoff = datetime.utcnow() - timedelta(days=days)
    runs = CryptoRun.query.filter(
        CryptoRun.kind == "sync",
        CryptoRun.status == "ok",
        CryptoRun.started_at >= cutoff,
    ).order_by(CryptoRun.started_at).all()
    pattern = re.compile(r"total value \$(\d+\.\d+)")
    points = []
    for r in runs:
        if not r.summary:
            continue
        m = pattern.search(r.summary)
        if not m:
            continue
        value = float(m.group(1))
        myt = r.started_at.replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=8)))
        points.append({"t": myt.strftime("%Y-%m-%d %H:%M"), "value": value})
    return points


def _daily_pnl(days: int = 30) -> list:
    """Per-day realized P&L for the dashboard bar chart.

    FIFO-pairs BUYs and SELLs per symbol; each closed pair contributes
    pnl_usd = (sell_price - buy_price) * buy_qty to the SELL's day.
    Days are bucketed in MYT (Asia/Kuala_Lumpur). Top-ups/withdrawals
    don't show up because they aren't trades — robust to deposit changes.

    Returns one row per day for the last `days` days (including empty days
    so the chart shows zero-bars for inactive days).
    """
    from datetime import timedelta, timezone
    MYT = timezone(timedelta(hours=8))
    today_myt = datetime.utcnow().replace(tzinfo=timezone.utc).astimezone(MYT).date()
    cutoff_myt = today_myt - timedelta(days=days - 1)  # window includes today

    # Pull ALL real (non-paper) trades — buys may predate the window.
    # Manual liquidations are excluded so they don't pollute strategy P&L.
    trades = (CryptoTrade.query
              .filter(CryptoTrade.is_paper == _displayed_is_paper())
              .order_by(CryptoTrade.executed_at)
              .all())

    by_sym: dict = {}
    for t in trades:
        if t.strategy == "manual_liquidation":
            continue
        by_sym.setdefault(t.symbol, []).append(t)

    # Fee subtraction — Binance Spot default taker = 0.1% per side. Configurable
    # via crypto_fee_rate_per_side (e.g., 0.00075 if user pays in BNB).
    try:
        fee_rate = float(_setting("crypto_fee_rate_per_side", "0.001"))
    except (TypeError, ValueError):
        fee_rate = 0.001

    days_data: dict = {}  # 'YYYY-MM-DD' → {pnl, capital, pct_sum, wins, losses, fees}
    # Pre-window cumulative P&L — sum of all closes BEFORE the visible window.
    # Used as the seed for "day-start portfolio value" so we can compute
    # portfolio_pct (= day_pnl / portfolio_value_at_day_start * 100) for every
    # bar in the chart, including the oldest one.
    pre_window_pnl = 0.0
    for _sym, ts in by_sym.items():
        # FIFO with REMAINING-QTY tracking — partial sells must consume only
        # the qty actually sold, not the whole buy. Without this, daily ROI
        # double-counts on partial-take + runner exits (buy popped on first
        # sell, runner has nothing to pair with → P&L lost or doubled).
        buys: list[list] = []  # [trade, remaining_qty]
        for t in ts:
            if t.side == "BUY":
                buys.append([t, float(t.qty)])
            elif t.side == "SELL" and buys:
                sell_qty_remaining = float(t.qty)
                sell_price = float(t.price)
                day_myt = t.executed_at.replace(tzinfo=timezone.utc).astimezone(MYT).date()
                while sell_qty_remaining > 1e-9 and buys:
                    buy, buy_remaining = buys[0]
                    consumed = min(buy_remaining, sell_qty_remaining)
                    buys[0][1] -= consumed
                    sell_qty_remaining -= consumed
                    _bq = float(buy.qty) or 1
                    _sq = float(t.qty) or 1
                    capital = float(buy.quote_amount or float(buy.price) * float(buy.qty)) * (consumed / _bq)
                    sell_proceeds = float(t.quote_amount or float(t.price) * float(t.qty)) * (consumed / _sq)
                    gross_pnl = sell_proceeds - capital
                    fee = (capital + sell_proceeds) * fee_rate
                    pnl = gross_pnl - fee
                    pnl_pct = ((pnl / capital) * 100) if capital > 0 else 0.0
                    if buys[0][1] <= 1e-9:
                        buys.pop(0)
                    if day_myt < cutoff_myt:
                        pre_window_pnl += pnl
                        continue
                    key = day_myt.isoformat()
                    d = days_data.setdefault(key, {
                        "pnl": 0.0, "capital": 0.0, "pct_sum": 0.0,
                        "wins": 0, "losses": 0, "fees": 0.0,
                    })
                    d["pnl"] += pnl
                    d["capital"] += capital
                    d["pct_sum"] += pnl_pct
                    d["fees"] += fee
                    if pnl > 0:
                        d["wins"] += 1
                    else:
                        d["losses"] += 1

    # Read existing snapshot rows for the window. The synth day-start values
    # are CACHED here — _daily_pnl writes them on first compute and re-uses on
    # subsequent calls. Rows with source != 'synth' are stale (legacy backfill
    # from an earlier semantics, or 'rollover' from before the synth migration)
    # and get auto-overwritten with the freshly computed synth value.
    #
    # Cache writes are skipped when `days` > 90 — for long-window admin queries
    # we still display the right values (computed below) but don't trigger a
    # 365-row write storm that would lock the DB. The dashboard polls days=30,
    # so the typical path always benefits from caching.
    from webapp.models import CryptoDailySnapshot, db
    cache_writes_enabled = days <= 90
    snap_start_iso = (today_myt - timedelta(days=days - 1)).isoformat()
    snapshots: dict = {}
    try:
        for s in (CryptoDailySnapshot.query
                  .filter(CryptoDailySnapshot.date >= snap_start_iso)
                  .all()):
            snapshots[s.date] = s
    except Exception:
        snapshots = {}

    # Walk days oldest→newest. For each day, the synth day-start is the running
    # value BEFORE that day's P&L is added. We always have the freshly computed
    # value (running_value) — write it to the cache row if missing or stale,
    # so subsequent queries hit the cache and tradar self-heals after git pull.
    # Per-day principal: each day's synth uses the principal AS OF THAT DAY
    # (initial + Σ deposits/withdrawals BEFORE that day). So when a deposit
    # happens, only NEW days' bars use the new principal. Historical bars
    # stay locked to the principal that was in effect when they happened.
    from analysis.crypto_executor import _principal_at_day
    cumulative_realized = pre_window_pnl  # Σ realized through prior days
    points = []
    rows_dirty = False
    for i in range(days - 1, -1, -1):
        d = today_myt - timedelta(days=i)
        date_iso = d.isoformat()
        v = days_data.get(date_iso, {
            "pnl": 0.0, "capital": 0.0, "pct_sum": 0.0,
            "wins": 0, "losses": 0, "fees": 0.0,
        })
        n = v["wins"] + v["losses"]
        avg_pct = (v["pct_sum"] / n) if n > 0 else 0.0
        return_pct = (v["pnl"] / v["capital"] * 100) if v["capital"] > 0 else 0.0
        # Time-aware principal — varies across the chart if user deposited mid-window
        synth_day_start = _principal_at_day(date_iso) + cumulative_realized

        # Cache read/write: deterministic from trades + deposits, so we can safely
        # overwrite stale rows. Trust source='synth' rows; overwrite anything else.
        if cache_writes_enabled:
            snap = snapshots.get(date_iso)
            if snap is None:
                db.session.add(CryptoDailySnapshot(
                    date=date_iso,
                    total_value_usd=round(synth_day_start, 4),
                    source="synth",
                ))
                rows_dirty = True
            elif snap.source == "live":
                # 'live' snapshot was written at midnight from actual Binance
                # balance — more accurate than synth. Never overwrite it.
                pass
            elif abs(float(snap.total_value_usd) - synth_day_start) > 0.01:
                snap.total_value_usd = round(synth_day_start, 4)
                snap.source = "synth"
                rows_dirty = True
        # Prefer 'live' snapshot when available; fall back to synth.
        snap = snapshots.get(date_iso)
        day_start_value = (float(snap.total_value_usd)
                           if snap and snap.source == "live"
                           else synth_day_start)

        portfolio_pct = (v["pnl"] / day_start_value * 100) if day_start_value > 0 else 0.0
        points.append({
            "date": date_iso,
            "pnl": round(v["pnl"], 4),       # NET (after fees)
            "capital": round(v["capital"], 2),
            "fees": round(v["fees"], 4),
            "return_pct": round(return_pct, 3),
            "portfolio_pct": round(portfolio_pct, 3),
            "day_start_value": round(day_start_value, 2),
            "avg_pct": round(avg_pct, 3),
            "wins": v["wins"],
            "losses": v["losses"],
        })
        cumulative_realized += v["pnl"]
    if rows_dirty:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
    return points


def _activity_summary(days: int = 7) -> dict:
    """Compute last-N-days operational + P&L stats. Pure DB read, zero LLM cost.

    P&L (and the per-trade rows in `recent_closed`) are NET of estimated
    round-trip fees — matches the convention used everywhere else (dashboard,
    journal, daily ROI chart).
    """
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(days=days)
    runs = CryptoRun.query.filter(CryptoRun.started_at >= cutoff).all()
    trades = CryptoTrade.query.filter(CryptoTrade.executed_at >= cutoff).all()
    try:
        fee_rate = float(_setting("crypto_fee_rate_per_side", "0.001"))
    except (TypeError, ValueError):
        fee_rate = 0.001

    loop_runs = [r for r in runs if r.kind == "trading_loop"]
    sync_runs = [r for r in runs if r.kind == "sync"]
    error_runs = [r for r in runs if r.status == "error"]

    # Parse summaries to extract signals/exec counts (format: "...signals=N exec=M skip=K")
    sig_total = exec_total = skip_total = 0
    for r in loop_runs:
        if not r.summary:
            continue
        for token in r.summary.split("·"):
            token = token.strip()
            for k in ("signals=", "exec=", "skip="):
                if token.startswith(k):
                    try:
                        v = int(token[len(k):].split()[0])
                        if k == "signals=": sig_total += v
                        elif k == "exec=": exec_total += v
                        else: skip_total += v
                    except (ValueError, IndexError):
                        pass

    # Trade-level stats — pair BUYs with SELLs by symbol
    by_symbol: dict[str, list] = {}
    for t in sorted(trades, key=lambda x: x.executed_at):
        by_symbol.setdefault(t.symbol, []).append(t)

    closed_trades = []   # (symbol, entry_price, exit_price, pnl_pct, pnl_usd, strategy)
    open_count = 0
    for sym, ts in by_symbol.items():
        # Skip manual liquidations (XVG sell wasn't a system trade)
        ts = [t for t in ts if t.strategy != "manual_liquidation"]
        # Walk through pairing: each BUY consumed by next SELL on same symbol
        buys = []
        for t in ts:
            if t.side == "BUY":
                buys.append([t, float(t.qty)])
            elif t.side == "SELL" and buys:
                # FIFO with REMAINING-QTY tracking for partial sells.
                sell_qty_remaining = float(t.qty)
                sell_price = float(t.price)
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
                    pnl_usd = gross - fee
                    pnl_pct = (pnl_usd / buy_value * 100) if buy_value > 0 else 0.0
                    closed_trades.append((sym, float(buy.price), float(t.price), pnl_pct, pnl_usd, buy.strategy))
                    if buys[0][1] <= 1e-9:
                        buys.pop(0)
        open_count += sum(1 for b in buys if b[1] > 1e-9)

    wins = [c for c in closed_trades if c[3] > 0]
    losses = [c for c in closed_trades if c[3] <= 0]
    realized_pnl = sum(c[4] for c in closed_trades)
    win_rate = (len(wins) / len(closed_trades) * 100) if closed_trades else None
    avg_win = (sum(c[3] for c in wins) / len(wins)) if wins else None
    avg_loss = (sum(c[3] for c in losses) / len(losses)) if losses else None

    return {
        "days": days,
        "loop_fires": len(loop_runs),
        "sync_runs": len(sync_runs),
        "error_runs": len(error_runs),
        "signals_generated": sig_total,
        "trades_executed": exec_total,
        "trades_skipped": skip_total,
        "closed_trades": len(closed_trades),
        "wins": len(wins), "losses": len(losses),
        "win_rate": win_rate,
        "realized_pnl_usd": realized_pnl,
        "avg_win_pct": avg_win, "avg_loss_pct": avg_loss,
        "open_positions": open_count,
        "recent_closed": closed_trades[-5:],  # last 5 for the table
    }


@bp.route("/")
def dashboard():
    """Renders fast shell — JS populates Binance-dependent sections via /api/dashboard."""
    recent_runs = CryptoRun.query.order_by(CryptoRun.started_at.desc()).limit(5).all()
    activity = _activity_summary(days=7)  # DB only, fast
    strat_breakdown = _strategy_breakdown()  # DB only, fast
    daily_pnl_points = _daily_pnl(days=30)  # DB only, fast
    latest_trade = CryptoTrade.query.order_by(CryptoTrade.id.desc()).first()
    latest_trade_id = latest_trade.id if latest_trade else 0

    return render_template(
        "crypto_dashboard.html",
        recent_runs=recent_runs,
        activity=activity,
        strat_breakdown=strat_breakdown,
        daily_pnl_json=json.dumps(daily_pnl_points),
        latest_trade_id=latest_trade_id,
        has_keys=_has_binance_keys(),
        kill_switch=_setting("crypto_kill_switch", "off"),
        trading_mode=_setting("crypto_trading_mode", "paper"),
    )


@bp.route("/api/manual-buy/<symbol>", methods=["POST"])
def api_manual_buy(symbol: str):
    """User-triggered manual BUY of a symbol from the universe page.

    Uses the same execute_intent path as automatic strategies — gets the same
    guardrails (kill switch, max concurrent, balance check, symbol status).
    Default stop/target match momentum_surge profile (5% stop, 8% target, 12h max_hold).
    """
    from analysis.crypto_executor import execute_intent
    symbol = symbol.upper()
    try:
        client = _binance_client()
        cur = float(client.get_symbol_ticker(symbol=symbol)["price"])
    except Exception as e:
        return jsonify({"ok": False, "reason": f"price lookup failed: {e}"}), 500

    size_usd = float(_setting("crypto_max_position_usd", "50"))
    intent = {
        "symbol": symbol,
        "strategy": "manual_buy",
        "side": "BUY",
        "entry_price": cur,
        "stop_price": cur * 0.95,        # -5% stop
        "target_price": cur * 1.08,      # +8% target
        "max_hold_bars": 12,             # 12 hours (uses 1h bar convention)
        "exit_rule": "rsi_overbought_80",
        "reason": "MANUAL BUY by user from universe page",
        "size_usd": size_usd,
    }
    result = execute_intent(intent)
    return jsonify({
        "ok": result["executed"],
        "mode": result["mode"],
        "reason": result["reason"],
        "fill_price": result.get("fill_price"),
        "fill_qty": result.get("fill_qty"),
        "size_usd": size_usd,
    })


@bp.route("/api/sell-position/<int:trade_id>", methods=["POST"])
def api_sell_position(trade_id: int):
    """Manually sell an open position by trade ID. Returns JSON result."""
    from analysis.crypto_executor import _open_positions, execute_sell
    open_pos = {p.id: p for p in _open_positions(is_paper=_displayed_is_paper())}
    pos = open_pos.get(trade_id)
    if not pos:
        return jsonify({"ok": False, "reason": "position not found or already closed"}), 404
    try:
        cur = float(_binance_client().get_symbol_ticker(symbol=pos.symbol)["price"])
    except Exception as e:
        return jsonify({"ok": False, "reason": f"price lookup failed: {e}"}), 500
    result = execute_sell(pos, cur, "MANUAL EXIT — user clicked Sell now")
    return jsonify({
        "ok": result["executed"],
        "reason": result["reason"],
        "fill_price": result.get("fill_price"),
        "fill_qty": result.get("fill_qty"),
    })


@bp.route("/api/partial-sell-position/<int:trade_id>", methods=["POST"])
def api_partial_sell_position(trade_id: int):
    """Manually trigger a partial profit-take on an open position."""
    from analysis.crypto_executor import _open_positions, execute_partial_sell, parse_entry_notes

    raw_fraction = (request.form.get("fraction")
                    or (request.get_json(silent=True) or {}).get("fraction")
                    or request.args.get("fraction"))
    if raw_fraction is None:
        try:
            fraction = float(_setting("crypto_partial_take_fraction", "0.5"))
        except (TypeError, ValueError):
            fraction = 0.5
    else:
        try:
            fraction = float(raw_fraction)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "reason": f"invalid fraction '{raw_fraction}'"}), 400
    if not (0 < fraction < 1):
        return jsonify({"ok": False, "reason": f"fraction must be between 0 and 1 (got {fraction})"}), 400

    open_pos = {p.id: p for p in _open_positions(is_paper=_displayed_is_paper())}
    pos = open_pos.get(trade_id)
    if not pos:
        return jsonify({"ok": False, "reason": "position not found or already closed"}), 404
    # Manual ladder-out: no `partial_done` gate. Each call sells `fraction × remaining`,
    # so 50% → 50% of remaining → ... terminates by dust filter / Binance lot step.
    # Stop is only moved on the first partial; subsequent calls leave notes unchanged.
    try:
        cur = float(_binance_client().get_symbol_ticker(symbol=pos.symbol)["price"])
    except Exception as e:
        return jsonify({"ok": False, "reason": f"price lookup failed: {e}"}), 500
    result = execute_partial_sell(pos, cur, fraction)
    return jsonify({
        "ok": result["executed"],
        "reason": result["reason"],
        "fill_price": result.get("fill_price"),
        "fill_qty": result.get("fill_qty"),
        "fraction": fraction,
    })


@bp.route("/api/dashboard/static")
def api_dashboard_static():
    """FAST endpoint — DB-only data. No Binance calls. Used for instant render.

    Imports _starting_capital here (not at module top) to keep the import
    graph the same as _account_summary's existing pattern.

    Account values that need Binance (account_value, usdt_free, today_unrealized)
    return None — JS fetches /api/dashboard separately to fill them in.
    """
    from analysis.crypto_executor import _principal_at_day
    cards = _open_position_cards(tickers=None)  # current_price=None on each
    # Build account summary from DB only (skip Binance calls)
    from datetime import timezone as _tz, timedelta as _td
    myt_now = datetime.utcnow().replace(tzinfo=_tz.utc).astimezone(_tz(_td(hours=8)))
    today_iso_static = myt_now.date().isoformat()
    today_start_utc = (myt_now.replace(hour=0, minute=0, second=0, microsecond=0)).astimezone(_tz.utc).replace(tzinfo=None)
    # Match the LIVE endpoint's NET-of-fees calc (gross was returned here previously,
    # making today_realized briefly jump on each refresh as the live endpoint took over).
    try:
        fee_rate = float(_setting("crypto_fee_rate_per_side", "0.001"))
    except (TypeError, ValueError):
        fee_rate = 0.001
    trades = CryptoTrade.query.filter(CryptoTrade.is_paper == _displayed_is_paper()).order_by(CryptoTrade.executed_at).all()
    by_sym: dict = {}
    for t in trades:
        if t.strategy == "manual_liquidation":
            continue
        by_sym.setdefault(t.symbol, []).append(t)
    today_realized = 0.0
    today_wins = today_losses = 0
    for sym, ts in by_sym.items():
        # FIFO with REMAINING-QTY tracking — partial sells consume only the
        # qty actually sold. Without this, today_realized doubles on partials.
        buys: list[list] = []  # [trade, remaining_qty]
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
                    if t.executed_at >= today_start_utc:
                        today_realized += pnl
                        if pnl > 0: today_wins += 1
                        else: today_losses += 1
                    if buys[0][1] <= 1e-9:
                        buys.pop(0)
    # Halt-banner fields populated from settings (no Binance call needed) so
    # the banner doesn't flicker off during the static-render window. JS reads
    # `=== true` strictly, so undefined values would have hidden the banner.
    try:
        loss_halt_pct = float(_setting("crypto_loss_halt_pct", "5.0"))
    except (TypeError, ValueError):
        loss_halt_pct = 5.0
    try:
        profit_halt_pct = float(_setting("crypto_profit_halt_pct", "5.0"))
    except (TypeError, ValueError):
        profit_halt_pct = 5.0
    account = {
        "account_value": None, "usdt_free": None,
        "today_realized": today_realized,
        "today_unrealized": None, "today_total": None,
        "today_total_pct": None, "today_pnl_pct": None,
        "today_wins": today_wins, "today_losses": today_losses,
        "today_win_rate": (today_wins / (today_wins + today_losses) * 100) if (today_wins + today_losses) else None,
        "open_count": len(cards), "open_value": 0.0,
        "starting_capital": _principal_at_day(today_iso_static),
        "all_time_pnl": 0.0, "all_time_pct": None,
        # Halt state — readable from settings without Binance.
        "loss_halted_today": (_setting("crypto_today_loss_halted", "0") == "1"),
        "profit_halted_today": (_setting("crypto_today_profit_halted", "0") == "1"),
        "loss_halt_overridden": (_setting("crypto_today_loss_overridden", "0") == "1"),
        "profit_halt_overridden": (_setting("crypto_today_profit_overridden", "0") == "1"),
        "loss_halt_pct": loss_halt_pct,
        "profit_halt_pct": profit_halt_pct,
    }
    return jsonify({"account": account, "position_cards": cards, "static": True})


@bp.route("/api/dashboard")
def api_dashboard():
    """LIVE endpoint — full Binance-dependent data. Called via JS after the static render.

    Fetches tickers ONCE (with timeout), passes to both account_summary and
    position_cards. If Binance is unreachable AND we have a previous good
    response cached, serve the cached payload with stale=True instead of
    leaving the dashboard hung at "loading…".
    """
    tickers: dict = {}
    binance_ok = False
    try:
        tickers = {t["symbol"]: float(t["price"]) for t in _binance_client().get_all_tickers()}
        binance_ok = True
    except Exception as e:
        import logging
        logging.getLogger("crypto").warning("api_dashboard: binance fetch failed: %s", e)

    if binance_ok:
        account = _account_summary(prefetched_tickers=tickers)
        cards = _open_position_cards(tickers=tickers)
        payload = {"account": account, "position_cards": cards,
                   "static": False, "stale": False}
        _DASHBOARD_CACHE["payload"] = payload
        _DASHBOARD_CACHE["ts"] = datetime.utcnow().isoformat() + "Z"
        return jsonify(payload)

    cached = _DASHBOARD_CACHE.get("payload")
    if cached is not None:
        stale_payload = dict(cached)
        stale_payload["static"] = False
        stale_payload["stale"] = True
        stale_payload["stale_since"] = _DASHBOARD_CACHE.get("ts")
        return jsonify(stale_payload)

    account = _account_summary(prefetched_tickers={})
    cards = _open_position_cards(tickers={})
    return jsonify({"account": account, "position_cards": cards,
                    "static": False, "stale": True, "stale_since": None})


@bp.route("/api/daily-pnl")
def api_daily_pnl():
    """Daily realized P&L points for the dashboard ROI bar chart.

    Replaces the previous server-rendered Jinja injection so the chart can
    refresh without a full page reload. Frontend polls this every 30s.
    """
    try:
        days = int(request.args.get("days", "30"))
    except (TypeError, ValueError):
        days = 30
    days = max(1, min(days, 365))
    return jsonify(_daily_pnl(days=days))


@bp.route("/api/recent-trades")
def api_recent_trades():
    """Returns trades since `since_id` query param. For browser notifications polling."""
    from flask import request as flask_request
    since_id = int(flask_request.args.get("since_id", 0))
    trades = (
        CryptoTrade.query
        .filter(CryptoTrade.id > since_id, CryptoTrade.is_paper == _displayed_is_paper())
        .order_by(CryptoTrade.id)
        .limit(20).all()
    )
    return jsonify([
        {
            "id": t.id, "symbol": t.symbol, "side": t.side,
            "price": float(t.price), "qty": float(t.qty),
            "quote": float(t.quote_amount or 0),
            "strategy": t.strategy or "?",
            "ts": t.executed_at.isoformat() + "Z",
        }
        for t in trades
    ])


_STRUCTURED_NOTES_PREFIXES = (
    "stop=$", "target=$", "max_hold=", "exit=", "original_stop=$",
    "PARTIAL DONE", "PAPER ENTRY", "LIVE ENTRY", "PAPER EXIT", "LIVE EXIT",
)


def _extract_entry_reason(notes: str | None, strategy_fallback: str | None = None) -> str:
    """Pull the human-readable entry reason out of a position's notes string.

    Notes mix structured tokens (stop=$X, partial_done=1, etc.) with one
    free-text reason like "fresh breakout +4.2%, vol=1.6x, RSI=67". The naive
    `notes.split("·")[-1]` grabs whichever token is last — after a partial-take
    rewrite that's `original_stop=$X`, NOT the entry reason. This walks the
    tokens and returns the LAST one that doesn't match a structured prefix.
    Falls back to strategy name when no free-text reason is available.
    """
    fallback = (strategy_fallback or "").strip()
    if not notes:
        return fallback
    last_freetext = ""
    for token in notes.split("·"):
        token = token.strip()
        if not token:
            continue
        if token == "partial_done=1":
            continue
        if token.startswith(_STRUCTURED_NOTES_PREFIXES):
            continue
        last_freetext = token
    return last_freetext or fallback


def _build_journal_entries(include_live_prices: bool = True) -> dict:
    """Build journal entries (open + closed). Returns dict with totals + entries.

    Heavy: makes a Binance API call for live prices on open positions.
    """
    from analysis.crypto_executor import parse_entry_notes
    # Fee subtraction — same as the daily-ROI chart so journal numbers match.
    # Default 0.1% per side (Binance Spot taker); user-overridable via setting.
    try:
        fee_rate = float(_setting("crypto_fee_rate_per_side", "0.001"))
    except (TypeError, ValueError):
        fee_rate = 0.001

    trades = CryptoTrade.query.filter(CryptoTrade.is_paper == _displayed_is_paper()).order_by(CryptoTrade.executed_at).all()
    by_sym: dict = {}
    for t in trades:
        if t.strategy == "manual_liquidation":
            continue
        by_sym.setdefault(t.symbol, []).append(t)

    closed_entries = []
    open_entries = []
    for sym, ts in by_sym.items():
        # FIFO with REMAINING-QTY tracking — handles partial sells correctly.
        buys: list[list] = []  # each entry: [buy_trade, remaining_qty]
        for t in ts:
            if t.side == "BUY":
                buys.append([t, float(t.qty)])
            elif t.side == "SELL" and buys:
                sell_qty_remaining = float(t.qty)
                sell_price = float(t.price)
                while sell_qty_remaining > 1e-9 and buys:
                    buy, buy_remaining = buys[0]
                    consumed = min(buy_remaining, sell_qty_remaining)
                    buys[0][1] -= consumed
                    sell_qty_remaining -= consumed
                    meta = parse_entry_notes(buy.notes)
                    _bq3 = float(buy.qty) or 1
                    buy_value = float(buy.quote_amount or float(buy.price) * float(buy.qty)) * (consumed / _bq3)
                    sell_value = sell_price * consumed
                    # Dust filter (matches the open-positions path): when a SELL
                    # consumes a tiny BUY residual left by lot-step rounding on a
                    # PRIOR sell, the FIFO produces a ghost segment ($X cents)
                    # paired against an unrelated strategy's exit-reason. Skip.
                    if buy_value < 1.0:
                        if buys[0][1] <= 1e-9:
                            buys.pop(0)
                        continue
                    gross_pnl_usd = sell_value - buy_value
                    fee_usd = (buy_value + sell_value) * fee_rate
                    pnl_usd = gross_pnl_usd - fee_usd
                    pnl_pct = (pnl_usd / buy_value * 100) if buy_value > 0 else 0.0
                    held_h = (t.executed_at - buy.executed_at).total_seconds() / 3600
                    exit_reason = "?"
                    if t.notes:
                        parts = t.notes.split("·", 1)
                        exit_reason = parts[1].strip() if len(parts) > 1 else t.notes.strip()
                    is_partial_fill = consumed < float(buy.qty)
                    # Each closed-entry row shows the stop active during ITS segment.
                    is_partial_event = "partial profit take" in (exit_reason or "")
                    has_partial_history = meta["original_stop"] is not None
                    if is_partial_event and has_partial_history:
                        seg_stop = meta["original_stop"]
                        seg_moved_to = meta["stop"]
                        seg_prior = None
                    elif has_partial_history:
                        seg_stop = meta["stop"]
                        seg_moved_to = None
                        seg_prior = meta["original_stop"]
                    else:
                        seg_stop = meta["stop"]
                        seg_moved_to = None
                        seg_prior = None
                    closed_entries.append({
                        "is_open": False,
                        "is_partial": is_partial_fill,
                        "symbol": sym, "strategy": buy.strategy,
                        "entry_at": buy.executed_at.isoformat() + "Z",
                        "exit_at":  t.executed_at.isoformat() + "Z",
                        "entry_price": float(buy.price), "exit_price": sell_price,
                        "qty": consumed, "notional": buy_value,
                        "entry_value": buy_value, "exit_value": sell_value,
                        "stop": seg_stop, "target": meta["target"],
                        "stop_moved_to": seg_moved_to,
                        "stop_was": seg_prior,
                        "original_stop": meta["original_stop"],
                        "pnl_pct": pnl_pct, "pnl_usd": pnl_usd,
                        "fee_usd": round(fee_usd, 4),
                        "gross_pnl_usd": round(gross_pnl_usd, 4),
                        "held_h": held_h,
                        "entry_reason": _extract_entry_reason(buy.notes, buy.strategy),
                        "exit_reason": exit_reason,
                    })
                    if buys[0][1] <= 1e-9:
                        buys.pop(0)
        # Remaining buys (positive qty) are still-open. Apply dust filter
        # (matches _open_positions): residuals < $1 are rounding remnants.
        for buy, remaining_qty in buys:
            if remaining_qty <= 1e-9:
                continue
            if remaining_qty * float(buy.price) < 1.0:
                continue  # dust — skip
            meta = parse_entry_notes(buy.notes)
            entry_value = float(buy.price) * remaining_qty
            held_h = (datetime.utcnow() - buy.executed_at).total_seconds() / 3600
            open_entries.append({
                "is_open": True,
                "is_partial": remaining_qty < float(buy.qty),
                "symbol": sym, "strategy": buy.strategy,
                "entry_at": buy.executed_at.isoformat() + "Z", "exit_at": None,
                "entry_price": float(buy.price), "exit_price": None,
                "qty": remaining_qty, "notional": entry_value,
                "entry_value": entry_value, "exit_value": None,
                "stop": meta["stop"], "target": meta["target"],
                "original_stop": meta["original_stop"],
                "pnl_pct": None, "pnl_usd": None, "held_h": held_h,
                "entry_reason": _extract_entry_reason(buy.notes, buy.strategy),
                "exit_reason": None,
            })

    if include_live_prices and open_entries:
        try:
            tickers = {t["symbol"]: float(t["price"]) for t in _binance_client().get_all_tickers()}
            for e in open_entries:
                cur = tickers.get(e["symbol"])
                if cur is not None:
                    e["exit_price"] = cur
                    buy_value = e["entry_price"] * e["qty"]
                    sell_value = cur * e["qty"]
                    gross = sell_value - buy_value
                    fee = (buy_value + sell_value) * fee_rate     # both sides estimated
                    e["exit_value"] = sell_value
                    e["pnl_usd"] = gross - fee                     # net of fees
                    e["pnl_pct"] = (e["pnl_usd"] / buy_value * 100) if buy_value > 0 else 0.0
                    e["fee_usd"] = round(fee, 4)
                    e["gross_pnl_usd"] = round(gross, 4)
        except Exception:
            pass

    open_entries.sort(key=lambda e: e["entry_at"], reverse=True)
    closed_entries.sort(key=lambda e: e["exit_at"], reverse=True)
    return {
        "open_entries": open_entries,
        "closed_entries": closed_entries,
        "realized_pnl": sum(e["pnl_usd"] for e in closed_entries),
        "unrealized_pnl": sum(e["pnl_usd"] for e in open_entries if e["pnl_usd"] is not None),
    }


@bp.route("/journal")
def journal():
    """Renders fast shell — JS populates from /api/journal."""
    return render_template("crypto_journal.html")


@bp.route("/api/journal")
def api_journal():
    """FAST endpoint — DB-only data. No Binance call. Cards render instantly.

    Filters by date in MYT (default = today). An entry is "on" a date if
    EITHER its BUY or its SELL happened on that date. Open positions only
    appear on their entry date; closed positions appear on both entry and
    exit dates.

    Open positions return with current_price=None — JS fetches live prices
    separately via /api/journal/prices and updates DOM in-place.
    """
    from datetime import timezone, timedelta
    MYT = timezone(timedelta(hours=8))
    today_myt = datetime.utcnow().replace(tzinfo=timezone.utc).astimezone(MYT).date()

    date_str = request.args.get("date") or today_myt.isoformat()
    try:
        from datetime import date as _date
        selected_date = _date.fromisoformat(date_str)
    except (TypeError, ValueError):
        selected_date = today_myt

    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 25))
    data = _build_journal_entries(include_live_prices=False)

    def _myt_date(iso: str | None):
        if not iso:
            return None
        from datetime import datetime as _dt
        try:
            dt = _dt.fromisoformat(iso)
        except ValueError:
            return None
        return dt.replace(tzinfo=timezone.utc).astimezone(MYT).date()

    def _matches(e):
        # Open positions ALWAYS appear on today's date (they're "still
        # affecting today" regardless of when opened — matches the dashboard
        # which shows them under today's P&L). They do NOT appear on past
        # dates — historical days only show what was actually closed.
        # Closed trades appear ONLY on the day they were SOLD (no double-listing
        # on the buy day, which was confusing for overnight trades).
        if e.get("is_open"):
            return selected_date == today_myt
        return _myt_date(e.get("exit_at")) == selected_date

    open_filtered = [e for e in data["open_entries"] if _matches(e)]
    closed_filtered = [e for e in data["closed_entries"] if _matches(e)]
    entries = open_filtered + closed_filtered
    total = len(entries)
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, pages))
    paged = entries[(page - 1) * per_page:page * per_page]
    open_symbols = sorted({e["symbol"] for e in open_filtered})
    try:
        fee_rate = float(_setting("crypto_fee_rate_per_side", "0.001"))
    except (TypeError, ValueError):
        fee_rate = 0.001
    return jsonify({
        "entries": paged,
        "open_count": len(open_filtered),
        "closed_count": len(closed_filtered),
        "realized_pnl": sum(e["pnl_usd"] for e in closed_filtered),  # net of fees
        "unrealized_pnl": 0,
        "open_symbols": open_symbols,
        "fee_rate": fee_rate,  # JS uses this to deduct estimated fees in live updates
        "selected_date": selected_date.isoformat(),
        "today_date": today_myt.isoformat(),
        "is_today": selected_date == today_myt,
        "page": page, "pages": pages, "per_page": per_page, "total": total,
        "has_prev": page > 1, "has_next": page < pages,
        "start_idx": (page - 1) * per_page + 1 if total else 0,
        "end_idx": min(page * per_page, total),
    })


@bp.route("/api/journal/prices")
def api_journal_prices():
    """Live prices for open positions — slow Binance call, called separately by JS.

    Returns: {symbol: price, ...} for all coins on Binance. JS picks the ones it needs.
    """
    try:
        prices = {t["symbol"]: float(t["price"]) for t in _binance_client().get_all_tickers()}
        return jsonify({"prices": prices, "ok": True})
    except Exception as e:
        return jsonify({"prices": {}, "ok": False, "error": str(e)}), 500


@bp.route("/holdings")
def holdings():
    from analysis.crypto_executor import _open_positions, parse_entry_notes
    rows = CryptoHolding.query.order_by(CryptoHolding.value_usd.desc().nullslast()).all()
    total = sum((r.value_usd or 0) for r in rows)
    # Map asset (e.g. "DOGE") → open position trade ID, so the holdings page can show a Sell button
    open_pos_by_asset = {}
    for p in _open_positions(is_paper=_displayed_is_paper()):
        # Strip the trading-quote suffix to get the base asset (DOGEUSDT → DOGE)
        if p.symbol.endswith("USDT"):
            asset = p.symbol[:-4]
            meta = parse_entry_notes(p.notes)
            open_pos_by_asset[asset] = {
                "trade_id": p.id,
                "symbol": p.symbol,
                "entry_price": float(p.price),
                "strategy": p.strategy,
                "partial_done": meta.get("partial_done", False),
            }
    from analysis.crypto_executor import _is_paper_mode_setting
    return render_template(
        "crypto_holdings.html",
        rows=rows, total=total,
        has_keys=_has_binance_keys(),
        open_pos_by_asset=open_pos_by_asset,
        is_paper_mode=_is_paper_mode_setting(),
    )


@bp.route("/coins")
def coins():
    rows = CryptoCoin.query.order_by(CryptoCoin.symbol).all()
    return render_template("crypto_coins.html", rows=rows)


def _setup_status(close: float, prior_high: float, vol_ratio: float, sma50: float | None,
                  rsi: float | None = None, chg_24h: float | None = None) -> tuple[str, str]:
    """Return (label, color) — now aligned with actual strategy filters.

    READY TO FIRE = passes all strategy gates (gap ≤5%, vol >1.5×, above SMA50, RSI<75, 24h<25%)
    """
    if close <= prior_high:
        # Hasn't broken out yet
        gap = (close - prior_high) / prior_high * 100
        if -2 < gap < 0 and vol_ratio > 1.2:
            return ("near breakout", "sky")
        if vol_ratio > 2:
            return ("volume surge (no breakout)", "sky")
        if sma50 is not None and close > sma50:
            return ("in trend", "zinc")
        return ("no setup", "zinc")

    # Above prior high — check ALL strategy filters
    gap = (close - prior_high) / prior_high * 100
    crit_v = vol_ratio > 1.5
    crit_t = sma50 is not None and close > sma50
    crit_gap = gap <= 5
    crit_rsi = rsi is None or rsi < 75
    crit_chg = chg_24h is None or chg_24h < 25

    if crit_v and crit_t and crit_gap and crit_rsi and crit_chg:
        return ("READY TO FIRE", "emerald")
    # Failed one or more strategy filters — diagnostic label
    fails = []
    if not crit_gap: fails.append(f"gap {gap:.1f}%>5%")
    if not crit_v: fails.append(f"vol {vol_ratio:.1f}x<1.5x")
    if not crit_t: fails.append("below SMA50")
    if not crit_rsi: fails.append(f"RSI {rsi:.0f}>75")
    if not crit_chg: fails.append(f"24h {chg_24h:.0f}%>25%")
    return (f"chase: {', '.join(fails[:2])}", "amber")


@bp.route("/universe")
def universe():
    """Renders shell page fast. Data loaded by JS via /api/universe."""
    return render_template("crypto_universe.html")


@bp.route("/api/universe")
def api_universe():
    """JSON endpoint returning dynamic universe + setup status. Uses CACHED klines only.
    Loop refreshes klines every 15 min, so cache is at most 15 min stale — that's fine.
    """
    from analysis.crypto_universe import get_dynamic_universe
    from analysis.crypto_data import load_cached
    from analysis.indicators import attach

    try:
        coins_data = get_dynamic_universe(top_volume=30, top_movers=10, min_volume_usd=10_000_000)
    except Exception as e:
        return jsonify({"error": str(e), "rows": []}), 500

    rows = []
    for c in coins_data:
        sym = c["symbol"]
        df = load_cached(sym, "4h")  # cache only — no fresh fetch
        gap = vr = rsi = sma50 = None
        status_label, status_color = ("no data", "zinc")
        if df is not None and not df.empty and len(df) >= 50:
            d = attach(df).dropna(subset=["high20", "sma50", "volratio"])
            if len(d) >= 2:
                last = d.iloc[-1]
                close = float(last["Close"])
                prior_h = float(d["high20"].iloc[-2])
                vr = float(last["volratio"])
                sma50 = float(last["sma50"]) if pd.notna(last["sma50"]) else None
                rsi = float(last["rsi14"]) if pd.notna(last["rsi14"]) else None
                gap = (close - prior_h) / prior_h * 100
                status_label, status_color = _setup_status(close, prior_h, vr, sma50, rsi, c["change_pct_24h"])
        rows.append({
            "symbol": sym, "price": c["price"], "change_24h": c["change_pct_24h"],
            "volume_usd": c["quote_volume"], "source": c["source"],
            "gap_to_high20": gap, "vol_ratio": vr, "rsi14": rsi, "sma50": sma50,
            "status_label": status_label, "status_color": status_color,
        })
    color_priority = {"emerald": 0, "amber": 1, "sky": 2, "zinc": 3}
    rows.sort(key=lambda r: (color_priority.get(r["status_color"], 4), -(r["change_24h"] or 0)))
    return jsonify({"rows": rows, "fetched_at": datetime.utcnow().isoformat()})


@bp.route("/coin/<symbol>")
def coin_detail(symbol: str):
    """Plotly chart for one coin: candles + SMA50 + 20-bar high + volume + RSI."""
    from analysis.crypto_data import load_cached, refresh_pair
    from analysis.indicators import attach

    symbol = symbol.upper()
    df = load_cached(symbol, "4h")
    if df is None or df.empty:
        # Try one fresh fetch
        try:
            df = refresh_pair(symbol, "4h")
        except Exception:
            df = None
    if df is None or df.empty:
        abort(404)

    df_ind = attach(df)
    df_tail = df_ind.tail(180).reset_index()  # last ~30 days

    def _series(name):
        return [None if pd.isna(x) else float(x) for x in df_tail[name]]

    chart_data = {
        "dates": [d.strftime("%Y-%m-%d %H:%M") for d in df_tail["date"]],
        "open": [float(x) for x in df_tail["Open"]],
        "high": [float(x) for x in df_tail["High"]],
        "low": [float(x) for x in df_tail["Low"]],
        "close": [float(x) for x in df_tail["Close"]],
        "volume": [float(x) for x in df_tail["Volume"]],
        "sma50": _series("sma50"),
        "high20": _series("high20"),
        "rsi14": _series("rsi14"),
    }

    last_row = df_ind.iloc[-1]
    last_close = float(last_row["Close"])
    prev_close = float(df_ind["Close"].iloc[-2]) if len(df_ind) > 1 else last_close
    chg_pct = (last_close - prev_close) / prev_close * 100 if prev_close else 0.0
    prior_high = float(df_ind["high20"].iloc[-2]) if len(df_ind) > 1 else last_close
    rsi = float(last_row["rsi14"]) if pd.notna(last_row["rsi14"]) else None
    vr = float(last_row["volratio"]) if pd.notna(last_row["volratio"]) else None
    sma50 = float(last_row["sma50"]) if pd.notna(last_row["sma50"]) else None
    gap = (last_close - prior_high) / prior_high * 100
    # Get 24h change for the status check
    try:
        _bclient = _binance_client()
        _t = _bclient.get_symbol_ticker(symbol=symbol)
        _t24 = next((x for x in _bclient.get_ticker() if x["symbol"] == symbol), None)
        _chg24 = float(_t24["priceChangePercent"]) if _t24 else None
    except Exception:
        _chg24 = None
    status_label, status_color = _setup_status(last_close, prior_high, vr or 0, sma50, rsi, _chg24)

    # Existing position? Use the proper net-quantity logic (BUYs - SELLs, dust-filtered)
    from analysis.crypto_executor import _open_positions
    open_pos = next((p for p in _open_positions(is_paper=_displayed_is_paper()) if p.symbol == symbol), None)
    has_position = open_pos is not None
    position_info = None
    if open_pos:
        # _open_positions attaches _remaining_qty (post-partial-sells); use it so
        # the coin page matches the journal after a partial profit-take.
        # P&L is NET of fees (matches dashboard card, journal, ROI graph) — was
        # GROSS before, causing this page to differ from every other surface.
        try:
            _coin_fee_rate = float(_setting("crypto_fee_rate_per_side", "0.001"))
        except (TypeError, ValueError):
            _coin_fee_rate = 0.001
        rem_qty = float(getattr(open_pos, "_remaining_qty", open_pos.qty))
        entry_price = float(open_pos.price)
        buy_value = entry_price * rem_qty
        sell_value = last_close * rem_qty
        gross = sell_value - buy_value
        fee = (buy_value + sell_value) * _coin_fee_rate
        net_pnl = gross - fee
        position_info = {
            "entry_price": entry_price,
            "qty": rem_qty,
            "value_now": sell_value,
            "pnl": net_pnl,
            "pnl_pct": (net_pnl / buy_value * 100) if buy_value > 0 else 0.0,
        }

    readout = {
        "last": last_close, "chg_pct": chg_pct,
        "prior_high20": prior_high, "gap_to_high": gap,
        "rsi14": rsi, "vol_ratio": vr, "sma50": sma50,
        "status_label": status_label, "status_color": status_color,
    }

    # Historical trade markers — find each trade's nearest 4h bar timestamp for chart alignment
    chart_start_ts = df_tail["date"].iloc[0].to_pydatetime()
    bar_dates = list(df_tail["date"])
    def _nearest_bar(t):
        """Snap a trade datetime to the closest 4h bar timestamp string."""
        diffs = [(abs((t - bd.to_pydatetime()).total_seconds()), bd) for bd in bar_dates]
        diffs.sort()
        return diffs[0][1].strftime("%Y-%m-%d %H:%M") if diffs else None

    trades_in_window = (
        CryptoTrade.query
        .filter(CryptoTrade.symbol == symbol, CryptoTrade.is_paper == _displayed_is_paper())
        .filter(CryptoTrade.executed_at >= chart_start_ts)
        .order_by(CryptoTrade.executed_at)
        .all()
    )
    trade_markers = []
    for t in trades_in_window:
        if t.strategy == "manual_liquidation":
            continue  # skip — historical XVG cleanup, not a system trade
        bar_x = _nearest_bar(t.executed_at)
        if bar_x is None:
            continue
        trade_markers.append({
            "x": bar_x,
            "price": float(t.price),
            "side": t.side,
            "qty": float(t.qty),
            "quote": float(t.quote_amount or 0),
            "strategy": t.strategy or "?",
            "ts_iso": t.executed_at.isoformat() + "Z",
        })

    return render_template(
        "crypto_coin.html",
        symbol=symbol,
        chart_json=json.dumps(chart_data),
        trades_json=json.dumps(trade_markers),
        readout=readout,
        has_position=has_position,
        position=position_info,
    )


def _paginate(query, page: int, per_page: int = 50) -> dict:
    """Return dict with rows, page metadata for templates."""
    total = query.count()
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, pages))
    rows = query.offset((page - 1) * per_page).limit(per_page).all()
    return {
        "rows": rows, "page": page, "per_page": per_page,
        "total": total, "pages": pages,
        "has_prev": page > 1, "has_next": page < pages,
        "start_idx": (page - 1) * per_page + 1 if total else 0,
        "end_idx": min(page * per_page, total),
    }


@bp.route("/trades")
def trades():
    page = int(request.args.get("page", 1))
    p = _paginate(CryptoTrade.query.order_by(CryptoTrade.executed_at.desc()), page, per_page=50)
    return render_template("crypto_trades.html", rows=p["rows"], pagination=p)


@bp.route("/runs")
def runs():
    page = int(request.args.get("page", 1))
    p = _paginate(CryptoRun.query.order_by(CryptoRun.started_at.desc()), page, per_page=50)
    return render_template("crypto_runs.html", rows=p["rows"], pagination=p)


_ip_cache = {"ip": None, "ts": 0}


def _get_public_ip() -> str | None:
    """Detect the server's outbound IP (what Binance sees). Cached for 60s."""
    import time
    import requests
    if _ip_cache["ip"] and (time.time() - _ip_cache["ts"]) < 60:
        return _ip_cache["ip"]
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            r = requests.get(url, timeout=3)
            if r.status_code == 200:
                ip = r.text.strip()
                if ip and len(ip) <= 45:
                    _ip_cache["ip"] = ip
                    _ip_cache["ts"] = time.time()
                    return ip
        except Exception:
            continue
    return None


@bp.route("/settings", methods=["GET", "POST"])
def settings():
    GUARDRAIL_KEYS = (
        "crypto_kill_switch", "crypto_trading_mode",
        "crypto_max_position_usd", "crypto_max_concurrent",
        "crypto_min_balance_usd",
        "crypto_loop_interval_min", "crypto_fast_check_interval_sec",
        "crypto_partial_take_enabled",
        "crypto_partial_take_trigger_pct",
        "crypto_partial_take_fraction",
        "crypto_breakeven_buffer_pct",
        "crypto_partial_lock_fraction",
        "crypto_loss_halt_enabled", "crypto_loss_halt_pct",
        "crypto_profit_halt_enabled", "crypto_profit_halt_pct",
        "crypto_ghost_feature_enabled",
    )
    if request.method == "POST":
        if request.form.get("settings_form_present") == "1":
            if "crypto_partial_take_enabled" not in request.form:
                _set_setting("crypto_partial_take_enabled", "off")
            if "crypto_loss_halt_enabled" not in request.form:
                _set_setting("crypto_loss_halt_enabled", "off")
            if "crypto_profit_halt_enabled" not in request.form:
                _set_setting("crypto_profit_halt_enabled", "off")
            if "crypto_ghost_feature_enabled" not in request.form:
                _set_setting("crypto_ghost_feature_enabled", "off")
        intervals_changed = False
        # Snapshot current trading mode BEFORE applying changes — used to detect transition
        current_mode = _setting("crypto_trading_mode", "paper")
        for key in GUARDRAIL_KEYS:
            val = request.form.get(key)
            if val is not None:
                # Live-mode safety: only require confirmation when TRANSITIONING from paper → live
                # (re-saving while already in LIVE shouldn't demand the confirm phrase)
                if key == "crypto_trading_mode" and val == "live" and current_mode != "live":
                    confirm = (request.form.get("live_confirm") or "").strip()
                    if confirm != "I UNDERSTAND":
                        flash("Switching to LIVE mode requires typing 'I UNDERSTAND' in the confirmation field.", "err")
                        continue
                    # Min-balance gate — refuse the mode switch if free USDT is below the
                    # configured floor. Per-trade guardrails would block trades anyway, but
                    # blocking the switch itself surfaces the problem immediately instead
                    # of silently going LIVE-but-inert.
                    try:
                        min_bal = float(_setting("crypto_min_balance_usd", "100"))
                        key_, secret_ = get_binance_creds()
                        if not key_ or not secret_:
                            flash("Switching to LIVE mode requires Binance API keys to be saved first.", "err")
                            continue
                        acct = _binance_client(key_, secret_).get_account()
                        usdt = next((b for b in acct["balances"] if b["asset"] == "USDT"), None)
                        free_usdt = float(usdt["free"]) if usdt else 0.0
                        if free_usdt < min_bal:
                            flash(f"Cannot switch to LIVE: free USDT ${free_usdt:.2f} is below the configured minimum ${min_bal:.2f}. Fund the account or lower the minimum first.", "err")
                            continue
                    except Exception as e:
                        flash(f"Cannot switch to LIVE: balance check failed ({e}). Verify API keys + Binance reachability.", "err")
                        continue
                if key in ("crypto_loop_interval_min", "crypto_fast_check_interval_sec"):
                    intervals_changed = True
                _set_setting(key, val)

        # Per-strategy enable/disable — checkboxes are sent only when checked,
        # so we compute disabled = ALL_STRATEGIES - (checkboxes posted).
        # The hidden marker "strategies_form_present=1" lets us tell a real
        # form submit apart from a partial POST that didn't include the section.
        if request.form.get("strategies_form_present") == "1":
            enabled = set(request.form.getlist("strategy_enabled"))
            disabled = [s for s in STRATEGY_NAMES if s not in enabled]
            _set_setting("crypto_disabled_strategies", ",".join(disabled))

        # If intervals changed, reschedule jobs live (no Flask restart needed)
        if intervals_changed:
            try:
                from webapp.scheduler import reschedule_crypto_jobs
                result = reschedule_crypto_jobs()
                fast_label = f"{result['fast_sec']}s" if result['fast_sec'] else "disabled"
                flash(
                    f"Settings updated. Scheduler rescheduled: loop={result['loop_min']}min, fast={fast_label}",
                    "ok",
                )
            except Exception as e:
                flash(f"Settings saved but scheduler reschedule failed: {e}", "err")
        else:
            flash("Crypto settings updated", "ok")
        return redirect(url_for("crypto.settings"))
    key, secret = get_binance_creds()
    health = _system_health()
    disabled_csv = _setting("crypto_disabled_strategies", "")
    disabled_strategies = {s.strip() for s in disabled_csv.split(",") if s.strip()}
    return render_template(
        "crypto_settings.html",
        kill_switch=_setting("crypto_kill_switch", "off"),
        trading_mode=_setting("crypto_trading_mode", "paper"),
        max_position_usd=_setting("crypto_max_position_usd", "50"),
        max_concurrent=_setting("crypto_max_concurrent", "2"),
        day_start_value_usd=_setting("crypto_day_start_value_usd", "0"),
        day_start_date=_setting("crypto_day_start_date", ""),
        min_balance_usd=_setting("crypto_min_balance_usd", "100"),
        loop_interval_min=_setting("crypto_loop_interval_min", "15"),
        fast_check_interval_sec=_setting("crypto_fast_check_interval_sec", "60"),
        partial_take_enabled=_setting("crypto_partial_take_enabled", "on"),
        partial_take_trigger_pct=_setting("crypto_partial_take_trigger_pct", "4.0"),
        partial_take_fraction=_setting("crypto_partial_take_fraction", "0.5"),
        breakeven_buffer_pct=_setting("crypto_breakeven_buffer_pct", "1.0"),
        partial_lock_fraction=_setting("crypto_partial_lock_fraction", "0.5"),
        loss_halt_enabled=_setting("crypto_loss_halt_enabled", "off"),
        loss_halt_pct=_setting("crypto_loss_halt_pct", "5.0"),
        profit_halt_enabled=_setting("crypto_profit_halt_enabled", "off"),
        profit_halt_pct=_setting("crypto_profit_halt_pct", "5.0"),
        today_loss_halted=_setting("crypto_today_loss_halted", "0"),
        today_profit_halted=_setting("crypto_today_profit_halted", "0"),
        ghost_feature_enabled=_setting("crypto_ghost_feature_enabled", "on"),
        health=health,
        strategy_names=STRATEGY_NAMES,
        disabled_strategies=disabled_strategies,
        has_keys=_has_binance_keys(),
        api_key_masked=_mask(key),
        api_secret_masked=_mask(secret),
    )


def _system_health() -> dict:
    """Snapshot of running-system state for the Settings page health panel.

    Aggregates: scheduler next fire, last sync, last error, today's start
    value + daily drawdown. All best-effort — missing data renders as None.
    """
    from datetime import timezone, timedelta
    MYT = timezone(timedelta(hours=8))

    def _to_myt(dt):
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(MYT).strftime("%Y-%m-%d %H:%M:%S MYT")

    health = {
        "next_loop_fire": None,
        "next_fast_fire": None,
        "last_sync_at": None,
        "last_sync_summary": None,
        "last_error_at": None,
        "last_error_summary": None,
        "day_start_value": None,
        "drawdown_pct": None,
        "halt_enabled": (_setting("crypto_loss_halt_enabled", "off") == "on"
                         or _setting("crypto_profit_halt_enabled", "off") == "on"),
    }

    try:
        from webapp.scheduler import get_next_run
        health["next_loop_fire"] = _to_myt(get_next_run("crypto_loop"))
        health["next_fast_fire"] = _to_myt(get_next_run("fast_exit_check"))
    except Exception:
        pass

    last_sync = None
    try:
        last_sync = (CryptoRun.query.filter_by(kind="sync", status="ok")
                     .order_by(CryptoRun.id.desc()).first())
        if last_sync:
            health["last_sync_at"] = _to_myt(last_sync.started_at)
            health["last_sync_summary"] = (last_sync.summary or "")[:120]
    except Exception:
        pass

    try:
        last_err = (CryptoRun.query.filter_by(status="error")
                    .order_by(CryptoRun.id.desc()).first())
        if last_err:
            health["last_error_at"] = _to_myt(last_err.started_at)
            health["last_error_summary"] = (last_err.error or last_err.summary or "")[:200]
    except Exception:
        pass

    try:
        day_start = float(_setting("crypto_day_start_value_usd", "0") or 0)
        if day_start > 0:
            health["day_start_value"] = day_start
            import re
            if last_sync and last_sync.summary:
                m = re.search(r"total value \$(\d+\.\d+)", last_sync.summary)
                if m:
                    current_val = float(m.group(1))
                    health["drawdown_pct"] = (day_start - current_val) / day_start * 100
    except Exception:
        pass

    return health


@bp.route("/api/snapshots/backfill", methods=["POST"])
def api_snapshots_backfill():
    """One-shot: seed crypto_daily_snapshots from historical CryptoRun summaries.

    Sync runs (kind='sync', status='ok') log strings like
    "synced N real + M dust assets, total value $XXX.XX". For each MYT day,
    we pick the EARLIEST sync (closest to MYT midnight) as the day-start
    approximation and insert it as a 'backfill' source snapshot. Existing
    rows (e.g. real rollover snapshots) are NOT overwritten — backfill only
    fills gaps.

    Returns: {"ok": True, "inserted": N, "skipped_existing": M, "scanned": K}
    """
    from datetime import timezone, timedelta
    from webapp.models import CryptoDailySnapshot, CryptoRun, db
    import re
    MYT = timezone(timedelta(hours=8))
    pattern = re.compile(r"total value \$([\d,]+\.\d+)")

    runs = (CryptoRun.query
            .filter(CryptoRun.kind == "sync",
                    CryptoRun.status == "ok",
                    CryptoRun.summary.isnot(None))
            .order_by(CryptoRun.started_at.asc())
            .all())

    # For each MYT date, take the EARLIEST sync we can parse — that's the
    # closest proxy for "value at start of that day."
    earliest_per_day: dict = {}  # date_iso → total_value_usd
    scanned = 0
    for r in runs:
        if not r.summary:
            continue
        scanned += 1
        m = pattern.search(r.summary)
        if not m:
            continue
        try:
            value = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if value <= 0:
            continue
        date_iso = (r.started_at.replace(tzinfo=timezone.utc)
                    .astimezone(MYT).date().isoformat())
        if date_iso not in earliest_per_day:
            earliest_per_day[date_iso] = value

    # Bulk-check existing rows so we know what to skip without per-iteration
    # round-trips.
    existing_dates = {s.date for s in CryptoDailySnapshot.query.all()}
    inserted = 0
    skipped = 0
    for date_iso, value in earliest_per_day.items():
        if date_iso in existing_dates:
            skipped += 1
            continue
        db.session.add(CryptoDailySnapshot(
            date=date_iso,
            total_value_usd=value,
            usdt_free=None,
            open_value_usd=None,
            deposits_during_day_usd=None,  # unknown from CryptoRun summaries
            source="backfill",
        ))
        inserted += 1
    if inserted:
        db.session.commit()
    return jsonify({
        "ok": True,
        "scanned": scanned,
        "inserted": inserted,
        "skipped_existing": skipped,
    })


@bp.route("/api/halts/override", methods=["POST"])
def api_halts_override():
    """User clicked 'Resume trading today' on the halt banner.

    Body: {"type": "loss" | "profit"}

    Effect:
      - Clears today_<kind>_halted to "0" (banner disappears, _check_guardrails
        no longer blocks new entries).
      - Sets today_<kind>_overridden to "1" so update_day_start_and_check_halt
        won't immediately re-fire the halt this tick (drawdown is still past
        threshold; without this flag, the next loop tick would re-trip).
      - Logs a CryptoRun entry kind="halt_override" for audit trail.

    The override flag auto-clears at next MYT midnight along with the halt
    flag, so each kind re-arms fully tomorrow. There's no permanent setting
    change — user has to consciously override every day they want to trade
    past their threshold.
    """
    from webapp.models import CryptoRun, db
    body = request.get_json(silent=True) or {}
    kind = (body.get("type") or "").strip().lower()
    if kind not in ("loss", "profit"):
        return jsonify({"ok": False, "reason": "type must be 'loss' or 'profit'"}), 400
    halted_key = f"crypto_today_{kind}_halted"
    overridden_key = f"crypto_today_{kind}_overridden"
    if _setting(halted_key, "0") != "1":
        return jsonify({"ok": False, "reason": f"no active {kind} halt to override"}), 400
    # Capture today's P&L pct for the audit log so we can later see at what
    # drawdown the user chose to push through. Best-effort — don't block override
    # if account_value lookup fails.
    pnl_pct = None
    try:
        try:
            day_start = float(_setting("crypto_day_start_value_usd", "0") or 0)
        except (TypeError, ValueError):
            day_start = 0.0
        if day_start > 0:
            acct = _account_summary()
            cur = acct.get("account_value")
            if cur is not None:
                pnl_pct = (cur - day_start) / day_start * 100.0
    except Exception:
        pass
    # ORDER MATTERS: set the override flag FIRST, then clear the halted flag.
    # Each _set_setting commits independently, so there's a brief window where
    # both flags are visible to other threads. Halt-fire condition is
    # (halted != "1") AND (overridden != "1"); writing in this order means the
    # window has (halted=1, overridden=1) — which still passes the gate (halt
    # stays armed, but no re-fire because halted is set). Reverse order would
    # produce (halted=0, overridden=0) — the fast-exit-check would re-fire
    # the halt and re-liquidate positions the user wanted to keep.
    _set_setting(overridden_key, "1")
    _set_setting(halted_key, "0")
    # Kill ghost — user has resumed real trading, ghost no longer represents
    # a meaningful counterfactual (real bot is now opening new positions).
    try:
        from analysis.crypto_ghost import kill_ghost
        kill_ghost(f"resumed (override {kind} halt)")
    except Exception as _e:
        import logging
        logging.getLogger("crypto").warning("ghost kill failed (non-fatal): %s", _e)
    ts = datetime.utcnow()
    pnl_str = f"{pnl_pct:+.2f}%" if pnl_pct is not None else "?"
    summary = (f"User override: {kind.upper()} halt cleared (today P&L {pnl_str}). "
               f"Re-arms at next MYT midnight; no further {kind} halt will fire today.")
    try:
        db.session.add(CryptoRun(kind="halt_override", status="ok",
                                  started_at=ts, ended_at=ts, summary=summary))
        db.session.commit()
    except Exception:
        db.session.rollback()
    import logging
    logging.getLogger("crypto").warning(summary)
    return jsonify({"ok": True, "kind": kind, "today_pnl_pct": pnl_pct})


@bp.route("/api/halts/halt_now", methods=["POST"])
def api_halts_halt_now():
    """Manual halt: sell all open positions + block new entries until next MYT midnight.

    Mirrors the auto-halt flow (including ghost portfolio start) so you can
    later see what your held positions would have done after the halt.
    Reuses the loss_halted flag for guardrail blocking — banner will show as
    a "LOSS HALT" with override + end+rearm buttons available.
    """
    from webapp.models import CryptoRun, db
    from analysis.crypto_executor import sell_all_open_positions, _collect_ghost_init_data
    ts = datetime.utcnow()
    # Snapshot positions + current account value BEFORE selling — ghost needs both
    ghost_pre = _collect_ghost_init_data()
    pre_value = None
    try:
        acct = _account_summary()
        pre_value = acct.get("account_value")
    except Exception:
        pass
    try:
        n_closed = sell_all_open_positions("MANUAL HALT")
    except Exception as e:
        return jsonify({"ok": False, "reason": f"sell_all failed: {e}"}), 500
    _set_setting("crypto_today_loss_halted", "1")
    # Start ghost portfolio so user can see what would have happened if held
    try:
        from analysis.crypto_ghost import start_ghost
        start_ghost(ghost_pre, pre_value)
    except Exception as _ge:
        import logging
        logging.getLogger("crypto").warning("ghost start failed (non-fatal): %s", _ge)
    summary = f"User MANUAL HALT: closed {n_closed} positions, blocking new entries until MYT midnight."
    try:
        db.session.add(CryptoRun(kind="manual_halt", status="ok",
                                  started_at=ts, ended_at=ts, summary=summary))
        db.session.commit()
    except Exception:
        db.session.rollback()
    import logging
    logging.getLogger("crypto").warning(summary)
    return jsonify({"ok": True, "closed": n_closed})


@bp.route("/api/halts/end_and_rearm", methods=["POST"])
def api_halts_end_and_rearm():
    """End the active halt AND re-arm at a new threshold percent.

    Body: {"type": "loss" | "profit", "new_pct": 8.0}

    Differs from /api/halts/override:
      - Override: clears halt + sets overridden flag (no re-fire today)
      - End+rearm: clears BOTH flags AND updates the threshold setting
        → halt CAN fire again today if P&L crosses the new threshold

    Use case: hit +5% profit halt, want to ride higher with re-armed safety
    net at, say, +8%. The user picks the new threshold via a dashboard prompt.
    """
    from webapp.models import CryptoRun, db
    body = request.get_json(silent=True) or {}
    kind = (body.get("type") or "").strip().lower()
    if kind not in ("loss", "profit"):
        return jsonify({"ok": False, "reason": "type must be 'loss' or 'profit'"}), 400
    try:
        new_pct = float(body.get("new_pct"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "reason": "new_pct must be a number"}), 400
    # Min/floor (kind='loss') accepts signed values: -5 = halt at -5%, +6 = halt at +6%
    # Max/ceiling (kind='profit') must be positive (no use case for negative ceiling)
    if kind == "profit" and new_pct <= 0:
        return jsonify({"ok": False, "reason": "max P&L must be positive"}), 400

    halted_key = f"crypto_today_{kind}_halted"
    overridden_key = f"crypto_today_{kind}_overridden"
    threshold_key = f"crypto_{kind}_halt_pct"

    old_pct = _setting(threshold_key, "?")
    # Update threshold first so the halt re-arms at the new value
    _set_setting(threshold_key, f"{new_pct:.2f}")
    # Clear BOTH flags so halt can fire again today
    _set_setting(overridden_key, "0")
    _set_setting(halted_key, "0")
    # Kill the current ghost — user resumed real trading, this ghost is stale.
    # If the halt re-fires later at the new threshold, a fresh ghost spawns.
    try:
        from analysis.crypto_ghost import kill_ghost
        kill_ghost(f"resumed (re-arm {kind} at {new_pct:.2f}%)")
    except Exception as _e:
        import logging
        logging.getLogger("crypto").warning("ghost kill failed (non-fatal): %s", _e)

    ts = datetime.utcnow()
    label = "MAX" if kind == "profit" else "MIN"
    summary = (f"User end-halt+rearm: {label} P&L halt cleared, threshold "
               f"changed from {old_pct}% to {new_pct:+.2f}%. Halt may re-fire today "
               f"if P&L crosses new threshold.")
    try:
        db.session.add(CryptoRun(kind="halt_end_rearm", status="ok",
                                  started_at=ts, ended_at=ts, summary=summary))
        db.session.commit()
    except Exception:
        db.session.rollback()
    import logging
    logging.getLogger("crypto").warning(summary)
    return jsonify({"ok": True, "kind": kind, "new_threshold_pct": new_pct,
                    "old_threshold_pct": old_pct})


@bp.route("/api/paper/deposit", methods=["POST"])
def api_paper_deposit():
    """Paper-mode deposit/withdraw form submission.

    Single text input — accepts signed numbers (+ deposit, − withdrawal).
    Increments today's snapshot row's deposits_during_day_usd by the entered
    amount, then recomputes synth so dashboard + halt threshold immediately
    reflect the new principal.

    Per-event detail is NOT preserved — only the aggregated daily total
    persists. Submit twice (e.g., +$50 then +$30) and today's row reads $80.
    Mistake recovery: enter the inverse amount (e.g., -$80) to zero it out.

    Live-mode users should use the 'Refresh deposit/withdraw' button instead,
    which queries Binance directly. This endpoint refuses in live mode to
    avoid muddying the live deposit ledger with manually-entered values.
    """
    from analysis.crypto_executor import (
        _compute_synth_day_start_today,
        _set_setting,
        _is_paper_mode_setting,
    )
    from webapp.models import CryptoDailySnapshot, db
    from datetime import timezone, timedelta

    if not _is_paper_mode_setting():
        flash("Paper-deposit form is paper-mode only. Live users: use the 'Refresh deposit/withdraw' button.", "error")
        return redirect(url_for("crypto.settings"))

    raw = (request.form.get("amount") or "").strip()
    try:
        amount = float(raw)
    except (TypeError, ValueError):
        flash(f"Invalid amount '{raw}' — enter a number, e.g. 100 or -50.", "error")
        return redirect(url_for("crypto.settings"))
    if abs(amount) < 0.01:
        flash("Amount too small (need ≥ $0.01 absolute). Skipped.", "warn")
        return redirect(url_for("crypto.settings"))

    MYT = timezone(timedelta(hours=8))
    today_iso = (datetime.utcnow().replace(tzinfo=timezone.utc)
                 .astimezone(MYT).date().isoformat())

    # Update today's row in the snapshot table. Increment, don't replace —
    # so multiple submissions accumulate (e.g., user splits a $200 deposit
    # into two $100 entries).
    row = CryptoDailySnapshot.query.get(today_iso)
    if row is None:
        row = CryptoDailySnapshot(
            date=today_iso,
            total_value_usd=0.0,
            deposits_during_day_usd=amount,
            source="synth",
        )
        db.session.add(row)
    else:
        prior = float(row.deposits_during_day_usd or 0.0)
        row.deposits_during_day_usd = prior + amount
    db.session.commit()

    try:
        synth = _compute_synth_day_start_today()
        _set_setting("crypto_day_start_value_usd", f"{synth:.4f}")
        _set_setting("crypto_day_start_format", "synth")
        row = CryptoDailySnapshot.query.get(today_iso)
        if row is not None:
            row.total_value_usd = float(synth)
            db.session.commit()
    except Exception as e:
        flash(f"Paper deposit recorded ({amount:+.2f}) but synth recompute failed: {e}", "warn")
        return redirect(url_for("crypto.settings"))

    new_total = float(row.deposits_during_day_usd or 0.0) if row else amount
    sign = "+" if amount >= 0 else ""
    flash(f"Paper {('deposit' if amount > 0 else 'withdrawal')} {sign}{amount:.2f} recorded. "
          f"Today's row total: ${new_total:+.2f}. Synth day-start: ${synth:.2f}.", "ok")
    return redirect(url_for("crypto.settings"))


@bp.route("/api/deposits/refresh", methods=["POST"])
def api_deposits_refresh():
    """Refresh today's deposits/withdrawals from Binance and re-sync synth.

    Replaces the old 'Force re-synth' button. Safer + more useful:
      1. Queries Binance for net deposits/withdrawals since today MYT 00:00
      2. Stores the value in crypto_today_deposits_so_far_usd setting
      3. Recomputes synth_day_start (now reflects today's contributions)
      4. Updates crypto_day_start_value_usd so halt threshold + dashboard align

    Without this button, today's deposits/withdrawals only flow into the
    principal at the next MYT-midnight rollover (when past-24h fetch captures
    them into tomorrow's snapshot row). With it, the user can apply same-day
    contributions immediately.

    Idempotent: clicking it twice gives the same result. Safe to re-fire.
    """
    from analysis.crypto_executor import (
        _fetch_net_usdt_deposits_since_today_midnight,
        _compute_synth_day_start_today,
        _set_setting,
    )
    fresh_deposits = _fetch_net_usdt_deposits_since_today_midnight()
    if fresh_deposits is None:
        flash("Couldn't reach Binance to fetch deposits — try again.", "error")
        return redirect(url_for("crypto.settings"))
    _set_setting("crypto_today_deposits_so_far_usd", f"{fresh_deposits:.4f}")
    # Adjust day_start by the net deposit so trading P&L is unaffected.
    # Do NOT recompute from synth formula — keep the live midnight baseline
    # and simply add today's net deposit/withdrawal.
    try:
        existing = float(_setting("crypto_day_start_value_usd") or 0)
        new_day_start = existing + fresh_deposits
        _set_setting("crypto_day_start_value_usd", f"{new_day_start:.4f}")
    except Exception as e:
        flash(f"Deposits refreshed (${fresh_deposits:+.2f}) but day-start update failed: {e}", "warn")
        return redirect(url_for("crypto.settings"))
    flash(f"Deposits refreshed: net ${fresh_deposits:+.2f} today. Day-start adjusted.", "ok")
    return redirect(url_for("crypto.settings"))


@bp.route("/reset-drawdown-peak", methods=["POST"])
def reset_drawdown_peak():
    """Reset today's start-of-day baseline used by daily P&L halts.

    Used after deposits/withdrawals when the user wants halt thresholds rebased
    to current portfolio value. The next account_value read snapshots the new
    baseline. Does NOT clear today's halt flags — those auto-clear at next MYT
    midnight. (To resume mid-day after a halt fire, manually set
    crypto_today_loss_halted/profit_halted to "0".)
    """
    _set_setting("crypto_day_start_value_usd", "0")
    _set_setting("crypto_day_start_date", "")
    flash("Day-start baseline reset — next sync sets new baseline.", "ok")
    return redirect(url_for("crypto.settings"))


@bp.route("/api/server-ip")
def api_server_ip():
    """Lazy-fetch the server's outbound IP — only called when user clicks reveal."""
    ip = _get_public_ip()
    return jsonify({"ip": ip})


@bp.route("/run-loop", methods=["POST"])
def run_loop_now():
    """Manually trigger a crypto trading loop tick."""
    try:
        from webapp.scheduler import trigger_crypto_now
        trigger_crypto_now()
        flash("Crypto loop triggered — check Runs for results.", "ok")
    except Exception as e:
        flash(f"Loop trigger failed: {e}", "err")
    return redirect(url_for("crypto.dashboard"))


@bp.route("/credentials", methods=["POST"])
def save_credentials():
    """Save Binance API key + secret to the Settings table."""
    api_key = (request.form.get("api_key") or "").strip()
    api_secret = (request.form.get("api_secret") or "").strip()
    if not api_key or not api_secret:
        flash("Both API key and secret are required.", "err")
        return redirect(url_for("crypto.settings"))
    _set_setting(KEY_SETTING, api_key)
    _set_setting(SECRET_SETTING, api_secret)
    flash("Binance credentials saved. Click 'Test connection' to verify.", "ok")
    return redirect(url_for("crypto.settings"))


@bp.route("/credentials/clear", methods=["POST"])
def clear_credentials():
    _delete_setting(KEY_SETTING)
    _delete_setting(SECRET_SETTING)
    flash("Binance credentials cleared.", "ok")
    return redirect(url_for("crypto.settings"))


@bp.route("/test-connection", methods=["POST"])
def test_connection():
    """Verify API keys by calling Binance get_account(). Read-only request."""
    key, secret = get_binance_creds()
    if not key or not secret:
        flash("No API keys to test. Save them first.", "err")
        return redirect(url_for("crypto.settings"))
    try:
        from binance.exceptions import BinanceAPIException
    except ImportError:
        flash("python-binance not installed. Run: pip install python-binance", "err")
        return redirect(url_for("crypto.settings"))
    try:
        client = _binance_client(key, secret)
        account = client.get_account()
        non_zero = [b for b in account.get("balances", []) if float(b.get("free", 0)) + float(b.get("locked", 0)) > 0]
        permissions = account.get("permissions", [])
        flash(
            f"✓ Connection OK. Account type: {account.get('accountType', '?')} · "
            f"permissions: {', '.join(permissions) or 'none'} · "
            f"{len(non_zero)} assets with non-zero balance",
            "ok",
        )
    except BinanceAPIException as e:
        flash(f"Binance API error: {e.message} (code {e.code})", "err")
    except Exception as e:
        flash(f"Connection failed: {e}", "err")
    return redirect(url_for("crypto.settings"))


@bp.route("/sync", methods=["POST"])
def sync_balances():
    """Pull current Binance balances and refresh the crypto_holdings table."""
    key, secret = get_binance_creds()
    if not key or not secret:
        flash("No API keys saved. Add them in Settings first.", "err")
        return redirect(url_for("crypto.settings"))
    from analysis.binance_sync import sync_holdings
    result = sync_holdings(key, secret)
    if result["ok"]:
        flash(
            f"✓ Synced {result['real_count']} real holdings "
            f"(+{result['dust_count']} dust filtered) · total ${result['total_value_usd']:.2f}",
            "ok",
        )
    else:
        errs = "; ".join(result["errors"]) or "unknown"
        flash(f"Sync failed: {errs}", "err")
    return redirect(url_for("crypto.dashboard"))


# ── Manual sell-all ───────────────────────────────────────────────────────

@bp.route("/api/sell-all", methods=["POST"])
def api_sell_all():
    """Manually close all open positions at market price."""
    try:
        from analysis.crypto_executor import sell_all_open_positions, _is_paper_mode_setting
        mode_filter = True if _is_paper_mode_setting() else False
        n = sell_all_open_positions("manual liquidation", mode_filter=mode_filter)
        return jsonify({"ok": True, "closed": n})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Ghost portfolio ────────────────────────────────────────────────────────

@bp.route("/simulation")
def simulation():
    """Ghost portfolio page — live 'no-halt' simulation view."""
    from analysis.crypto_ghost import ghost_summary
    from datetime import timezone, timedelta
    MYT = timezone(timedelta(hours=8))
    summary = ghost_summary()
    # Convert UTC trade times to MYT for display
    if summary.get("trades"):
        from datetime import datetime
        for t in summary["trades"]:
            try:
                utc = datetime.fromisoformat(t["time_utc"])
                t["time_myt"] = utc.replace(tzinfo=timezone.utc).astimezone(MYT).strftime("%H:%M")
            except Exception:
                t["time_myt"] = t.get("time_utc", "")[:16]
    return render_template("crypto_simulation.html", ghost=summary)


@bp.route("/api/ghost/summary")
def api_ghost_summary():
    """JSON ghost portfolio summary for the dashboard card (polled every 30s)."""
    try:
        from analysis.crypto_ghost import ghost_summary
        return jsonify(ghost_summary())
    except Exception as e:
        return jsonify({"enabled": False, "error": str(e)})
