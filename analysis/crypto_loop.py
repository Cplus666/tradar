"""Crypto trading loop — runs every N hours via APScheduler.

Sequence per tick:
  1) Refresh klines for all active coins (4h)
  2) Resync Binance balances (one read-only call)
  3) Check exits on open paper positions (stop / target / time)
  4) Run scanner → produce intents
  5) Execute each intent through the guardrailed executor
  6) Log everything to a CryptoRun
"""

from __future__ import annotations

import logging
import traceback
from datetime import datetime

from webapp.models import CryptoRun, CryptoTrade, db

log = logging.getLogger("crypto_loop")


def _check_strategy_exit(exit_rule: str, df) -> tuple[bool, str]:
    """Per-strategy exit signals using current indicators."""
    import pandas as pd
    from analysis.indicators import attach
    if exit_rule == "stop_target_time":
        return False, ""
    d = attach(df)
    last = d.iloc[-1]
    close = float(last["Close"])
    if exit_rule == "sma50_break":
        sma50 = last.get("sma50")
        if pd.notna(sma50) and close < float(sma50):
            return True, f"SMA50 break (close ${close:.6f} < SMA50 ${float(sma50):.6f})"
    elif exit_rule == "rsi_overbought_70":
        rsi = last.get("rsi14")
        if pd.notna(rsi) and float(rsi) > 70:
            return True, f"RSI overbought ({float(rsi):.0f} > 70)"
    elif exit_rule == "rsi_overbought_80":
        rsi = last.get("rsi14")
        if pd.notna(rsi) and float(rsi) > 80:
            return True, f"RSI overbought ({float(rsi):.0f} > 80)"
    return False, ""


def _check_exits() -> list[str]:
    """Close any open position (paper or live) hitting any exit trigger.

    Triggers (any one fires):
      - STOP hit
      - TARGET hit
      - TIME STOP (max_hold bars elapsed)
      - STRATEGY EXIT (per-strategy signal: SMA50 break, RSI overbought, etc.)
      - REGIME EXIT (BTC turns down — exit ALL longs as safety)
    """
    from analysis.crypto_data import load_cached
    from analysis.crypto_executor import _open_positions, parse_entry_notes, execute_sell
    from analysis.crypto_strategies import _btc_trend_ok

    summaries: list[str] = []
    regime_off = not _btc_trend_ok("4h")

    for pos in _open_positions():
        df = load_cached(pos.symbol, "4h")
        if df is None or df.empty:
            continue
        cur_price = float(df["Close"].iloc[-1])
        meta = parse_entry_notes(pos.notes)
        # Bar size depends on strategy: 1h strategies use 1-hour bars; others use 4-hour
        strat = (pos.strategy or "").lower()
        if "1h" in strat or "momentum" in strat or "oversold" in strat:
            bar_seconds = 3600  # 1h
        else:
            bar_seconds = 4 * 3600  # 4h
        bars_held = int((datetime.utcnow() - pos.executed_at).total_seconds() / bar_seconds)

        exit_reason = None
        # 1) Regime exit — overrides everything
        if regime_off:
            exit_reason = "REGIME EXIT (BTC trend broken)"
        # 2) Stop loss
        elif meta["stop"] is not None and cur_price <= meta["stop"]:
            exit_reason = f"stop hit (${cur_price:.6f} <= ${meta['stop']:.6f})"
        # 3) Profit target
        elif meta["target"] is not None and cur_price >= meta["target"]:
            exit_reason = f"target hit (${cur_price:.6f} >= ${meta['target']:.6f})"
        # 4) Time stop
        elif bars_held >= meta["max_hold"]:
            exit_reason = f"time stop ({bars_held}/{meta['max_hold']} bars)"
        # 5) Strategy-specific exit
        else:
            triggered, why = _check_strategy_exit(meta["exit_rule"], df)
            if triggered:
                exit_reason = f"strategy exit: {why}"

        if exit_reason:
            mode_tag = "PAPER" if pos.is_paper else "LIVE"
            res = execute_sell(pos, cur_price, exit_reason)
            if res["executed"]:
                summaries.append(f"{pos.symbol} [{mode_tag}]: {res['reason']}")
            else:
                summaries.append(f"{pos.symbol} [{mode_tag}]: SELL FAILED — {res['reason']}")
    return summaries


# Backward-compat alias used by the loop
def _check_paper_exits() -> list[str]:
    return _check_exits()


def run_fast_exit_check() -> None:
    """Lightweight loop — checks current prices against stops/targets only.

    Uses ONE batched ticker call (not per-symbol klines refresh). Fast (1-2 sec).
    Fires stop/target/regime exits within 1 min instead of waiting for the full
    15-min loop. Does NOT write a CryptoRun row (would explode DB).

    Returns nothing; logs exits via standard logger.
    """
    from analysis.crypto_executor import (
        _open_positions, parse_entry_notes, execute_sell, execute_partial_sell,
        _get_setting, _binance_client,
    )
    from analysis.crypto_strategies import _btc_trend_ok

    # Iterate BOTH modes — paper positions also need their stops/targets/time-stops
    # and partial-take auto-trigger to fire. execute_sell / execute_partial_sell
    # already branch on position.is_paper, so the per-position behavior is correct.
    # Hardcoding is_paper=False here used to silently strand all paper trades.
    open_pos = _open_positions()
    if not open_pos:
        return  # nothing to check

    # ONE API call gets all prices (with timeout — paper positions don't need
    # auth, but live ones might; bare client is fine for the public ticker endpoint).
    try:
        prices = {t["symbol"]: float(t["price"]) for t in _binance_client().get_all_tickers()}
    except Exception as e:
        log.warning("fast exit check: ticker fetch failed: %s", e)
        return

    regime_off = not _btc_trend_ok("4h")

    for pos in open_pos:
        cur = prices.get(pos.symbol)
        if cur is None:
            continue
        meta = parse_entry_notes(pos.notes)
        # Bar size depends on strategy
        strat = (pos.strategy or "").lower()
        bar_seconds = 3600 if ("1h" in strat or "momentum" in strat or "oversold" in strat) else 14400
        bars_held = int((datetime.utcnow() - pos.executed_at).total_seconds() / bar_seconds)

        exit_reason = None
        if regime_off:
            exit_reason = "REGIME EXIT (BTC trend broken)"
        elif meta["stop"] is not None and cur <= meta["stop"]:
            exit_reason = f"stop hit (${cur:.6f} <= ${meta['stop']:.6f})"
        elif meta["target"] is not None and cur >= meta["target"]:
            exit_reason = f"target hit (${cur:.6f} >= ${meta['target']:.6f})"
        elif bars_held >= meta["max_hold"]:
            exit_reason = f"time stop ({bars_held}/{meta['max_hold']} bars)"

        if exit_reason:
            res = execute_sell(pos, cur, exit_reason)
            mode = "LIVE" if not pos.is_paper else "PAPER"
            if res["executed"]:
                log.info("fast exit %s [%s]: %s", pos.symbol, mode, res["reason"])
            else:
                log.warning("fast exit %s [%s] FAILED: %s", pos.symbol, mode, res["reason"])
            continue

        # Partial-take + breakeven-move: only fires if NO full-exit triggered above.
        # Once price reaches entry × (1 + trigger_pct/100), sell `fraction` of the
        # position and tighten the stop on the remainder to entry × (1 + buffer_pct/100).
        # Each position can only fire this once (tracked via partial_done flag in notes).
        if meta.get("partial_done"):
            continue  # already done — runner uses tightened stop
        try:
            partial_enabled = (_get_setting("crypto_partial_take_enabled") or "on").lower() == "on"
        except Exception:
            partial_enabled = True
        if not partial_enabled:
            continue
        if meta["stop"] is None or meta["target"] is None:
            continue  # need both bounds to compute breakeven move sensibly
        try:
            trigger_pct = float(_get_setting("crypto_partial_take_trigger_pct") or "4.0")
            fraction    = float(_get_setting("crypto_partial_take_fraction")    or "0.5")
        except (TypeError, ValueError):
            trigger_pct, fraction = 4.0, 0.5
        entry = float(pos.price)
        if cur >= entry * (1 + trigger_pct / 100.0):
            res = execute_partial_sell(pos, cur, fraction)
            mode = "LIVE" if not pos.is_paper else "PAPER"
            if res["executed"]:
                log.info("partial take %s [%s]: sold %.0f%% @ $%.6f", pos.symbol, mode, fraction * 100, cur)
            else:
                log.warning("partial take %s [%s] FAILED: %s", pos.symbol, mode, res["reason"])


def run_crypto_loop() -> None:
    """One tick of the crypto trading loop. Caller provides app context."""
    started = datetime.utcnow()
    run = CryptoRun(kind="trading_loop", status="running", started_at=started)
    db.session.add(run)
    db.session.commit()

    log_lines: list[str] = []
    summary_parts: list[str] = []

    try:
        # 1) Build dynamic universe (top by 24h volume + top movers)
        from analysis.crypto_universe import get_dynamic_universe
        universe = get_dynamic_universe(top_volume=30, top_movers=10, min_volume_usd=10_000_000)
        coins = [u["symbol"] for u in universe]
        log_lines.append(f"universe: {len(coins)} coins (top vol + movers)")
        summary_parts.append(f"universe={len(coins)}")

        # 2) Refresh klines for the universe — BOTH 4h and 1h for multi-timeframe strategies
        from analysis.crypto_data import refresh_pair
        if "BTCUSDT" not in coins:
            coins.insert(0, "BTCUSDT")
        # CRITICAL: also refresh klines for any open position not in today's universe.
        # Otherwise stale data can hide a stop trigger (KNC bug from 2026-05-04).
        from analysis.crypto_executor import _open_positions
        for op in _open_positions():
            if op.symbol not in coins:
                coins.append(op.symbol)
                log_lines.append(f"added open position {op.symbol} to refresh list (not in universe)")
        ok_4h = ok_1h = 0
        failed: list[str] = []
        for sym in coins:
            try:
                df = refresh_pair(sym, "4h")
                if df is not None and not df.empty:
                    ok_4h += 1
            except Exception as e:
                failed.append(f"{sym}/4h")
                log.warning("4h kline fetch failed for %s: %s", sym, e)
            try:
                df = refresh_pair(sym, "1h")
                if df is not None and not df.empty:
                    ok_1h += 1
            except Exception as e:
                failed.append(f"{sym}/1h")
                log.warning("1h kline fetch failed for %s: %s", sym, e)
        log_lines.append(f"klines: 4h={ok_4h}/{len(coins)}  1h={ok_1h}/{len(coins)}  (failed: {','.join(failed[:3])})")
        summary_parts.append(f"klines=4h:{ok_4h} 1h:{ok_1h}")

        # 3) Resync balances (best-effort, never blocks the loop)
        try:
            from analysis.binance_sync import sync_holdings
            from webapp.crypto.routes import get_binance_creds
            key, secret = get_binance_creds()
            if key and secret:
                sync_result = sync_holdings(key, secret)
                log_lines.append(
                    f"balance sync: {sync_result.get('real_count', 0)} real, "
                    f"${sync_result.get('total_value_usd', 0):.2f}"
                )
                # Ratchet peak + auto-halt on drawdown breach. Runs every loop
                # so the halt fires even when the dashboard isn't being viewed.
                try:
                    from analysis.crypto_executor import update_day_start_and_check_halt
                    risk = update_day_start_and_check_halt(sync_result.get("total_value_usd", 0))
                    if risk["halt_triggered"]:
                        log_lines.append(
                            f"DAILY DRAWDOWN HALT: -{risk['drawdown_pct']:.2f}% "
                            f"from day-start ${risk['day_start']:.2f} → kill switch auto-flipped ON"
                        )
                except Exception as e:
                    log_lines.append(f"drawdown check skipped: {e}")
        except Exception as e:
            log_lines.append(f"balance sync skipped: {e}")

        # 4) Exits
        exits = _check_paper_exits()
        log_lines.extend(exits)
        summary_parts.append(f"exits={len(exits)}")

        # 5) Kline freshness guard — refuse to scan with stale data
        from analysis.crypto_data import load_cached
        btc = load_cached("BTCUSDT", "4h")
        if btc is None or btc.empty:
            run.status = "error"; run.summary = "no BTC kline data"
            return
        last_bar_age_h = (datetime.utcnow() - btc.index.max()).total_seconds() / 3600
        if last_bar_age_h > 8:
            run.status = "error"
            run.summary = f"STALE DATA — last BTC bar {last_bar_age_h:.1f}h old (max 8h). Refusing to scan."
            log_lines.append(run.summary)
            return
        log_lines.append(f"freshness: BTC last bar {last_bar_age_h:.1f}h old — OK")

        # 6 + 7) Scan and execute
        from webapp.models import Setting
        from analysis.crypto_strategies import scan_crypto
        from analysis.crypto_executor import execute_intent

        signals, blocked = scan_crypto(coins, "4h")
        log_lines.append(f"scan: {len(signals)} signals, {len(blocked)} blocked")

        executed = 0
        skipped = 0
        max_pos_setting = Setting.query.get("crypto_max_position_usd")
        try:
            size_usd = float(max_pos_setting.value) if max_pos_setting and max_pos_setting.value else 50.0
        except (TypeError, ValueError):
            size_usd = 50.0
        for sig in signals:
            intent = {**sig, "size_usd": size_usd}
            r = execute_intent(intent)
            if r["executed"]:
                executed += 1
                log_lines.append(
                    f"  EXEC ({r['mode']}) {sig['symbol']} qty={r['fill_qty']:.8f} @ ${r['fill_price']:.4f}"
                )
            else:
                skipped += 1
                log_lines.append(f"  SKIP {sig['symbol']}: {r['reason']}")

        summary_parts.append(f"signals={len(signals)} exec={executed} skip={skipped}")

        run.status = "ok"
        run.summary = " · ".join(summary_parts)
    except Exception as e:
        run.status = "error"
        run.error = f"{e}\n{traceback.format_exc()}"
        run.summary = f"failed: {e}"
        log.exception("crypto loop failed")
    finally:
        run.ended_at = datetime.utcnow()
        run.log_excerpt = "\n".join(log_lines)[-4000:]
        db.session.commit()
