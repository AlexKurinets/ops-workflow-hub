from __future__ import annotations

import logging
from typing import Any

import httpx

from hub.adapters.base import ActionContext, Adapter, Registry
from hub.engine.errors import PermanentError, TransientError

log = logging.getLogger("hub.slack")


class SlackAdapter(Adapter):
    """Slack notifications. sandbox stores them, console prints them, webhook posts them.

    A repeated notification is annoying but harmless, so the action is duplicate tolerant:
    if an earlier attempt may have posted and the hub cannot check, it posts again rather
    than parking the run.
    """

    system = "slack"
    rate_per_second = 1.0
    burst = 5

    def register(self, registry: Registry) -> None:
        self.action(
            registry,
            "post",
            self.post,
            side_effect=True,
            verify=self.verify_by_key("messages")
            if self.settings.slack_mode == "sandbox"
            else None,
            duplicate_tolerant=True,
        )

    def post(self, ctx: ActionContext, channel: str, text: str) -> dict[str, Any]:
        mode = self.settings.slack_mode
        if mode == "webhook":
            return self._post_webhook(channel, text)
        if mode == "console":
            print(f"[slack] {channel}: {text}")
        rec = {"channel": channel, "text": text}
        return self.store.put(
            ctx.session, self.system, "messages", self.store.new_id("msg"), rec, ctx.idempotency_key
        )

    def _post_webhook(self, channel: str, text: str) -> dict[str, Any]:
        url = self.settings.slack_webhook_url
        if not url:
            raise PermanentError("SLACK_WEBHOOK_URL is not set")
        try:
            resp = httpx.post(url, json={"channel": channel, "text": text}, timeout=10.0)
        except httpx.HTTPError as exc:
            raise TransientError(f"slack request failed: {exc}") from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientError(f"slack returned {resp.status_code}")
        if resp.status_code >= 400:
            raise PermanentError(f"slack rejected the message: {resp.status_code} {resp.text}")
        return {"channel": channel, "text": text, "delivered": True}
