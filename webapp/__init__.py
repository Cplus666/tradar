"""Flask app factory for Tradar — crypto-only autonomous trading bot."""

from __future__ import annotations

import os
from pathlib import Path

from flask import Flask, redirect, url_for
from sqlalchemy import event
from sqlalchemy.engine import Engine

from webapp.models import Setting, db


# Apply WAL mode + busy_timeout to every SQLite connection. Without these,
# the Flask request thread + APScheduler worker thread can race and one
# gets "database is locked" — silently rolling back trades. WAL allows
# concurrent reads while one writer holds the lock; busy_timeout makes
# contended writes wait+retry instead of failing immediately.
@event.listens_for(Engine, "connect")
def _enable_sqlite_concurrency_pragmas(dbapi_connection, connection_record):
    if "sqlite" not in str(type(dbapi_connection)).lower():
        return
    cur = dbapi_connection.cursor()
    try:
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=NORMAL")
    finally:
        cur.close()

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "app.db"


def create_app() -> Flask:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    app = Flask(__name__, instance_relative_config=False)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_PATH}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "connect_args": {
            "check_same_thread": False,
            "timeout": 30,
        },
    }
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-please-set-SECRET_KEY-env")

    db.init_app(app)

    # Crypto blueprint (the only one)
    from webapp.crypto.routes import bp as crypto_bp
    app.register_blueprint(crypto_bp)

    # Root → /crypto/ (single-purpose app)
    @app.route("/")
    def _root():
        return redirect(url_for("crypto.dashboard"))

    with app.app_context():
        db.create_all()
        _seed_settings()

        # Start in-process scheduler INSIDE the app context so _next_fire_time
        # can query CryptoRun history (otherwise the query silently fails and
        # the scheduler defaults to "fire NOW" on every Flask restart).
        # Only start in the serving worker (Flask reloader child).
        # Production (gunicorn / docker without --reload) sets STOCK_RUN_SCHEDULER=1.
        if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or os.environ.get("STOCK_RUN_SCHEDULER") == "1":
            from webapp.scheduler import start_scheduler
            start_scheduler()

    return app


def _seed_settings() -> None:
    """Safe defaults for a fresh installation.

    First-time users land on a SAFE configuration:
      - kill switch ON       → no trades fire until user deliberately enables
      - paper mode           → never start a fresh install on live money
      - small position size  → if user enables before reading docs, blast radius is tiny
      - tighter drawdown    → halt earlier on a bad streak

    Existing users keep their configured values (only inserts if key is absent).
    """
    defaults = {
        # Safety
        "crypto_kill_switch": "on",            # blocks trading until user opts in
        "crypto_trading_mode": "paper",        # paper-trade by default
        # Position sizing
        "crypto_max_position_usd": "50",       # $50 per trade (matches Binance min-order comfort zone)
        "crypto_max_concurrent": "4",          # up to 4 simultaneous positions
        "crypto_min_balance_usd": "20",
        # Scheduler
        "crypto_loop_interval_min": "10",      # full scan every 10 min
        "crypto_fast_check_interval_sec": "30", # exit-checker every 30 sec — fast stop/target reactions
        # User-configurable starting capital (auto-detect if user doesn't set)
        "crypto_starting_capital_usd": "0",    # 0 = auto-detect from first sync
        # Day-start baseline — auto-snapshotted at first call each MYT day.
        # Used by the soft loss/profit halts in analysis/crypto_executor.py.
        "crypto_day_start_value_usd": "0",     # auto-managed: today's start
        "crypto_day_start_date": "",           # auto-managed: ISO date in MYT
        # Partial profit-take + breakeven-move — fires once per position when
        # price reaches entry × (1 + trigger_pct/100). Sells `fraction` of the
        # position and moves stop on the remainder to entry × (1 + buffer_pct/100).
        # Locks in profit on chopped winners while letting runners go to target.
        "crypto_partial_take_enabled":      "on",
        "crypto_partial_take_trigger_pct":  "4.0",   # fire at entry +4%
        "crypto_partial_take_fraction":     "0.5",   # sell half
        "crypto_breakeven_buffer_pct":      "1.0",   # new stop = entry +1%
        # Per-side fee rate used for net P&L calculations across views (default
        # 0.1% = Binance Spot taker; set 0.00075 if paying fees in BNB)
        "crypto_fee_rate_per_side":         "0.001",
    }
    for k, v in defaults.items():
        if not Setting.query.get(k):
            db.session.add(Setting(key=k, value=v))
    db.session.commit()
