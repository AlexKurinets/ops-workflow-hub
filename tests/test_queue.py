from __future__ import annotations

from datetime import timedelta

from hub.db.models import Run, RunStatus
from hub.db.queue import claim_run


def _two_runs(rt, sim):
    a = sim.placement()["runs"][0]
    b = sim.placement()["runs"][0]
    return a, b


def test_concurrent_workers_never_claim_the_same_run(seeded, sim, clock):
    a, b = _two_runs(seeded, sim)
    s1 = seeded.session_factory()
    s2 = seeded.session_factory()
    try:
        first = claim_run(s1, "w1", clock.now())
        second = claim_run(s2, "w2", clock.now())
        assert first is not None and second is not None
        assert {str(first.id), str(second.id)} == {a, b}
        assert first.worker_id == "w1" and second.worker_id == "w2"
        with seeded.session_factory() as s3:
            assert claim_run(s3, "w3", clock.now()) is None
            s3.rollback()
    finally:
        s1.rollback()
        s2.rollback()
        s1.close()
        s2.close()


def test_runs_waiting_for_retry_are_claimed_only_when_due(seeded, sim, clock):
    run_id = sim.placement()["runs"][0]
    with seeded.session_factory() as session:
        run = session.get(Run, __import__("uuid").UUID(run_id))
        run.status = RunStatus.waiting_retry
        run.next_attempt_at = clock.now() + timedelta(seconds=30)
        session.commit()
    with seeded.session_factory() as session:
        assert claim_run(session, "w1", clock.now()) is None
        session.rollback()
    clock.advance(31)
    with seeded.session_factory() as session:
        claimed = claim_run(session, "w1", clock.now())
        assert claimed is not None and str(claimed.id) == run_id
        assert claimed.status == RunStatus.running
        session.rollback()
