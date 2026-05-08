"""SQLAlchemy models for Tradar — crypto-only."""

from __future__ import annotations

from datetime import datetime

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def utcnow() -> datetime:
    return datetime.utcnow()


class Setting(db.Model):
    """Key-value settings — Binance API keys, guardrails, intervals, mode flags."""
    __tablename__ = "settings"
    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class CryptoCoin(db.Model):
    """Static coin watchlist (LEGACY — kept for backward compat).

    The trading loop now uses dynamic universe (top by 24h volume + movers).
    This table is unused by the loop but kept so old DBs upgrade cleanly.
    """
    __tablename__ = "crypto_coins"
    symbol = db.Column(db.String(32), primary_key=True)
    base = db.Column(db.String(16))
    quote = db.Column(db.String(16))
    why = db.Column(db.Text)
    active = db.Column(db.Boolean, default=True, nullable=False)
    added_at = db.Column(db.DateTime, default=utcnow, nullable=False)


class CryptoHolding(db.Model):
    """Snapshot of coin balances pulled from Binance (read-only)."""
    __tablename__ = "crypto_holdings"
    id = db.Column(db.Integer, primary_key=True)
    asset = db.Column(db.String(16), nullable=False, index=True)
    free = db.Column(db.Float, nullable=False)
    locked = db.Column(db.Float, default=0.0, nullable=False)
    avg_cost_usd = db.Column(db.Float)
    last_price_usd = db.Column(db.Float)
    value_usd = db.Column(db.Float)
    fetched_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    notes = db.Column(db.Text)


class CryptoTrade(db.Model):
    """Audit log of crypto trades — paper or live."""
    __tablename__ = "crypto_trades"
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(32), nullable=False, index=True)
    side = db.Column(db.String(8), nullable=False)  # BUY | SELL
    qty = db.Column(db.Float, nullable=False)
    price = db.Column(db.Float, nullable=False)
    quote_amount = db.Column(db.Float)
    executed_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)
    status = db.Column(db.String(16), default="filled", nullable=False)
    is_paper = db.Column(db.Boolean, default=True, nullable=False)
    binance_order_id = db.Column(db.String(64))
    strategy = db.Column(db.String(64))
    notes = db.Column(db.Text)


class CryptoRun(db.Model):
    """Audit log of trading-loop / sync / cleanup runs."""
    __tablename__ = "crypto_runs"
    id = db.Column(db.Integer, primary_key=True)
    started_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)
    ended_at = db.Column(db.DateTime)
    kind = db.Column(db.String(32), nullable=False)  # trading_loop | sync | db_cleanup
    status = db.Column(db.String(16), default="running", nullable=False)
    summary = db.Column(db.Text)
    log_excerpt = db.Column(db.Text)
    error = db.Column(db.Text)


class CryptoDailySnapshot(db.Model):
    """Daily portfolio-value snapshot used as the day-start denominator for
    the 'Daily trading ROI — % on portfolio' chart. One row per MYT day.
    Populated at MYT-midnight rollover by update_day_start_and_check_halt;
    backfillable from CryptoRun.summary strings via the snapshot-backfill
    endpoint. See stock/webapp/models.py docstring for full field details."""
    __tablename__ = "crypto_daily_snapshots"
    date = db.Column(db.String(10), primary_key=True)
    total_value_usd = db.Column(db.Float, nullable=False)
    usdt_free = db.Column(db.Float)
    open_value_usd = db.Column(db.Float)
    deposits_during_day_usd = db.Column(db.Float)
    source = db.Column(db.String(16), default="rollover", nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
