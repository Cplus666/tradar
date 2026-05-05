"""APScheduler integration for Tradar.

Three jobs (all run in-process inside the Flask worker):
  - crypto_loop       every N min (Setting: crypto_loop_interval_min, default 15)
  - fast_exit_check   every N sec (Setting: crypto_fast_check_interval_sec, default 60)
  - db_cleanup        daily 03:30 MYT — prunes old run log_excerpts

Intervals are user-configurable via /crypto/settings and apply live (no restart).
next_fire_time is computed from the last recorded run, so Flask reloads don't
reset cadence.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

# APScheduler is configured with timezone="Asia/Kuala_Lumpur" below. When we hand
# it next_run_time as a NAIVE datetime, it interprets the wall-clock as MYT — so
# passing datetime.utcnow() gets misinterpreted as MYT and shifts the schedule
# 8 hours into the future. Always hand APScheduler tz-AWARE datetimes localized
# to MYT to avoid this trap.
_TZ = pytz.timezone("Asia/Kuala_Lumpur")

ROOT = Path(__file__).resolve().parent.parent

log = logging.getLogger("scheduler")
_scheduler: BackgroundScheduler | None = None


def _run_crypto_loop() -> None:
    """Invoke crypto trading loop in-process (uses Flask app context)."""
    log.info("scheduled job firing: crypto_loop")
    try:
        # Late import — keeps app startup decoupled from crypto deps
        from webapp import create_app
        from analysis.crypto_loop import run_crypto_loop
        app = create_app()
        with app.app_context():
            run_crypto_loop()
        log.info("crypto_loop ok")
    except Exception as e:
        log.exception("crypto_loop failed: %s", e)


def _run_db_cleanup() -> None:
    """Daily DB cleanup — prune old run logs to keep DB fast."""
    log.info("scheduled job firing: db_cleanup")
    try:
        from webapp import create_app
        from webapp.cleanup import run_cleanup
        app = create_app()
        with app.app_context():
            counts = run_cleanup(apply=True, aggressive=False)
        touched = sum(counts.values())
        log.info("db_cleanup ok — touched %d rows", touched)
    except Exception as e:
        log.exception("db_cleanup failed: %s", e)


def _run_fast_exit_check() -> None:
    """Lightweight 1-min loop — checks current prices against open positions only."""
    try:
        from webapp import create_app
        from analysis.crypto_loop import run_fast_exit_check
        app = create_app()
        with app.app_context():
            run_fast_exit_check()
    except Exception as e:
        log.exception("fast exit check failed: %s", e)


# Settings keys + defaults + valid ranges (clamped to prevent abuse)
_LOOP_INTERVAL_KEY = "crypto_loop_interval_min"
_LOOP_INTERVAL_DEFAULT = 15
_LOOP_INTERVAL_MIN = 5      # below 5 = Binance rate limit risk
_LOOP_INTERVAL_MAX = 60

# Fast check is now in SECONDS for finer control
_FAST_INTERVAL_KEY = "crypto_fast_check_interval_sec"
_FAST_INTERVAL_DEFAULT = 60   # 1 minute default
_FAST_INTERVAL_MIN = 0        # 0 = disabled
_FAST_INTERVAL_MAX = 600      # cap at 10 min (use full loop instead if you want slower)
_FAST_INTERVAL_FLOOR_WHEN_ENABLED = 10  # min 10s to avoid hammering API


def _read_intervals() -> tuple[int, int]:
    """Read configured intervals from Setting table, with clamping.

    Returns (loop_minutes, fast_seconds).
    """
    try:
        from webapp.models import Setting
        loop_row = Setting.query.get(_LOOP_INTERVAL_KEY)
        fast_row = Setting.query.get(_FAST_INTERVAL_KEY)
        loop_min = int(loop_row.value) if loop_row and loop_row.value else _LOOP_INTERVAL_DEFAULT
        fast_sec = int(fast_row.value) if fast_row and fast_row.value else _FAST_INTERVAL_DEFAULT
    except Exception:
        loop_min, fast_sec = _LOOP_INTERVAL_DEFAULT, _FAST_INTERVAL_DEFAULT
    # Clamp full-loop minutes
    loop_min = max(_LOOP_INTERVAL_MIN, min(_LOOP_INTERVAL_MAX, loop_min))
    # Clamp fast-check seconds: 0 means disabled, otherwise floor to 10s
    if fast_sec <= 0:
        fast_sec = 0
    else:
        fast_sec = max(_FAST_INTERVAL_FLOOR_WHEN_ENABLED, min(_FAST_INTERVAL_MAX, fast_sec))
    return loop_min, fast_sec


def _next_fire_time(interval_seconds: int, last_fire_kind: str) -> datetime:
    """Compute next fire time that survives Flask restarts gracefully.

    - If last fire was MORE than `interval_seconds` ago → fire immediately (we're overdue)
    - If last fire was recent → fire at last_fire + interval (preserves cadence)

    Prevents Flask reloader from resetting the schedule and creating endless waits.
    """
    try:
        from webapp.models import CryptoRun
        last_run = (
            CryptoRun.query.filter_by(kind=last_fire_kind)
            .order_by(CryptoRun.id.desc()).first()
        )
        if last_run is not None:
            elapsed = (datetime.utcnow() - last_run.started_at).total_seconds()
            if elapsed >= interval_seconds:
                return datetime.now(_TZ)  # overdue → fire now (tz-aware MYT)
            else:
                # last_run.started_at is naive UTC (DB stores datetime.utcnow()).
                # Tag it as UTC, add the interval, then convert to MYT-aware so
                # APScheduler reads the wall-clock correctly.
                next_utc = pytz.utc.localize(last_run.started_at + timedelta(seconds=interval_seconds))
                return next_utc.astimezone(_TZ)
    except Exception:
        pass
    return datetime.now(_TZ)  # default: fire ASAP (tz-aware MYT)


def reschedule_crypto_jobs() -> dict:
    """Re-add crypto jobs with currently-configured intervals.

    Survives Flask reloads: computes next fire time based on LAST run history,
    not "now + interval". So a Flask restart doesn't reset the schedule.
    """
    if _scheduler is None or not _scheduler.running:
        return {"loop_min": None, "fast_sec": None, "error": "scheduler not running"}
    loop_min, fast_sec = _read_intervals()

    # Compute next fire time for trading_loop — fires immediately if overdue
    next_loop = _next_fire_time(loop_min * 60, "trading_loop")
    _scheduler.add_job(
        _run_crypto_loop,
        trigger=IntervalTrigger(minutes=loop_min),
        next_run_time=next_loop,
        id="crypto_loop",
        name=f"Crypto trading loop (every {loop_min} min)",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    # Fast exit check (or remove if disabled)
    if fast_sec > 0:
        _scheduler.add_job(
            _run_fast_exit_check,
            trigger=IntervalTrigger(seconds=fast_sec),
            id="fast_exit_check",
            name=f"Fast exit check (every {fast_sec}s)",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
    else:
        try:
            _scheduler.remove_job("fast_exit_check")
        except Exception:
            pass

    log.info("crypto jobs rescheduled: loop=%dmin (next fire %s), fast=%ds",
             loop_min, next_loop.isoformat(), fast_sec)
    return {"loop_min": loop_min, "fast_sec": fast_sec, "next_loop_fire": next_loop.isoformat()}


def start_scheduler() -> BackgroundScheduler:
    """Start the in-process scheduler. Idempotent."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return _scheduler

    _scheduler = BackgroundScheduler(timezone="Asia/Kuala_Lumpur")
    # Crypto jobs (interval configurable via Settings — see reschedule_crypto_jobs)
    # next_run_time is computed from last fire time so Flask restarts don't reset cadence.
    loop_min, fast_sec = _read_intervals()
    next_loop = _next_fire_time(loop_min * 60, "trading_loop")
    _scheduler.add_job(
        _run_crypto_loop,
        trigger=IntervalTrigger(minutes=loop_min),
        next_run_time=next_loop,
        id="crypto_loop",
        name=f"Crypto trading loop (every {loop_min} min)",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    log.info("crypto_loop scheduled: every %d min, next fire %s", loop_min, next_loop.isoformat())
    if fast_sec > 0:
        _scheduler.add_job(
            _run_fast_exit_check,
            trigger=IntervalTrigger(seconds=fast_sec),
            id="fast_exit_check",
            name=f"Fast exit check (every {fast_sec}s)",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
    # DB cleanup: daily at 03:30 MYT (low-activity window). Prunes old run logs
    # so the DB stays fast over months/years. Fully automatic — no cron needed.
    _scheduler.add_job(
        _run_db_cleanup,
        trigger=CronTrigger(hour=3, minute=30, timezone="Asia/Kuala_Lumpur"),
        id="db_cleanup",
        name="DB cleanup (daily 03:30 MYT)",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    _scheduler.start()
    log.info("scheduler started; next crypto_loop=%s", get_next_run("crypto_loop"))
    return _scheduler


def get_next_run(job_id: str = "crypto_loop") -> datetime | None:
    if not _scheduler:
        return None
    job = _scheduler.get_job(job_id)
    return job.next_run_time if job else None


def trigger_crypto_now() -> None:
    """Force the crypto loop to run now (used by the 'Run loop now' button)."""
    _run_crypto_loop()
