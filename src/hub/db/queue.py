from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from hub.db.models import Run, RunStatus


def claim_run(session: Session, worker_id: str, now: datetime) -> Run | None:
    """Claim one due run with FOR UPDATE SKIP LOCKED so concurrent workers never share a run.

    The caller owns the transaction: the row stays locked until commit, and the status
    change to `running` is what other workers see once it lands.
    """
    stmt = (
        select(Run)
        .where(Run.status.in_([RunStatus.pending, RunStatus.waiting_retry]))
        .where(or_(Run.next_attempt_at.is_(None), Run.next_attempt_at <= now))
        .order_by(Run.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    run = session.execute(stmt).scalar_one_or_none()
    if run is None:
        return None
    run.status = RunStatus.running
    run.worker_id = worker_id
    run.locked_at = now
    session.flush()
    return run


def get_run(session: Session, run_id: uuid.UUID | str) -> Run | None:
    if isinstance(run_id, str):
        run_id = uuid.UUID(run_id)
    return session.get(Run, run_id)
