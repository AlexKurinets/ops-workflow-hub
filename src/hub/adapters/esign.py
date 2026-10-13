from __future__ import annotations

from typing import Any

from hub.adapters.base import ActionContext, Adapter, Registry
from hub.engine.errors import PermanentError


class EsignAdapter(Adapter):
    """E-signature service: envelopes built from templates and sent to one recipient."""

    system = "esign"
    rate_per_second = 3.0
    burst = 6

    def register(self, registry: Registry) -> None:
        self.action(
            registry,
            "send_envelope",
            self.send_envelope,
            side_effect=True,
            verify=self.verify_by_key("envelopes"),
            description="Create and send an envelope; sending twice means two packets",
        )
        self.action(registry, "get_envelope", self.get_envelope)

    def send_envelope(
        self,
        ctx: ActionContext,
        template: str,
        recipient_name: str,
        recipient_email: str,
        fields: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if "@" not in str(recipient_email):
            raise PermanentError(f"invalid recipient email {recipient_email}")
        rec = {
            "template": template,
            "recipient_name": recipient_name,
            "recipient_email": recipient_email,
            "fields": fields or {},
            "status": "sent",
        }
        return self.store.put(
            ctx.session,
            self.system,
            "envelopes",
            self.store.new_id("env"),
            rec,
            ctx.idempotency_key,
        )

    def get_envelope(self, ctx: ActionContext, envelope_id: str) -> dict[str, Any]:
        rec = self.store.get(ctx.session, self.system, "envelopes", str(envelope_id))
        if rec is None:
            raise PermanentError(f"esign envelope {envelope_id} not found")
        return rec
