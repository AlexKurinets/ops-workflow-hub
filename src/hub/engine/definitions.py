from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from hub.engine.errors import DefinitionError
from hub.engine.templating import Templating

_IDENT = re.compile(r"^[a-z][a-z0-9_]*$")


class RetrySpec(BaseModel):
    max_attempts: int = Field(default=5, ge=1, le=20)
    base_seconds: float = Field(default=2.0, gt=0)
    max_seconds: float = Field(default=300.0, gt=0)


class StepDef(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    id: str = Field(pattern=_IDENT.pattern)
    action: str
    description: str | None = None
    with_: dict[str, Any] = Field(default_factory=dict, alias="with")
    when: str | None = None
    idempotency_key: str | None = None
    retry: RetrySpec | None = None


class EventTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    type: str


class PollTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    collection: str
    type: str
    interval_seconds: int = Field(default=60, ge=1)


class ScheduleTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cron: str
    timezone: str = "UTC"


class Trigger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: EventTrigger | None = None
    poll: PollTrigger | None = None
    schedule: ScheduleTrigger | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Trigger:
        kinds = [k for k in ("event", "poll", "schedule") if getattr(self, k) is not None]
        if len(kinds) != 1:
            raise ValueError("trigger must have exactly one of: event, poll, schedule")
        return self

    @property
    def kind(self) -> str:
        return "event" if self.event else "poll" if self.poll else "schedule"


class WorkflowDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=_IDENT.pattern)
    version: int = Field(ge=1)
    description: str | None = None
    trigger: Trigger
    steps: list[StepDef] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_steps(self) -> WorkflowDef:
        seen: set[str] = set()
        for s in self.steps:
            if s.id in seen:
                raise ValueError(f"duplicate step id {s.id}")
            seen.add(s.id)
        return self

    def step(self, step_id: str) -> StepDef:
        for s in self.steps:
            if s.id == step_id:
                return s
        raise KeyError(step_id)

    def step_index(self, step_id: str | None) -> int:
        if step_id is None:
            return 0
        for i, s in enumerate(self.steps):
            if s.id == step_id:
                return i
        raise KeyError(step_id)


def load_workflow(path: Path) -> WorkflowDef:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise DefinitionError(f"{path.name}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise DefinitionError(f"{path.name}: top level must be a mapping")
    try:
        return WorkflowDef.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first.get("loc", ()))
        raise DefinitionError(f"{path.name}: {loc}: {first['msg']}") from exc


class Catalog:
    """Every workflow definition, validated against the action registry at load time."""

    def __init__(self, defs: dict[str, WorkflowDef]) -> None:
        self.defs = defs

    @classmethod
    def load(cls, directory: Path, action_names: set[str] | None = None) -> Catalog:
        defs: dict[str, WorkflowDef] = {}
        for path in sorted(directory.glob("*.yaml")):
            wf = load_workflow(path)
            if wf.name in defs:
                raise DefinitionError(f"{path.name}: workflow {wf.name} is defined twice")
            defs[wf.name] = wf
        catalog = cls(defs)
        catalog.validate(action_names)
        return catalog

    def validate(self, action_names: set[str] | None) -> None:
        t = Templating()
        for wf in self.defs.values():
            for s in wf.steps:
                where = f"{wf.name}.{s.id}"
                if action_names is not None and s.action not in action_names:
                    raise DefinitionError(f"{where}: unknown action {s.action}")
                t.validate(s.with_, where)
                if s.when is not None:
                    t.validate_expression(s.when, where)
                if s.idempotency_key is not None:
                    t.validate(s.idempotency_key, where)

    def get(self, name: str) -> WorkflowDef:
        try:
            return self.defs[name]
        except KeyError as exc:
            raise DefinitionError(f"unknown workflow {name}") from exc

    def for_event(self, source: str, event_type: str) -> list[WorkflowDef]:
        out = []
        for wf in self.defs.values():
            tr = wf.trigger
            if tr.event and tr.event.source == source and tr.event.type == event_type:
                out.append(wf)
            elif tr.poll and tr.poll.source == source and tr.poll.type == event_type:
                out.append(wf)
            elif tr.schedule and source == "schedule" and event_type == wf.name:
                out.append(wf)
        return out

    def polls(self) -> list[WorkflowDef]:
        return [wf for wf in self.defs.values() if wf.trigger.poll]

    def schedules(self) -> list[WorkflowDef]:
        return [wf for wf in self.defs.values() if wf.trigger.schedule]
