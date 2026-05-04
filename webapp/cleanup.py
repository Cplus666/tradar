"""Database cleanup — runs daily via scheduler, prunes old run logs.

What gets pruned:
  - CryptoTrade (BUY/SELL)     : KEEP FOREVER (tax + journal)
  - CryptoRun summary/status   : KEEP FOREVER (small, useful for stats)
  - CryptoRun.log_excerpt       : cleared after 30 days (the bulk of data)
  - CryptoRun.error             : cleared after 90 days
  - CryptoRun rows entirely     : deleted after 2 years (aggressive mode only)

Caller must provide a Flask app context.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

log = logging.getLogger("cleanup")


def run_cleanup(apply: bool = True, aggressive: bool = False) -> dict:
    """Perform DB cleanup. Returns dict of counts touched."""
    from webapp.models import CryptoRun, db

    now = datetime.utcnow()
    cutoff_30d = now - timedelta(days=30)
    cutoff_90d = now - timedelta(days=90)
    cutoff_2y = now - timedelta(days=730)
    counts: dict[str, int] = {}

    # 1) Clear CryptoRun.log_excerpt for runs >30d old
    q = CryptoRun.query.filter(
        CryptoRun.started_at < cutoff_30d,
        CryptoRun.log_excerpt.isnot(None),
        CryptoRun.log_excerpt != "",
    )
    counts["crypto_run_logs_cleared"] = q.count()
    if apply and counts["crypto_run_logs_cleared"]:
        q.update({CryptoRun.log_excerpt: None}, synchronize_session=False)

    # 2) Clear CryptoRun.error for runs >90d old
    q = CryptoRun.query.filter(
        CryptoRun.started_at < cutoff_90d,
        CryptoRun.error.isnot(None),
        CryptoRun.error != "",
    )
    counts["crypto_run_errors_cleared"] = q.count()
    if apply and counts["crypto_run_errors_cleared"]:
        q.update({CryptoRun.error: None}, synchronize_session=False)

    # 3) Aggressive: delete CryptoRun rows >2y old
    if aggressive:
        q = CryptoRun.query.filter(CryptoRun.started_at < cutoff_2y)
        counts["crypto_runs_deleted"] = q.count()
        if apply and counts["crypto_runs_deleted"]:
            q.delete(synchronize_session=False)
    else:
        counts["crypto_runs_deleted"] = 0

    if apply:
        db.session.commit()
        try:
            db.session.execute(db.text("VACUUM"))
        except Exception as e:
            log.warning("VACUUM failed (non-fatal): %s", e)

    total_touched = sum(counts.values())
    if total_touched > 0:
        log.info("cleanup %s: %s",
                 "applied" if apply else "dry-run",
                 ", ".join(f"{k}={v}" for k, v in counts.items() if v))
    return counts
