"""Running scenarios: repeats, broken agents, retries, and tool-call visibility.

The agents here are tiny stand-ins for "the agent under test": a Python function, a program, or an
HTTP server whose replies the test decides. roadtest itself is the real thing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from roadtest.config import load_config

from conftest import json_reply, run_config, scripted_server, statuses, turn_of, unused_port, write_project

ASK_AND_EXPECT_OK = """
    scenarios:
      - id: asks
        user: Is it working?
        expect:
          contains: ok
"""


def test_a_repeated_scenario_passes_only_if_every_attempt_passes(tmp_path: Path) -> None:
    # An agent that is wrong one time in three has a real bug; one lucky run must not hide it.
    config_path = write_project(
        tmp_path,
        ASK_AND_EXPECT_OK,
        adapter="""
            import itertools
            counter = itertools.count(1)

            def call(message):
                return "wrong" if next(counter) == 3 else "ok"
        """,
    )
    run = run_config(load_config(config_path), repeat=3)
    (result,) = run["scenarios"]
    assert result["status"] == "fail"
    assert result["passed_attempts"] == 2
    assert len(result["attempts"]) == 3
    assert run["ok"] is False


@pytest.mark.parametrize(
    "reply, timeout, expected",
    [
        pytest.param(json_reply({"error": "boom"}, status=500), 5, "HTTP 500", id="http-500"),
        pytest.param(json_reply({"reply": "ok"}, delay=3), 0.3, "no reply from", id="timeout"),
    ],
)
def test_an_http_agent_that_fails_is_an_error_and_blocks_the_gate(reply: tuple, timeout: float, expected: str, tmp_path: Path) -> None:
    # "The agent is down" must never be reported as a quality failure, and must never pass the gate either.
    with scripted_server(lambda number, body: reply) as (url, _):
        config_path = write_project(
            tmp_path,
            ASK_AND_EXPECT_OK,
            agent=f"url: {url}\ntimeout: {timeout}\nresponse:\n  text: reply\n",
        )
        run = run_config(load_config(config_path))
    assert statuses(run) == {"asks": "error"}
    assert expected in run["scenarios"][0]["attempts"][0]["error"]
    assert run["ok"] is False


def test_an_agent_that_is_not_running_is_an_error_that_says_so(tmp_path: Path) -> None:
    closed_port = unused_port()
    config_path = write_project(tmp_path, ASK_AND_EXPECT_OK, agent=f"url: http://127.0.0.1:{closed_port}/chat\nretries: 0\n")
    run = run_config(load_config(config_path))
    assert statuses(run) == {"asks": "error"}
    assert "Is the agent running?" in run["scenarios"][0]["attempts"][0]["error"]


@pytest.mark.parametrize(
    "agent, adapter, expected",
    [
        pytest.param(
            "",
            "def call(message):\n    raise KeyError('order_id')\n",
            "raised KeyError",
            id="python-adapter-raises",
        ),
        pytest.param(
            f"type: command\nrun: ['{sys.executable}', adapter.py]\n",
            "import sys\nsys.stderr.write('database is down')\nsys.exit(3)\n",
            "exited with code 3: database is down",
            id="command-exits-nonzero",
        ),
    ],
)
def test_a_crashing_adapter_or_command_is_an_error_naming_the_cause(agent: str, adapter: str, expected: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Otherwise one bad adapter either crashes the whole run with a traceback or looks like a quality failure.
    config_path = write_project(tmp_path, ASK_AND_EXPECT_OK, agent=agent, adapter=adapter)
    monkeypatch.chdir(config_path.parent)
    run = run_config(load_config(config_path))
    assert statuses(run) == {"asks": "error"}
    assert expected in run["scenarios"][0]["attempts"][0]["error"]


def test_a_503_is_retried_and_the_scenario_passes(tmp_path: Path) -> None:
    # A deploy or a rate limit that blips for a second must not turn a whole run red.
    def script(number: int, body: dict) -> tuple[int, str, str, float]:
        return json_reply({"error": "busy"}, status=503) if number == 1 else json_reply({"reply": "ok"})

    with scripted_server(script) as (url, received):
        config_path = write_project(tmp_path, ASK_AND_EXPECT_OK, agent=f"url: {url}\nresponse:\n  text: reply\n")
        run = run_config(load_config(config_path))
    assert statuses(run) == {"asks": "pass"}
    assert len(received) == 2


def test_tool_checks_on_an_agent_that_does_not_report_tools_are_errors_with_guidance(tmp_path: Path) -> None:
    # "No tools were called" and "I cannot see tool calls" are different facts. Treating the second as the
    # first fails (or passes) scenarios for a reason that has nothing to do with the agent's behaviour.
    config_path = write_project(
        tmp_path,
        """
            scenarios:
              - id: unreported
                user: no-report
                expect:
                  tools_called: [lookup_order]
              - id: reported-none
                user: report
                expect:
                  tools_not_called: ["*"]
        """,
        adapter="""
            def call(message):
                if message == "no-report":
                    return "Hello"
                return {"text": "Hello", "tool_calls": []}
        """,
    )
    run = run_config(load_config(config_path))
    assert statuses(run) == {"unreported": "error", "reported-none": "pass"}
    (check,) = turn_of(run, "unreported")["checks"]
    assert check["name"] == "calls lookup_order"
    assert check["status"] == "error"
    assert "agent.response.tool_calls" in check["detail"]
