from __future__ import annotations

import uuid


def _arm(rt, action, kind, count):
    with rt.session_factory() as session:
        rt.adapters.faults.arm(session, action, kind, count)
        session.commit()


def test_health_and_catalog_endpoints(client):
    health = client.get("/health").json()
    assert health["status"] == "ok" and health["workflows"] == 5
    workflows = {w["name"]: w for w in client.get("/workflows").json()}
    assert workflows["timesheet_approved"]["trigger"]["poll"]["collection"] == "timesheets"
    assert workflows["candidate_placed"]["steps"][0] == "load_placement"
    actions = {a["name"]: a for a in client.get("/actions").json()}
    assert actions["accounting.create_invoice"] == {
        "name": "accounting.create_invoice",
        "side_effect": True,
        "verifiable": True,
        "duplicate_tolerant": False,
        "description": "Create a draft invoice; a duplicate here is a double bill",
    }
    assert actions["slack.post"]["duplicate_tolerant"] is True
    assert actions["ats.get_candidate"]["side_effect"] is False


def test_runs_listing_filters_and_timeline(client, seeded, sim, runner):
    _arm(seeded, "esign.send_envelope", "permanent", 1)
    bad = sim.placement()["runs"][0]
    good = sim.placement()["runs"][0]
    runner.run_pending()

    assert {r["id"] for r in client.get("/runs").json()} == {bad, good}
    dead = client.get("/runs", params={"status": "dead_lettered"}).json()
    assert [r["id"] for r in dead] == [bad]
    assert client.get("/runs", params={"workflow": "onboarding_signed"}).json() == []

    detail = client.get(f"/runs/{good}").json()
    assert detail["status"] == "succeeded"
    assert [s["step"] for s in detail["timeline"]][:3] == [
        "load_placement",
        "load_candidate",
        "load_client",
    ]
    assert detail["event"]["source"] == "ats" and detail["event"]["type"] == "placement.created"
    assert detail["context"]["steps"]["send_packet"]["status"] == "sent"

    assert client.get(f"/runs/{uuid.uuid4()}").status_code == 404
    assert client.get("/runs/not-a-uuid").status_code == 404


def test_replay_endpoint(client, seeded, sim, runner):
    _arm(seeded, "esign.send_envelope", "permanent", 1)
    run_id = sim.placement()["runs"][0]
    runner.run_pending()
    assert client.get(f"/runs/{run_id}").json()["status"] == "dead_lettered"
    bad = client.post(f"/runs/{run_id}/replay", params={"from_step": "notify_recruiter"})
    assert bad.status_code == 409 and "after the failed step" in bad.json()["detail"]
    assert client.post(f"/runs/{run_id}/replay", params={"from_step": "nope"}).status_code == 409
    replayed = client.post(f"/runs/{run_id}/replay", params={"from_step": "load_client"}).json()
    assert replayed["current_step"] == "load_client"
    assert replayed["status"] == "pending" and replayed["replay_count"] == 1
    assert client.post(f"/runs/{run_id}/replay").status_code == 409  # not dead-lettered now
    runner.run_pending()
    assert client.get(f"/runs/{run_id}").json()["status"] == "succeeded"


def test_stats_reports_unattended_rate(client, seeded, sim, runner):
    _arm(seeded, "esign.send_envelope", "permanent", 1)
    sim.placement()
    sim.placement()
    sim.placement()
    sim.placement()
    runner.run_pending()
    stats = client.get("/stats").json()
    assert stats["total"] == 4
    assert stats["by_status"]["succeeded"] == 3 and stats["by_status"]["dead_lettered"] == 1
    assert stats["unattended_rate"] == 0.75


def test_ops_pages_render(client, seeded, sim, runner):
    _arm(seeded, "payroll.upsert_worker", "permanent", 1)
    run_id = sim.placement()["runs"][0]
    runner.run_pending()
    page = client.get("/ops")
    assert page.status_code == 200
    assert "candidate_placed" in page.text and "dead_lettered" in page.text
    filtered = client.get("/ops", params={"status": "succeeded"}).text
    assert run_id not in filtered
    detail = client.get(f"/ops/runs/{run_id}")
    assert detail.status_code == 200
    assert "Needs attention" in detail.text
    assert "injected permanent failure" in detail.text
    assert "create_worker" in detail.text


def test_faults_and_sandbox_inspection(client, seeded):
    assert client.post("/faults", json={"action": "nope.x", "kind": "transient"}).status_code == 404
    resp = client.post("/faults", json={"action": "slack.post", "kind": "transient", "count": 2})
    assert resp.status_code == 201 and resp.json()["remaining"] == 2
    assert client.post("/faults", json={"action": "slack.post", "kind": "weird"}).status_code == 422
    candidates = client.get("/sandbox/ats/candidates").json()
    assert len(candidates) == 12 and candidates[0]["email"].endswith("@example.com")
