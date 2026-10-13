from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class RunStatus(enum.StrEnum):
    pending = "pending"
    running = "running"
    waiting_retry = "waiting_retry"
    succeeded = "succeeded"
    dead_lettered = "dead_lettered"
    cancelled = "cancelled"


class StepStatus(enum.StrEnum):
    succeeded = "succeeded"
    failed = "failed"
    skipped = "skipped"
    reused = "reused"


class EffectStatus(enum.StrEnum):
    pending = "pending"
    done = "done"


class Event(Base):
    """One inbound event. The unique key is what makes redelivered webhooks harmless."""

    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("source", "event_type", "external_id", name="uq_event"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    source: Mapped[str] = mapped_column(String(40))
    event_type: Mapped[str] = mapped_column(String(80))
    external_id: Mapped[str] = mapped_column(String(200))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    runs: Mapped[list[Run]] = relationship(back_populates="event")


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        Index("ix_runs_claim", "status", "next_attempt_at", "created_at"),
        Index("ix_runs_workflow", "workflow_name", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    workflow_name: Mapped[str] = mapped_column(String(80))
    workflow_version: Mapped[int] = mapped_column(Integer)
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id"))
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, name="run_status"), default=RunStatus.pending
    )
    current_step: Mapped[str | None] = mapped_column(String(80))
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    dead_letter_reason: Mapped[str | None] = mapped_column(Text)
    replay_count: Mapped[int] = mapped_column(Integer, default=0)
    worker_id: Mapped[str | None] = mapped_column(String(80))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    event: Mapped[Event] = relationship(back_populates="runs")
    steps: Mapped[list[StepRun]] = relationship(
        back_populates="run",
        order_by="StepRun.started_at, StepRun.seq",
        cascade="all, delete-orphan",
    )


class StepRun(Base):
    """One attempt at one step. The timeline of a run is this table ordered by time."""

    __tablename__ = "step_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    seq: Mapped[int] = mapped_column(Integer, Identity(), unique=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    step_id: Mapped[str] = mapped_column(String(80))
    attempt: Mapped[int] = mapped_column(Integer)
    status: Mapped[StepStatus] = mapped_column(Enum(StepStatus, name="step_status"))
    action: Mapped[str] = mapped_column(String(80))
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    run: Mapped[Run] = relationship(back_populates="steps")


class Effect(Base):
    """A side effect that must happen at most once, keyed before the call is made."""

    __tablename__ = "effects"

    key: Mapped[str] = mapped_column(String(300), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    step_id: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(80))
    status: Mapped[EffectStatus] = mapped_column(
        Enum(EffectStatus, name="effect_status"), default=EffectStatus.pending
    )
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EntityLink(Base):
    """Crosswalk: the same person or company under each application's own identifier."""

    __tablename__ = "entity_links"
    __table_args__ = (
        UniqueConstraint("entity_type", "system", "external_id", name="uq_entity_link"),
        Index("ix_entity_links_canonical", "entity_type", "canonical_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(40))
    system: Mapped[str] = mapped_column(String(40))
    external_id: Mapped[str] = mapped_column(String(200))
    canonical_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PollCursor(Base):
    __tablename__ = "poll_cursors"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    cursor: Mapped[str | None] = mapped_column(String(200))
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ScheduleState(Base):
    __tablename__ = "schedule_state"

    workflow_name: Mapped[str] = mapped_column(String(80), primary_key=True)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SandboxRecord(Base):
    """Backing store for the sandbox adapters that stand in for the SaaS applications."""

    __tablename__ = "sandbox_records"
    __table_args__ = (
        Index("ix_sandbox_key", "system", "collection", "idempotency_key"),
        Index("ix_sandbox_seq", "system", "collection", "seq"),
    )

    system: Mapped[str] = mapped_column(String(40), primary_key=True)
    collection: Mapped[str] = mapped_column(String(60), primary_key=True)
    record_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, Identity(), unique=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    idempotency_key: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
