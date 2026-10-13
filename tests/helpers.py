from __future__ import annotations

import uuid

from sqlalchemy import select

from hub.db.models import Effect, Run, StepRun
from hub.runtime import Runtime


def get_run(rt: Runtime, run_id: str | uuid.UUID) -> Run:
    with rt.session_factory() as session:
        run = session.get(Run, uuid.UUID(str(run_id)))
        assert run is not None
        return run


def step_runs(rt: Runtime, run_id: str | uuid.UUID, step_id: str | None = None) -> list[StepRun]:
    with rt.session_factory() as session:
        stmt = select(StepRun).where(StepRun.run_id == uuid.UUID(str(run_id))).order_by(StepRun.seq)
        if step_id:
            stmt = stmt.where(StepRun.step_id == step_id)
        return list(session.execute(stmt).scalars())


def effects(rt: Runtime) -> list[Effect]:
    with rt.session_factory() as session:
        return list(session.execute(select(Effect)).scalars())


def records(rt: Runtime, system: str, collection: str, **equals) -> list[dict]:
    with rt.session_factory() as session:
        return rt.adapters.store.find(session, system, collection, **equals)


def statuses(rt: Runtime, run_id: str | uuid.UUID) -> list[tuple[str, int, str]]:
    return [(s.step_id, s.attempt, s.status.value) for s in step_runs(rt, run_id)]
