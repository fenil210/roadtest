"""The bundled demo agent, end to end, with the judge off (so no model and no key are needed).

The demo agent has two planted bugs, and the demo scenarios exist to catch them. If roadtest stops
catching known bugs, or reports a green gate while a gate scenario fails, these tests fail.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from roadtest import cli
from roadtest.config import Config
from roadtest.demo import agent as demo_agent
from roadtest.runner import RunOptions, run_scenarios
from roadtest.scenarios import load_scenarios

from conftest import statuses, turn_of


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """`roadtest demo --no-judge`, run once for this module: the exit code and the saved run."""
    out = tmp_path_factory.mktemp("demo")
    code = cli.main(["demo", "--no-judge", "--out", str(out)])
    (run_file,) = out.glob("*/run.json")
    return {"code": code, "run": json.loads(run_file.read_text(encoding="utf-8"))}


def test_both_planted_bugs_are_caught_and_the_gate_fails(demo: dict) -> None:
    # A refund outside the 30-day window is a gate scenario; the Spanish reply is only a watch item.
    run = demo["run"]
    assert statuses(run)["refund-outside-window"] == "fail"
    assert statuses(run)["replies-in-spanish"] == "fail"
    tiers = {scenario["id"]: scenario["tier"] for scenario in run["scenarios"]}
    assert tiers["refund-outside-window"] == "gate"
    assert tiers["replies-in-spanish"] == "watch"
    assert run["ok"] is False
    # Exactly the planted bugs fail: no good scenario is reported as failed or errored.
    assert run["totals"]["gate"]["failed"] == 1
    assert run["totals"]["gate"]["errors"] == 0
    assert run["totals"]["watch"]["failed"] == 1
    assert run["totals"]["watch"]["errors"] == 0


def test_the_agent_bug_is_reported_with_the_check_that_caught_it(demo: dict) -> None:
    # The failure must say what went wrong, or nobody can act on it.
    checks = turn_of(demo["run"], "refund-outside-window")["checks"]
    failed = {check["name"] for check in checks if check["status"] == "fail"}
    assert failed == {"does not call start_refund", "contains any of '30 days', '30-day'"}


def test_a_later_turn_remembers_the_order_from_an_earlier_turn(demo: dict) -> None:
    # Fails when the conversation id is not carried from turn to turn: the agent then asks for the order number.
    assert statuses(demo["run"])["refund-remembers-order"] == "pass"
    second = turn_of(demo["run"], "refund-remembers-order", number=1)
    assert [call["name"] for call in second["tool_calls"]] == ["lookup_order", "start_refund"]


def test_the_simulated_user_is_skipped_without_the_judge(demo: dict) -> None:
    # A scenario that could not run must not count as passed (or failed).
    assert statuses(demo["run"])["impatient-customer-refund"] == "skip"
    assert demo["run"]["totals"]["all"]["skipped"] == 1


def test_the_demo_exits_zero_even_though_the_gate_failed(demo: dict) -> None:
    # CI runs `roadtest demo --no-judge` as a smoke test of a clean install.
    assert demo["code"] == 0


def test_a_scenario_that_only_has_a_rubric_is_skipped_not_passed(tmp_path: Path) -> None:
    # With no judge nothing was checked, so reporting "pass" would be a green light for nothing.
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir()
    (scenarios_dir / "s.yaml").write_text(
        "scenarios:\n  - id: only-a-rubric\n    user: Hello\n    expect:\n      rubric:\n        - Greets the user.\n",
        encoding="utf-8",
    )
    server, url = demo_agent.start(0)
    try:
        config = Config.model_validate({"agent": {"url": url}, "scenarios": str(scenarios_dir)})
        config.path = tmp_path / "roadtest.yaml"
        run = asyncio.run(run_scenarios(config, load_scenarios(scenarios_dir), RunOptions(judge=False)))
    finally:
        server.shutdown()
        server.server_close()
    assert statuses(run) == {"only-a-rubric": "skip"}
    assert run["ok"] is True
