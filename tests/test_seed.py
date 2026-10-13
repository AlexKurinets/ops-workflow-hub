from __future__ import annotations

import re

from sqlalchemy import select

from hub.db.models import EntityLink, SandboxRecord


def test_seed_is_synthetic_and_internally_consistent(seeded):
    with seeded.session_factory() as session:
        rows = list(session.execute(select(SandboxRecord)).scalars())
        links = list(session.execute(select(EntityLink)).scalars())
    emails = [r.data["email"] for r in rows if "email" in r.data]
    assert emails and all(e.endswith("@example.com") or ".example.com" in e for e in emails)
    phones = [r.data["phone"] for r in rows if "phone" in r.data]
    assert phones and all(re.fullmatch(r"555-01\d\d", p) for p in phones)
    domains = [r.data["domain"] for r in rows if "domain" in r.data]
    assert domains and all(d.endswith(".example.com") for d in domains)

    placements = [r.data for r in rows if r.collection == "placements"]
    candidates = {r.record_id for r in rows if r.collection == "candidates"}
    clients = {r.record_id for r in rows if r.collection == "clients"}
    assert all(p["candidate_id"] in candidates and p["client_id"] in clients for p in placements)
    assert sum(p["status"] == "active" for p in placements) == 6

    assert len(links) == 10
    payroll_ids = {r.record_id for r in rows if r.system == "payroll" and r.collection == "workers"}
    assert {lk.external_id for lk in links if lk.system == "payroll"} == payroll_ids
