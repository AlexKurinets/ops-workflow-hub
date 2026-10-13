"""Synthetic data for the sandbox applications.

Every person, company, address, phone number and figure here is invented. Phone numbers use
the fictional 555-01xx range and every email address is under example.com.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from sqlalchemy.orm import Session

from hub.adapters.sandbox import SandboxStore
from hub.db.models import EntityLink

CANDIDATES = [
    ("Priya", "Lindqvist"),
    ("Marcus", "Oyelaran"),
    ("Dana", "Whitcombe"),
    ("Tomas", "Reinholt"),
    ("Aisha", "Beaumont"),
    ("Kenji", "Albrecht"),
    ("Rosa", "Villanueva"),
    ("Elliot", "Marchetti"),
    ("Noor", "Haddadin"),
    ("Grace", "Okonkwo"),
    ("Samuel", "Trevino"),
    ("Ingrid", "Falkenrath"),
]

CLIENTS = [
    ("Harbor Point Logistics", "harborpointlogistics.example.com", "Lena Marsh"),
    ("Bluefin Dental Group", "bluefindental.example.com", "Owen Castellano"),
    ("Cedar Ridge Manufacturing", "cedarridgemfg.example.com", "Harriet Bloom"),
    ("Northgate Credit Union", "northgatecu.example.com", "Devon Akintola"),
    ("Summit Hospitality Partners", "summithospitality.example.com", "Mara Lindgren"),
    ("Lakeshore Analytics", "lakeshoreanalytics.example.com", "Felix Oduya"),
]

# (candidate index, client index, job title, weekly hours, bill rate, pay rate, recruiter, status)
PLACEMENTS = [
    (0, 0, "Warehouse Coordinator", 40, 38.0, 24.0, "J. Ramos", "active"),
    (1, 1, "Front Desk Associate", 32, 31.0, 19.5, "J. Ramos", "active"),
    (2, 2, "Quality Inspector", 40, 42.0, 27.0, "K. Oduya", "active"),
    (3, 3, "Member Services Rep", 40, 34.0, 21.0, "K. Oduya", "active"),
    (4, 4, "Banquet Server", 20, 29.0, 18.0, "S. Whitfield", "active"),
    (5, 5, "Data Entry Specialist", 40, 33.0, 20.5, "S. Whitfield", "active"),
    (6, 0, "Forklift Operator", 40, 36.0, 23.0, "J. Ramos", "ended"),
    (7, 2, "Machine Operator", 40, 40.0, 25.5, "K. Oduya", "ended"),
]

# (placement index, hours delta from weekly hours, status)
TIMESHEETS = [
    (0, 0, "approved"),
    (1, -2, "approved"),
    (2, 12, "approved"),
    (3, 0, "pending"),
]

# Candidates whose active placement already has a payroll worker and a crosswalk link.
LINKED_CANDIDATES = {0, 1, 2, 3, 4}


def _email(first: str, last: str) -> str:
    return f"{first}.{last}@example.com".lower()


def seed(session: Session, store: SandboxStore, base_date: date | None = None) -> dict[str, int]:
    base = base_date or date(2026, 10, 5)
    counts: dict[str, int] = {}

    candidate_ids: list[str] = []
    for i, (first, last) in enumerate(CANDIDATES):
        cid = f"cand_{1001 + i}"
        candidate_ids.append(cid)
        store.put(
            session,
            "ats",
            "candidates",
            cid,
            {
                "first_name": first,
                "last_name": last,
                "email": _email(first, last),
                "phone": f"555-01{10 + i:02d}",
                "status": "placed" if i < 6 else "available",
            },
        )
    counts["ats.candidates"] = len(candidate_ids)

    client_ids: list[str] = []
    for i, (name, domain, contact) in enumerate(CLIENTS):
        clid = f"cli_{201 + i}"
        client_ids.append(clid)
        store.put(
            session,
            "ats",
            "clients",
            clid,
            {"name": name, "domain": domain, "billing_contact": contact},
        )
    counts["ats.clients"] = len(client_ids)

    placement_ids: list[str] = []
    for i, (ci, cli, title, hours, bill, pay, recruiter, status) in enumerate(PLACEMENTS):
        pid = f"plc_{5001 + i}"
        placement_ids.append(pid)
        store.put(
            session,
            "ats",
            "placements",
            pid,
            {
                "candidate_id": candidate_ids[ci],
                "client_id": client_ids[cli],
                "job_title": title,
                "start_date": (base - timedelta(days=7 * (i + 1))).isoformat(),
                "weekly_hours": hours,
                "bill_rate": bill,
                "pay_rate": pay,
                "recruiter": recruiter,
                "status": status,
            },
        )
    counts["ats.placements"] = len(placement_ids)

    for i, (pi, delta, status) in enumerate(TIMESHEETS):
        placement = PLACEMENTS[pi]
        period_end = base - timedelta(days=1)
        store.put(
            session,
            "ats",
            "timesheets",
            f"ts_{9001 + i}",
            {
                "placement_id": placement_ids[pi],
                "candidate_id": candidate_ids[placement[0]],
                "period_start": (period_end - timedelta(days=6)).isoformat(),
                "period_end": period_end.isoformat(),
                "hours": placement[3] + delta,
                "status": status,
            },
        )
    counts["ats.timesheets"] = len(TIMESHEETS)

    workers = 0
    for ci in sorted(LINKED_CANDIDATES):
        first, last = CANDIDATES[ci]
        placement = next(p for p in PLACEMENTS if p[0] == ci)
        wid = f"wk_{7001 + ci}"
        store.put(
            session,
            "payroll",
            "workers",
            wid,
            {
                "email": _email(first, last),
                "first_name": first,
                "last_name": last,
                "pay_rate": placement[5],
                "status": "active",
            },
        )
        canonical = uuid.uuid4()
        session.add(
            EntityLink(
                entity_type="person",
                system="ats",
                external_id=candidate_ids[ci],
                canonical_id=canonical,
            )
        )
        session.add(
            EntityLink(
                entity_type="person", system="payroll", external_id=wid, canonical_id=canonical
            )
        )
        workers += 1
    counts["payroll.workers"] = workers
    counts["crosswalk.links"] = workers * 2

    for i, (name, domain, contact) in enumerate(CLIENTS):
        coid = f"co_{301 + i}"
        store.put(
            session,
            "crm",
            "companies",
            coid,
            {"name": name, "domain": domain, "owner": "sales-inbox"},
        )
        first, last = contact.split(" ", 1)
        store.put(
            session,
            "crm",
            "contacts",
            f"ct_{401 + i}",
            {
                "email": f"{first}.{last}@{domain}".lower(),
                "first_name": first,
                "last_name": last,
                "company_id": coid,
            },
        )
    counts["crm.companies"] = len(CLIENTS)
    counts["crm.contacts"] = len(CLIENTS)

    for i, (name, _domain, _contact) in enumerate(CLIENTS[:3]):
        store.put(session, "accounting", "customers", f"cus_{601 + i}", {"name": name})
    counts["accounting.customers"] = 3

    session.flush()
    return counts
