from __future__ import annotations

from typing import Any

from hub.adapters.base import ActionContext, Adapter, Registry
from hub.engine.errors import PermanentError


class AccountingAdapter(Adapter):
    """Accounting: customers and invoices."""

    system = "accounting"
    rate_per_second = 2.0
    burst = 4

    def register(self, registry: Registry) -> None:
        self.action(registry, "find_customer", self.find_customer)
        self.action(
            registry,
            "ensure_customer",
            self.ensure_customer,
            side_effect=True,
            verify=self.verify_by_key("customers"),
        )
        self.action(
            registry,
            "create_invoice",
            self.create_invoice,
            side_effect=True,
            verify=self.verify_by_key("invoices"),
            description="Create a draft invoice; a duplicate here is a double bill",
        )

    def find_customer(self, ctx: ActionContext, name: str) -> dict[str, Any]:
        rows = self.store.find(ctx.session, self.system, "customers", name=name)
        if not rows:
            return {"found": False, "id": None}
        return {"found": True, **rows[0]}

    def ensure_customer(self, ctx: ActionContext, name: str) -> dict[str, Any]:
        rows = self.store.find(ctx.session, self.system, "customers", name=name)
        if rows:
            return rows[0]
        return self.store.put(
            ctx.session,
            self.system,
            "customers",
            self.store.new_id("cus"),
            {"name": name},
            ctx.idempotency_key,
        )

    def create_invoice(
        self,
        ctx: ActionContext,
        customer_id: str,
        lines: list[dict[str, Any]],
        memo: str | None = None,
    ) -> dict[str, Any]:
        if self.store.get(ctx.session, self.system, "customers", str(customer_id)) is None:
            raise PermanentError(f"accounting customer {customer_id} not found")
        if not lines:
            raise PermanentError("an invoice needs at least one line")
        total = 0.0
        for line in lines:
            try:
                total += float(line["quantity"]) * float(line["unit_price"])
            except (KeyError, TypeError, ValueError) as exc:
                raise PermanentError(f"invalid invoice line {line}") from exc
        rec = {
            "customer_id": customer_id,
            "lines": lines,
            "memo": memo,
            "total": round(total, 2),
            "status": "draft",
        }
        return self.store.put(
            ctx.session, self.system, "invoices", self.store.new_id("inv"), rec, ctx.idempotency_key
        )
