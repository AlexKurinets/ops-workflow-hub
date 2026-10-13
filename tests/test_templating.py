from __future__ import annotations

import pytest

from hub.engine.errors import PermanentError
from hub.engine.templating import Templating

CTX = {
    "event": {"data": {"hours": 38, "rate": 31.5, "email": "Mira.Falkenrath@Example.com"}},
    "steps": {"check": {"mismatch": False, "tags": ["a", "b"]}},
}


def test_single_expression_keeps_native_types():
    t = Templating()
    assert t.render("{{ event.data.hours }}", CTX) == 38
    assert t.render("{{ event.data.hours * event.data.rate }}", CTX) == pytest.approx(1197.0)
    assert t.render("{{ steps.check.mismatch }}", CTX) is False
    assert t.render("{{ steps.check.tags }}", CTX) == ["a", "b"]


def test_mixed_text_renders_to_string():
    t = Templating()
    out = t.render("{{ event.data.hours }} hours at {{ event.data.rate | money }}", CTX)
    assert out == "38 hours at $31.50"


def test_nested_structures_are_rendered_recursively():
    t = Templating()
    out = t.render({"lines": [{"qty": "{{ event.data.hours }}", "note": "plain"}], "n": 3}, CTX)
    assert out == {"lines": [{"qty": 38, "note": "plain"}], "n": 3}


def test_undefined_value_is_a_permanent_error():
    t = Templating()
    with pytest.raises(PermanentError, match="undefined"):
        t.render("{{ event.data.missing }}", CTX)


def test_when_conditions_evaluate_to_bool():
    t = Templating()
    assert t.evaluate("not steps.check.mismatch", CTX) is True
    assert t.evaluate("event.data.hours > 40", CTX) is False
    assert t.evaluate("steps.check.tags | length == 2", CTX) is True


def test_filters_and_methods_work_in_sandbox():
    t = Templating()
    assert t.render('{{ (event.data.email | lower).split("@")[1] }}', CTX) == "example.com"
