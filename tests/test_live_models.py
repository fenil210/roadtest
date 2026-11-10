"""Real model calls. Skipped unless OPENAI_API_KEY is set; they cost a few cents.

These are the only tests that talk to the OpenAI API, and they do so for real: the judge and the
simulated user are the parts of roadtest whose quality only a real model can show.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from roadtest import ToolCall
from roadtest.config import Config, ModelsSpec
from roadtest.demo import agent as demo_agent
from roadtest.judge import judge
from roadtest.llm import LLM
from roadtest.runner import RunOptions, run_scenarios
from roadtest.scenarios import Criterion, load_scenarios

pytestmark = pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="OPENAI_API_KEY is not set")

QUESTION = [{"role": "user", "content": "Where is my order A-1003?"}]
LOOKUP = [ToolCall("lookup_order", {"order_id": "A-1003"})]


def grade(criterion: str, reply: str, models: ModelsSpec | None = None) -> Any:
    models = models or ModelsSpec()

    async def go() -> Any:
        llm = LLM(models)
        try:
            return await judge(llm, models.judge, Criterion(name="criterion", criterion=criterion), QUESTION, reply, LOOKUP)
        finally:
            await llm.aclose()

    return asyncio.run(go())


def test_the_judge_passes_a_met_criterion_and_fails_a_violated_one() -> None:
    criterion = "Says the order is on its way and when it should arrive."
    met = grade(criterion, "Order A-1003 (Gravel tyres) is on its way and should arrive in 2 days.")
    violated = grade(criterion, "I'm sorry, I can only help with refunds. Is there a refund you need?")
    assert (met.status, violated.status) == ("pass", "fail"), (met.detail, violated.detail)
    assert met.detail and violated.detail  # the reasoning is kept, so a failure can be read


def test_a_rejected_key_is_reported_as_an_error_check_not_a_failed_one(monkeypatch: pytest.MonkeyPatch) -> None:
    # A wrong key must never look like the agent got the answer wrong.
    monkeypatch.setenv("ROADTEST_TEST_REJECTED_KEY", "sk-this-key-is-not-valid")
    result = grade(
        "Says the order is on its way.",
        "Order A-1003 is on its way.",
        ModelsSpec(api_key_env="ROADTEST_TEST_REJECTED_KEY"),
    )
    assert result.status == "error"
    assert "rejected" in result.detail


def test_a_simulated_user_holds_a_short_conversation_with_the_demo_agent(tmp_path: Path) -> None:
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir()
    (scenarios_dir / "s.yaml").write_text(
        "scenarios:\n"
        "  - id: simulated-order-question\n"
        "    simulate:\n"
        "      persona: A customer who writes short messages.\n"
        "      goal: Find out where order A-1003 is, then stop.\n"
        "      opening: Where is my order A-1003?\n"
        "      max_turns: 3\n"
        "    expect:\n"
        "      tools_called: [lookup_order]\n",
        encoding="utf-8",
    )
    server, url = demo_agent.start(0)
    try:
        config = Config.model_validate(
            {
                "agent": {
                    "url": url,
                    "body": {"message": "{{message}}", "session_id": "{{session_id}}"},
                    "response": {"text": "reply", "tool_calls": "tool_calls[*]"},
                },
                "scenarios": str(scenarios_dir),
            }
        )
        config.path = tmp_path / "roadtest.yaml"
        run = asyncio.run(run_scenarios(config, load_scenarios(scenarios_dir), RunOptions(judge=True)))
    finally:
        server.shutdown()
        server.server_close()
    (result,) = run["scenarios"]
    attempt = result["attempts"][0]
    assert result["status"] == "pass", attempt["error"] or attempt["turns"]
    assert 1 <= len(attempt["turns"]) <= 3
    assert all(turn["user"].strip() for turn in attempt["turns"])
    assert run["usage"][config.models.generator.model]["calls"] >= 1  # the model really played the user
