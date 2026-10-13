from __future__ import annotations

from typing import Any

from hub.adapters.base import ActionContext, Adapter, Registry
from hub.engine.errors import PermanentError


class PayrollAdapter(Adapter):
    """Payroll: worker records and submitted hours. Both writes move money, so both verify."""

    system = "payroll"
    rate_per_second = 2.0
    burst = 4

    def register(self, registry: Registry) -> None:
        self.action(registry, "find_worker", self.find_worker)
        self.action(registry, "list_workers", self.list_workers)
        self.action(
            registry,
            "upsert_worker",
            self.upsert_worker,
            side_effect=True,
            verify=self.verify_by_key("workers"),
        )
        self.action(
            registry,
            "submit_hours",
            self.submit_hours,
            side_effect=True,
            verify=self.verify_by_key("hours"),
        )

    def find_worker(self, ctx: ActionContext, email: str) -> dict[str, Any]:
        rows = self.store.find(ctx.session, self.system, "workers", email=str(email).lower())
        if not rows:
            return {"found": False, "id": None}
        return {"found": True, **rows[0]}

    def list_workers(self, ctx: ActionContext) -> dict[str, Any]:
        rows = self.store.find(ctx.session, self.system, "workers")
        return {"workers": rows, "emails": [r["email"] for r in rows], "count": len(rows)}

    def upsert_worker(
        self,
        ctx: ActionContext,
        email: str,
        first_name: str,
        last_name: str,
        pay_rate: float,
    ) -> dict[str, Any]:
        email = str(email).lower()
        if float(pay_rate) <= 0:
            raise PermanentError("pay_rate must be positive")
        rows = self.store.find(ctx.session, self.system, "workers", email=email)
        rec = rows[0] if rows else {"email": email, "status": "active"}
        rec.update({"first_name": first_name, "last_name": last_name, "pay_rate": float(pay_rate)})
        record_id = rec.get("id") or self.store.new_id("wk")
        return self.store.put(
            ctx.session, self.system, "workers", record_id, rec, ctx.idempotency_key
        )

    def submit_hours(
        self,
        ctx: ActionContext,
        worker_id: str,
        period_start: str,
        period_end: str,
        hours: float,
    ) -> dict[str, Any]:
        if self.store.get(ctx.session, self.system, "workers", str(worker_id)) is None:
            raise PermanentError(f"payroll worker {worker_id} not found")
        rec = {
            "worker_id": worker_id,
            "period_start": period_start,
            "period_end": period_end,
            "hours": float(hours),
        }
        return self.store.put(
            ctx.session, self.system, "hours", self.store.new_id("hrs"), rec, ctx.idempotency_key
        )
