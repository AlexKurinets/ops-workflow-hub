from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from hub import __version__
from hub.adapters.faults import KINDS
from hub.adapters.webhooks import KNOWN_SOURCES, extract
from hub.db.models import Event, Run, RunStatus, StepRun
from hub.engine.errors import HubError, PermanentError
from hub.engine.ingest import ingest
from hub.engine.runner import replay_run
from hub.runtime import Runtime

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
SIGNATURE_HEADER = "X-Hub-Signature"


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _run_dict(run: Run, steps: list[StepRun] | None = None, event: Event | None = None) -> dict:
    out: dict[str, Any] = {
        "id": str(run.id),
        "workflow": run.workflow_name,
        "version": run.workflow_version,
        "status": run.status.value,
        "current_step": run.current_step,
        "attempt": run.attempt,
        "next_attempt_at": run.next_attempt_at.isoformat() if run.next_attempt_at else None,
        "dead_letter_reason": run.dead_letter_reason,
        "replay_count": run.replay_count,
        "worker_id": run.worker_id,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }
    if steps is not None:
        out["timeline"] = [
            {
                "step": s.step_id,
                "action": s.action,
                "attempt": s.attempt,
                "status": s.status.value,
                "output": s.output,
                "error": s.error,
                "started_at": s.started_at.isoformat(),
                "duration_ms": s.duration_ms,
            }
            for s in steps
        ]
        out["context"] = run.context
    if event is not None:
        out["event"] = {
            "id": str(event.id),
            "source": event.source,
            "type": event.event_type,
            "external_id": event.external_id,
            "data": (event.payload or {}).get("data"),
            "received_at": event.received_at.isoformat() if event.received_at else None,
        }
    return out


class FaultRequest(BaseModel):
    action: str
    kind: str = Field(pattern="^(" + "|".join(KINDS) + ")$")
    count: int = Field(default=1, ge=1, le=100)


def create_app(runtime: Runtime | None = None) -> FastAPI:
    rt = runtime or Runtime.build()
    app = FastAPI(title="ops-workflow-hub", version=__version__)
    app.state.runtime = rt

    def _parse_run_id(run_id: str) -> uuid.UUID:
        try:
            return uuid.UUID(run_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc

    def _load_run(session, run_id: str) -> tuple[Run, list[StepRun], Event]:
        run = session.get(Run, _parse_run_id(run_id))
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        steps = (
            session.execute(select(StepRun).where(StepRun.run_id == run.id).order_by(StepRun.seq))
            .scalars()
            .all()
        )
        event = session.get(Event, run.event_id)
        return run, list(steps), event

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__, "workflows": len(rt.catalog.defs)}

    @app.post("/webhooks/{source}", status_code=202)
    async def webhook(source: str, request: Request) -> dict[str, Any]:
        if source not in KNOWN_SOURCES:
            raise HTTPException(status_code=404, detail=f"unknown source {source}")
        body = await request.body()
        expected = sign(rt.settings.webhook_secret_for(source), body)
        given = request.headers.get(SIGNATURE_HEADER, "")
        if not hmac.compare_digest(given, expected):
            raise HTTPException(status_code=401, detail="bad signature")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="body is not JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="body must be a JSON object")
        try:
            event_type, external_id, data = extract(source, payload)
        except PermanentError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        with rt.session_factory() as session:
            result = ingest(session, rt.catalog, source, event_type, external_id, data, raw=payload)
            session.commit()
        return {
            "event_id": str(result.event_id),
            "duplicate": result.duplicate,
            "runs": [str(r) for r in result.run_ids],
        }

    @app.get("/workflows")
    def workflows() -> list[dict[str, Any]]:
        return [
            {
                "name": wf.name,
                "version": wf.version,
                "trigger": wf.trigger.model_dump(exclude_none=True),
                "steps": [s.id for s in wf.steps],
                "description": (wf.description or "").strip(),
            }
            for wf in rt.catalog.defs.values()
        ]

    @app.get("/actions")
    def actions() -> list[dict[str, Any]]:
        return [
            {
                "name": a.name,
                "side_effect": a.side_effect,
                "verifiable": a.verify is not None,
                "duplicate_tolerant": a.duplicate_tolerant,
                "description": a.description,
            }
            for a in rt.adapters.registry.all()
        ]

    @app.get("/runs")
    def list_runs(
        status: RunStatus | None = None,
        workflow: str | None = None,
        limit: int = Query(default=50, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        stmt = select(Run).order_by(Run.created_at.desc()).limit(limit)
        if status is not None:
            stmt = stmt.where(Run.status == status)
        if workflow is not None:
            stmt = stmt.where(Run.workflow_name == workflow)
        with rt.session_factory() as session:
            return [_run_dict(r) for r in session.execute(stmt).scalars()]

    @app.get("/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        with rt.session_factory() as session:
            run, steps, event = _load_run(session, run_id)
            return _run_dict(run, steps, event)

    @app.post("/runs/{run_id}/replay")
    def replay(
        run_id: str, skip_step: bool = False, from_step: str | None = None
    ) -> dict[str, Any]:
        with rt.session_factory() as session:
            try:
                run = replay_run(
                    session,
                    _parse_run_id(run_id),
                    rt.catalog,
                    skip_step=skip_step,
                    from_step=from_step,
                )
            except HubError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return _run_dict(run)

    @app.get("/stats")
    def stats(hours: int = Query(default=24, ge=1, le=24 * 30)) -> dict[str, Any]:
        since = rt.clock.now() - timedelta(hours=hours)
        with rt.session_factory() as session:
            rows = session.execute(
                select(Run.status, func.count()).where(Run.created_at >= since).group_by(Run.status)
            ).all()
            by_status = {s.value: 0 for s in RunStatus}
            for status, count in rows:
                by_status[status.value] = count
            total = sum(by_status.values())
            finished = by_status["succeeded"] + by_status["dead_lettered"]
            return {
                "hours": hours,
                "total": total,
                "by_status": by_status,
                "unattended_rate": (by_status["succeeded"] / finished) if finished else None,
            }

    @app.get("/sandbox/{system}/{collection}")
    def sandbox(system: str, collection: str) -> list[dict[str, Any]]:
        with rt.session_factory() as session:
            return rt.adapters.store.find(session, system, collection)

    @app.post("/faults", status_code=201)
    def arm_fault(body: FaultRequest) -> dict[str, Any]:
        if body.action not in rt.adapters.registry:
            raise HTTPException(status_code=404, detail=f"unknown action {body.action}")
        with rt.session_factory() as session:
            rec = rt.adapters.faults.arm(session, body.action, body.kind, body.count)
            session.commit()
            return rec

    @app.get("/ops", response_class=HTMLResponse)
    def ops(request: Request, status: RunStatus | None = None) -> HTMLResponse:
        stmt = select(Run).order_by(Run.created_at.desc()).limit(100)
        if status is not None:
            stmt = stmt.where(Run.status == status)
        with rt.session_factory() as session:
            runs = [_run_dict(r) for r in session.execute(stmt).scalars()]
            counts = dict(
                session.execute(select(Run.status, func.count()).group_by(Run.status)).all()
            )
        return TEMPLATES.TemplateResponse(
            request,
            "ops.html",
            {
                "runs": runs,
                "counts": {s.value: counts.get(s, 0) for s in RunStatus},
                "selected": status.value if status else None,
                "statuses": [s.value for s in RunStatus],
            },
        )

    @app.get("/ops/runs/{run_id}", response_class=HTMLResponse)
    def ops_run(request: Request, run_id: str) -> HTMLResponse:
        with rt.session_factory() as session:
            run, steps, event = _load_run(session, run_id)
            data = _run_dict(run, steps, event)
        return TEMPLATES.TemplateResponse(request, "run.html", {"run": data})

    return app
