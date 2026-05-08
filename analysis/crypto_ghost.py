"""crypto_ghost.py — Shadow portfolio that runs after a halt fires.

When a profit/loss halt fires, the bot seeds this module with the positions
it just sold. The ghost re-enters those positions at halt prices and keeps
running the real strategy logic in parallel — giving a live "what if we
hadn't halted?" comparison on the dashboard.

Completely isolated from live trading:
  • Own tables: CryptoGhostPosition, CryptoGhostTrade
  • Own settings: crypto_ghost_enabled / _day / _usdt
  • Never touches CryptoTrade, live positions, or trading decisions
  • Every public function is non-fatal (try/except at caller)

Called from:
  analysis/crypto_executor.py  →  start_ghost()  when halt fires
  analysis/crypto_loop.py      →  run_ghost_exit_check()  every 30s
  analysis/crypto_loop.py      →  run_ghost_scan()        every 10min
  webapp/crypto/routes.py      →  ghost_summary()         for dashboard/page
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger("crypto_ghost")
MYT      = timezone(timedelta(hours=8))
FEE_RATE = 0.001


# ── setting helpers (self-contained to avoid circular imports) ─────────────

def _get(key: str, default: str = "") -> str:
    from webapp.models import Setting
    row = Setting.query.get(key)
    return row.value if row else default


def _set(key: str, value: str) -> None:
    from webapp.models import Setting, db
    row = Setting.query.get(key)
    if row:
        row.value = value
    else:
        db.session.add(Setting(key=key, value=value))


def _f(key: str, default: float = 0.0) -> float:
    try:
        return float(_get(key) or default)
    except (TypeError, ValueError):
        return default


def _ghost_usdt() -> float:
    return _f("crypto_ghost_usdt", 0.0)


def _add_usdt(delta: float) -> None:
    _set("crypto_ghost_usdt", f"{_ghost_usdt() + delta:.6f}")


# ── start ghost ────────────────────────────────────────────────────────────

def start_ghost(sold_positions: list[dict], starting_usdt: float) -> None:
    """Clone sold positions into the ghost portfolio at halt-exit prices.

    sold_positions: list of dicts — symbol, qty, price (halt exit price),
        stop, target, strategy, entered_at, max_hold_bars, bar_interval_h,
        partial_done, original_stop
    starting_usdt: total USDT the ghost starts with (= locked value at halt)
    """
    if _get("crypto_ghost_feature_enabled", "on") != "on":
        return
    from webapp.models import CryptoGhostPosition, CryptoGhostTrade, db

    today_myt = datetime.now(MYT).date().isoformat()
    now_utc   = datetime.utcnow()

    # Close any leftover ghost positions from a previous halt today
    (CryptoGhostPosition.query
     .filter_by(status="open")
     .update({"status": "closed", "closed_at": now_utc,
               "close_reason": "replaced by new halt"}))

    valid = 0
    for pd in sold_positions:
        if not pd.get("stop") or not pd.get("target"):
            log.warning("ghost: skipping %s — missing stop/target", pd.get("symbol"))
            continue
        if not pd.get("price") or float(pd["price"]) <= 0:
            continue

        qty   = float(pd["qty"])
        price = float(pd["price"])

        db.session.add(CryptoGhostPosition(
            symbol        = pd["symbol"],
            qty           = qty,
            entry_price   = price,
            stop_price    = float(pd["stop"]),
            target_price  = float(pd["target"]),
            strategy      = pd.get("strategy") or "unknown",
            entered_at    = now_utc,
            max_hold_bars = int(pd.get("max_hold_bars", 12)),
            bar_interval_h= int(pd.get("bar_interval_h", 4)),
            partial_done  = bool(pd.get("partial_done", False)),
            original_stop = pd.get("original_stop"),
            status        = "open",
        ))
        db.session.add(CryptoGhostTrade(
            symbol       = pd["symbol"],
            side         = "BUY",
            qty          = qty,
            price        = price,
            executed_at  = now_utc,
            strategy     = pd.get("strategy") or "ghost",
            pnl          = None,
            notes        = "cloned at halt",
            quote_amount = qty * price,
        ))
        valid += 1

    # Free USDT = total value at halt minus the cost of cloned positions.
    # Ghost "buys" them at halt exit price — that capital is tied up in positions.
    pos_cost  = sum(float(pd["qty"]) * float(pd["price"])
                    for pd in sold_positions
                    if pd.get("stop") and pd.get("target") and float(pd.get("price", 0)) > 0)
    free_usdt = max(0.0, starting_usdt - pos_cost)

    _set("crypto_ghost_enabled", "1")
    _set("crypto_ghost_day",     today_myt)
    _set("crypto_ghost_usdt",    f"{free_usdt:.6f}")
    _set("crypto_ghost_locked",  f"{starting_usdt:.6f}")  # real account value at halt
    db.session.commit()
    log.info("Ghost started: %d positions, $%.2f pos + $%.2f free USDT (locked=$%.2f)",
             valid, pos_cost, free_usdt, starting_usdt)


# ── ghost exit check ───────────────────────────────────────────────────────

def run_ghost_exit_check() -> None:
    """Check ghost positions against live prices. Fire stop/target/time-stop.
    Called from fast_exit_check every 30s. Non-fatal."""
    if _get("crypto_ghost_feature_enabled", "on") != "on":
        if _get("crypto_ghost_enabled") == "1":
            _reset()  # feature toggled off mid-run — shut down cleanly
        return
    if _get("crypto_ghost_enabled") != "1":
        return

    today_myt = datetime.now(MYT).date().isoformat()
    if _get("crypto_ghost_day") != today_myt:
        _reset()
        return

    from webapp.models import CryptoGhostPosition, CryptoGhostTrade, db

    positions = CryptoGhostPosition.query.filter_by(status="open").all()
    if not positions:
        return

    try:
        from analysis.crypto_executor import _binance_client
        tickers = {t["symbol"]: float(t["price"])
                   for t in _binance_client().get_all_tickers()}
    except Exception as e:
        log.warning("ghost exit: ticker fetch failed: %s", e)
        return

    partial_trigger = _f("crypto_partial_take_trigger_pct", 4.0)
    partial_frac    = _f("crypto_partial_take_fraction",    0.5)
    buf_pct         = _f("crypto_breakeven_buffer_pct",     1.0)
    lock_frac       = _f("crypto_partial_lock_fraction",    0.5)
    partial_on      = _get("crypto_partial_take_enabled") == "on"
    now_utc         = datetime.utcnow()

    for pos in positions:
        cur = tickers.get(pos.symbol)
        if cur is None:
            continue

        # Partial profit take
        if (partial_on and not pos.partial_done
                and cur >= pos.entry_price * (1 + partial_trigger / 100)):
            sell_qty  = pos.qty * partial_frac
            sell_val  = cur * sell_qty
            fee       = sell_val * FEE_RATE
            pnl       = (cur - pos.entry_price) * sell_qty - fee * 2
            _add_usdt(sell_val - fee)
            db.session.add(CryptoGhostTrade(
                symbol=pos.symbol, side="SELL(partial)", qty=sell_qty,
                price=cur, executed_at=now_utc, strategy=pos.strategy,
                pnl=pnl, notes="partial profit take",
                quote_amount=sell_val,
            ))
            gain_pct        = (cur - pos.entry_price) / pos.entry_price * 100
            new_stop_pct    = max(buf_pct, gain_pct * lock_frac)
            pos.qty        -= sell_qty
            pos.stop_price  = pos.entry_price * (1 + new_stop_pct / 100)
            pos.partial_done= True
            log.info("GHOST partial %s @ $%.6f  P&L %+.2f", pos.symbol, cur, pnl)
            continue

        # Stop hit
        if cur <= pos.stop_price:
            _close_pos(pos, pos.stop_price, "stop hit", now_utc)
            continue

        # Target hit
        if cur >= pos.target_price:
            _close_pos(pos, pos.target_price, "target hit", now_utc)
            continue

        # Time stop
        bars = int((now_utc - pos.entered_at).total_seconds()
                   / (pos.bar_interval_h * 3600))
        if bars >= pos.max_hold_bars:
            _close_pos(pos, cur, f"time stop ({bars}/{pos.max_hold_bars})", now_utc)

    db.session.commit()


def _close_pos(pos, price: float, reason: str, now_utc: datetime) -> None:
    from webapp.models import CryptoGhostTrade, db
    fee  = (pos.entry_price * pos.qty + price * pos.qty) * FEE_RATE
    pnl  = (price - pos.entry_price) * pos.qty - fee
    _add_usdt(price * pos.qty)
    db.session.add(CryptoGhostTrade(
        symbol=pos.symbol, side="SELL", qty=pos.qty,
        price=price, executed_at=now_utc, strategy=pos.strategy,
        pnl=pnl, notes=reason, quote_amount=price * pos.qty,
    ))
    pos.status      = "closed"
    pos.closed_at   = now_utc
    pos.close_price = price
    pos.close_reason= reason
    log.info("GHOST close %s @ $%.6f  %s  P&L %+.2f", pos.symbol, price, reason, pnl)


# ── auto-seed from existing halt ──────────────────────────────────────────

def maybe_seed_from_today_halt() -> None:
    """Auto-seed ghost if a halt fired today but ghost was never started.

    Handles two cases:
      1. Bot restarted mid-day after a halt (ghost state lost in memory)
      2. Ghost code deployed after a halt already fired (today's scenario)

    Only seeds once per day — if ghost_day == today, either ghost is already
    running or user manually reset it; either way we don't re-seed.
    """
    if _get("crypto_ghost_feature_enabled", "on") != "on":
        return

    today_myt = datetime.now(MYT).date().isoformat()

    # Already seeded today (running or manually reset)
    if _get("crypto_ghost_day") == today_myt:
        return

    # No halt today
    if (_get("crypto_today_loss_halted")   != "1"
            and _get("crypto_today_profit_halted") != "1"):
        return

    log.info("Ghost: detected halt with no ghost running — auto-seeding from DB")

    from webapp.models import CryptoTrade
    from datetime import timezone

    today_start_utc = (datetime.now(MYT)
                       .replace(hour=0, minute=0, second=0, microsecond=0)
                       .astimezone(timezone.utc)
                       .replace(tzinfo=None))

    halt_kw  = ("LOSS HALT" if _get("crypto_today_loss_halted") == "1"
                else "PROFIT HALT")
    is_paper = (_get("crypto_trading_mode", "paper") == "paper")
    halt_sells = (CryptoTrade.query
                  .filter(CryptoTrade.side == "SELL",
                          CryptoTrade.executed_at >= today_start_utc,
                          CryptoTrade.is_paper == is_paper,
                          CryptoTrade.notes.contains(halt_kw))
                  .order_by(CryptoTrade.executed_at)
                  .all())

    if not halt_sells:
        log.warning("Ghost auto-seed: no halt sell records found")
        return

    # For each halt sell, find the most recent BUY to get stop/target/strategy
    sold_positions = []
    for sell in halt_sells:
        buy = (CryptoTrade.query
               .filter(CryptoTrade.symbol == sell.symbol,
                       CryptoTrade.side == "BUY",
                       CryptoTrade.executed_at < sell.executed_at,
                       CryptoTrade.is_paper == is_paper)
               .order_by(CryptoTrade.executed_at.desc())
               .first())
        if not buy:
            continue
        try:
            from analysis.crypto_executor import parse_entry_notes
            meta  = parse_entry_notes(buy.notes)
            strat = (buy.strategy or "").lower()
            bar_h = 1 if ("1h" in strat or "momentum" in strat
                          or "oversold" in strat) else 4
            sold_positions.append({
                "symbol":        sell.symbol,
                "qty":           float(sell.qty),
                "price":         float(sell.price),   # halt exit price
                "stop":          meta.get("stop"),
                "target":        meta.get("target"),
                "strategy":      buy.strategy,
                "entered_at":    buy.executed_at,
                "max_hold_bars": meta.get("max_hold", 12),
                "bar_interval_h":bar_h,
                "partial_done":  bool(meta.get("partial_done")),
                "original_stop": meta.get("original_stop"),
            })
        except Exception as e:
            log.warning("Ghost auto-seed: skipping %s — %s", sell.symbol, e)

    if not sold_positions:
        log.warning("Ghost auto-seed: no valid positions after BUY lookup")
        return

    # Total value at halt = current Binance USDT balance
    # (bot has been halted since halt fired, so balance = what was locked)
    try:
        from analysis.crypto_executor import _binance_client
        account   = _binance_client().get_account()
        total_usdt = next((float(b["free"]) for b in account["balances"]
                           if b["asset"] == "USDT"), 0.0)
    except Exception as e:
        log.warning("Ghost auto-seed: Binance balance fetch failed (%s), estimating", e)
        total_usdt = sum(float(p["price"]) * float(p["qty"]) for p in sold_positions) + 21.0

    log.info("Ghost auto-seed: %d positions, $%.2f total",
             len(sold_positions), total_usdt)
    start_ghost(sold_positions, total_usdt)


# ── ghost scanner ──────────────────────────────────────────────────────────

def run_ghost_scan() -> None:
    """Run strategy scanner and open new ghost positions on signals.
    Called from main loop every 10 min — uses freshly-refreshed klines.
    Non-fatal."""
    # Auto-seed if halt fired today but ghost never started
    maybe_seed_from_today_halt()

    if _get("crypto_ghost_enabled") != "1":
        return

    today_myt = datetime.now(MYT).date().isoformat()
    if _get("crypto_ghost_day") != today_myt:
        _reset()
        return

    if _get("crypto_kill_switch") == "on":
        return

    from webapp.models import CryptoGhostPosition, CryptoGhostTrade, db
    from analysis.crypto_strategies import scan_crypto
    from analysis.crypto_universe import get_dynamic_universe

    open_pos       = CryptoGhostPosition.query.filter_by(status="open").all()
    max_concurrent = int(_f("crypto_max_concurrent", 8))
    if len(open_pos) >= max_concurrent:
        return

    ghost_usdt  = _ghost_usdt()
    max_pos_usd = _f("crypto_max_position_usd", 50.0)
    open_syms   = {p.symbol for p in open_pos}
    now_utc     = datetime.utcnow()

    try:
        universe = [u["symbol"] for u in
                    get_dynamic_universe(top_volume=30, top_movers=10,
                                        min_volume_usd=10_000_000)]
    except Exception as e:
        log.warning("ghost scan: universe failed: %s", e)
        return

    try:
        signals, _ = scan_crypto(universe, "4h")
    except Exception as e:
        log.warning("ghost scan: scanner failed: %s", e)
        return

    added = 0
    for sig in signals:
        if len(open_pos) + added >= max_concurrent:
            break
        sym = sig["symbol"]
        if sym in open_syms:
            continue

        entry = sig["entry_price"]
        qty   = max_pos_usd / entry
        cost  = entry * qty * (1 + FEE_RATE)
        if ghost_usdt < cost:
            continue

        strat = sig.get("strategy", "unknown")
        bar_h = 1 if strat == "breakout_1h" else 4

        pos = CryptoGhostPosition(
            symbol=sym, qty=qty, entry_price=entry,
            stop_price=sig["stop_price"], target_price=sig["target_price"],
            strategy=strat, entered_at=now_utc,
            max_hold_bars=sig.get("max_hold_bars", 12),
            bar_interval_h=bar_h, partial_done=False, status="open",
        )
        db.session.add(pos)
        db.session.add(CryptoGhostTrade(
            symbol=sym, side="BUY", qty=qty, price=entry,
            executed_at=now_utc, strategy=strat, pnl=None,
            notes=f"stop={sig['stop_price']:.6f} target={sig['target_price']:.6f}",
            quote_amount=qty * entry,
        ))
        ghost_usdt -= cost
        open_syms.add(sym)
        added += 1
        log.info("GHOST buy %s @ $%.6f  [%s]", sym, entry, strat)

    if added:
        _set("crypto_ghost_usdt", f"{ghost_usdt:.6f}")
        db.session.commit()


# ── reset ──────────────────────────────────────────────────────────────────

def _reset() -> None:
    """Close all open ghost positions at current market prices and disable ghost.
    Called when a new MYT day is detected — same midnight boundary as the halt reset.
    """
    from webapp.models import CryptoGhostPosition, CryptoGhostTrade, db

    positions = CryptoGhostPosition.query.filter_by(status="open").all()
    now_utc   = datetime.utcnow()

    # Fetch current prices to compute final P&L
    tickers = {}
    if positions:
        try:
            from analysis.crypto_executor import _binance_client
            tickers = {t["symbol"]: float(t["price"])
                       for t in _binance_client().get_all_tickers()}
        except Exception as e:
            log.warning("ghost reset: ticker fetch failed (%s) — closing at entry price", e)

    ghost_usdt   = _ghost_usdt()
    total_realized = 0.0

    for pos in positions:
        price = tickers.get(pos.symbol, pos.entry_price)
        fee   = (pos.entry_price * pos.qty + price * pos.qty) * FEE_RATE
        pnl   = (price - pos.entry_price) * pos.qty - fee
        total_realized += pnl
        ghost_usdt     += price * pos.qty

        db.session.add(CryptoGhostTrade(
            symbol=pos.symbol, side="SELL", qty=pos.qty,
            price=price, executed_at=now_utc, strategy=pos.strategy,
            pnl=pnl, notes="midnight reset — EOD close",
            quote_amount=price * pos.qty,
        ))
        pos.status      = "closed"
        pos.closed_at   = now_utc
        pos.close_price = price
        pos.close_reason= "midnight reset"

    # Final verdict log
    locked = _f("crypto_ghost_locked", 0.0)
    if locked > 0:
        delta   = ghost_usdt - locked
        verdict = "NO-HALT WON" if delta > 0 else "HALT WAS CORRECT"
        log.info("Ghost EOD: ghost=$%.2f locked=$%.2f diff=%+.2f → %s",
                 ghost_usdt, locked, delta, verdict)

    _set("crypto_ghost_enabled", "0")
    _set("crypto_ghost_usdt",    "0")
    _set("crypto_ghost_locked",  "0")
    db.session.commit()
    log.info("Ghost reset at MYT midnight (%d positions closed, realized P&L %+.2f)",
             len(positions), total_realized)


# ── summary for dashboard / simulation page ────────────────────────────────

def ghost_summary() -> dict:
    """Return ghost portfolio state. Safe to call when ghost is inactive."""
    if _get("crypto_ghost_enabled") != "1":
        return {"enabled": False}

    from webapp.models import CryptoGhostPosition, CryptoGhostTrade

    ghost_usdt = _ghost_usdt()
    positions  = CryptoGhostPosition.query.filter_by(status="open").all()
    all_trades = (CryptoGhostTrade.query
                  .order_by(CryptoGhostTrade.executed_at.desc())
                  .limit(100).all())

    # Current prices for open positions
    pos_value = 0.0
    pos_list  = []
    if positions:
        try:
            from analysis.crypto_executor import _binance_client
            tickers = {t["symbol"]: float(t["price"])
                       for t in _binance_client().get_all_tickers()}
            for p in positions:
                cur = tickers.get(p.symbol, p.entry_price)
                unr = (cur - p.entry_price) * p.qty
                pos_value += cur * p.qty
                pos_list.append({
                    "symbol":       p.symbol,
                    "qty":          p.qty,
                    "entry":        p.entry_price,
                    "current":      cur,
                    "stop":         p.stop_price,
                    "target":       p.target_price,
                    "unr":          unr,
                    "pct":          (cur / p.entry_price - 1) * 100,
                    "strategy":     p.strategy or "",
                    "partial_done": p.partial_done,
                })
        except Exception:
            for p in positions:
                pos_list.append({
                    "symbol": p.symbol, "qty": p.qty,
                    "entry": p.entry_price, "current": p.entry_price,
                    "stop": p.stop_price, "target": p.target_price,
                    "unr": 0.0, "pct": 0.0,
                    "strategy": p.strategy or "", "partial_done": p.partial_done,
                })

    total    = ghost_usdt + pos_value
    realized = sum(t.pnl for t in all_trades if t.pnl is not None)

    # Locked = real account USDT after halt (stored at ghost start time)
    locked = _f("crypto_ghost_locked", 0.0) or sum(
        t.qty * t.price for t in all_trades if t.side == "BUY"
    )

    return {
        "enabled":    True,
        "total":      total,
        "locked":     locked,
        "diff":       total - locked,
        "ghost_usdt": ghost_usdt,
        "pos_value":  pos_value,
        "realized":   realized,
        "n_open":     len(pos_list),
        "positions":  pos_list,
        "trades": [
            {
                "symbol":      t.symbol,
                "side":        t.side,
                "qty":         float(t.qty),
                "price":       float(t.price),
                "pnl":         t.pnl,
                "time_utc":    t.executed_at.isoformat(),
                "strategy":    t.strategy or "",
                "notes":       t.notes or "",
            }
            for t in all_trades
        ],
    }
