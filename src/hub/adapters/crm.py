from __future__ import annotations

from typing import Any

from hub.adapters.base import ActionContext, Adapter, Registry
from hub.engine.errors import PermanentError


class CrmAdapter(Adapter):
    """CRM: companies, contacts and the activity log sales reads."""

    system = "crm"
    rate_per_second = 8.0
    burst = 16

    def register(self, registry: Registry) -> None:
        self.action(registry, "find_company", self.find_company)
        self.action(registry, "upsert_company", self.upsert_company)
        self.action(registry, "upsert_contact", self.upsert_contact)
        self.action(
            registry,
            "log_activity",
            self.log_activity,
            side_effect=True,
            verify=self.verify_by_key("activities"),
            description="Append an activity to a contact timeline",
        )

    def find_company(self, ctx: ActionContext, domain: str) -> dict[str, Any]:
        rows = self.store.find(ctx.session, self.system, "companies", domain=str(domain).lower())
        if not rows:
            return {"found": False, "id": None}
        return {"found": True, **rows[0]}

    def upsert_company(
        self, ctx: ActionContext, name: str, domain: str, owner: str | None = None
    ) -> dict[str, Any]:
        domain = str(domain).lower()
        rows = self.store.find(ctx.session, self.system, "companies", domain=domain)
        if rows:
            rec = rows[0]
            rec.update({"name": name, "owner": owner or rec.get("owner")})
            return self.store.put(ctx.session, self.system, "companies", rec["id"], rec)
        rec = {"name": name, "domain": domain, "owner": owner}
        return self.store.put(
            ctx.session, self.system, "companies", self.store.new_id("co"), rec, ctx.idempotency_key
        )

    def upsert_contact(
        self,
        ctx: ActionContext,
        email: str,
        first_name: str,
        last_name: str,
        company_id: str | None = None,
    ) -> dict[str, Any]:
        email = str(email).lower()
        rows = self.store.find(ctx.session, self.system, "contacts", email=email)
        rec = rows[0] if rows else {"email": email}
        rec.update({"first_name": first_name, "last_name": last_name})
        if company_id:
            rec["company_id"] = company_id
        record_id = rec.get("id") or self.store.new_id("ct")
        return self.store.put(ctx.session, self.system, "contacts", record_id, rec)

    def log_activity(self, ctx: ActionContext, contact_id: str, kind: str, note: str) -> dict:
        if self.store.get(ctx.session, self.system, "contacts", str(contact_id)) is None:
            raise PermanentError(f"crm contact {contact_id} not found")
        rec = {"contact_id": contact_id, "kind": kind, "note": note}
        return self.store.put(
            ctx.session,
            self.system,
            "activities",
            self.store.new_id("act"),
            rec,
            ctx.idempotency_key,
        )
