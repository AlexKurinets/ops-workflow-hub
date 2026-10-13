from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from hub.clock import FrozenClock
from hub.db.models import Event, PollCursor, ScheduleState
from hub.engine.scheduler import Scheduler
from hub.runtime import Runtime


def _sched(rt: Runtime) -> Scheduler:
    return Scheduler(rt.session_factory, rt.catalog, rt.adapters, rt.clock)


def test_poller_ingests_only_approved_timesheets_and_advances_the_cursor(seeded, clock):
    sched = _sched(seeded)
    assert sched.tick()["polled"] == 3
    with seeded.session_factory() as session:
        cursor = session.get(PollCursor, "timesheet_approved:ats:timesheets")
        assert cursor is not None and int(cursor.cursor) > 0
        events = list(session.execute(select(Event).where(Event.source == "ats")).scalars())
    assert sorted(e.external_id for e in events) == ["ts_9001", "ts_9002", "ts_9003"]


def test_poller_respects_its_interval(seeded, sim, clock):
    sched = _sched(seeded)
    sched.tick()
    sim.timesheet()
    assert sched.tick()["polled"] == 0  # too soon
    clock.advance(61)
    assert sched.tick()["polled"] == 1
    clock.advance(61)
    assert sched.tick()["polled"] == 0  # nothing new


def test_poll_overlap_is_deduplicated(seeded, clock):
    sched = _sched(seeded)
    sched.tick()
    with seeded.session_factory() as session:
        cursor = session.get(PollCursor, "timesheet_approved:ats:timesheets")
        cursor.cursor = "0"  # pretend the cursor write was lost
        session.commit()
    clock.advance(61)
    assert sched.tick()["polled"] == 0
    with seeded.session_factory() as session:
        assert (
            session.execute(select(Event).where(Event.source == "ats")).scalars().all().__len__()
            == 3
        )


def test_schedule_does_not_fire_for_a_time_before_it_was_registered(seeded, clock):
    # 09:00 UTC on 2026-10-12 is 04:00 in Chicago: the 02:00 run is in the past, so it must
    # wait for tomorrow's 02:00 rather than firing on first sight.
    sched = _sched(seeded)
    assert sched.tick()["fired"] == 0
    with seeded.session_factory() as session:
        state = session.get(ScheduleState, "nightly_payroll_reconciliation")
        assert state is not None and state.last_fired_at.astimezone(UTC) == clock.now()
    clock.advance(21 * 3600)  # 01:00 Chicago next day
    assert sched.tick()["fired"] == 0
    clock.advance(3600 + 5)  # 02:00:05 Chicago
    assert sched.tick()["fired"] == 1
    assert sched.tick()["fired"] == 0
    with seeded.session_factory() as session:
        state = session.get(ScheduleState, "nightly_payroll_reconciliation")
        assert state.last_fired_at.astimezone(UTC) == datetime(2026, 10, 13, 7, 0, tzinfo=UTC)
        event = session.execute(select(Event).where(Event.source == "schedule")).scalar_one()
    assert event.payload["data"]["fired_at"] == "2026-10-13T02:00:00-05:00"


def test_schedule_catches_up_one_missed_fire_per_tick(seeded, clock):
    sched = _sched(seeded)
    sched.tick()
    clock.advance(3 * 24 * 3600)  # the scheduler was down for three days
    assert sched.tick()["fired"] == 1
    assert sched.tick()["fired"] == 1
    assert sched.tick()["fired"] == 1
    assert sched.tick()["fired"] == 0


def test_schedule_not_due_before_its_time(settings, clock):
    from hub.db import reset_db

    reset_db(settings.database_url)
    early = FrozenClock(datetime(2026, 10, 12, 6, 30, tzinfo=UTC))  # 01:30 Chicago
    rt = Runtime.build(settings, early)
    sched = _sched(rt)
    assert sched.tick()["fired"] == 0
    early.advance(45 * 60)  # 02:15 Chicago
    assert sched.tick()["fired"] == 1
