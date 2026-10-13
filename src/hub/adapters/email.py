from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Any

from hub.adapters.base import ActionContext, Adapter, Registry
from hub.engine.errors import PermanentError, TransientError


class EmailAdapter(Adapter):
    """Outbound email. sandbox stores it, console prints it, smtp sends it."""

    system = "email"
    rate_per_second = 2.0
    burst = 10

    def register(self, registry: Registry) -> None:
        self.action(
            registry,
            "send",
            self.send,
            side_effect=True,
            verify=self.verify_by_key("messages")
            if self.settings.email_mode == "sandbox"
            else None,
            duplicate_tolerant=True,
        )

    def send(self, ctx: ActionContext, to: str, subject: str, body: str) -> dict[str, Any]:
        if "@" not in str(to):
            raise PermanentError(f"invalid recipient {to}")
        mode = self.settings.email_mode
        if mode == "smtp":
            return self._send_smtp(to, subject, body)
        if mode == "console":
            print(f"[email] to={to} subject={subject}\n{body}")
        rec = {"to": to, "subject": subject, "body": body}
        return self.store.put(
            ctx.session, self.system, "messages", self.store.new_id("em"), rec, ctx.idempotency_key
        )

    def _send_smtp(self, to: str, subject: str, body: str) -> dict[str, Any]:
        host = self.settings.smtp_host
        if not host:
            raise PermanentError("SMTP_HOST is not set")
        msg = EmailMessage()
        msg["To"] = to
        msg["From"] = "ops-hub@example.com"
        msg["Subject"] = subject
        msg.set_content(body)
        try:
            with smtplib.SMTP(host, self.settings.smtp_port, timeout=10) as smtp:
                smtp.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            raise TransientError(f"smtp send failed: {exc}") from exc
        return {"to": to, "subject": subject, "delivered": True}
