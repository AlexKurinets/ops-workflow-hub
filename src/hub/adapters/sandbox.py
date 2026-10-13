from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from hub.db.models import SandboxRecord


class SandboxStore:
    """Postgres-backed stand-in for the SaaS applications the adapters talk to.

    Each application is a `system`, each of its resources a `collection`. Records carry the
    idempotency key they were created with, which is what lets an adapter answer "did this
    call already happen?" the way the real APIs do with their own idempotency headers or
    searchable external references.
    """

    @staticmethod
    def new_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:10]}"

    def get(self, session: Session, system: str, collection: str, record_id: str) -> dict | None:
        row = session.get(SandboxRecord, (system, collection, record_id))
        return dict(row.data) if row else None

    def put(
        self,
        session: Session,
        system: str,
        collection: str,
        record_id: str,
        data: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> dict:
        data = {**data, "id": record_id}
        row = session.get(SandboxRecord, (system, collection, record_id))
        if row is None:
            row = SandboxRecord(
                system=system,
                collection=collection,
                record_id=record_id,
                data=data,
                idempotency_key=idempotency_key,
            )
            session.add(row)
        else:
            row.data = data
            if idempotency_key:
                row.idempotency_key = idempotency_key
        session.flush()
        return dict(data)

    def find_by_key(self, session: Session, system: str, collection: str, key: str) -> dict | None:
        stmt = select(SandboxRecord).where(
            SandboxRecord.system == system,
            SandboxRecord.collection == collection,
            SandboxRecord.idempotency_key == key,
        )
        row = session.execute(stmt).scalars().first()
        return dict(row.data) if row else None

    def find(self, session: Session, system: str, collection: str, **equals: Any) -> list[dict]:
        stmt = (
            select(SandboxRecord)
            .where(SandboxRecord.system == system, SandboxRecord.collection == collection)
            .order_by(SandboxRecord.seq)
        )
        if equals:
            stmt = stmt.where(SandboxRecord.data.contains(equals))
        return [dict(r.data) for r in session.execute(stmt).scalars()]

    def list_after(
        self, session: Session, system: str, collection: str, seq_after: int, limit: int = 200
    ) -> list[tuple[int, dict]]:
        stmt = (
            select(SandboxRecord)
            .where(
                SandboxRecord.system == system,
                SandboxRecord.collection == collection,
                SandboxRecord.seq > seq_after,
            )
            .order_by(SandboxRecord.seq)
            .limit(limit)
        )
        return [(r.seq, dict(r.data)) for r in session.execute(stmt).scalars()]

    def count(self, session: Session, system: str, collection: str) -> int:
        return len(self.find(session, system, collection))
