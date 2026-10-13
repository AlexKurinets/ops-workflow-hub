from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from hub.db.models import Event, Run, RunStatus
from hub.engine.definitions import Catalog


@dataclass
class IngestResult:
    event_id: uuid.UUID
    duplicate: bool
    run_ids: list[uuid.UUID] = field(default_factory=list)


def ingest(
    session: Session,
    catalog: Catalog,
    source: str,
    event_type: str,
    external_id: str,
    data: dict[str, Any],
    raw: dict[str, Any] | None = None,
) -> IngestResult:
    """Store an event once and start a run for every workflow that listens for it.

    The insert is ON CONFLICT DO NOTHING on (source, event_type, external_id), so a webhook
    the application redelivers, or a poller that overlaps its previous window, produces no
    second run. The event and its runs land in the caller's transaction, which is the
    outbox: either the event is recorded with its runs queued, or nothing is.
    """
    stmt = (
        insert(Event)
        .values(
            source=source,
            event_type=event_type,
            external_id=str(external_id),
            payload={"data": data, "raw": raw},
        )
        .on_conflict_do_nothing(constraint="uq_event")
        .returning(Event.id)
    )
    event_id = session.execute(stmt).scalar_one_or_none()
    if event_id is None:
        existing = session.execute(
            select(Event.id).where(
                Event.source == source,
                Event.event_type == event_type,
                Event.external_id == str(external_id),
            )
        ).scalar_one()
        return IngestResult(event_id=existing, duplicate=True)

    runs: list[Run] = []
    for wf in catalog.for_event(source, event_type):
        run = Run(
            workflow_name=wf.name,
            workflow_version=wf.version,
            event_id=event_id,
            status=RunStatus.pending,
            context={"steps": {}},
        )
        session.add(run)
        runs.append(run)
    session.flush()
    return IngestResult(event_id=event_id, duplicate=False, run_ids=[r.id for r in runs])
