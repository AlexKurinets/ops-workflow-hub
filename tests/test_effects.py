"""The effects ledger: side effects happen at most once across retries, replays and runs."""

from __future__ import annotations

from hub.adapters.base import Action
from hub.db.models import RunStatus
from hub.engine.definitions import Catalog
from hub.engine.errors import TransientError
from hub.engine.ingest import ingest
from hub.engine.runner import Runner, mark_effect_done, replay_run
from tests.helpers import effects, get_run, records, statuses


def _arm(rt, action, kind, count):
    with rt.session_factory() as session:
        rt.adapters.faults.arm(session, action, kind, count)
        session.commit()


def _custom_runner(rt, tmp_path, action_name, **step_fields):
    extra = "".join(f"    {k}: {v}\n" for k, v in step_fields.items())
    (tmp_path / "wf.yaml").write_text(
        "name: wf\nversion: 1\ntrigger:\n  event: {source: ats, type: x}\nsteps:\n"
        f"  - id: charge\n    action: {action_name}\n{extra}"
        "  - id: after\n    action: hub.set\n    with: {done: true}\n",
        encoding="utf-8",
    )
    catalog = Catalog.load(tmp_path, rt.adapters.registry.names())
    runner = Runner(rt.session_factory, catalog, rt.adapters, rt.settings, rt.clock)
    with rt.session_factory() as session:
        run_id = ingest(session, catalog, "ats", "x", "1", {}).run_ids[0]
        session.commit()
    return runner, run_id


def test_lost_response_is_verified_by_key_not_repeated(seeded, sim, runner, clock):
    """The call reached payroll but the response was lost: no second worker on retry."""
    _arm(seeded, "payroll.upsert_worker", "after", 1)
    out = sim.placement()
    run_id = out["runs"][0]
    runner.run_once()
    assert get_run(seeded, run_id).status == RunStatus.waiting_retry
    clock.advance(10)
    runner.run_once()
    run = get_run(seeded, run_id)
    assert run.status == RunStatus.succeeded
    candidate = records(seeded, "ats", "candidates", id=out["candidate_id"])[0]
    assert len(records(seeded, "payroll", "workers", email=candidate["email"])) == 1
    create = [s for s in statuses(seeded, run_id) if s[0] == "create_worker"]
    assert create == [("create_worker", 1, "failed"), ("create_worker", 2, "reused")]
    key = f"placement:{out['placement_id']}:payroll-worker"
    ledger = {e.key: e.status.value for e in effects(seeded)}
    assert ledger[key] == "done"


def test_unverifiable_effect_parks_the_run_until_a_person_decides(seeded, tmp_path):
    calls = {"n": 0}

    def charge(ctx, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TransientError("timeout after the request was sent")
        return {"charged": True}

    seeded.adapters.registry.register(Action(name="test.charge", fn=charge, side_effect=True))
    runner, run_id = _custom_runner(seeded, tmp_path, "test.charge", idempotency_key="charge-1")
    runner.run_once()
    assert get_run(seeded, run_id).status == RunStatus.waiting_retry
    seeded.clock.advance(10)
    runner.run_once()
    run = get_run(seeded, run_id)
    assert run.status == RunStatus.dead_lettered
    assert "cannot be verified" in run.dead_letter_reason
    assert calls["n"] == 1  # never called a second time

    with seeded.session_factory() as session:
        mark_effect_done(session, "charge-1", {"charged": True, "checked_by": "ops"})
        replay_run(session, run_id, runner.catalog)
    runner.run_once()
    run = get_run(seeded, run_id)
    assert run.status == RunStatus.succeeded
    assert run.context["steps"]["charge"] == {"charged": True, "checked_by": "ops"}
    assert calls["n"] == 1
    assert [s for s in statuses(seeded, run_id) if s[0] == "charge"][-1][2] == "reused"


def test_duplicate_tolerant_effect_is_simply_repeated(seeded, tmp_path):
    calls = {"n": 0}

    def notify(ctx, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TransientError("socket closed")
        return {"sent": True}

    seeded.adapters.registry.register(
        Action(name="test.notify", fn=notify, side_effect=True, duplicate_tolerant=True)
    )
    runner, run_id = _custom_runner(seeded, tmp_path, "test.notify")
    runner.run_once()
    seeded.clock.advance(10)
    runner.run_once()
    assert get_run(seeded, run_id).status == RunStatus.succeeded
    assert calls["n"] == 2


def test_same_idempotency_key_across_runs_reuses_the_effect(seeded, sim, runner):
    """The ATS sends placement.created twice with different event ids: one packet, not two."""
    first = sim.placement()
    runner.run_pending()
    payload = sim.placement_webhook(first["placement_id"], first["candidate_id"])
    payload["eventId"] = "evt_retry_with_new_id"
    second = sim.deliver("ats", payload)
    assert second["duplicate"] is False
    runner.run_pending()
    run = get_run(seeded, second["runs"][0])
    assert run.status == RunStatus.succeeded
    by_step = {s[0]: s[2] for s in statuses(seeded, run.id)}
    assert by_step["send_packet"] == "reused"
    assert by_step["log_placement"] == "reused"
    candidate = records(seeded, "ats", "candidates", id=first["candidate_id"])[0]
    assert len(records(seeded, "esign", "envelopes", recipient_email=candidate["email"])) == 1
    assert len(records(seeded, "sheets", "rows", sheet="placements")) == 1


def test_replay_with_skip_step_marks_it_done_by_operator(seeded, sim, runner):
    _arm(seeded, "esign.send_envelope", "permanent", 1)
    run_id = sim.placement()["runs"][0]
    runner.run_once()
    assert get_run(seeded, run_id).status == RunStatus.dead_lettered
    with seeded.session_factory() as session:
        replay_run(session, run_id, seeded.catalog, skip_step=True)
    assert get_run(seeded, run_id).current_step == "notify_recruiter"
    runner.run_once()
    run = get_run(seeded, run_id)
    assert run.status == RunStatus.succeeded
    assert run.context["steps"]["send_packet"] == {"skipped": True, "by": "operator"}
    assert records(seeded, "esign", "envelopes") == []
