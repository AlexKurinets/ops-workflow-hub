"""Produce synthetic events the way the real applications would, for demos and smoke tests."""

from __future__ import annotations

import json
import random
from datetime import date, timedelta
from typing import Any

import httpx

from hub.adapters.webhooks import extract
from hub.api.app import SIGNATURE_HEADER, sign
from hub.engine.errors import PermanentError
from hub.engine.ingest import ingest
from hub.runtime import Runtime

FIRST_NAMES = ["Jordan", "Mei", "Theo", "Amara", "Luis", "Hanna", "Kwame", "Sofia", "Nikolai"]
LAST_NAMES = ["Brightwater", "Kessler", "Adeyemi", "Palladino", "Sorensen", "Macallister"]
COMPANIES = [
    ("Ridgeview Pediatrics", "ridgeviewpeds.example.com"),
    ("Copperline Freight", "copperlinefreight.example.com"),
    ("Juniper Hill Senior Living", "juniperhill.example.com"),
    ("Tidewater Marine Supply", "tidewatermarine.example.com"),
]
JOB_TITLES = ["Shipping Clerk", "Dental Assistant", "Line Lead", "Teller", "Event Staff"]


class Simulator:
    def __init__(self, runtime: Runtime, api_url: str | None = None, seed: int | None = None):
        self.rt = runtime
        self.api_url = api_url.rstrip("/") if api_url else None
        self.rng = random.Random(seed)

    # ----- delivery ----------------------------------------------------------------------

    def deliver(self, source: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST a signed webhook to the API, or ingest in-process when no API URL is set."""
        if self.api_url:
            body = json.dumps(payload).encode("utf-8")
            secret = self.rt.settings.webhook_secret_for(source)
            resp = httpx.post(
                f"{self.api_url}/webhooks/{source}",
                content=body,
                headers={"Content-Type": "application/json", SIGNATURE_HEADER: sign(secret, body)},
                timeout=10.0,
            )
            resp.raise_for_status()
            return resp.json()
        event_type, external_id, data = extract(source, payload)
        with self.rt.session_factory() as session:
            result = ingest(
                session, self.rt.catalog, source, event_type, external_id, data, payload
            )
            session.commit()
        return {
            "event_id": str(result.event_id),
            "duplicate": result.duplicate,
            "runs": [str(r) for r in result.run_ids],
        }

    # ----- scenarios ---------------------------------------------------------------------

    def placement(self) -> dict[str, Any]:
        """A recruiter places a brand-new candidate: the ATS sends placement.created."""
        store = self.rt.adapters.store
        first = self.rng.choice(FIRST_NAMES)
        last = self.rng.choice(LAST_NAMES)
        suffix = self.rng.randint(100, 999)
        with self.rt.session_factory() as session:
            clients = store.find(session, "ats", "clients")
            if not clients:
                raise PermanentError("no clients in the ATS sandbox; run hub seed first")
            client = self.rng.choice(clients)
            cid = store.new_id("cand")
            store.put(
                session,
                "ats",
                "candidates",
                cid,
                {
                    "first_name": first,
                    "last_name": last,
                    "email": f"{first}.{last}{suffix}@example.com".lower(),
                    "phone": f"555-01{self.rng.randint(50, 99)}",
                    "status": "placed",
                },
            )
            pid = store.new_id("plc")
            pay = round(self.rng.uniform(18, 30), 2)
            store.put(
                session,
                "ats",
                "placements",
                pid,
                {
                    "candidate_id": cid,
                    "client_id": client["id"],
                    "job_title": self.rng.choice(JOB_TITLES),
                    "start_date": (date.today() + timedelta(days=7)).isoformat(),
                    "weekly_hours": self.rng.choice([20, 32, 40]),
                    "bill_rate": round(pay * 1.55, 2),
                    "pay_rate": pay,
                    "recruiter": "J. Ramos",
                    "status": "active",
                },
            )
            session.commit()
        payload = self.placement_webhook(pid, cid)
        return {"placement_id": pid, "candidate_id": cid, **self.deliver("ats", payload)}

    @staticmethod
    def placement_webhook(placement_id: str, candidate_id: str) -> dict[str, Any]:
        return {
            "eventType": "placement.created",
            "eventId": f"evt_{placement_id}",
            "payload": {"placementId": placement_id, "candidateId": candidate_id},
        }

    def duplicate_placement(self) -> dict[str, Any]:
        """Deliver the same placement webhook twice, as applications do on a timeout."""
        first = self.placement()
        payload = self.placement_webhook(first["placement_id"], first["candidate_id"])
        second = self.deliver("ats", payload)
        return {"first": first, "second": second}

    def onboarding_signed(self) -> dict[str, Any]:
        """The newest sent envelope gets signed: the e-signature service reports it."""
        store = self.rt.adapters.store
        with self.rt.session_factory() as session:
            sent = store.find(session, "esign", "envelopes", status="sent")
            if not sent:
                raise PermanentError("no sent envelopes; run the placement scenario first")
            env = sent[-1]
            env["status"] = "completed"
            store.put(session, "esign", "envelopes", env["id"], env)
            session.commit()
        payload = {"event": "envelope.completed", "data": {"envelopeId": env["id"]}}
        return {"envelope_id": env["id"], **self.deliver("esign", payload)}

    def timesheet(self, mismatch: bool = False, unlinked: bool = False) -> dict[str, Any]:
        """Approve a timesheet in the ATS. No webhook: the scheduler's poller picks it up."""
        store = self.rt.adapters.store
        with self.rt.session_factory() as session:
            placements = store.find(session, "ats", "placements", status="active")
            if not placements:
                raise PermanentError("no active placements; run hub seed first")
            chosen = None
            if unlinked:
                linked = self._linked_ats_ids(session)
                chosen = next((p for p in placements if p["candidate_id"] not in linked), None)
                if chosen is None:
                    raise PermanentError("every active placement is already linked to payroll")
            placement = chosen or self.rng.choice(placements)
            hours = placement["weekly_hours"] + (12 if mismatch else self.rng.choice([0, -1, 1]))
            end = date.today() - timedelta(days=1)
            tid = store.new_id("ts")
            rec = store.put(
                session,
                "ats",
                "timesheets",
                tid,
                {
                    "placement_id": placement["id"],
                    "candidate_id": placement["candidate_id"],
                    "period_start": (end - timedelta(days=6)).isoformat(),
                    "period_end": end.isoformat(),
                    "hours": hours,
                    "status": "approved",
                },
            )
            session.commit()
        return {"timesheet": rec, "note": "approved in the ATS; the scheduler poll will pick it up"}

    @staticmethod
    def _linked_ats_ids(session) -> set[str]:
        from sqlalchemy import select

        from hub.db.models import EntityLink

        stmt = select(EntityLink.external_id).where(EntityLink.system == "ats")
        return set(session.execute(stmt).scalars())

    def payroll_worker(self, candidate_id: str | None = None) -> dict[str, Any]:
        """Create a candidate's worker record in payroll, as the ops team would in that application.

        This is the manual half of fixing a timesheet that dead-lettered at require_worker:
        create the worker here, link it with `hub link`, then replay from find_worker. The id
        follows the seed convention (cand_1006 becomes wk_7006) so the README can name it.
        """
        store = self.rt.adapters.store
        with self.rt.session_factory() as session:
            if candidate_id is None:
                linked = self._linked_ats_ids(session)
                placements = store.find(session, "ats", "placements", status="active")
                chosen = next((p for p in placements if p["candidate_id"] not in linked), None)
                if chosen is None:
                    raise PermanentError("every active placement is already linked to payroll")
                candidate_id = chosen["candidate_id"]
            candidate = store.get(session, "ats", "candidates", candidate_id)
            if candidate is None:
                raise PermanentError(f"no ATS candidate {candidate_id}")
            active = store.find(
                session, "ats", "placements", candidate_id=candidate_id, status="active"
            )
            pay_rate = active[0]["pay_rate"] if active else 18.0
            if candidate_id.startswith("cand_1"):
                worker_id = candidate_id.replace("cand_1", "wk_7", 1)
            else:
                worker_id = store.new_id("wk")
            rec = store.put(
                session,
                "payroll",
                "workers",
                worker_id,
                {
                    "email": str(candidate["email"]).lower(),
                    "first_name": candidate["first_name"],
                    "last_name": candidate["last_name"],
                    "pay_rate": pay_rate,
                    "status": "active",
                },
            )
            session.commit()
        return {
            "worker": rec,
            "note": (
                f"created in payroll; now hub link person ats:{candidate_id} payroll:{worker_id}, "
                "then hub replay <run-id> --from-step find_worker"
            ),
        }

    def lead(self, existing_company: bool = False) -> dict[str, Any]:
        """A prospect submits the website form."""
        first = self.rng.choice(FIRST_NAMES)
        last = self.rng.choice(LAST_NAMES)
        if existing_company:
            with self.rt.session_factory() as session:
                companies = self.rt.adapters.store.find(session, "crm", "companies")
            company = companies[0]
            name, domain = company["name"], company["domain"]
        else:
            name, domain = self.rng.choice(COMPANIES)
        payload = {
            "submission_id": f"sub_{self.rng.randint(100000, 999999)}",
            "form": "contact-us",
            "fields": {
                "first_name": first,
                "last_name": last,
                "email": f"{first}.{last}@{domain}".lower(),
                "company": name,
                "phone": f"555-01{self.rng.randint(20, 49)}",
                "message": "We need three temporary staff for a six-week project next month.",
            },
        }
        return {"submission_id": payload["submission_id"], **self.deliver("forms", payload)}
