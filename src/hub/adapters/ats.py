from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from hub.adapters.base import ActionContext, Adapter, Registry
from hub.engine.errors import PermanentError


class AtsAdapter(Adapter):
    """Applicant tracking system: candidates, clients, placements and timesheets.

    The production system sends webhooks for placements but not for timesheet approvals,
    so timesheets are polled through `poll` with a cursor the scheduler persists.
    """

    system = "ats"
    rate_per_second = 5.0
    burst = 10

    def register(self, registry: Registry) -> None:
        self.action(registry, "get_candidate", self.get_candidate)
        self.action(registry, "get_client", self.get_client)
        self.action(registry, "get_placement", self.get_placement)
        self.action(registry, "get_timesheet", self.get_timesheet)
        self.action(registry, "update_candidate", self.update_candidate)
        self.action(registry, "list_active_placements", self.list_active_placements)

    def _get(self, ctx: ActionContext, collection: str, record_id: str) -> dict[str, Any]:
        rec = self.store.get(ctx.session, self.system, collection, str(record_id))
        if rec is None:
            raise PermanentError(f"ats {collection} {record_id} not found")
        return rec

    def get_candidate(self, ctx: ActionContext, candidate_id: str) -> dict[str, Any]:
        return self._get(ctx, "candidates", candidate_id)

    def get_client(self, ctx: ActionContext, client_id: str) -> dict[str, Any]:
        return self._get(ctx, "clients", client_id)

    def get_placement(self, ctx: ActionContext, placement_id: str) -> dict[str, Any]:
        return self._get(ctx, "placements", placement_id)

    def get_timesheet(self, ctx: ActionContext, timesheet_id: str) -> dict[str, Any]:
        return self._get(ctx, "timesheets", timesheet_id)

    def update_candidate(self, ctx: ActionContext, candidate_id: str, **fields: Any) -> dict:
        rec = self._get(ctx, "candidates", candidate_id)
        rec.update(fields)
        return self.store.put(ctx.session, self.system, "candidates", str(candidate_id), rec)

    def list_active_placements(self, ctx: ActionContext) -> dict[str, Any]:
        placements = self.store.find(ctx.session, self.system, "placements", status="active")
        emails = []
        for p in placements:
            cand = self.store.get(ctx.session, self.system, "candidates", p["candidate_id"])
            if cand:
                emails.append(cand["email"])
        return {"placements": placements, "count": len(placements), "candidate_emails": emails}

    def poll(self, session: Session, collection: str, cursor: str | None) -> tuple[list[dict], str]:
        seq_after = int(cursor) if cursor else 0
        rows = self.store.list_after(session, self.system, collection, seq_after)
        new_cursor = str(rows[-1][0]) if rows else str(seq_after)
        if collection == "timesheets":
            rows = [(s, r) for s, r in rows if r.get("status") == "approved"]
        return [r for _, r in rows], new_cursor
