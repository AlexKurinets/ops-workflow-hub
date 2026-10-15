from __future__ import annotations

import json
import signal
import time
import uuid
from typing import Any

import typer
from sqlalchemy import select

from hub import __version__
from hub.db import init_db, reset_db
from hub.db.models import EntityLink, Run, RunStatus, StepRun
from hub.engine.errors import HubError
from hub.engine.ingest import ingest
from hub.engine.runner import mark_effect_done, replay_run
from hub.runtime import Runtime, configure_logging
from hub.seed import seed as seed_data
from hub.simulate import Simulator

app = typer.Typer(
    help="Integration hub: run cross-application workflows reliably.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)


def _echo(obj: Any) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str))


@app.command()
def version() -> None:
    typer.echo(__version__)


@app.command("init-db")
def init_db_cmd() -> None:
    """Create the schema."""
    rt = Runtime.build()
    init_db(rt.settings.database_url)
    typer.echo("schema ready")


@app.command()
def seed(
    reset: bool = typer.Option(True, help="Truncate every table first."),
    if_empty: bool = typer.Option(False, help="Do nothing when the sandbox already has data."),
) -> None:
    """Load the synthetic data into the sandbox applications."""
    rt = Runtime.build()
    init_db(rt.settings.database_url)
    with rt.session_factory() as session:
        if if_empty and rt.adapters.store.count(session, "ats", "candidates"):
            typer.echo("sandbox already seeded")
            return
    if reset:
        reset_db(rt.settings.database_url)
    with rt.session_factory() as session:
        counts = seed_data(session, rt.adapters.store)
        session.commit()
    _echo(counts)


@app.command()
def validate() -> None:
    """Load every workflow file and check it against the action registry."""
    rt = Runtime.build()
    for wf in rt.catalog.defs.values():
        typer.echo(f"{wf.name} v{wf.version}: {wf.trigger.kind} trigger, {len(wf.steps)} steps")
    typer.echo("all workflows valid")


@app.command()
def actions() -> None:
    """List the actions a step can name."""
    rt = Runtime.build()
    for a in rt.adapters.registry.all():
        flags = []
        if a.side_effect:
            flags.append("side-effect")
        if a.verify is not None:
            flags.append("verifiable")
        if a.duplicate_tolerant:
            flags.append("duplicate-tolerant")
        typer.echo(f"{a.name:32s} {' '.join(flags)}")


@app.command()
def api(
    host: str = "0.0.0.0",
    port: int = 8000,
    reload: bool = False,
) -> None:
    """Serve webhooks, the runs API and the ops page."""
    import uvicorn

    configure_logging()
    uvicorn.run("hub.api.app:create_app", host=host, port=port, reload=reload, factory=True)


@app.command()
def worker(once: bool = typer.Option(False, help="Drain the queue and exit.")) -> None:
    """Claim and execute runs."""
    configure_logging()
    rt = Runtime.build()
    runner = rt.runner()
    if once:
        typer.echo(f"executed {runner.run_pending()} run(s)")
        return
    stop = {"now": False}

    def _stop(*_: Any) -> None:
        stop["now"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    typer.echo(f"worker {rt.settings.worker_id} started")
    while not stop["now"]:
        if runner.run_once() is None:
            time.sleep(rt.settings.worker_poll_seconds)
    typer.echo("worker stopped")


@app.command()
def scheduler(once: bool = typer.Option(False, help="Run one tick and exit.")) -> None:
    """Poll the applications without webhooks and fire cron triggers."""
    configure_logging()
    rt = Runtime.build()
    sched = rt.scheduler()
    if once:
        _echo(sched.tick(force=True))
        return
    stop = {"now": False}

    def _stop(*_: Any) -> None:
        stop["now"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    typer.echo("scheduler started")
    while not stop["now"]:
        sched.tick()
        time.sleep(rt.settings.scheduler_poll_seconds)
    typer.echo("scheduler stopped")


@app.command()
def runs(
    status: str | None = typer.Option(
        None, help="pending, running, waiting_retry, succeeded, dead_lettered"
    ),
    limit: int = 20,
) -> None:
    """List recent runs."""
    rt = Runtime.build()
    stmt = select(Run).order_by(Run.created_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(Run.status == RunStatus(status))
    with rt.session_factory() as session:
        for r in session.execute(stmt).scalars():
            step = f" at {r.current_step}" if r.current_step else ""
            reason = f"  {r.dead_letter_reason}" if r.dead_letter_reason else ""
            typer.echo(f"{r.id}  {r.workflow_name:32s} {r.status.value:14s}{step}{reason}")


@app.command()
def show(run_id: str) -> None:
    """Print a run's timeline."""
    rt = Runtime.build()
    with rt.session_factory() as session:
        run = session.get(Run, uuid.UUID(run_id))
        if run is None:
            raise typer.BadParameter("run not found")
        typer.echo(f"{run.workflow_name} v{run.workflow_version}  {run.status.value}")
        if run.dead_letter_reason:
            typer.echo(f"reason: {run.dead_letter_reason}")
        steps = session.execute(
            select(StepRun).where(StepRun.run_id == run.id).order_by(StepRun.seq)
        ).scalars()
        for s in steps:
            detail = s.error if s.error else json.dumps(s.output, default=str)[:120]
            typer.echo(
                f"  {s.started_at:%H:%M:%S}  {s.step_id:20s} #{s.attempt} "
                f"{s.status.value:10s} {detail}"
            )


@app.command()
def replay(
    run_id: str,
    skip_step: bool = typer.Option(
        False, help="Mark the failed step done by hand and resume after it."
    ),
    from_step: str | None = typer.Option(
        None, help="Restart at an earlier step, repeating lookups; side effects are reused."
    ),
) -> None:
    """Put a dead-lettered run back in the queue at the step that failed."""
    rt = Runtime.build()
    with rt.session_factory() as session:
        try:
            run = replay_run(session, run_id, rt.catalog, skip_step=skip_step, from_step=from_step)
        except HubError as exc:
            raise typer.BadParameter(str(exc)) from exc
        typer.echo(f"{run.id} is {run.status.value} at step {run.current_step}")


@app.command()
def trigger(workflow: str) -> None:
    """Start a scheduled workflow now instead of waiting for its cron time."""
    rt = Runtime.build()
    try:
        wf = rt.catalog.get(workflow)
    except HubError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if wf.trigger.schedule is None:
        raise typer.BadParameter(f"{workflow} is not a scheduled workflow")
    now = rt.clock.now()
    with rt.session_factory() as session:
        result = ingest(
            session,
            rt.catalog,
            "schedule",
            wf.name,
            f"manual:{now.isoformat()}",
            {"fired_at": now.isoformat(), "manual": True},
        )
        session.commit()
    _echo({"event_id": str(result.event_id), "runs": [str(r) for r in result.run_ids]})


@app.command("mark-effect-done")
def mark_effect_done_cmd(key: str) -> None:
    """Record that a side effect happened, after checking the application by hand."""
    rt = Runtime.build()
    with rt.session_factory() as session:
        try:
            effect = mark_effect_done(session, key)
        except HubError as exc:
            raise typer.BadParameter(str(exc)) from exc
        typer.echo(f"effect {effect.key} marked done")


@app.command()
def link(entity: str, ids: list[str]) -> None:
    """Link ids across applications, e.g. hub link person ats:cand_1006 payroll:wk_7006."""
    rt = Runtime.build()
    pairs: dict[str, str] = {}
    for item in ids:
        if ":" not in item:
            raise typer.BadParameter(f"{item} must look like system:id")
        system, external_id = item.split(":", 1)
        pairs[system] = external_id
    from hub.adapters.base import ActionContext

    with rt.session_factory() as session:
        ctx = ActionContext(session, rt.settings, rt.clock, rt.adapters.store, "cli", "link", None)
        result = rt.adapters["crosswalk"].link(ctx, entity, pairs)  # type: ignore[attr-defined]
        session.commit()
    _echo(result)


@app.command()
def links(entity: str = "person") -> None:
    """Show the crosswalk."""
    rt = Runtime.build()
    with rt.session_factory() as session:
        rows = session.execute(
            select(EntityLink)
            .where(EntityLink.entity_type == entity)
            .order_by(EntityLink.canonical_id)
        ).scalars()
        for r in rows:
            typer.echo(f"{r.canonical_id}  {r.system:10s} {r.external_id}")


@app.command()
def fault(action: str, kind: str, count: int = typer.Argument(1)) -> None:
    """Arm an injected failure: kind is transient, permanent or after (response lost)."""
    rt = Runtime.build()
    if action not in rt.adapters.registry:
        raise typer.BadParameter(f"unknown action {action}")
    with rt.session_factory() as session:
        rec = rt.adapters.faults.arm(session, action, kind, count)
        session.commit()
    _echo(rec)


@app.command()
def simulate(
    scenario: str = typer.Argument(
        ..., help="placement, duplicate, onboarding-signed, timesheet, lead, payroll-worker"
    ),
    api_url: str | None = typer.Option(
        None, help="Deliver through the API; omit to ingest in-process."
    ),
    mismatch: bool = typer.Option(False, help="timesheet: hours disagree with the placement"),
    unlinked: bool = typer.Option(False, help="timesheet: pick a candidate with no payroll worker"),
    existing: bool = typer.Option(False, help="lead: use a company already in the CRM"),
    candidate: str | None = typer.Option(
        None, help="payroll-worker: ATS candidate id; default is the first unlinked one"
    ),
) -> None:
    """Emit a synthetic event the way the real application would."""
    rt = Runtime.build()
    sim = Simulator(rt, api_url)
    try:
        if scenario == "placement":
            _echo(sim.placement())
        elif scenario == "duplicate":
            _echo(sim.duplicate_placement())
        elif scenario == "onboarding-signed":
            _echo(sim.onboarding_signed())
        elif scenario == "timesheet":
            _echo(sim.timesheet(mismatch=mismatch, unlinked=unlinked))
        elif scenario == "lead":
            _echo(sim.lead(existing_company=existing))
        elif scenario == "payroll-worker":
            _echo(sim.payroll_worker(candidate))
        else:
            raise typer.BadParameter(f"unknown scenario {scenario}")
    except HubError as exc:
        raise typer.BadParameter(str(exc)) from exc


if __name__ == "__main__":
    app()
