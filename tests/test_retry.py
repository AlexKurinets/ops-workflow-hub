from __future__ import annotations

import random

import pytest

from hub.db.models import RunStatus
from hub.engine.retry import RetryPolicy
from tests.helpers import effects, get_run, records, statuses


def _arm(rt, action, kind, count):
    with rt.session_factory() as session:
        rt.adapters.faults.arm(session, action, kind, count)
        session.commit()


def test_backoff_grows_and_is_capped():
    p = RetryPolicy(base_seconds=2, multiplier=2, max_seconds=20, jitter_ratio=0)
    assert [p.delay_for(n) for n in (1, 2, 3, 4, 5)] == [2, 4, 8, 16, 20]
    assert p.exhausted(5) and not p.exhausted(4)
    with pytest.raises(ValueError):
        p.delay_for(0)


def test_jitter_stays_within_ratio():
    p = RetryPolicy(base_seconds=10, jitter_ratio=0.1)
    rng = random.Random(3)
    for _ in range(50):
        assert 9.0 <= p.delay_for(1, rng) <= 11.0


def test_transient_failure_waits_with_backoff_then_succeeds(seeded, sim, runner, clock):
    _arm(seeded, "payroll.upsert_worker", "transient", 2)
    run_id = sim.placement()["runs"][0]

    assert runner.run_once() is not None
    run = get_run(seeded, run_id)
    assert run.status == RunStatus.waiting_retry
    assert run.current_step == "create_worker" and run.attempt == 1
    first_delay = (run.next_attempt_at - clock.now()).total_seconds()
    assert 1.8 <= first_delay <= 2.2

    assert runner.run_once() is None  # not due yet
    clock.advance(3)
    runner.run_once()
    run = get_run(seeded, run_id)
    assert run.status == RunStatus.waiting_retry and run.attempt == 2
    second_delay = (run.next_attempt_at - clock.now()).total_seconds()
    assert 3.6 <= second_delay <= 4.4

    clock.advance(5)
    runner.run_once()
    run = get_run(seeded, run_id)
    assert run.status == RunStatus.succeeded
    create = [s for s in statuses(seeded, run_id) if s[0] == "create_worker"]
    assert create == [
        ("create_worker", 1, "failed"),
        ("create_worker", 2, "failed"),
        ("create_worker", 3, "succeeded"),
    ]
    # the earlier steps were not re-run on retry
    assert len([s for s in statuses(seeded, run_id) if s[0] == "load_placement"]) == 1
    assert len(records(seeded, "payroll", "workers")) == 6  # five seeded plus one


def test_transient_failures_exhaust_into_dead_letter(seeded, sim, runner, clock):
    _arm(seeded, "payroll.upsert_worker", "transient", 10)
    run_id = sim.placement()["runs"][0]
    for _ in range(5):
        runner.run_once()
        clock.advance(600)
    run = get_run(seeded, run_id)
    assert run.status == RunStatus.dead_lettered
    assert "failed 5 times" in run.dead_letter_reason
    assert len(records(seeded, "slack", "messages", channel="#ops-alerts")) == 1


def test_permanent_failure_dead_letters_immediately(seeded, sim, runner):
    _arm(seeded, "esign.send_envelope", "permanent", 1)
    run_id = sim.placement()["runs"][0]
    runner.run_once()
    run = get_run(seeded, run_id)
    assert run.status == RunStatus.dead_lettered
    assert run.current_step == "send_packet"
    assert "injected permanent failure" in run.dead_letter_reason
    assert [s for s in statuses(seeded, run_id) if s[0] == "send_packet"] == [
        ("send_packet", 1, "failed")
    ]
    # the ledger row stays pending so a replay goes through verification
    pending = [e for e in effects(seeded) if e.step_id == "send_packet"]
    assert len(pending) == 1 and pending[0].status.value == "pending"


def test_bad_arguments_are_permanent(seeded, sim, runner, tmp_path):
    from hub.engine.definitions import Catalog
    from hub.engine.ingest import ingest
    from hub.engine.runner import Runner

    (tmp_path / "wf.yaml").write_text(
        "name: wf\nversion: 1\ntrigger:\n  event: {source: ats, type: x}\nsteps:\n"
        "  - id: a\n    action: ats.get_candidate\n"
        "    with: {candidate_id: cand_1001, colour: red}\n",
        encoding="utf-8",
    )
    catalog = Catalog.load(tmp_path, seeded.adapters.registry.names())
    r = Runner(seeded.session_factory, catalog, seeded.adapters, seeded.settings, seeded.clock)
    with seeded.session_factory() as session:
        run_id = ingest(session, catalog, "ats", "x", "1", {}).run_ids[0]
        session.commit()
    r.run_once()
    run = get_run(seeded, run_id)
    assert run.status == RunStatus.dead_lettered
    assert "bad arguments" in run.dead_letter_reason


def test_unexpected_exception_parks_the_run_and_keeps_the_worker_alive(seeded, runner, tmp_path):
    from hub.adapters.base import Action
    from hub.engine.definitions import Catalog
    from hub.engine.ingest import ingest
    from hub.engine.runner import Runner

    def boom(ctx, **kwargs):
        raise RuntimeError("adapter bug")

    seeded.adapters.registry.register(Action(name="test.boom", fn=boom))
    (tmp_path / "wf.yaml").write_text(
        "name: wf\nversion: 1\ntrigger:\n  event: {source: ats, type: x}\nsteps:\n"
        "  - id: a\n    action: test.boom\n",
        encoding="utf-8",
    )
    catalog = Catalog.load(tmp_path, seeded.adapters.registry.names())
    r = Runner(seeded.session_factory, catalog, seeded.adapters, seeded.settings, seeded.clock)
    with seeded.session_factory() as session:
        run_id = ingest(session, catalog, "ats", "x", "1", {}).run_ids[0]
        session.commit()
    assert r.run_once() is not None
    run = get_run(seeded, run_id)
    assert run.status == RunStatus.dead_lettered
    assert "RuntimeError: adapter bug" in run.dead_letter_reason
    assert r.run_once() is None
