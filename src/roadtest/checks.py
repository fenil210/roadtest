"""Code checks: the expectations that need no model. Each one is a plain rule over the reply."""

from __future__ import annotations

import fnmatch
import inspect
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .adapters import ToolCall, load_callable
from .scenarios import Expect

PASS, FAIL, ERROR, SKIP = "pass", "fail", "error", "skip"

TOOLS_NOT_VISIBLE = (
    "roadtest cannot see this agent's tool calls. Set `agent.response.tool_calls` in roadtest.yaml "
    "(or return `tool_calls` from your adapter). See `roadtest guide setup`."
)


@dataclass
class CheckResult:
    name: str
    kind: str  # code | judge | custom
    status: str  # pass | fail | error | skip
    detail: str = ""
    criterion: str = ""  # the full rubric text, for judge checks

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class CheckContext:
    """What a custom check function receives."""

    text: str
    tool_calls: list[ToolCall] | None
    seconds: float
    messages: list[dict[str, str]] = field(default_factory=list)
    scenario_id: str = ""
    vars: dict[str, Any] = field(default_factory=dict)

    @property
    def tool_names(self) -> list[str]:
        return [call.name for call in self.tool_calls or []]


def run_code_checks(expect: Expect, text: str, tool_calls: list[ToolCall] | None, seconds: float) -> list[CheckResult]:
    results: list[CheckResult] = []
    lowered = text.lower()

    def add(name: str, passed: bool, detail: str = "") -> None:
        results.append(CheckResult(name, "code", PASS if passed else FAIL, "" if passed else detail))

    for needle in expect.contains:
        add(f"contains {needle!r}", needle.lower() in lowered, "the reply does not contain it")
    if expect.contains_any:
        listed = ", ".join(repr(needle) for needle in expect.contains_any)
        add(f"contains any of {listed}", any(n.lower() in lowered for n in expect.contains_any), "the reply contains none of them")
    for needle in expect.not_contains:
        add(f"does not contain {needle!r}", needle.lower() not in lowered, "the reply contains it")
    for pattern in expect.matches:
        add(f"matches /{pattern}/", re.search(pattern, text, re.IGNORECASE | re.DOTALL) is not None, "no match in the reply")

    if expect.uses_tools:
        if tool_calls is None:
            for name in _tool_check_names(expect):
                results.append(CheckResult(name, "code", ERROR, TOOLS_NOT_VISIBLE))
        else:
            called = [call.name for call in tool_calls]
            said = f"called: {', '.join(called) or 'no tools'}"
            for tool in expect.tools_called:
                add(f"calls {tool}", _any_match(called, tool), said)
            if expect.tools_any:
                add(
                    f"calls any of {', '.join(expect.tools_any)}",
                    any(_any_match(called, tool) for tool in expect.tools_any),
                    said,
                )
            for tool in expect.tools_not_called:
                label = "calls no tools" if tool == "*" else f"does not call {tool}"
                add(label, not _any_match(called, tool), said)

    if expect.max_seconds is not None:
        add(f"replies within {expect.max_seconds:g}s", seconds <= expect.max_seconds, f"took {seconds:.1f}s")
    return results


def run_custom_checks(expect: Expect, context: CheckContext, base_dir: Path) -> list[CheckResult]:
    """Run each `checks:` entry. A check returns True/False, or a string that explains a failure."""
    results = []
    for entry in expect.checks:
        name = entry.rpartition(":")[2]
        try:
            function = load_callable(entry, base_dir)
            outcome = function(context)
            if inspect.isawaitable(outcome):
                raise TypeError("custom checks must be plain functions, not async")
        except Exception as error:
            results.append(CheckResult(name, "custom", ERROR, f"{type(error).__name__}: {error}"))
            continue
        detail = ""
        if isinstance(outcome, tuple) and len(outcome) == 2:
            outcome, detail = outcome
        if isinstance(outcome, str):
            results.append(CheckResult(name, "custom", FAIL, outcome))
        elif outcome is None or outcome is True:
            results.append(CheckResult(name, "custom", PASS))
        elif outcome is False:
            results.append(CheckResult(name, "custom", FAIL, str(detail or "the check returned False")))
        else:
            results.append(
                CheckResult(name, "custom", ERROR, f"returned {type(outcome).__name__}; return True, False or a message")
            )
    return results


def _any_match(called: list[str], pattern: str) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for name in called)


def _tool_check_names(expect: Expect) -> list[str]:
    names = [f"calls {tool}" for tool in expect.tools_called]
    if expect.tools_any:
        names.append(f"calls any of {', '.join(expect.tools_any)}")
    names.extend("calls no tools" if tool == "*" else f"does not call {tool}" for tool in expect.tools_not_called)
    return names
