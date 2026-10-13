from __future__ import annotations

from sqlalchemy.orm import Session

from hub.adapters.sandbox import SandboxStore
from hub.engine.errors import PermanentError, TransientError

KINDS = ("transient", "permanent", "after")


class FaultInjector:
    """Armed failures for a named action, stored in the sandbox so every process sees them.

    `transient` and `permanent` fail before the adapter does anything. `after` lets the call
    reach the application and then loses the response, which is the case that makes
    at-most-once side effects hard; the effect ledger and verify hooks exist for it.
    """

    SYSTEM = "hub"
    COLLECTION = "faults"

    def __init__(self, store: SandboxStore) -> None:
        self.store = store

    def arm(self, session: Session, action: str, kind: str, count: int) -> dict:
        if kind not in KINDS:
            raise ValueError(f"fault kind must be one of {KINDS}")
        return self.store.put(
            session, self.SYSTEM, self.COLLECTION, action, {"kind": kind, "remaining": count}
        )

    def clear(self, session: Session, action: str) -> None:
        self.store.put(
            session, self.SYSTEM, self.COLLECTION, action, {"kind": "none", "remaining": 0}
        )

    def list(self, session: Session) -> list[dict]:
        records = self.store.find(session, self.SYSTEM, self.COLLECTION)
        return [r for r in records if r["remaining"] > 0]

    def _take(self, session: Session, action: str, kinds: tuple[str, ...]) -> str | None:
        rec = self.store.get(session, self.SYSTEM, self.COLLECTION, action)
        if not rec or rec["remaining"] <= 0 or rec["kind"] not in kinds:
            return None
        self.store.put(
            session,
            self.SYSTEM,
            self.COLLECTION,
            action,
            {"kind": rec["kind"], "remaining": rec["remaining"] - 1},
        )
        return rec["kind"]

    def before_call(self, session: Session, action: str) -> None:
        kind = self._take(session, action, ("transient", "permanent"))
        if kind == "transient":
            raise TransientError(f"injected transient failure for {action}")
        if kind == "permanent":
            raise PermanentError(f"injected permanent failure for {action}")

    def after_call(self, session: Session, action: str) -> None:
        if self._take(session, action, ("after",)) == "after":
            raise TransientError(f"injected lost response for {action}")
