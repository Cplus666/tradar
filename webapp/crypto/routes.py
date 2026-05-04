"""Crypto workspace blueprint. URL prefix: /crypto."""

from __future__ import annotations

import json
from datetime import datetime

import pandas as pd
from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for

from webapp.models import CryptoCoin, CryptoHolding, CryptoRun, CryptoTrade, Setting, db

bp = Blueprint("crypto", __name__, url_prefix="/crypto", template_folder="templates")

KEY_SETTING = "binance_api_key"
SECRET_SETTING = "binance_api_secret"


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

    Today = since 00:00 MYT today. Pass prefetched_tickers to avoid duplicate fetch.
    """
    from datetime import timezone, timedelta
    from analysis.crypto_executor import _open_positions, parse_entry_notes
    from binance.client import Client

    out = {
        "account_value": None, "usdt_free": None,
        "today_realized": 0.0, "today_unrealized": 0.0, "today_total": 0.0,
        "today_wins": 0, "today_losses": 0, "today_win_rate": None,
        "open_count": 0, "open_value": 0.0, "starting_capital": 200.29,
        "all_time_pnl": 0.0,
    }

    # Live USDT balance + ticker prices for open positions
    try:
        key, secret = get_binance_creds()
        if not key or not secret:
            return out
        client = Client(key, secret)
        acct = client.get_account()
        usdt = next((b for b in acct["balances"] if b["asset"] == "USDT"), None)
        out["usdt_free"] = float(usdt["free"]) if usdt else 0.0
    except Exception:
        return out

    open_pos = _open_positions(is_paper=False)
    open_value = 0.0
    today_unrealized = 0.0
    myt_now = datetime.utcnow().replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=8)))
    today_start_utc = (myt_now.replace(hour=0, minute=0, second=0, microsecond=0)).astimezone(timezone.utc).replace(tzinfo=None)

    # Use prefetched tickers if caller provided (avoids duplicate Binance call)
    if prefetched_tickers is not None:
        all_tickers = prefetched_tickers
    else:
        try:
            all_tickers = {t["symbol"]: float(t["price"]) for t in client.get_all_tickers()}
        except Exception:
            all_tickers = {}

    # Sync the holdings table to whatever we just fetched live — keeps /crypto/holdings
    # in lock-step with the dashboard. Cheap: no extra API calls, just DB writes.
    try:
        from analysis.binance_sync import persist_holdings_from_data
        persist_holdings_from_data(acct.get("balances", []), all_tickers)
    except Exception:
        pass  # never let this break the dashboard

    for p in open_pos:
        cur = all_tickers.get(p.symbol)
        if cur is None:
            continue
        value = cur * float(p.qty)
        open_value += value
        if p.executed_at >= today_start_utc:
            today_unrealized += (cur - float(p.price)) * float(p.qty)
    out["open_count"] = len(open_pos)
    out["open_value"] = open_value
    out["account_value"] = out["usdt_free"] + open_value

    # Today's realized P&L: pair BUY-SELL where SELL closed today
    from webapp.models import CryptoTrade
    trades = CryptoTrade.query.filter(CryptoTrade.is_paper == False).order_by(CryptoTrade.executed_at).all()
    by_sym: dict = {}
    for t in trades:
        if t.strategy == "manual_liquidation":
            continue
        by_sym.setdefault(t.symbol, []).append(t)
    today_realized = 0.0
    today_wins = today_losses = 0
    all_realized = 0.0
    for sym, ts in by_sym.items():
        buys = []
        for t in ts:
            if t.side == "BUY":
                buys.append(t)
            elif t.side == "SELL" and buys:
                buy = buys.pop(0)
                pnl = (float(t.price) - float(buy.price)) * float(buy.qty)
                all_realized += pnl
                if t.executed_at >= today_start_utc:
                    today_realized += pnl
                    if pnl > 0: today_wins += 1
                    else: today_losses += 1
    out["today_realized"] = today_realized
    out["today_unrealized"] = today_unrealized
    out["today_total"] = today_realized + today_unrealized
    out["today_wins"] = today_wins
    out["today_losses"] = today_losses
    if today_wins + today_losses > 0:
        out["today_win_rate"] = today_wins / (today_wins + today_losses) * 100
    out["all_time_pnl"] = out["account_value"] - out["starting_capital"] if out["account_value"] else 0
    # Percentages — today's P&L as % of value at start of today (= now - today's gain)
    start_of_day_value = out["account_value"] - out["today_total"] if out["account_value"] else None
    if start_of_day_value and start_of_day_value > 0:
        out["today_total_pct"] = out["today_total"] / start_of_day_value * 100
    else:
        out["today_total_pct"] = None
    out["all_time_pct"] = (out["all_time_pnl"] / out["starting_capital"] * 100) if out["starting_capital"] else None
    return out


def _open_position_cards(tickers: dict | None = None) -> list:
    """Position cards. If tickers=None, returns DB-only data with current=None.

    Caller passes tickers dict (symbol → price) for the live version.
    """
    from analysis.crypto_executor import _open_positions, parse_entry_notes
    out = []
    for p in _open_positions(is_paper=False):
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
        pnl_pct = (cur - entry) / entry * 100
        pnl_usd = (cur - entry) * float(p.qty)
        out.append({
            "id": p.id, "symbol": p.symbol, "strategy": p.strategy,
            "entry": entry, "current": cur, "stop": stop, "target": target,
            "qty": float(p.qty), "notional": entry * float(p.qty),
            "pnl_pct": pnl_pct, "pnl_usd": pnl_usd,
            "held_h": held_h, "time_to_stop_h": time_to_stop_h,
            "gauge_pct": gauge * 100,  # 0-100 for CSS width
            "risk_pct": (entry - stop) / entry * 100,
            "reward_pct": (target - entry) / entry * 100,
        })
    out.sort(key=lambda x: -x["pnl_pct"])  # winners first
    return out


def _strategy_breakdown() -> list:
    """Per-strategy stats: trades, win rate, avg P&L, total."""
    from webapp.models import CryptoTrade
    trades = CryptoTrade.query.filter(CryptoTrade.is_paper == False).order_by(CryptoTrade.executed_at).all()
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


def _activity_summary(days: int = 7) -> dict:
    """Compute last-N-days operational + P&L stats. Pure DB read, zero LLM cost."""
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(days=days)
    runs = CryptoRun.query.filter(CryptoRun.started_at >= cutoff).all()
    trades = CryptoTrade.query.filter(CryptoTrade.executed_at >= cutoff).all()

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
                pnl_usd = (float(t.price) - float(buy.price)) * float(buy.qty)
                pnl_pct = (float(t.price) - float(buy.price)) / float(buy.price) * 100
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
    equity_points = _equity_curve(days=7)  # DB only, fast
    latest_trade = CryptoTrade.query.order_by(CryptoTrade.id.desc()).first()
    latest_trade_id = latest_trade.id if latest_trade else 0

    return render_template(
        "crypto_dashboard.html",
        recent_runs=recent_runs,
        activity=activity,
        strat_breakdown=strat_breakdown,
        equity_json=json.dumps(equity_points),
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
    from binance.client import Client
    from analysis.crypto_executor import execute_intent
    symbol = symbol.upper()
    try:
        client = Client()
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
    from binance.client import Client
    open_pos = {p.id: p for p in _open_positions(is_paper=False)}
    pos = open_pos.get(trade_id)
    if not pos:
        return jsonify({"ok": False, "reason": "position not found or already closed"}), 404
    try:
        cur = float(Client().get_symbol_ticker(symbol=pos.symbol)["price"])
    except Exception as e:
        return jsonify({"ok": False, "reason": f"price lookup failed: {e}"}), 500
    result = execute_sell(pos, cur, "MANUAL EXIT — user clicked Sell now")
    return jsonify({
        "ok": result["executed"],
        "reason": result["reason"],
        "fill_price": result.get("fill_price"),
        "fill_qty": result.get("fill_qty"),
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
    trades = CryptoTrade.query.filter(CryptoTrade.is_paper == False).order_by(CryptoTrade.executed_at).all()
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

    Fetches tickers ONCE, passes to both account_summary and position_cards.
    Saves 4 weight/call vs separate fetches.
    """
    from binance.client import Client
    try:
        tickers = {t["symbol"]: float(t["price"]) for t in Client().get_all_tickers()}
    except Exception:
        tickers = {}
    account = _account_summary(prefetched_tickers=tickers)
    cards = _open_position_cards(tickers=tickers)
    return jsonify({"account": account, "position_cards": cards, "static": False})


@bp.route("/api/recent-trades")
def api_recent_trades():
    """Returns trades since `since_id` query param. For browser notifications polling."""
    from flask import request as flask_request
    since_id = int(flask_request.args.get("since_id", 0))
    trades = (
        CryptoTrade.query
        .filter(CryptoTrade.id > since_id, CryptoTrade.is_paper == False)
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


def _build_journal_entries(include_live_prices: bool = True) -> dict:
    """Build journal entries (open + closed). Returns dict with totals + entries.

    Heavy: makes a Binance API call for live prices on open positions.
    """
    from analysis.crypto_executor import parse_entry_notes
    trades = CryptoTrade.query.filter(CryptoTrade.is_paper == False).order_by(CryptoTrade.executed_at).all()
    by_sym: dict = {}
    for t in trades:
        if t.strategy == "manual_liquidation":
            continue
        by_sym.setdefault(t.symbol, []).append(t)

    closed_entries = []
    open_entries = []
    for sym, ts in by_sym.items():
        buys = []
        for t in ts:
            if t.side == "BUY":
                buys.append(t)
            elif t.side == "SELL" and buys:
                buy = buys.pop(0)
                meta = parse_entry_notes(buy.notes)
                pnl_pct = (float(t.price) - float(buy.price)) / float(buy.price) * 100
                pnl_usd = (float(t.price) - float(buy.price)) * float(buy.qty)
                held_h = (t.executed_at - buy.executed_at).total_seconds() / 3600
                exit_reason = "?"
                if t.notes:
                    parts = t.notes.split("·", 1)
                    exit_reason = parts[1].strip() if len(parts) > 1 else t.notes.strip()
                entry_value = float(buy.quote_amount or (buy.price * buy.qty))
                exit_value = float(t.quote_amount or (t.price * t.qty))
                closed_entries.append({
                    "is_open": False,
                    "symbol": sym, "strategy": buy.strategy,
                    "entry_at": buy.executed_at.isoformat(), "exit_at": t.executed_at.isoformat(),
                    "entry_price": float(buy.price), "exit_price": float(t.price),
                    "qty": float(buy.qty), "notional": entry_value,
                    "entry_value": entry_value, "exit_value": exit_value,
                    "stop": meta["stop"], "target": meta["target"],
                    "pnl_pct": pnl_pct, "pnl_usd": pnl_usd, "held_h": held_h,
                    "entry_reason": (buy.notes or "").split("·")[-1].strip() if buy.notes else "",
                    "exit_reason": exit_reason,
                })
        # Remaining unmatched BUYs are still-open positions
        for buy in buys:
            meta = parse_entry_notes(buy.notes)
            entry_value = float(buy.quote_amount or (buy.price * buy.qty))
            held_h = (datetime.utcnow() - buy.executed_at).total_seconds() / 3600
            open_entries.append({
                "is_open": True,
                "symbol": sym, "strategy": buy.strategy,
                "entry_at": buy.executed_at.isoformat(), "exit_at": None,
                "entry_price": float(buy.price), "exit_price": None,
                "qty": float(buy.qty), "notional": entry_value,
                "entry_value": entry_value, "exit_value": None,
                "stop": meta["stop"], "target": meta["target"],
                "pnl_pct": None, "pnl_usd": None, "held_h": held_h,
                "entry_reason": (buy.notes or "").split("·")[-1].strip() if buy.notes else "",
                "exit_reason": None,
            })

    if include_live_prices and open_entries:
        try:
            from binance.client import Client
            tickers = {t["symbol"]: float(t["price"]) for t in Client().get_all_tickers()}
            for e in open_entries:
                cur = tickers.get(e["symbol"])
                if cur is not None:
                    e["exit_price"] = cur
                    e["exit_value"] = cur * e["qty"]
                    e["pnl_pct"] = (cur - e["entry_price"]) / e["entry_price"] * 100
                    e["pnl_usd"] = (cur - e["entry_price"]) * e["qty"]
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

    Open positions return with current_price=None — JS fetches live prices
    separately via /api/journal/prices and updates DOM in-place.
    """
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 25))
    data = _build_journal_entries(include_live_prices=False)
    entries = data["open_entries"] + data["closed_entries"]
    total = len(entries)
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, pages))
    paged = entries[(page - 1) * per_page:page * per_page]
    open_symbols = sorted({e["symbol"] for e in data["open_entries"]})
    return jsonify({
        "entries": paged,
        "open_count": len(data["open_entries"]),
        "closed_count": len(data["closed_entries"]),
        "realized_pnl": data["realized_pnl"],
        "unrealized_pnl": 0,  # not yet known — will be updated after price fetch
        "open_symbols": open_symbols,  # tells JS which to fetch live prices for
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
        from binance.client import Client
        prices = {t["symbol"]: float(t["price"]) for t in Client().get_all_tickers()}
        return jsonify({"prices": prices, "ok": True})
    except Exception as e:
        return jsonify({"prices": {}, "ok": False, "error": str(e)}), 500


@bp.route("/holdings")
def holdings():
    from analysis.crypto_executor import _open_positions
    rows = CryptoHolding.query.order_by(CryptoHolding.value_usd.desc().nullslast()).all()
    total = sum((r.value_usd or 0) for r in rows)
    # Map asset (e.g. "DOGE") → open position trade ID, so the holdings page can show a Sell button
    open_pos_by_asset = {}
    for p in _open_positions(is_paper=False):
        # Strip the trading-quote suffix to get the base asset (DOGEUSDT → DOGE)
        if p.symbol.endswith("USDT"):
            asset = p.symbol[:-4]
            open_pos_by_asset[asset] = {
                "trade_id": p.id,
                "symbol": p.symbol,
                "entry_price": float(p.price),
                "strategy": p.strategy,
            }
    return render_template(
        "crypto_holdings.html",
        rows=rows, total=total,
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
        from binance.client import Client
        _t = Client().get_symbol_ticker(symbol=symbol)
        _t24 = next((x for x in Client().get_ticker() if x["symbol"] == symbol), None)
        _chg24 = float(_t24["priceChangePercent"]) if _t24 else None
    except Exception:
        _chg24 = None
    status_label, status_color = _setup_status(last_close, prior_high, vr or 0, sma50, rsi, _chg24)

    # Existing position? Use the proper net-quantity logic (BUYs - SELLs, dust-filtered)
    from analysis.crypto_executor import _open_positions
    open_pos = next((p for p in _open_positions(is_paper=False) if p.symbol == symbol), None)
    has_position = open_pos is not None
    position_info = None
    if open_pos:
        position_info = {
            "entry_price": float(open_pos.price),
            "qty": float(open_pos.qty),
            "value_now": float(open_pos.qty) * last_close,
            "pnl": (last_close - float(open_pos.price)) * float(open_pos.qty),
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
        .filter(CryptoTrade.symbol == symbol, CryptoTrade.is_paper == False)
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
        "crypto_drawdown_halt_pct", "crypto_min_balance_usd",
        "crypto_loop_interval_min", "crypto_fast_check_interval_sec",
    )
    if request.method == "POST":
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
                if key in ("crypto_loop_interval_min", "crypto_fast_check_interval_sec"):
                    intervals_changed = True
                _set_setting(key, val)

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
    return render_template(
        "crypto_settings.html",
        kill_switch=_setting("crypto_kill_switch", "off"),
        trading_mode=_setting("crypto_trading_mode", "paper"),
        max_position_usd=_setting("crypto_max_position_usd", "50"),
        max_concurrent=_setting("crypto_max_concurrent", "2"),
        drawdown_halt_pct=_setting("crypto_drawdown_halt_pct", "15"),
        min_balance_usd=_setting("crypto_min_balance_usd", "100"),
        loop_interval_min=_setting("crypto_loop_interval_min", "15"),
        fast_check_interval_sec=_setting("crypto_fast_check_interval_sec", "60"),
        has_keys=_has_binance_keys(),
        api_key_masked=_mask(key),
        api_secret_masked=_mask(secret),
    )


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
        from binance.client import Client
        from binance.exceptions import BinanceAPIException
    except ImportError:
        flash("python-binance not installed. Run: pip install python-binance", "err")
        return redirect(url_for("crypto.settings"))
    try:
        client = Client(key, secret)
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
