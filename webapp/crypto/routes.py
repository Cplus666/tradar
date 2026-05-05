"""Crypto workspace blueprint. URL prefix: /tradar."""

from __future__ import annotations

import json
from datetime import datetime

import pandas as pd
from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for

from webapp.models import CryptoCoin, CryptoHolding, CryptoRun, CryptoTrade, Setting, db

bp = Blueprint("crypto", __name__, url_prefix="/tradar", template_folder="templates")

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


# ---- Jinja template filters (registered app-wide via app_template_filter) ----

@bp.app_template_filter("dt")
def fmt_dt(value):
    """UTC datetime → Malaysia Time string. All DB times are stored as UTC."""
    if not value:
        return ""
    from datetime import timezone, timedelta
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    myt = value.astimezone(timezone(timedelta(hours=8)))
    return myt.strftime("%Y-%m-%d %H:%M MYT")


@bp.app_template_filter("money")
def fmt_money(value):
    if value is None:
        return "—"
    return f"${value:,.2f}"


@bp.app_template_filter("pct")
def fmt_pct(value):
    if value is None:
        return "—"
    return f"{value:+.2f}%"


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


def _is_paper_mode() -> bool:
    """True if the bot is currently in paper-trading mode.

    Single source of truth — every view that filters CryptoTrade by is_paper
    should call this and pass the result, so mode-switching is consistent
    across dashboard / journal / charts / coin pages / notifications.
    """
    return _setting("crypto_trading_mode", "paper") == "paper"


@bp.context_processor
def _inject_mode_state():
    """Make current mode + leftover other-mode position count available to
    every template — used by the navbar to surface a "you also have N
    positions in the other mode" warning so users don't lose track when
    toggling between paper and live.
    """
    try:
        from analysis.crypto_executor import _open_positions
        is_paper = _is_paper_mode()
        other_count = len(_open_positions(is_paper=(not is_paper)))
    except Exception:
        is_paper, other_count = True, 0
    return {
        "current_mode": "paper" if is_paper else "live",
        "leftover_other_count": other_count,
        "leftover_other_mode": "live" if is_paper else "paper",
    }


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

    Mode-aware:
      - LIVE  → real Binance USDT balance + real position values
      - PAPER → virtual wallet (starting_capital + cumulative realized + unrealized).
                Public Binance ticker endpoint used for prices (no API key required).

    Today = since 00:00 MYT today. Pass prefetched_tickers to avoid duplicate fetch.
    """
    from datetime import timezone, timedelta
    from analysis.crypto_executor import _open_positions, parse_entry_notes

    is_paper_mode = (_setting("crypto_trading_mode", "paper") == "paper")

    out = {
        "account_value": None, "usdt_free": None,
        "today_realized": 0.0, "today_unrealized": 0.0, "today_total": 0.0,
        "today_wins": 0, "today_losses": 0, "today_win_rate": None,
        "open_count": 0, "open_value": 0.0, "starting_capital": 200.29,
        "all_time_pnl": 0.0,
        "mode": "paper" if is_paper_mode else "live",
    }

    # Optional Binance client. LIVE needs it for the real USDT balance;
    # PAPER only needs it for ticker prices (and the public endpoint works
    # without keys, so paper users with no keys still get a working dashboard).
    client = None
    acct = None
    try:
        key, secret = get_binance_creds()
        if key and secret:
            client = _binance_client(key, secret)
    except Exception:
        client = None

    # Fee rate — used to net both today's realized P&L and unrealized P&L
    # so dashboard numbers match journal + daily ROI chart conventions.
    try:
        fee_rate = float(_setting("crypto_fee_rate_per_side", "0.001"))
    except (TypeError, ValueError):
        fee_rate = 0.001

    if not is_paper_mode:
        # LIVE — real balance is required
        if client is None:
            return out  # no keys → can't show live balance
        try:
            acct = client.get_account()
            usdt = next((b for b in acct["balances"] if b["asset"] == "USDT"), None)
            out["usdt_free"] = float(usdt["free"]) if usdt else 0.0
        except Exception:
            return out

    # Open positions of THIS MODE
    open_pos = _open_positions(is_paper=is_paper_mode)
    open_value = 0.0
    today_unrealized = 0.0
    myt_now = datetime.utcnow().replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=8)))
    today_start_utc = (myt_now.replace(hour=0, minute=0, second=0, microsecond=0)).astimezone(timezone.utc).replace(tzinfo=None)

    # Tickers — public endpoint, no auth needed. Use authenticated client if we
    # have one (cheaper API weight) but fall back to anonymous for paper users.
    if prefetched_tickers is not None:
        all_tickers = prefetched_tickers
    else:
        try:
            tickers_client = client or _binance_client()
            all_tickers = {t["symbol"]: float(t["price"]) for t in tickers_client.get_all_tickers()}
        except Exception:
            all_tickers = {}

    # Sync real holdings table — LIVE only (paper has no real holdings to sync)
    if not is_paper_mode and acct is not None:
        try:
            from analysis.binance_sync import persist_holdings_from_data
            persist_holdings_from_data(acct.get("balances", []), all_tickers)
        except Exception:
            pass  # never let this break the dashboard

    open_notional = 0.0  # used for paper "free virtual cash" calc
    all_unrealized = 0.0  # unrealized across all open positions, NET of fees
    for p in open_pos:
        cur = all_tickers.get(p.symbol)
        # Use REMAINING qty (handles positions that did partial profit-take).
        # _open_positions attaches _remaining_qty; fall back to original p.qty
        # for backward compat with positions that were tracked before the fix.
        qty = float(getattr(p, "_remaining_qty", p.qty))
        notional = float(p.price) * qty
        open_notional += notional
        if cur is None:
            value = notional
            unr_net = 0.0
        else:
            value = cur * qty
            sell_value = value
            buy_value = notional
            gross = sell_value - buy_value
            fee = (buy_value + sell_value) * fee_rate    # round-trip estimate
            unr_net = gross - fee                         # NET unrealized
        open_value += value
        all_unrealized += unr_net
        if p.executed_at >= today_start_utc:
            today_unrealized += unr_net
    out["open_count"] = len(open_pos)
    out["open_value"] = open_value

    # Today's realized P&L: pair BUY-SELL of THIS MODE where SELL closed today
    from webapp.models import CryptoTrade
    trades = (CryptoTrade.query
              .filter(CryptoTrade.is_paper == is_paper_mode)
              .order_by(CryptoTrade.executed_at).all())
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
    # Each SELL may consume only PART of the current BUY (partial profit-take)
    # — we attribute pnl based on the consumed qty, not the full buy.qty.
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
                    buy_value = float(buy.price) * consumed
                    sell_value = float(t.price) * consumed
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

    # Account value & free-cash:
    #   LIVE  → real free USDT + open positions value (already set above)
    #   PAPER → virtual wallet: starting + cumulative realized + current unrealized
    if is_paper_mode:
        starting = 0.0
        try:
            starting = float(_setting("crypto_starting_capital_usd", "0") or 0)
        except (TypeError, ValueError):
            starting = 0.0
        if starting <= 0:
            starting = 10000.0  # paper sandbox default — generous so % moves
                                # on small trades stay readable, and there's
                                # room for max_concurrent positions at any
                                # reasonable max_position_usd setting.
        out["starting_capital"] = starting
        out["account_value"] = starting + all_realized + all_unrealized
        # Virtual free cash = starting + realized P&L − notional currently in positions
        out["usdt_free"] = max(0.0, starting + all_realized - open_notional)
    else:
        out["account_value"] = (out["usdt_free"] or 0.0) + open_value

    # Ratchet day-start + auto-halt on daily drawdown (works for both modes).
    # Done AFTER account_value is computed so the snapshot is meaningful.
    try:
        from analysis.crypto_executor import update_day_start_and_check_halt
        risk = update_day_start_and_check_halt(out["account_value"])
        out["day_start_value"] = risk["day_start"]
        out["drawdown_pct"] = risk["drawdown_pct"]
    except Exception:
        out["day_start_value"] = 0.0
        out["drawdown_pct"] = 0.0

    # Today's P&L — intuitive interpretation that matches what's visible on
    # the position cards:
    #   today_realized   = trades closed today (sum of net P&L)
    #   today_unrealized = current unrealized on ALL open positions (vs entry)
    #   today_total      = realized + currently_unrealized
    # This decomposes cleanly: the unrealized number always equals the sum
    # of P&L shown on the open-position cards. Trade-off: "today" is loose
    # for the unrealized portion since it includes positions held over from
    # earlier days. The day_start_value snapshot is still tracked separately
    # in the system-health panel for users who want pure account-delta.
    out["today_realized"] = today_realized
    out["today_unrealized"] = all_unrealized       # sum across ALL open positions
    out["today_total"] = today_realized + all_unrealized
    out["today_wins"] = today_wins
    out["today_losses"] = today_losses
    if today_wins + today_losses > 0:
        out["today_win_rate"] = today_wins / (today_wins + today_losses) * 100
    out["all_time_pnl"] = out["account_value"] - out["starting_capital"] if out["account_value"] else 0
    # Today's % return — base on the day's start (not start-of-day-derived guess)
    if day_start > 0:
        out["today_total_pct"] = out["today_total"] / day_start * 100
    else:
        out["today_total_pct"] = None
    out["all_time_pct"] = (out["all_time_pnl"] / out["starting_capital"] * 100) if out["starting_capital"] else None
    return out


def _open_position_cards(tickers: dict | None = None) -> list:
    """Position cards. If tickers=None, returns DB-only data with current=None.

    Mode-aware: shows paper positions in PAPER mode, live positions in LIVE mode.
    Caller passes tickers dict (symbol → price) for the live version.

    P&L is NET of estimated round-trip fees (matches journal + daily ROI chart).
    """
    from analysis.crypto_executor import _open_positions, parse_entry_notes
    is_paper_mode = (_setting("crypto_trading_mode", "paper") == "paper")
    try:
        fee_rate = float(_setting("crypto_fee_rate_per_side", "0.001"))
    except (TypeError, ValueError):
        fee_rate = 0.001
    out = []
    for p in _open_positions(is_paper=is_paper_mode):
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
        # Use REMAINING qty (post-partial-sells); falls back to original buy qty.
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
        })
    out.sort(key=lambda x: -x["pnl_pct"])  # winners first
    return out


def _strategy_breakdown() -> list:
    """Per-strategy stats: trades, win rate, avg P&L, total. Mode-aware."""
    from webapp.models import CryptoTrade
    trades = (CryptoTrade.query
              .filter(CryptoTrade.is_paper == _is_paper_mode())
              .order_by(CryptoTrade.executed_at).all())
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
                buys.append(t)
            elif t.side == "SELL" and buys:
                buy = buys.pop(0)
                pnl_pct = (float(t.price) - float(buy.price)) / float(buy.price) * 100
                pnl_usd = (float(t.price) - float(buy.price)) * float(buy.qty)
                strat = buy.strategy or "unknown"
                d = by_strat.setdefault(strat, {"wins": 0, "losses": 0, "win_pcts": [], "loss_pcts": [], "net_usd": 0.0})
                d["net_usd"] += pnl_usd
                if pnl_pct > 0:
                    d["wins"] += 1; d["win_pcts"].append(pnl_pct)
                else:
                    d["losses"] += 1; d["loss_pcts"].append(pnl_pct)
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

    # Pull ALL trades of the current mode — buys may predate the window.
    # Manual liquidations are excluded so they don't pollute strategy P&L.
    trades = (CryptoTrade.query
              .filter(CryptoTrade.is_paper == _is_paper_mode())
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
    for _sym, ts in by_sym.items():
        buys = []
        for t in ts:
            if t.side == "BUY":
                buys.append(t)
            elif t.side == "SELL" and buys:
                buy = buys.pop(0)
                buy_price = float(buy.price)
                qty = float(buy.qty)
                capital = buy_price * qty                   # $ this pair tied up
                gross_pnl = (float(t.price) - buy_price) * qty
                fee = (capital + float(t.price) * qty) * fee_rate  # buy + sell sides
                pnl = gross_pnl - fee                        # NET P&L (what you keep)
                pnl_pct = ((pnl / capital) * 100) if capital > 0 else 0.0
                day_myt = t.executed_at.replace(tzinfo=timezone.utc).astimezone(MYT).date()
                if day_myt < cutoff_myt:
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

    # Densify: emit zero-bars for days with no closes so the chart doesn't lie
    # by visually compressing dead time.
    #   return_pct = day's pnl_usd / capital_deployed * 100 — REAL return on
    #                the capital that actually worked that day. Used as bar
    #                height so 5 trades at ~4% each show ~4%, not ~20%.
    #   avg_pct    = mean per-trade % (used in headline as "avg trade quality").
    points = []
    for i in range(days - 1, -1, -1):
        d = today_myt - timedelta(days=i)
        v = days_data.get(d.isoformat(), {
            "pnl": 0.0, "capital": 0.0, "pct_sum": 0.0,
            "wins": 0, "losses": 0, "fees": 0.0,
        })
        n = v["wins"] + v["losses"]
        avg_pct = (v["pct_sum"] / n) if n > 0 else 0.0
        return_pct = (v["pnl"] / v["capital"] * 100) if v["capital"] > 0 else 0.0
        points.append({
            "date": d.isoformat(),
            "pnl": round(v["pnl"], 4),       # NET (after fees)
            "capital": round(v["capital"], 2),
            "fees": round(v["fees"], 4),
            "return_pct": round(return_pct, 3),
            "avg_pct": round(avg_pct, 3),
            "wins": v["wins"],
            "losses": v["losses"],
        })
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
                buys.append(t)
            elif t.side == "SELL" and buys:
                buy = buys.pop(0)
                # Net P&L (after both buy + sell fees) — matches dashboard / journal
                buy_value = float(buy.price) * float(buy.qty)
                sell_value = float(t.price) * float(buy.qty)
                gross = sell_value - buy_value
                fee = (buy_value + sell_value) * fee_rate
                pnl_usd = gross - fee
                pnl_pct = (pnl_usd / buy_value * 100) if buy_value > 0 else 0.0
                closed_trades.append((sym, float(buy.price), float(t.price), pnl_pct, pnl_usd, buy.strategy))
        open_count += len(buys)

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
    """Manually sell an open position by trade ID. Returns JSON result.
    Mode-aware: paper mode looks up paper positions; live looks up live ones.
    """
    from analysis.crypto_executor import _open_positions, execute_sell
    open_pos = {p.id: p for p in _open_positions(is_paper=_is_paper_mode())}
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
    """Manually trigger a partial profit-take on an open position.

    Body / query: `fraction` (float, 0 < f < 1) — defaults to crypto_partial_take_fraction
    setting. Same logic as the auto-fired partial: sells `fraction × qty`,
    moves stop to entry × (1 + crypto_breakeven_buffer_pct/100), marks
    partial_done=1 so the auto-trigger won't double-fire.
    """
    from analysis.crypto_executor import _open_positions, execute_partial_sell, parse_entry_notes

    # Parse fraction (form, JSON body, or query string — accept any)
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

    open_pos = {p.id: p for p in _open_positions(is_paper=_is_paper_mode())}
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

    Account values that need Binance (account_value, usdt_free, today_unrealized)
    return None — JS fetches /api/dashboard separately to fill them in.
    """
    cards = _open_position_cards(tickers=None)  # current_price=None on each
    # Build account summary from DB only (skip Binance calls)
    from datetime import timezone as _tz, timedelta as _td
    myt_now = datetime.utcnow().replace(tzinfo=_tz.utc).astimezone(_tz(_td(hours=8)))
    today_start_utc = (myt_now.replace(hour=0, minute=0, second=0, microsecond=0)).astimezone(_tz.utc).replace(tzinfo=None)
    trades = (CryptoTrade.query
              .filter(CryptoTrade.is_paper == _is_paper_mode())
              .order_by(CryptoTrade.executed_at).all())
    by_sym: dict = {}
    for t in trades:
        if t.strategy == "manual_liquidation":
            continue
        by_sym.setdefault(t.symbol, []).append(t)
    today_realized = 0.0
    today_wins = today_losses = 0
    for sym, ts in by_sym.items():
        buys = []
        for t in ts:
            if t.side == "BUY":
                buys.append(t)
            elif t.side == "SELL" and buys:
                buy = buys.pop(0)
                pnl = (float(t.price) - float(buy.price)) * float(buy.qty)
                if t.executed_at >= today_start_utc:
                    today_realized += pnl
                    if pnl > 0: today_wins += 1
                    else: today_losses += 1
    account = {
        "account_value": None, "usdt_free": None,
        "today_realized": today_realized,
        "today_unrealized": None, "today_total": None,
        "today_total_pct": None,
        "today_wins": today_wins, "today_losses": today_losses,
        "today_win_rate": (today_wins / (today_wins + today_losses) * 100) if (today_wins + today_losses) else None,
        "open_count": len(cards), "open_value": 0.0,
        "starting_capital": 200.29, "all_time_pnl": 0.0, "all_time_pct": None,
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
        # Wedged socket / DNS hiccup / Binance outage / rate limit — handled below.
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

    # Binance unreachable — serve last good payload if we have one.
    cached = _DASHBOARD_CACHE.get("payload")
    if cached is not None:
        stale_payload = dict(cached)
        stale_payload["static"] = False
        stale_payload["stale"] = True
        stale_payload["stale_since"] = _DASHBOARD_CACHE.get("ts")
        return jsonify(stale_payload)

    # No cache yet (first dashboard load failed) — fall back to DB-only render.
    account = _account_summary(prefetched_tickers={})
    cards = _open_position_cards(tickers={})
    return jsonify({"account": account, "position_cards": cards,
                    "static": False, "stale": True, "stale_since": None})


@bp.route("/api/recent-trades")
def api_recent_trades():
    """Returns trades since `since_id` query param. For browser notifications polling."""
    from flask import request as flask_request
    since_id = int(flask_request.args.get("since_id", 0))
    trades = (
        CryptoTrade.query
        .filter(CryptoTrade.id > since_id, CryptoTrade.is_paper == _is_paper_mode())
        .order_by(CryptoTrade.id)
        .limit(20).all()
    )
    return jsonify([
        {
            "id": t.id, "symbol": t.symbol, "side": t.side,
            "price": float(t.price), "qty": float(t.qty),
            "quote": float(t.quote_amount or 0),
            "strategy": t.strategy or "?",
            "ts": t.executed_at.isoformat(),
        }
        for t in trades
    ])


_STRUCTURED_NOTES_PREFIXES = (
    "stop=$", "target=$", "max_hold=", "exit=", "original_stop=$",
    "PARTIAL DONE", "PAPER ENTRY", "LIVE ENTRY", "PAPER EXIT", "LIVE EXIT",
)


def _extract_entry_reason(notes: str | None, strategy_fallback: str | None = None) -> str:
    """Pull the human-readable entry reason out of a position's notes string.

    Notes are stored as `· · ·`-joined tokens, mixing structured fields
    (stop=$X, target=$Y, partial_done=1, original_stop=$Z, etc.) with one
    free-text reason like "fresh breakout +4.2%, vol=1.6x, RSI=67". The
    naive `notes.split("·")[-1]` grabs whichever token is last — after a
    partial-take rewrite that's `original_stop=$X`, NOT the entry reason.

    This walks the tokens and returns the LAST one that doesn't match a
    structured prefix. If nothing free-text is found, falls back to the
    strategy name (so the journal still shows something meaningful).
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

    Mode-aware: shows paper trades in PAPER mode, live trades in LIVE mode.
    Heavy: makes a Binance API call for live prices on open positions.
    """
    from analysis.crypto_executor import parse_entry_notes
    # Fee subtraction — same as the daily-ROI chart so journal numbers match.
    # Default 0.1% per side (Binance Spot taker); user-overridable via setting.
    try:
        fee_rate = float(_setting("crypto_fee_rate_per_side", "0.001"))
    except (TypeError, ValueError):
        fee_rate = 0.001

    trades = (CryptoTrade.query
              .filter(CryptoTrade.is_paper == _is_paper_mode())
              .order_by(CryptoTrade.executed_at).all())
    by_sym: dict = {}
    for t in trades:
        if t.strategy == "manual_liquidation":
            continue
        by_sym.setdefault(t.symbol, []).append(t)

    closed_entries = []
    open_entries = []
    for sym, ts in by_sym.items():
        # FIFO with REMAINING-QTY tracking: a SELL may consume only PART of the
        # current BUY (partial profit-take). The unconsumed remainder of the BUY
        # stays "open" with a tightened stop. Each partial creates its OWN
        # closed-entry row with its own qty.
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
                    # Create a closed-entry row for THIS chunk (consumed qty)
                    meta = parse_entry_notes(buy.notes)
                    buy_value = float(buy.price) * consumed
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
                    # Each closed-entry row should show the stop that was
                    # ACTIVE during its segment of the position:
                    #   - PARTIAL sell event → original_stop (active until partial fired)
                    #   - RUNNER sell after partial → current stop (active after move)
                    #   - Regular full sell → current stop (no movement)
                    is_partial_event = "partial profit take" in (exit_reason or "")
                    has_partial_history = meta["original_stop"] is not None
                    if is_partial_event and has_partial_history:
                        seg_stop = meta["original_stop"]     # what was active up to partial
                        seg_moved_to = meta["stop"]           # → moved to this after partial
                        seg_prior = None
                    elif has_partial_history:
                        seg_stop = meta["stop"]               # tightened stop on runner
                        seg_moved_to = None
                        seg_prior = meta["original_stop"]     # was this before partial
                    else:
                        seg_stop = meta["stop"]
                        seg_moved_to = None
                        seg_prior = None
                    closed_entries.append({
                        "is_open": False,
                        "is_partial": is_partial_fill,    # mark for UI ("partial" badge)
                        "symbol": sym, "strategy": buy.strategy,
                        "entry_at": buy.executed_at.isoformat(),
                        "exit_at":  t.executed_at.isoformat(),
                        "entry_price": float(buy.price), "exit_price": sell_price,
                        "qty": consumed, "notional": buy_value,
                        "entry_value": buy_value, "exit_value": sell_value,
                        "stop": seg_stop, "target": meta["target"],
                        "stop_moved_to": seg_moved_to,    # set on partial-sell card → "→ moved to $X"
                        "stop_was": seg_prior,            # set on runner card → "(was $X before partial)"
                        "original_stop": meta["original_stop"],   # raw value for any future use
                        "pnl_pct": pnl_pct, "pnl_usd": pnl_usd,
                        "fee_usd": round(fee_usd, 4),
                        "gross_pnl_usd": round(gross_pnl_usd, 4),
                        "held_h": held_h,
                        "entry_reason": _extract_entry_reason(buy.notes, buy.strategy),
                        "exit_reason": exit_reason,
                    })
                    if buys[0][1] <= 1e-9:
                        buys.pop(0)
        # Remaining buys (with positive remaining_qty) are still-open positions.
        # Apply dust filter (matches _open_positions): residuals worth < $1 are
        # rounding remnants from prior sells and should NOT show as journal entries.
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
                "is_partial": remaining_qty < float(buy.qty),  # runner = partial sold
                "symbol": sym, "strategy": buy.strategy,
                "entry_at": buy.executed_at.isoformat(), "exit_at": None,
                "entry_price": float(buy.price), "exit_price": None,
                "qty": remaining_qty, "notional": entry_value,
                "entry_value": entry_value, "exit_value": None,
                "stop": meta["stop"], "target": meta["target"],
                "original_stop": meta["original_stop"],   # for journal display
                "pnl_pct": None, "pnl_usd": None, "held_h": held_h,
                "entry_reason": (buy.notes or "").split("·")[-1].strip() if buy.notes else "",
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
        # entry_at/exit_at are naive UTC ISO strings written by .isoformat()
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
        "unrealized_pnl": 0,  # not yet known — will be updated after price fetch
        "open_symbols": open_symbols,  # tells JS which to fetch live prices for
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
    from analysis.crypto_executor import _open_positions
    is_paper = _is_paper_mode()

    if is_paper:
        # Paper mode has no real wallet to sync — what the bot "owns" IS the
        # set of open paper positions. Synthesize rows in CryptoHolding shape
        # so the template doesn't have to care about mode.
        # NO live-price fetch here — JS lazy-loads via /api/journal/prices and
        # updates the DOM in place. Initial render uses ENTRY price as
        # placeholder so totals render immediately instead of blocking on a
        # ~1-3s Binance call.
        from types import SimpleNamespace
        now = datetime.utcnow()
        rows = []
        for p in _open_positions(is_paper=True):
            asset = p.symbol[:-4] if p.symbol.endswith("USDT") else p.symbol
            # _remaining_qty reflects post-partial-sell qty; falls back to p.qty.
            qty = float(getattr(p, "_remaining_qty", p.qty))
            entry_price = float(p.price)
            rows.append(SimpleNamespace(
                asset=asset, free=qty, locked=0.0,
                last_price_usd=entry_price,        # placeholder; JS will update
                value_usd=entry_price * qty,       # placeholder; JS will update
                fetched_at=now,
                _is_paper=True,                    # template can flag "live price loading"
                _symbol=p.symbol,                  # full pair for JS price lookup
            ))
        rows.sort(key=lambda r: r.value_usd or 0, reverse=True)
    else:
        rows = CryptoHolding.query.order_by(CryptoHolding.value_usd.desc().nullslast()).all()
    total = sum((r.value_usd or 0) for r in rows)

    # Map asset (e.g. "DOGE") → open position trade ID, so the page can show a Sell button
    from analysis.crypto_executor import parse_entry_notes
    open_pos_by_asset = {}
    for p in _open_positions(is_paper=is_paper):
        # Strip the trading-quote suffix to get the base asset (DOGEUSDT → DOGE)
        if p.symbol.endswith("USDT"):
            asset = p.symbol[:-4]
            meta = parse_entry_notes(p.notes)
            open_pos_by_asset[asset] = {
                "trade_id": p.id,
                "symbol": p.symbol,
                "entry_price": float(p.price),
                "strategy": p.strategy,
                "partial_done": meta.get("partial_done", False),  # disable Partial btn if true
            }
    return render_template(
        "crypto_holdings.html",
        rows=rows, total=total,
        is_paper_mode=is_paper,
        has_keys=_has_binance_keys(),
        open_pos_by_asset=open_pos_by_asset,
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
    open_pos = next((p for p in _open_positions(is_paper=_is_paper_mode()) if p.symbol == symbol), None)
    has_position = open_pos is not None
    position_info = None
    if open_pos:
        # _open_positions attaches _remaining_qty (post-partial-sells); use it so
        # the coin page matches the journal after a partial profit-take.
        rem_qty = float(getattr(open_pos, "_remaining_qty", open_pos.qty))
        position_info = {
            "entry_price": float(open_pos.price),
            "qty": rem_qty,
            "value_now": rem_qty * last_close,
            "pnl": (last_close - float(open_pos.price)) * rem_qty,
            "pnl_pct": (last_close - float(open_pos.price)) / float(open_pos.price) * 100,
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
        .filter(CryptoTrade.symbol == symbol, CryptoTrade.is_paper == _is_paper_mode())
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
            "ts_iso": t.executed_at.isoformat(),
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
        "crypto_drawdown_halt_pct", "crypto_drawdown_halt_enabled",
        "crypto_min_balance_usd",
        "crypto_loop_interval_min", "crypto_fast_check_interval_sec",
        # Partial profit-take controls
        "crypto_partial_take_enabled",
        "crypto_partial_take_trigger_pct",
        "crypto_partial_take_fraction",
        "crypto_breakeven_buffer_pct",
    )
    if request.method == "POST":
        # Checkboxes only POST when checked. The settings form sends a hidden
        # marker so we can detect submit-but-unchecked and coerce to "off"
        # for any tickbox setting.
        if request.form.get("settings_form_present") == "1":
            if "crypto_drawdown_halt_enabled" not in request.form:
                _set_setting("crypto_drawdown_halt_enabled", "off")
            if "crypto_partial_take_enabled" not in request.form:
                _set_setting("crypto_partial_take_enabled", "off")
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
    # Drawdown halt: read the one-shot notice and CLEAR it (so it shows once).
    # Stored as "<isoformat>|<message>" by update_peak_and_check_halt.
    drawdown_notice_raw = _setting("crypto_drawdown_notice", "")
    drawdown_notice = None
    if drawdown_notice_raw:
        if "|" in drawdown_notice_raw:
            ts, msg = drawdown_notice_raw.split("|", 1)
        else:
            ts, msg = "", drawdown_notice_raw
        drawdown_notice = {"ts": ts, "msg": msg}
        _set_setting("crypto_drawdown_notice", "")  # one-shot — clear after read

    # System health snapshot — single panel so user doesn't have to grep logs.
    health = _system_health()

    # Per-strategy toggles — current disabled set, plus the ordered name list
    # the template renders.
    disabled_csv = _setting("crypto_disabled_strategies", "")
    disabled_strategies = {s.strip() for s in disabled_csv.split(",") if s.strip()}

    return render_template(
        "crypto_settings.html",
        kill_switch=_setting("crypto_kill_switch", "off"),
        trading_mode=_setting("crypto_trading_mode", "paper"),
        max_position_usd=_setting("crypto_max_position_usd", "50"),
        max_concurrent=_setting("crypto_max_concurrent", "2"),
        drawdown_halt_pct=_setting("crypto_drawdown_halt_pct", "15"),
        drawdown_halt_enabled=_setting("crypto_drawdown_halt_enabled", "on"),
        min_balance_usd=_setting("crypto_min_balance_usd", "100"),
        loop_interval_min=_setting("crypto_loop_interval_min", "15"),
        fast_check_interval_sec=_setting("crypto_fast_check_interval_sec", "60"),
        partial_take_enabled=_setting("crypto_partial_take_enabled", "on"),
        partial_take_trigger_pct=_setting("crypto_partial_take_trigger_pct", "4.0"),
        partial_take_fraction=_setting("crypto_partial_take_fraction", "0.5"),
        breakeven_buffer_pct=_setting("crypto_breakeven_buffer_pct", "1.0"),
        day_start_value_usd=_setting("crypto_day_start_value_usd", "0"),
        day_start_date=_setting("crypto_day_start_date", ""),
        drawdown_notice=drawdown_notice,
        health=health,
        strategy_names=STRATEGY_NAMES,
        disabled_strategies=disabled_strategies,
        has_keys=_has_binance_keys(),
        api_key_masked=_mask(key),
        api_secret_masked=_mask(secret),
    )


def _system_health() -> dict:
    """Snapshot of running-system state for the Settings page health panel.

    Aggregates: scheduler next fire, last sync, last error, current peak,
    drawdown vs peak. All best-effort — missing data renders as None.
    """
    from datetime import timezone, timedelta
    MYT = timezone(timedelta(hours=8))

    def _to_myt(dt):
        if dt is None:
            return None
        if dt.tzinfo is None:
            # Scheduler's get_next_run returns tz-aware; CryptoRun.started_at is naive UTC
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
        "halt_enabled": (_setting("crypto_drawdown_halt_enabled", "on") == "on"),
    }

    # Scheduler next fires
    try:
        from webapp.scheduler import get_next_run
        health["next_loop_fire"] = _to_myt(get_next_run("crypto_loop"))
        health["next_fast_fire"] = _to_myt(get_next_run("fast_exit_check"))
    except Exception:
        pass

    # Last sync run (kind='sync')
    try:
        last_sync = (CryptoRun.query.filter_by(kind="sync", status="ok")
                     .order_by(CryptoRun.id.desc()).first())
        if last_sync:
            health["last_sync_at"] = _to_myt(last_sync.started_at)
            health["last_sync_summary"] = (last_sync.summary or "")[:120]
    except Exception:
        pass

    # Most recent error (any kind)
    try:
        last_err = (CryptoRun.query.filter_by(status="error")
                    .order_by(CryptoRun.id.desc()).first())
        if last_err:
            health["last_error_at"] = _to_myt(last_err.started_at)
            health["last_error_summary"] = (last_err.error or last_err.summary or "")[:200]
    except Exception:
        pass

    # Today's start-of-day value + current daily drawdown
    try:
        day_start = float(_setting("crypto_day_start_value_usd", "0") or 0)
        if day_start > 0:
            health["day_start_value"] = day_start
            # Latest account value from the most recent sync summary.
            # Format: "synced N real + M dust assets, total value $XXX.XX"
            import re
            if last_sync and last_sync.summary:
                m = re.search(r"total value \$(\d+\.\d+)", last_sync.summary)
                if m:
                    current_val = float(m.group(1))
                    health["drawdown_pct"] = (day_start - current_val) / day_start * 100
    except Exception:
        pass

    return health


@bp.route("/reset-drawdown-peak", methods=["POST"])
def reset_drawdown_peak():
    """Reset today's start-of-day baseline so the daily drawdown halt clears.

    Called after the user has acknowledged a halt and wants to resume trading
    mid-day. Clears the day-start snapshot and the one-shot notice; the next
    account_value read will reset the snapshot to the current value, so
    drawdown calculation effectively restarts from now. Does NOT auto-flip the
    kill switch off — user toggles it deliberately so there's a beat to think.
    """
    _set_setting("crypto_day_start_value_usd", "0")
    _set_setting("crypto_day_start_date", "")
    _set_setting("crypto_drawdown_notice", "")
    flash("Day-start baseline reset — drawdown halt cleared. Toggle the kill switch off to resume trading.", "ok")
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
