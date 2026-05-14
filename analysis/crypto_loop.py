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
        # Require a meaningful break — not a 0.1% wick under SMA50 that recovers
        # next bar (today's PEPE: closed at $4.09e-6 vs SMA50 $4.12e-6, sold,
        # then bounced back to entry $4.10e-6 within minutes). The 0.5% buffer
        # mirrors the SMA50_MIN_MARGIN_PCT entry filter — symmetric trend test.
        SMA50_BREAK_MARGIN_PCT = 0.5
        if pd.notna(sma50):
            sma50_f = float(sma50)
            threshold = sma50_f * (1 - SMA50_BREAK_MARGIN_PCT / 100.0)
            if close < threshold:
                breach_pct = (close - sma50_f) / sma50_f * 100
                return True, (
                    f"SMA50 break (close ${close:.6f} is {breach_pct:.2f}% under "
                    f"SMA50 ${sma50_f:.6f}, threshold -{SMA50_BREAK_MARGIN_PCT:.1f}%)"
                )
    elif exit_rule == "rsi_overbought_70":
        rsi = last.get("rsi14")
        if pd.notna(rsi) and float(rsi) > 70:
            return True, f"RSI overbought ({float(rsi):.0f} > 70)"
    elif exit_rule == "rsi_overbought_80":
        rsi = last.get("rsi14")
        if pd.notna(rsi) and float(rsi) > 80:
            return True, f"RSI overbought ({float(rsi):.0f} > 80)"
    elif exit_rule == "rsi_overbought_85":
        rsi = last.get("rsi14")
        if pd.notna(rsi) and float(rsi) > 85:
            return True, f"RSI overbought ({float(rsi):.0f} > 85)"
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
    from analysis.crypto_executor import (
        _open_positions, parse_entry_notes, execute_sell,
        _detect_surge, _set_trail_in_notes,
    )
    from analysis.crypto_strategies import _btc_trend_ok
    import pandas as pd

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
        # NOTE: BTC regime check (regime_off) used to auto-sell all positions
        # when BTC closed below its 4h SMA50. The trigger was way too sensitive
        # (a 0.07% breach would dump everything) and caused real losses on
        # 2026-05-12. Now regime is INFORMATIONAL ONLY — exposed as a warning
        # banner on the dashboard. User decides whether to act via Halt Now.
        # 2) Stop loss
        if meta["stop"] is not None and cur_price <= meta["stop"]:
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
                # RSI overbought = "price rising fast." That's a SURGE signal,
                # not always a top. If surge gate confirms (ROC + volume),
                # promote to trail mode instead of exiting — let the move run.
                # Only exit on RSI overbought when surge has died (volume drying).
                rule = (meta.get("exit_rule") or "")
                if rule.startswith("rsi_overbought") and not meta.get("trail_active"):
                    if _detect_surge(pos.symbol):
                        _set_trail_in_notes(pos, cur_price, activate=True)
                        log.info("RSI-OVERBOUGHT PROMOTED %s @ $%.6f — switched to trail (surge confirmed)",
                                 pos.symbol, cur_price)
                        triggered = False  # don't exit
                if triggered:
                    exit_reason = f"strategy exit: {why}"
            # 6) Weakness exit — losing position with decaying momentum.
            # Don't wait for the full -5% stop when momentum is clearly dead.
            if not exit_reason and meta.get("stop") is not None:
                try:
                    entry_price = float(pos.price)
                    pnl_pct = (cur_price - entry_price) / entry_price * 100
                except (ZeroDivisionError, TypeError):
                    pnl_pct = 0.0
                if pnl_pct < -2.0:
                    rsi_now = df["rsi14"].iloc[-1] if "rsi14" in df.columns else None
                    rsi_prev = df["rsi14"].iloc[-2] if "rsi14" in df.columns and len(df) >= 2 else None
                    if (rsi_now is not None and rsi_prev is not None
                            and pd.notna(rsi_now) and pd.notna(rsi_prev)
                            and float(rsi_now) < 45 and float(rsi_now) < float(rsi_prev)):
                        exit_reason = (f"weakness exit (pnl {pnl_pct:+.2f}% · "
                                       f"RSI {float(rsi_now):.0f} falling from {float(rsi_prev):.0f})")

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
        _get_setting, _binance_client, _detect_surge, _set_trail_in_notes,
    )
    from analysis.crypto_strategies import _btc_trend_ok

    # Iterate BOTH modes — paper positions also need their stops/targets/time-stops
    # and partial-take auto-trigger to fire. execute_sell / execute_partial_sell
    # already branch on position.is_paper, so the per-position behavior is correct.
    # Hardcoding is_paper=False here used to silently strand all paper trades.
    open_pos = _open_positions()
    # NOTE: do NOT early-return when open_pos is empty. The daily-rollover halt
    # check at the end of this function MUST run every tick so day_start
    # auto-snaps at MYT midnight even when the bot is between trades. Skipping
    # the per-position iteration is fine; skipping the whole function leaves
    # day_start stale until the slower 15-min main loop catches up.

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
        # regime_off no longer auto-sells (too sensitive — see _check_exits comment)
        if meta["stop"] is not None and cur <= meta["stop"]:
            exit_reason = f"stop hit (${cur:.6f} <= ${meta['stop']:.6f})"
        elif meta.get("trail_active"):
            # In surge-promoted trail mode: refresh high water mark, exit on
            # configured pullback. Skips the normal target check entirely
            # (target was already passed when trail was activated).
            try:
                trail_pct = float(_get_setting("crypto_surge_trail_pct") or "3.0")
            except (TypeError, ValueError):
                trail_pct = 3.0
            high = max(float(meta.get("trail_high") or cur), cur)
            if high > float(meta.get("trail_high") or 0):
                _set_trail_in_notes(pos, high)
            trail_stop = high * (1 - trail_pct / 100.0)
            if cur <= trail_stop:
                exit_reason = (f"trail stop hit (${cur:.6f} <= ${trail_stop:.6f}, "
                               f"peak ${high:.6f}, trail {trail_pct:.1f}%)")
        elif meta["target"] is not None and cur >= meta["target"]:
            # Surge gate: if a sudden price+volume surge is in progress, promote
            # to trail mode instead of exiting. Otherwise exit at target as
            # before. Surge detection is one extra API call per target hit.
            if _detect_surge(pos.symbol):
                _set_trail_in_notes(pos, cur, activate=True)
                log.info("SURGE PROMOTED %s @ $%.6f — runner switched to trail mode", pos.symbol, cur)
            else:
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

        # Partial-take + breakeven-move (only fires if no full-exit triggered above)
        if meta.get("partial_done"):
            continue
        try:
            partial_enabled = (_get_setting("crypto_partial_take_enabled") or "on").lower() == "on"
        except Exception:
            partial_enabled = True
        if not partial_enabled:
            continue
        if meta["stop"] is None or meta["target"] is None:
            continue
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

    # === Daily P&L halt check (live mode only) ===
    # Without this, halts only fire on the 15-min main-loop tick, leaving up to
    # 15 min of unprotected drawdown when dashboard isn't open. Adding it here
    # gives sub-minute halt response. Cost: 1 extra Binance balance call per
    # fast-exit tick (~1440/day at 60s cadence — well under rate limit).
    # Narrow exception handling so genuine bugs (NameError, TypeError, etc.)
    # propagate up to the scheduler's outer try and fail loudly. Only swallow
    # network/API errors that legitimately happen on a flaky connection, plus
    # write the last failure to a setting so the user can see silent failures
    # in the health panel.
    from binance.exceptions import BinanceAPIException, BinanceRequestException
    from requests.exceptions import RequestException
    from webapp.crypto.routes import get_binance_creds
    from analysis.crypto_executor import (
        update_day_start_and_check_halt, _set_setting,
    )
    try:
        key, secret = get_binance_creds()
        if key and secret:
            client = _binance_client(key, secret)
            acct = client.get_account()
            usdt = next((b for b in acct["balances"] if b["asset"] == "USDT"), None)
            usdt_free = float(usdt["free"]) if usdt else 0.0
            # Recompute open positions AFTER the exit loop ran above, so the
            # halt sees an accurate post-exit snapshot. Reuses the `prices`
            # dict we already fetched — no extra ticker call.
            open_live = _open_positions(is_paper=False)
            open_value = 0.0
            for p in open_live:
                cp = prices.get(p.symbol)
                if cp is None:
                    continue
                qty = float(getattr(p, "_remaining_qty", p.qty))
                open_value += cp * qty
            total = usdt_free + open_value
            if total > 0:
                risk = update_day_start_and_check_halt(total, usdt_free=usdt_free)
                if risk.get("loss_halt_fired"):
                    log.warning("FAST LOSS HALT fired (today %+.2f%%)",
                                risk.get("today_pnl_pct", 0))
                elif risk.get("profit_halt_fired"):
                    log.warning("FAST PROFIT HALT fired (today +%.2f%%)",
                                risk.get("today_pnl_pct", 0))
                _set_setting("crypto_last_fast_halt_error", "")
    except (BinanceAPIException, BinanceRequestException, RequestException, ConnectionError, TimeoutError) as e:
        # Expected transient — log + persist for health-panel visibility, then continue.
        log.error("fast-exit halt check network failure: %s", e)
        try:
            _set_setting("crypto_last_fast_halt_error",
                         f"{datetime.utcnow().isoformat()}|{type(e).__name__}: {str(e)[:200]}")
        except Exception:
            pass

    # Ghost exit check — runs after real exits, completely non-fatal
    try:
        from analysis.crypto_ghost import run_ghost_exit_check
        run_ghost_exit_check()
    except Exception as _ge:
        log.debug("ghost exit check skipped: %s", _ge)


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
                # Snapshot today's start + check daily drawdown halt. Runs every
                # loop so the halt fires even when the dashboard isn't being viewed.
                try:
                    from analysis.crypto_executor import update_day_start_and_check_halt
                    risk = update_day_start_and_check_halt(sync_result.get("total_value_usd", 0))
                    if risk.get("loss_halt_fired"):
                        log_lines.append(
                            f"LOSS HALT fired: today {risk.get('today_pnl_pct', 0):+.2f}% "
                            f"(day-start ${risk['day_start']:.2f}) — positions liquidated"
                        )
                    elif risk.get("profit_halt_fired"):
                        log_lines.append(
                            f"PROFIT HALT fired: today {risk.get('today_pnl_pct', 0):+.2f}% "
                            f"(day-start ${risk['day_start']:.2f}) — positions liquidated"
                        )
                except Exception as e:
                    log_lines.append(f"halt check skipped: {e}")
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

        # Ghost scan — uses freshly-refreshed klines, completely non-fatal
        try:
            from analysis.crypto_ghost import run_ghost_scan
            run_ghost_scan()
        except Exception as _ge:
            log.debug("ghost scan skipped: %s", _ge)

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
