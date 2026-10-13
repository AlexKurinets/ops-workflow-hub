from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy.orm import Session, sessionmaker

from hub.adapters import Adapters
from hub.clock import Clock
from hub.db.models import PollCursor, ScheduleState
from hub.engine.definitions import Catalog, WorkflowDef
from hub.engine.ingest import ingest

log = logging.getLogger("hub.scheduler")

SCHEDULE_SOURCE = "schedule"


class Scheduler:
    """Two jobs: poll the applications that cannot send webhooks, and fire cron triggers.

    Both go through `ingest`, so a poll window that overlaps the last one or a schedule
    that fires twice after a restart is deduplicated like any other event.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        catalog: Catalog,
        adapters: Adapters,
        clock: Clock | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.catalog = catalog
        self.adapters = adapters
        self.clock = clock or Clock()

    def tick(self, force: bool = False) -> dict[str, int]:
        """One pass. With force, pollers ignore their interval (used by `hub scheduler --once`)."""
        polled = sum(self._poll(wf, force) for wf in self.catalog.polls())
        fired = sum(self._fire(wf) for wf in self.catalog.schedules())
        return {"polled": polled, "fired": fired}

    def _poll(self, wf: WorkflowDef, force: bool = False) -> int:
        trig = wf.trigger.poll
        assert trig is not None
        key = f"{wf.name}:{trig.source}:{trig.collection}"
        now = self.clock.now()
        with self.session_factory() as session:
            cursor = session.get(PollCursor, key)
            if cursor is None:
                cursor = PollCursor(key=key, cursor=None, last_polled_at=None)
                session.add(cursor)
            elif (
                not force
                and cursor.last_polled_at is not None
                and (now - cursor.last_polled_at < timedelta(seconds=trig.interval_seconds))
            ):
                return 0
            records, new_cursor = self.adapters[trig.source].poll(
                session, trig.collection, cursor.cursor
            )
            created = 0
            for rec in records:
                result = ingest(session, self.catalog, trig.source, trig.type, rec["id"], rec)
                created += 0 if result.duplicate else len(result.run_ids)
            cursor.cursor = new_cursor
            cursor.last_polled_at = now
            session.commit()
            if created:
                log.info("poll %s created %d run(s)", key, created)
            return created

    def _fire(self, wf: WorkflowDef) -> int:
        trig = wf.trigger.schedule
        assert trig is not None
        tz = ZoneInfo(trig.timezone)
        now = self.clock.now().astimezone(tz)
        with self.session_factory() as session:
            state = session.get(ScheduleState, wf.name)
            if state is None:
                # First sight: the baseline is now, so a schedule registered at 10:00 does
                # not fire for the 02:00 that already passed. Missed fires after that are
                # caught up one per tick.
                state = ScheduleState(workflow_name=wf.name, last_fired_at=now)
                session.add(state)
            base = state.last_fired_at.astimezone(tz)
            due: datetime = croniter(trig.cron, base).get_next(datetime)
            if due > now:
                session.commit()
                return 0
            result = ingest(
                session,
                self.catalog,
                SCHEDULE_SOURCE,
                wf.name,
                due.isoformat(),
                {"fired_at": due.isoformat(), "cron": trig.cron},
            )
            state.last_fired_at = due
            session.commit()
            if not result.duplicate:
                log.info("schedule %s fired for %s", wf.name, due.isoformat())
            return 0 if result.duplicate else len(result.run_ids)
