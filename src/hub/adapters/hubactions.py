from __future__ import annotations

import logging
from typing import Any

from hub.adapters.base import ActionContext, Adapter, Registry
from hub.engine.errors import PermanentError

log = logging.getLogger("hub.actions")


class HubAdapter(Adapter):
    """Steps that need no application: assign values, log, or fail on purpose."""

    system = "hub"
    rate_per_second = 1000.0
    burst = 1000

    def register(self, registry: Registry) -> None:
        self.action(registry, "set", self.set_values, description="Return the given values")
        self.action(registry, "log", self.log_message, description="Write a line to the log")
        self.action(registry, "fail", self.fail, description="Dead-letter the run with a message")

    def set_values(self, ctx: ActionContext, **values: Any) -> dict[str, Any]:
        return dict(values)

    def log_message(self, ctx: ActionContext, message: str, **extra: Any) -> dict[str, Any]:
        log.info("run=%s step=%s %s %s", ctx.run_id, ctx.step_id, message, extra or "")
        return {"message": message, **extra}

    def fail(self, ctx: ActionContext, message: str) -> dict[str, Any]:
        raise PermanentError(message)
