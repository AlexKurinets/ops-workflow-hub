from __future__ import annotations

from hub.adapters.base import ActionContext


def _ctx(rt, session):
    return ActionContext(session, rt.settings, rt.clock, rt.adapters.store, "t", "s", None)


def test_link_then_lookup_in_both_directions(rt):
    xw = rt.adapters["crosswalk"]
    with rt.session_factory() as session:
        ctx = _ctx(rt, session)
        out = xw.link(ctx, "person", {"ats": "cand_1", "payroll": "wk_9"})
        assert out["created"] == 2 and out["merged"] is False
        assert xw.lookup(ctx, "person", "ats", "cand_1", "payroll") == {
            "found": True,
            "canonical_id": out["canonical_id"],
            "target_id": "wk_9",
        }
        assert xw.lookup(ctx, "person", "payroll", "wk_9", "ats")["target_id"] == "cand_1"
        # known in one application, not yet in the other
        assert xw.lookup(ctx, "person", "ats", "cand_1", "crm") == {
            "found": False,
            "canonical_id": out["canonical_id"],
            "target_id": None,
        }
        # unknown everywhere
        assert xw.lookup(ctx, "person", "ats", "nope", "payroll")["found"] is False


def test_linking_an_existing_id_adds_to_its_identity(rt):
    xw = rt.adapters["crosswalk"]
    with rt.session_factory() as session:
        ctx = _ctx(rt, session)
        first = xw.link(ctx, "person", {"ats": "cand_1", "payroll": "wk_9"})
        second = xw.link(ctx, "person", {"payroll": "wk_9", "crm": "ct_4"})
        assert second["canonical_id"] == first["canonical_id"]
        assert second["created"] == 1
        assert xw.lookup(ctx, "person", "ats", "cand_1", "crm")["target_id"] == "ct_4"


def test_linking_two_identities_merges_them(rt):
    xw = rt.adapters["crosswalk"]
    with rt.session_factory() as session:
        ctx = _ctx(rt, session)
        a = xw.link(ctx, "person", {"ats": "cand_1"})
        b = xw.link(ctx, "person", {"payroll": "wk_9"})
        assert a["canonical_id"] != b["canonical_id"]
        merged = xw.link(ctx, "person", {"ats": "cand_1", "payroll": "wk_9"})
        assert merged["merged"] is True and merged["created"] == 0
        assert xw.lookup(ctx, "person", "ats", "cand_1", "payroll")["target_id"] == "wk_9"


def test_entity_types_are_separate_namespaces(rt):
    xw = rt.adapters["crosswalk"]
    with rt.session_factory() as session:
        ctx = _ctx(rt, session)
        xw.link(ctx, "person", {"ats": "1", "payroll": "p1"})
        xw.link(ctx, "company", {"ats": "1", "accounting": "c1"})
        assert xw.lookup(ctx, "person", "ats", "1", "accounting")["found"] is False
        assert xw.lookup(ctx, "company", "ats", "1", "accounting")["target_id"] == "c1"
