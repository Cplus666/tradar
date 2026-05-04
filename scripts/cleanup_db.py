"""Database cleanup — prune old logs, keep what matters.

Strategy (honest, conservative):
  - CryptoTrade (BUY/SELL records) — KEEP FOREVER (tax + journal)
  - CryptoRun summary/status — KEEP FOREVER (small, useful for stats)
  - CryptoRun.log_excerpt — clear for runs older than 30 days
  - CryptoRun.error — clear for runs older than 90 days
  - Old CryptoRun rows entirely — delete after 2 years (still keep trades)
  - Stock-side Recommendation — keep 1 year (rare anyway)

Runs in dry-run by default. Pass --apply to actually delete.

Usage:
    python scripts/cleanup_db.py            # dry-run, shows what would be deleted
    python scripts/cleanup_db.py --apply    # actually do it
    python scripts/cleanup_db.py --apply --aggressive   # also delete 2yr+ runs entirely

Recommended schedule: monthly via cron.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Allow running as a script from anywhere (e.g. `python scripts/cleanup_db.py` or via cron)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webapp import create_app
from webapp.models import CryptoRun, CryptoTrade, Recommendation, Run, db


def cleanup(apply: bool, aggressive: bool) -> None:
    now = datetime.utcnow()

    # 1) Clear log_excerpt for runs older than 30 days
    cutoff_30d = now - timedelta(days=30)
    q1 = CryptoRun.query.filter(
        CryptoRun.started_at < cutoff_30d,
        CryptoRun.log_excerpt.isnot(None),
        CryptoRun.log_excerpt != "",
    )
    n1 = q1.count()
    print(f"  CryptoRun log_excerpts to clear (>30d old): {n1}")
    if apply and n1:
        q1.update({CryptoRun.log_excerpt: None}, synchronize_session=False)

    # 2) Clear error for runs older than 90 days
    cutoff_90d = now - timedelta(days=90)
    q2 = CryptoRun.query.filter(
        CryptoRun.started_at < cutoff_90d,
        CryptoRun.error.isnot(None),
        CryptoRun.error != "",
    )
    n2 = q2.count()
    print(f"  CryptoRun errors to clear (>90d old):       {n2}")
    if apply and n2:
        q2.update({CryptoRun.error: None}, synchronize_session=False)

    # 3) Aggressive: delete CryptoRun rows entirely after 2 years
    if aggressive:
        cutoff_2y = now - timedelta(days=730)
        q3 = CryptoRun.query.filter(CryptoRun.started_at < cutoff_2y)
        n3 = q3.count()
        print(f"  CryptoRun rows to DELETE (>2yr old):        {n3}")
        if apply and n3:
            q3.delete(synchronize_session=False)

    # 4) Stock-side Recommendation older than 1 year
    cutoff_1y = now - timedelta(days=365)
    q4 = Recommendation.query.filter(Recommendation.created_at < cutoff_1y)
    n4 = q4.count()
    print(f"  Recommendation (stock) to delete (>1y old): {n4}")
    if apply and n4:
        q4.delete(synchronize_session=False)

    # 5) Stock-side Run.log_excerpt older than 90 days
    q5 = Run.query.filter(
        Run.started_at < cutoff_90d,
        Run.log_excerpt.isnot(None),
    )
    n5 = q5.count()
    print(f"  Run (stock) log_excerpts to clear (>90d):   {n5}")
    if apply and n5:
        q5.update({Run.log_excerpt: None}, synchronize_session=False)

    if apply:
        db.session.commit()
        # Reclaim disk space (SQLite VACUUM)
        db.session.execute(db.text("VACUUM"))
        print("\n  ✓ Changes committed + VACUUM ran (disk space reclaimed)")
    else:
        print("\n  (dry-run only — pass --apply to actually delete)")


def show_db_size() -> None:
    """Show current DB size + row counts for context."""
    import os
    db_path = "data/app.db"
    if os.path.exists(db_path):
        size_mb = os.path.getsize(db_path) / 1024 / 1024
        print(f"DB size: {size_mb:.1f} MB ({db_path})")
    print(f"  CryptoRun rows:   {CryptoRun.query.count():,}")
    print(f"  CryptoTrade rows: {CryptoTrade.query.count():,}")
    print(f"  Recommendation:   {Recommendation.query.count():,}")
    print(f"  Run (stock):      {Run.query.count():,}")
    print()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="Actually perform cleanup (default: dry-run)")
    p.add_argument("--aggressive", action="store_true", help="Also delete CryptoRun rows >2 years old")
    args = p.parse_args()

    app = create_app()
    with app.app_context():
        print("=== DB state BEFORE ===")
        show_db_size()
        print("=== Cleanup actions ===")
        cleanup(apply=args.apply, aggressive=args.aggressive)
        if args.apply:
            print()
            print("=== DB state AFTER ===")
            show_db_size()
    return 0


if __name__ == "__main__":
    sys.exit(main())
