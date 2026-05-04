"""Flask app factory for Tradar — crypto-only autonomous trading bot."""

from __future__ import annotations

import os
from pathlib import Path

from flask import Flask, redirect, url_for

from webapp.models import Setting, db

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "app.db"


def create_app() -> Flask:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    app = Flask(__name__, instance_relative_config=False)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_PATH}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
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

    # Start in-process scheduler in the serving worker (Flask reloader child).
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
        # Position sizing (small + safe)
        "crypto_max_position_usd": "10",
        "crypto_max_concurrent": "2",
        "crypto_drawdown_halt_pct": "10",
        "crypto_min_balance_usd": "20",
        # Scheduler
        "crypto_loop_interval_min": "15",
        "crypto_fast_check_interval_sec": "60",
        # User-configurable starting capital (auto-detect if user doesn't set)
        "crypto_starting_capital_usd": "0",    # 0 = auto-detect from first sync
    }
    for k, v in defaults.items():
        if not Setting.query.get(k):
            db.session.add(Setting(key=k, value=v))
    db.session.commit()
