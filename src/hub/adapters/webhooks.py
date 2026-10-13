from __future__ import annotations

from typing import Any

from hub.engine.errors import PermanentError

KNOWN_SOURCES = ("ats", "esign", "forms", "crm")


def _need(payload: dict[str, Any], *path: str) -> Any:
    cur: Any = payload
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            raise PermanentError(f"webhook payload is missing {'.'.join(path)}")
        cur = cur[p]
    return cur


def extract(source: str, payload: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """Map each source's own envelope to (event_type, external_id, data).

    Every application wraps its events differently; this is the only place that knows how.
    """
    if source == "ats":
        event_type = str(_need(payload, "eventType"))
        return event_type, str(_need(payload, "eventId")), _need(payload, "payload")
    if source == "esign":
        event_type = str(_need(payload, "event"))
        data = _need(payload, "data")
        return event_type, f"{_need(data, 'envelopeId')}:{event_type}", data
    if source == "forms":
        return "lead.received", str(_need(payload, "submission_id")), _need(payload, "fields")
    if source == "crm":
        return str(_need(payload, "type")), str(_need(payload, "id")), _need(payload, "data")
    raise PermanentError(f"unknown webhook source {source}")
