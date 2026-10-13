from __future__ import annotations

import logging
import random
import traceback
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from hub.adapters import Adapters
from hub.adapters.base import Action, ActionContext
from hub.clock import Clock
from hub.config import Settings
from hub.db.models import Effect, EffectStatus, Event, Run, RunStatus, StepRun, StepStatus
from hub.db.queue import claim_run
from hub.engine.definitions import Catalog, StepDef, WorkflowDef
from hub.engine.errors import HubError, PermanentError, TransientError
from hub.engine.retry import RetryPolicy
from hub.engine.templating import Templating

log = logging.getLogger("hub.runner")

STOPPED = "stopped"


class Runner:
    """Executes runs step by step, checkpointing after every step.

    A run's position is `current_step` plus the outputs saved in `context.steps`, so a worker
    that dies mid-run leaves a row another worker can resume from. Side effects go through
    the effects ledger (see `_run_effect`), which is what makes resuming safe.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        catalog: Catalog,
        adapters: Adapters,
        settings: Settings,
        clock: Clock | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.catalog = catalog
        self.adapters = adapters
        self.settings = settings
        self.clock = clock or Clock()
        self.rng = rng or random.Random()
        self.templating = Templating()

    # ----- claiming -----------------------------------------------------------------------

    def run_once(self) -> Run | None:
        """Claim one due run and drive it until it succeeds, waits for a retry or dead-letters."""
        with self.session_factory() as session:
            run = claim_run(session, self.settings.worker_id, self.clock.now())
            if run is None:
                session.rollback()
                return None
            session.commit()
            self.execute(session, run)
            return run

    def run_pending(self, limit: int = 1000) -> int:
        """Drain every due run. Tests and the simulate command use this instead of a loop."""
        done = 0
        while done < limit and self.run_once() is not None:
            done += 1
        return done

    # ----- execution ----------------------------------------------------------------------

    def execute(self, session: Session, run: Run) -> None:
        wf = self.catalog.get(run.workflow_name)
        event = session.get(Event, run.event_id)
        assert event is not None
        start = wf.step_index(run.current_step)
        for step in wf.steps[start:]:
            run.current_step = step.id
            context = self._context(run, event)
            if self._execute_step(session, run, wf, step, context) == STOPPED:
                return
        run.status = RunStatus.succeeded
        run.current_step = None
        run.finished_at = self.clock.now()
        session.commit()
        log.info("run %s %s succeeded", run.workflow_name, run.id)

    def _context(self, run: Run, event: Event) -> dict[str, Any]:
        payload = event.payload or {}
        return {
            "event": {
                "id": str(event.id),
                "source": event.source,
                "type": event.event_type,
                "external_id": event.external_id,
                "data": payload.get("data", {}),
                "raw": payload.get("raw"),
            },
            "steps": dict(run.context.get("steps", {})),
            "run": {
                "id": str(run.id),
                "workflow": run.workflow_name,
                "attempt": run.attempt,
                "replay_count": run.replay_count,
            },
            "now": self.clock.now().isoformat(),
        }

    def _execute_step(
        self, session: Session, run: Run, wf: WorkflowDef, step: StepDef, context: dict[str, Any]
    ) -> str | None:
        started = self.clock.now()
        failures = run.attempt + 1
        try:
            if step.when is not None and not self.templating.evaluate(step.when, context):
                self._save_step(session, run, step, StepStatus.skipped, {"skipped": True}, started)
                return None
            args = self.templating.render(step.with_, context)
            if not isinstance(args, dict):
                raise PermanentError(f"step {step.id}: with must render to a mapping")
            key = (
                str(self.templating.render(step.idempotency_key, context))
                if step.idempotency_key
                else f"{run.id}:{step.id}"
            )
            action = self.adapters.registry.get(step.action)
            ctx = ActionContext(
                session=session,
                settings=self.settings,
                clock=self.clock,
                store=self.adapters.store,
                run_id=str(run.id),
                step_id=step.id,
                idempotency_key=key,
            )
            if action.side_effect:
                output, reused = self._run_effect(session, run, step, action, ctx, args, key)
            else:
                output, reused = action.fn(ctx, **args), False
            status = StepStatus.reused if reused else StepStatus.succeeded
            self._save_step(session, run, step, status, output, started)
            return None
        except TransientError as exc:
            policy = RetryPolicy(**step.retry.model_dump()) if step.retry else RetryPolicy()
            self._record_failure(session, run, step, started, failures, str(exc))
            if policy.exhausted(failures):
                self._dead_letter(session, run, f"step {step.id} failed {failures} times: {exc}")
            else:
                delay = policy.delay_for(failures, self.rng)
                run.attempt = failures
                run.status = RunStatus.waiting_retry
                run.next_attempt_at = self.clock.now() + timedelta(seconds=delay)
                log.warning("run %s step %s retry in %.1fs: %s", run.id, step.id, delay, exc)
            session.commit()
            return STOPPED
        except HubError as exc:
            self._record_failure(session, run, step, started, failures, str(exc))
            self._dead_letter(session, run, f"step {step.id}: {exc}")
            session.commit()
            return STOPPED
        except Exception as exc:  # noqa: BLE001 - a bug must park the run, not crash the worker
            session.rollback()
            detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            log.exception("run %s step %s crashed", run.id, step.id)
            self._record_failure(session, run, step, started, failures, detail)
            self._dead_letter(session, run, f"step {step.id} crashed: {detail}")
            session.commit()
            return STOPPED

    def _run_effect(
        self,
        session: Session,
        run: Run,
        step: StepDef,
        action: Action,
        ctx: ActionContext,
        args: dict[str, Any],
        key: str,
    ) -> tuple[dict[str, Any], bool]:
        """Perform a side effect at most once.

        The ledger row is committed *before* the call. A retry that finds the row `done`
        reuses its output. One that finds it `pending` knows an earlier attempt may have
        reached the application: it asks the adapter to verify by key, repeats the call only
        if the adapter can say it never happened or the action tolerates duplicates, and
        otherwise parks the run for a person.
        """
        effect = session.get(Effect, key)
        if effect is None:
            effect = Effect(
                key=key,
                run_id=run.id,
                step_id=step.id,
                action=action.name,
                status=EffectStatus.pending,
            )
            session.add(effect)
            session.commit()
            output = action.fn(ctx, **args)
            self._complete_effect(effect, output)
            return output, False

        if effect.status == EffectStatus.done:
            return dict(effect.output or {}), True

        if action.verify is not None:
            found = action.verify(ctx, key)
            if found is not None:
                self._complete_effect(effect, found)
                return found, True
            output = action.fn(ctx, **args)
            self._complete_effect(effect, output)
            return output, False

        if action.duplicate_tolerant:
            output = action.fn(ctx, **args)
            self._complete_effect(effect, output)
            return output, False

        raise PermanentError(
            f"an earlier attempt of {action.name} may have reached the application and the "
            f"result cannot be verified (effect {key}); check it, then replay with --skip-step "
            f"or mark the effect done"
        )

    def _complete_effect(self, effect: Effect, output: dict[str, Any]) -> None:
        effect.status = EffectStatus.done
        effect.output = output
        effect.completed_at = self.clock.now()

    # ----- bookkeeping --------------------------------------------------------------------

    def _save_step(
        self, session: Session, run: Run, step: StepDef, status: StepStatus, output: Any, started
    ) -> None:
        finished = self.clock.now()
        session.add(
            StepRun(
                run_id=run.id,
                step_id=step.id,
                attempt=run.attempt + 1,
                status=status,
                action=step.action,
                output=output if isinstance(output, dict) else {"value": output},
                started_at=started,
                finished_at=finished,
                duration_ms=int((finished - started).total_seconds() * 1000),
            )
        )
        steps = dict(run.context.get("steps", {}))
        steps[step.id] = output
        run.context = {**run.context, "steps": steps}
        run.attempt = 0
        session.commit()

    def _record_failure(
        self, session: Session, run: Run, step: StepDef, started, attempt: int, error: str
    ) -> None:
        finished = self.clock.now()
        session.add(
            StepRun(
                run_id=run.id,
                step_id=step.id,
                attempt=attempt,
                status=StepStatus.failed,
                action=step.action,
                error=error,
                started_at=started,
                finished_at=finished,
                duration_ms=int((finished - started).total_seconds() * 1000),
            )
        )

    def _dead_letter(self, session: Session, run: Run, reason: str) -> None:
        run.status = RunStatus.dead_lettered
        run.dead_letter_reason = reason
        run.next_attempt_at = None
        run.finished_at = self.clock.now()
        log.error("run %s %s dead-lettered: %s", run.workflow_name, run.id, reason)
        self._alert(session, run, reason)

    def _alert(self, session: Session, run: Run, reason: str) -> None:
        """Tell the ops channel. Best effort: an alerting failure must not hide the run."""
        url = f"{self.settings.public_base_url}/ops/runs/{run.id}"
        text = f"Run {run.workflow_name} {run.id} needs attention: {reason}\nTimeline: {url}"
        ctx = ActionContext(
            session=session,
            settings=self.settings,
            clock=self.clock,
            store=self.adapters.store,
            run_id=str(run.id),
            step_id="alert",
            idempotency_key=f"alert:{run.id}:{run.replay_count}",
        )
        try:
            slack = self.adapters["slack"]
            slack.post(ctx, self.settings.slack_alert_channel, text)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            log.exception("could not send dead-letter alert for run %s", run.id)


# ----- operator actions -----------------------------------------------------------------


def replay_run(
    session: Session,
    run_id: uuid.UUID | str,
    catalog: Catalog,
    skip_step: bool = False,
    from_step: str | None = None,
) -> Run:
    """Put a dead-lettered run back in the queue at the step that failed.

    With skip_step the failed step is marked skipped by the operator and the run resumes at
    the next one, for the case where the person fixed the effect by hand. With from_step the
    run restarts at an earlier step and the checkpointed outputs from that step onward are
    dropped, for the case where a lookup must be repeated after the data was corrected.
    Restarting earlier is safe because side effects are reused through the ledger.
    """
    if isinstance(run_id, str):
        run_id = uuid.UUID(run_id)
    run = session.get(Run, run_id)
    if run is None:
        raise PermanentError(f"run {run_id} not found")
    if run.status != RunStatus.dead_lettered:
        raise PermanentError(f"run {run_id} is {run.status.value}, only dead-lettered runs replay")
    wf = catalog.get(run.workflow_name)
    if from_step is not None:
        try:
            idx = wf.step_index(from_step)
        except KeyError as exc:
            raise PermanentError(f"{run.workflow_name} has no step {from_step}") from exc
        if idx > wf.step_index(run.current_step):
            raise PermanentError(f"{from_step} is after the failed step {run.current_step}")
        keep = {s.id for s in wf.steps[:idx]}
        steps = {k: v for k, v in run.context.get("steps", {}).items() if k in keep}
        run.context = {**run.context, "steps": steps}
        run.current_step = from_step
    elif skip_step and run.current_step is not None:
        steps = dict(run.context.get("steps", {}))
        steps[run.current_step] = {"skipped": True, "by": "operator"}
        run.context = {**run.context, "steps": steps}
        idx = wf.step_index(run.current_step) + 1
        run.current_step = wf.steps[idx].id if idx < len(wf.steps) else None
        if run.current_step is None:
            run.status = RunStatus.succeeded
            run.dead_letter_reason = None
            run.replay_count += 1
            session.commit()
            return run
    run.status = RunStatus.pending
    run.attempt = 0
    run.next_attempt_at = None
    run.dead_letter_reason = None
    run.finished_at = None
    run.replay_count += 1
    session.commit()
    return run


def mark_effect_done(session: Session, key: str, output: dict[str, Any] | None = None) -> Effect:
    effect = session.get(Effect, key)
    if effect is None:
        raise PermanentError(f"effect {key} not found")
    effect.status = EffectStatus.done
    effect.output = output or effect.output or {"marked_done_by": "operator"}
    session.commit()
    return effect
