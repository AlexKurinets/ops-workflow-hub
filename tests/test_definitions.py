from __future__ import annotations

from pathlib import Path

import pytest

from hub.config import REPO_ROOT
from hub.engine.definitions import Catalog, load_workflow
from hub.engine.errors import DefinitionError

WORKFLOWS = REPO_ROOT / "workflows"

MINIMAL = """
name: {name}
version: 1
trigger:
  event:
    source: ats
    type: thing.happened
steps:
{steps}
"""


def _write(tmp_path: Path, name: str, steps: str, filename: str | None = None) -> Path:
    path = tmp_path / (filename or f"{name}.yaml")
    path.write_text(MINIMAL.format(name=name, steps=steps), encoding="utf-8")
    return path


def test_shipped_workflows_load_and_validate_against_registry(rt):
    names = set(rt.catalog.defs)
    assert names == {
        "candidate_placed",
        "onboarding_signed",
        "timesheet_approved",
        "client_lead_received",
        "nightly_payroll_reconciliation",
    }
    kinds = {wf.name: wf.trigger.kind for wf in rt.catalog.defs.values()}
    assert kinds["timesheet_approved"] == "poll"
    assert kinds["nightly_payroll_reconciliation"] == "schedule"
    assert kinds["candidate_placed"] == "event"


def test_unknown_action_is_rejected_at_load_time(tmp_path):
    _write(tmp_path, "wf", "  - id: a\n    action: payroll.teleport\n")
    with pytest.raises(DefinitionError, match="unknown action payroll.teleport"):
        Catalog.load(tmp_path, {"hub.set"})


def test_duplicate_step_ids_are_rejected(tmp_path):
    path = _write(
        tmp_path, "wf", "  - id: a\n    action: hub.set\n  - id: a\n    action: hub.set\n"
    )
    with pytest.raises(DefinitionError, match="duplicate step id a"):
        load_workflow(path)


def test_template_syntax_errors_are_caught_before_runtime(tmp_path):
    _write(
        tmp_path, "wf", "  - id: a\n    action: hub.set\n    with:\n      x: '{{ event.data. }}'\n"
    )
    with pytest.raises(DefinitionError, match="template syntax error"):
        Catalog.load(tmp_path, {"hub.set"})


def test_condition_syntax_errors_are_caught_before_runtime(tmp_path):
    _write(tmp_path, "wf", "  - id: a\n    action: hub.set\n    when: 'not ('\n")
    with pytest.raises(DefinitionError, match="condition syntax error"):
        Catalog.load(tmp_path, {"hub.set"})


def test_trigger_must_have_exactly_one_kind(tmp_path):
    path = tmp_path / "wf.yaml"
    path.write_text(
        "name: wf\nversion: 1\ntrigger:\n  event: {source: a, type: b}\n"
        "  schedule: {cron: '0 2 * * *'}\nsteps:\n  - id: a\n    action: hub.set\n",
        encoding="utf-8",
    )
    with pytest.raises(DefinitionError, match="exactly one"):
        load_workflow(path)


def test_unknown_fields_are_rejected(tmp_path):
    _write(tmp_path, "wf", "  - id: a\n    action: hub.set\n    retries: 3\n")
    with pytest.raises(DefinitionError, match="retries"):
        Catalog.load(tmp_path, {"hub.set"})


def test_same_workflow_name_in_two_files_is_rejected(tmp_path):
    _write(tmp_path, "wf", "  - id: a\n    action: hub.set\n", filename="one.yaml")
    _write(tmp_path, "wf", "  - id: a\n    action: hub.set\n", filename="two.yaml")
    with pytest.raises(DefinitionError, match="defined twice"):
        Catalog.load(tmp_path, {"hub.set"})


def test_for_event_matches_event_poll_and_schedule_triggers(rt):
    cat = rt.catalog
    assert [wf.name for wf in cat.for_event("ats", "placement.created")] == ["candidate_placed"]
    assert [wf.name for wf in cat.for_event("ats", "timesheet.approved")] == ["timesheet_approved"]
    assert [wf.name for wf in cat.for_event("schedule", "nightly_payroll_reconciliation")] == [
        "nightly_payroll_reconciliation"
    ]
    assert cat.for_event("ats", "candidate.deleted") == []
