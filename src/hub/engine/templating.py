from __future__ import annotations

import re
from typing import Any

from jinja2 import StrictUndefined, TemplateSyntaxError, Undefined, UndefinedError
from jinja2.sandbox import SandboxedEnvironment

from hub.engine.errors import DefinitionError, PermanentError

_SINGLE_EXPR = re.compile(r"^\s*\{\{\s*(.+?)\s*\}\}\s*$", re.DOTALL)


def _build_env() -> SandboxedEnvironment:
    env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
    env.filters["money"] = lambda v: f"${float(v):,.2f}"
    env.filters["abs"] = abs
    return env


ENV = _build_env()


class Templating:
    """Renders the with and when fields of a step against the run context.

    A string that is exactly one {{ expression }} is evaluated to its native value, so a
    step can pass numbers, booleans, lists and dicts through, not just text. Anything else
    is rendered as a string. Undefined names fail the step permanently: a typo in a
    workflow file should never silently pass an empty value to a payroll system.
    """

    def __init__(self, env: SandboxedEnvironment = ENV) -> None:
        self.env = env

    def validate(self, value: Any, where: str) -> None:
        try:
            self._walk(value, self._compile)
        except TemplateSyntaxError as exc:
            raise DefinitionError(f"{where}: template syntax error: {exc}") from exc

    def render(self, value: Any, context: dict[str, Any]) -> Any:
        try:
            return self._walk(value, lambda s: self._render_str(s, context))
        except UndefinedError as exc:
            raise PermanentError(f"template refers to an undefined value: {exc}") from exc
        except TemplateSyntaxError as exc:
            raise PermanentError(f"template syntax error: {exc}") from exc

    def evaluate(self, expression: str, context: dict[str, Any]) -> Any:
        """Evaluate a bare expression (no braces), used for when conditions."""
        try:
            value = self.env.compile_expression(expression, undefined_to_none=False)(**context)
        except UndefinedError as exc:
            raise PermanentError(f"condition refers to an undefined value: {exc}") from exc
        except TemplateSyntaxError as exc:
            raise PermanentError(f"condition syntax error: {exc}") from exc
        if isinstance(value, Undefined):
            raise PermanentError(f"condition refers to an undefined value: {expression}")
        return value

    def validate_expression(self, expression: str, where: str) -> None:
        try:
            self.env.compile_expression(expression, undefined_to_none=False)
        except TemplateSyntaxError as exc:
            raise DefinitionError(f"{where}: condition syntax error: {exc}") from exc

    def _compile(self, s: str) -> Any:
        m = _SINGLE_EXPR.match(s)
        if m and "{{" not in m.group(1):
            return self.env.compile_expression(m.group(1), undefined_to_none=False)
        return self.env.from_string(s)

    def _render_str(self, s: str, context: dict[str, Any]) -> Any:
        if "{{" not in s and "{%" not in s:
            return s
        m = _SINGLE_EXPR.match(s)
        if m and "{{" not in m.group(1):
            value = self.env.compile_expression(m.group(1), undefined_to_none=False)(**context)
            if isinstance(value, Undefined):
                raise PermanentError(f"template refers to an undefined value: {s.strip()}")
            return value
        return self.env.from_string(s).render(**context)

    def _walk(self, value: Any, fn: Any) -> Any:
        if isinstance(value, str):
            return fn(value)
        if isinstance(value, dict):
            return {k: self._walk(v, fn) for k, v in value.items()}
        if isinstance(value, list):
            return [self._walk(v, fn) for v in value]
        return value
