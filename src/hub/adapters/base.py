from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from hub.adapters.faults import FaultInjector
from hub.adapters.sandbox import SandboxStore
from hub.clock import Clock
from hub.config import Settings
from hub.engine.errors import DefinitionError, PermanentError
from hub.engine.ratelimit import TokenBucket


@dataclass
class ActionContext:
    session: Session
    settings: Settings
    clock: Clock
    store: SandboxStore
    run_id: str
    step_id: str
    idempotency_key: str | None


ActionFn = Callable[..., dict[str, Any]]
VerifyFn = Callable[[ActionContext, str], dict[str, Any] | None]


@dataclass
class Action:
    """One callable a workflow step can name.

    side_effect: the call changes something outside the hub, so it goes through the effect
    ledger. verify: given an idempotency key, report whether the application already has
    the result (used when a previous attempt may have reached it). duplicate_tolerant: a
    repeated call is acceptable when verification is impossible, as for a Slack message.
    """

    name: str
    fn: ActionFn
    side_effect: bool = False
    verify: VerifyFn | None = None
    duplicate_tolerant: bool = False
    description: str = ""


class Registry:
    def __init__(self) -> None:
        self._actions: dict[str, Action] = {}

    def register(self, action: Action) -> None:
        if action.name in self._actions:
            raise DefinitionError(f"action {action.name} registered twice")
        self._actions[action.name] = action

    def get(self, name: str) -> Action:
        try:
            return self._actions[name]
        except KeyError as exc:
            raise DefinitionError(f"unknown action {name}") from exc

    def names(self) -> set[str]:
        return set(self._actions)

    def all(self) -> list[Action]:
        return sorted(self._actions.values(), key=lambda a: a.name)

    def __contains__(self, name: str) -> bool:
        return name in self._actions


class Adapter:
    """Base for one application. Subclasses register their actions and set their rate limit."""

    system: str = ""
    rate_per_second: float = 10.0
    burst: int = 20
    acquire_timeout: float = 30.0

    def __init__(
        self,
        store: SandboxStore,
        faults: FaultInjector,
        settings: Settings,
        bucket: TokenBucket | None = None,
    ) -> None:
        self.store = store
        self.faults = faults
        self.settings = settings
        self.bucket = bucket or TokenBucket(self.rate_per_second, self.burst)

    def register(self, registry: Registry) -> None:
        raise NotImplementedError

    def action(
        self,
        registry: Registry,
        name: str,
        fn: ActionFn,
        *,
        side_effect: bool = False,
        verify: VerifyFn | None = None,
        duplicate_tolerant: bool = False,
        description: str = "",
    ) -> None:
        full = f"{self.system}.{name}"
        registry.register(
            Action(
                name=full,
                fn=self._wrap(full, fn),
                side_effect=side_effect,
                verify=verify,
                duplicate_tolerant=duplicate_tolerant,
                description=description,
            )
        )

    def _wrap(self, full: str, fn: ActionFn) -> ActionFn:
        def call(ctx: ActionContext, **kwargs: Any) -> dict[str, Any]:
            self.bucket.acquire(timeout=self.acquire_timeout)
            self.faults.before_call(ctx.session, full)
            try:
                result = fn(ctx, **kwargs)
            except TypeError as exc:
                raise PermanentError(f"{full}: bad arguments: {exc}") from exc
            self.faults.after_call(ctx.session, full)
            return result

        return call

    def verify_by_key(self, collection: str) -> VerifyFn:
        def verify(ctx: ActionContext, key: str) -> dict[str, Any] | None:
            return self.store.find_by_key(ctx.session, self.system, collection, key)

        return verify

    def poll(self, session: Session, collection: str, cursor: str | None) -> tuple[list[dict], str]:
        """Return records created after the cursor and the new cursor. Default: unsupported."""
        raise PermanentError(f"{self.system} does not support polling")
