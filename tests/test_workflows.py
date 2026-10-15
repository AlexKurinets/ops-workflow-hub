"""End-to-end runs of the shipped workflows against the sandbox applications."""

from __future__ import annotations

from hub.adapters.base import ActionContext
from hub.db.models import RunStatus
from hub.engine.runner import replay_run
from hub.engine.scheduler import Scheduler
from tests.helpers import get_run, records, statuses, step_runs


def test_candidate_placed_creates_worker_links_ids_sends_packet_and_notifies(seeded, sim, runner):
    out = sim.placement()
    assert runner.run_pending() == 1
    run = get_run(seeded, out["runs"][0])
    assert run.status == RunStatus.succeeded
    assert [s[0] for s in statuses(seeded, run.id)] == [
        "load_placement",
        "load_candidate",
        "load_client",
        "find_worker",
        "create_worker",
        "link_worker",
        "send_packet",
        "notify_recruiter",
        "log_placement",
    ]
    assert all(s[2] == "succeeded" for s in statuses(seeded, run.id))
    candidate = records(seeded, "ats", "candidates", id=out["candidate_id"])[0]
    workers = records(seeded, "payroll", "workers", email=candidate["email"])
    assert len(workers) == 1
    envelopes = records(seeded, "esign", "envelopes", recipient_email=candidate["email"])
    assert len(envelopes) == 1 and envelopes[0]["fields"]["placement_id"] == out["placement_id"]
    slack = records(seeded, "slack", "messages", channel="#placements")
    assert len(slack) == 1 and candidate["last_name"] in slack[0]["text"]
    rows = records(seeded, "sheets", "rows", sheet="placements")
    assert len(rows) == 1 and rows[0]["row"]["placement_id"] == out["placement_id"]
    # outputs are checkpointed in the run context
    assert run.context["steps"]["create_worker"]["id"] == workers[0]["id"]
    assert run.context["steps"]["link_worker"]["created"] == 2


def test_candidate_placed_skips_worker_creation_when_already_linked(seeded, sim, runner):
    # Seeded candidate cand_1001 already has a payroll worker and a crosswalk link.
    payload = sim.placement_webhook("plc_5001", "cand_1001")
    out = sim.deliver("ats", payload)
    runner.run_pending()
    run = get_run(seeded, out["runs"][0])
    assert run.status == RunStatus.succeeded
    by_step = {s[0]: s[2] for s in statuses(seeded, run.id)}
    assert by_step["create_worker"] == "skipped"
    assert by_step["link_worker"] == "skipped"
    assert len(records(seeded, "payroll", "workers", email="priya.lindqvist@example.com")) == 1


def test_timesheet_within_tolerance_is_paid_and_invoiced_once(seeded, runner, clock):
    sched = Scheduler(seeded.session_factory, seeded.catalog, seeded.adapters, clock)
    assert sched.tick()["polled"] == 3  # three approved seeded timesheets, one pending
    runner.run_pending()
    with seeded.session_factory() as session:
        from sqlalchemy import select

        from hub.db.models import Run

        runs = list(session.execute(select(Run)).scalars())
    by_status = {}
    for r in runs:
        by_status.setdefault(r.status.value, 0)
        by_status[r.status.value] += 1
    assert by_status == {"succeeded": 3}
    hours = records(seeded, "payroll", "hours")
    invoices = records(seeded, "accounting", "invoices")
    assert len(hours) == 2 and len(invoices) == 2
    # ts_9001: 40 hours at 38.00 for Harbor Point Logistics
    harbor = next(i for i in invoices if i["memo"].startswith("Priya"))
    assert harbor["total"] == 40 * 38.0
    assert harbor["lines"][0]["quantity"] == 40
    exceptions = records(seeded, "sheets", "rows", sheet="timesheet_exceptions")
    assert len(exceptions) == 1 and exceptions[0]["row"]["submitted_hours"] == 52
    billing = records(seeded, "slack", "messages", channel="#billing")
    assert len(billing) == 1 and "52 hours against 40" in billing[0]["text"]
    # a customer that did not exist in accounting was created once
    assert len(records(seeded, "accounting", "customers", name="Bluefin Dental Group")) == 1


def test_timesheet_for_unlinked_candidate_dead_letters_then_replays(seeded, sim, runner, clock):
    sim.timesheet(unlinked=True)
    sched = Scheduler(seeded.session_factory, seeded.catalog, seeded.adapters, clock)
    sched.tick()
    runner.run_pending()
    with seeded.session_factory() as session:
        from sqlalchemy import select

        from hub.db.models import Run

        dead = session.execute(
            select(Run).where(Run.status == RunStatus.dead_lettered)
        ).scalar_one()
    assert dead.current_step == "require_worker"
    assert "cand_1006" in dead.dead_letter_reason
    alerts = records(seeded, "slack", "messages", channel="#ops-alerts")
    assert len(alerts) == 1 and f"/ops/runs/{dead.id}" in alerts[0]["text"]
    loads_before = len(step_runs(seeded, dead.id, "load_timesheet"))

    # The operator creates the payroll worker (the simulator stands in for the payroll
    # application), links it, and replays.
    created = sim.payroll_worker("cand_1006")["worker"]
    assert created["id"] == "wk_7006" and created["pay_rate"] == 20.5
    with seeded.session_factory() as session:
        store = seeded.adapters.store
        ctx = ActionContext(session, seeded.settings, clock, store, "test", "link", None)
        seeded.adapters["crosswalk"].link(ctx, "person", {"ats": "cand_1006", "payroll": "wk_7006"})
        session.commit()
        replayed = replay_run(session, dead.id, seeded.catalog, from_step="find_worker")
        assert replayed.status == RunStatus.pending and replayed.replay_count == 1
    runner.run_pending()
    run = get_run(seeded, dead.id)
    assert run.status == RunStatus.succeeded
    assert len(step_runs(seeded, dead.id, "load_timesheet")) == loads_before  # not re-executed
    assert len(records(seeded, "payroll", "hours", worker_id="wk_7006")) == 1
    assert [s for s in statuses(seeded, dead.id) if s[0] == "find_worker"][-1][2] == "succeeded"


def test_client_lead_creates_company_contact_activity_and_acknowledgement(seeded, sim, runner):
    out = sim.lead()
    runner.run_pending()
    run = get_run(seeded, out["runs"][0])
    assert run.status == RunStatus.succeeded, run.dead_letter_reason
    assert {s[0]: s[2] for s in statuses(seeded, run.id)}["create_company"] == "succeeded"
    contacts = records(seeded, "crm", "contacts", email=run.context["steps"]["normalise"]["email"])
    assert len(contacts) == 1
    activities = records(seeded, "crm", "activities", contact_id=contacts[0]["id"])
    assert len(activities) == 1 and activities[0]["kind"] == "web_lead"
    emails = records(seeded, "email", "messages", to=contacts[0]["email"])
    assert len(emails) == 1
    assert "New account." in records(seeded, "slack", "messages", channel="#new-leads")[0]["text"]


def test_client_lead_for_existing_company_skips_company_creation(seeded, sim, runner):
    out = sim.lead(existing_company=True)
    runner.run_pending()
    run = get_run(seeded, out["runs"][0])
    assert run.status == RunStatus.succeeded
    assert {s[0]: s[2] for s in statuses(seeded, run.id)}["create_company"] == "skipped"
    assert len(records(seeded, "crm", "companies", domain="harborpointlogistics.example.com")) == 1
    assert (
        "Existing account." in records(seeded, "slack", "messages", channel="#new-leads")[0]["text"]
    )


def test_onboarding_signed_updates_ats_and_crm(seeded, sim, runner):
    sim.placement()
    runner.run_pending()
    out = sim.onboarding_signed()
    runner.run_pending()
    run = get_run(seeded, out["runs"][0])
    assert run.status == RunStatus.succeeded, run.dead_letter_reason
    env = records(seeded, "esign", "envelopes", id=out["envelope_id"])[0]
    candidate = records(seeded, "ats", "candidates", id=env["fields"]["candidate_id"])[0]
    assert candidate["status"] == "onboarding_complete"
    contact = records(seeded, "crm", "contacts", email=candidate["email"])[0]
    assert (
        records(seeded, "crm", "activities", contact_id=contact["id"])[0]["kind"]
        == "onboarding_signed"
    )


def test_nightly_reconciliation_reports_the_unlinked_candidate(seeded, runner, clock):
    sched = Scheduler(seeded.session_factory, seeded.catalog, seeded.adapters, clock)
    sched.tick()
    clock.advance(22 * 3600 + 30)  # 02:00:30 Chicago the next day
    assert sched.tick()["fired"] == 1
    runner.run_pending()
    rows = records(seeded, "sheets", "rows", sheet="payroll_reconciliation")
    assert len(rows) == 1
    assert rows[0]["row"]["missing"] == "kenji.albrecht@example.com"
    assert rows[0]["row"]["checked"] == 6
    texts = [m["text"] for m in records(seeded, "slack", "messages", channel="#billing")]
    assert any("1 of 6 active placements" in t for t in texts)
