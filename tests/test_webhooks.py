from __future__ import annotations

import json

from hub.api.app import SIGNATURE_HEADER, sign

PLACEMENT = {
    "eventType": "placement.created",
    "eventId": "evt_42",
    "payload": {"placementId": "plc_5001", "candidateId": "cand_1001"},
}


def _post(client, settings, source, payload, secret=None):
    body = json.dumps(payload).encode()
    secret = secret or settings.webhook_secret_for(source)
    return client.post(
        f"/webhooks/{source}",
        content=body,
        headers={"Content-Type": "application/json", SIGNATURE_HEADER: sign(secret, body)},
    )


def test_signed_webhook_is_accepted_and_starts_a_run(client, settings, seeded):
    resp = _post(client, settings, "ats", PLACEMENT)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["duplicate"] is False
    assert len(body["runs"]) == 1
    assert client.get(f"/runs/{body['runs'][0]}").json()["workflow"] == "candidate_placed"


def test_redelivered_webhook_is_deduplicated(client, settings, seeded):
    first = _post(client, settings, "ats", PLACEMENT).json()
    second = _post(client, settings, "ats", PLACEMENT).json()
    assert second["duplicate"] is True
    assert second["event_id"] == first["event_id"]
    assert second["runs"] == []
    assert len(client.get("/runs").json()) == 1


def test_bad_signature_is_rejected(client, settings, seeded):
    resp = _post(client, settings, "ats", PLACEMENT, secret="wrong")
    assert resp.status_code == 401
    assert client.get("/runs").json() == []


def test_per_source_secret_is_used(client, settings, seeded):
    payload = {"event": "envelope.completed", "data": {"envelopeId": "env_1"}}
    assert _post(client, settings, "esign", payload, secret="esign-secret").status_code == 202
    assert _post(client, settings, "esign", payload, secret="test-secret").status_code == 401


def test_unknown_source_is_404(client, settings, seeded):
    assert _post(client, settings, "fax", {"x": 1}).status_code == 404


def test_malformed_envelope_is_422(client, settings, seeded):
    resp = _post(client, settings, "ats", {"eventType": "placement.created"})
    assert resp.status_code == 422
    assert "eventId" in resp.json()["detail"]


def test_non_json_body_is_400(client, settings):
    body = b"not json"
    resp = client.post(
        "/webhooks/ats",
        content=body,
        headers={SIGNATURE_HEADER: sign(settings.webhook_secret_for("ats"), body)},
    )
    assert resp.status_code == 400


def test_event_with_no_listening_workflow_is_stored_without_runs(client, settings, seeded):
    payload = {"eventType": "candidate.updated", "eventId": "evt_9", "payload": {}}
    body = _post(client, settings, "ats", payload).json()
    assert body["duplicate"] is False
    assert body["runs"] == []
