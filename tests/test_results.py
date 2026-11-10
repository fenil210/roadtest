"""Saved runs: finding them again, comparing two of them, and the committed baseline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from roadtest.config import load_config
from roadtest.errors import RoadtestError
from roadtest.reports import compare_runs, find_run, save_baseline, save_run

from conftest import run_config, write_project

TWO_SCENARIOS = """
    scenarios:
      - id: greets
        user: hello
        expect: {contains: hello}
      - id: known-gap
        tier: watch
        user: hello
        expect: {contains: goodbye}
"""
SAYS_HELLO = "def call(message):\n    return 'hello'\n"


def make_run(run_id: str, *scenarios: tuple[str, str, str]) -> dict[str, Any]:
    """A run record with just what comparing needs: (id, status, tier) per scenario."""
    return {
        "id": run_id,
        "target": "default",
        "scenarios": [{"id": name, "title": "", "status": status, "tier": tier} for name, status, tier in scenarios],
    }


def test_a_saved_run_is_found_as_latest_previous_and_by_id_prefix(tmp_path: Path) -> None:
    config = load_config(write_project(tmp_path, TWO_SCENARIOS, adapter=SAYS_HELLO))
    first = save_run(config, run_config(config, label="alpha"))
    save_run(config, run_config(config, label="beta"))

    assert find_run(config, "latest")[0]["label"] == "beta"
    assert find_run(config, "previous")[0]["label"] == "alpha"
    assert find_run(config, first.name[:-2])[0]["label"] == "alpha"  # a unique start of the id is enough
    assert find_run(config, first.name)[0]["label"] == "alpha"
    with pytest.raises(RoadtestError, match="matches several runs"):
        find_run(config, first.name[:3])
    with pytest.raises(RoadtestError, match="no run `nope`"):
        find_run(config, "nope")


def test_compare_separates_regressions_fixes_and_changes_in_the_scenario_set() -> None:
    base = make_run(
        "before",
        ("stays-green", "pass", "gate"),
        ("breaks-gate", "pass", "gate"),
        ("breaks-watch", "pass", "watch"),
        ("gets-fixed", "fail", "gate"),
        ("stays-broken", "error", "gate"),
        ("dropped", "pass", "gate"),
    )
    head = make_run(
        "after",
        ("stays-green", "pass", "gate"),
        ("breaks-gate", "fail", "gate"),
        ("breaks-watch", "error", "watch"),
        ("gets-fixed", "pass", "gate"),
        ("stays-broken", "fail", "gate"),
        ("brand-new", "pass", "gate"),
    )
    comparison = compare_runs(base, head)

    def ids(key: str) -> list[str]:
        return [item["id"] for item in comparison[key]]

    assert ids("regressions") == ["breaks-gate", "breaks-watch"]
    assert ids("fixed") == ["gets-fixed"]
    assert ids("still_failing") == ["stays-broken"]
    assert ids("added") == ["brand-new"]
    assert ids("removed") == ["dropped"]
    assert [(item["before"], item["after"]) for item in comparison["regressions"]] == [("pass", "fail"), ("pass", "error")]
    assert comparison["ok"] is False


@pytest.mark.parametrize(
    "before, after, tier, ok",
    [
        ("pass", "fail", "gate", False),
        ("pass", "error", "gate", False),
        ("pass", "fail", "watch", True),
        ("fail", "fail", "gate", True),
        ("fail", "pass", "gate", True),
    ],
)
def test_compare_is_not_ok_only_when_a_gate_scenario_regressed(before: str, after: str, tier: str, ok: bool) -> None:
    # `roadtest compare` is a release check: a watch scenario getting worse, or a gate scenario that was
    # already broken, must not block it, but a gate scenario that used to pass must.
    comparison = compare_runs(make_run("a", ("one", before, tier)), make_run("b", ("one", after, tier)))
    assert comparison["ok"] is ok


def test_a_baseline_keeps_the_pass_fail_picture_and_compares_clean_against_its_own_run(tmp_path: Path) -> None:
    config = load_config(write_project(tmp_path, TWO_SCENARIOS, adapter=SAYS_HELLO))
    run = run_config(config)
    save_baseline(config, run)

    baseline, folder = find_run(config, "baseline")
    assert folder is None
    assert sorted((item["id"], item["status"], item["tier"]) for item in baseline["scenarios"]) == [
        ("greets", "pass", "gate"),
        ("known-gap", "fail", "watch"),
    ]
    assert "attempts" not in baseline["scenarios"][0]  # small enough to commit
    comparison = compare_runs(baseline, run)
    assert (comparison["regressions"], comparison["fixed"], comparison["added"], comparison["removed"]) == ([], [], [], [])
    assert [item["id"] for item in comparison["still_failing"]] == ["known-gap"]
    assert comparison["ok"] is True


def test_a_run_is_saved_even_when_a_tool_argument_is_not_plain_json(tmp_path: Path) -> None:
    # A Python adapter that reports dates, decimals or ids as they are in memory must not lose the whole
    # run (and its report) at the very end because the results file cannot be written.
    config = load_config(
        write_project(
            tmp_path,
            """
                scenarios:
                  - id: books-a-day
                    user: book it
                    expect: {tools_called: [book]}
            """,
            adapter="""
                import datetime

                def call(message):
                    return {"text": "Booked.", "tool_calls": [{"name": "book", "arguments": {"day": datetime.date(2026, 1, 2)}}]}
            """,
        )
    )
    folder = save_run(config, run_config(config))
    saved = json.loads((folder / "run.json").read_text(encoding="utf-8"))
    assert saved["scenarios"][0]["attempts"][0]["turns"][0]["tool_calls"] == [{"name": "book", "arguments": {"day": "2026-01-02"}}]
    assert "2026-01-02" in (folder / "report.html").read_text(encoding="utf-8")
