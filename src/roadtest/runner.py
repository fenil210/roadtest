"""The runner: plays each scenario against the agent, checks the replies, and builds the run record."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from . import __version__
from .adapters import Agent, Session, ToolCall, build_agent
from .checks import ERROR, FAIL, PASS, SKIP, CheckContext, CheckResult, run_code_checks, run_custom_checks
from .config import DEFAULT_TARGET, Config
from .errors import AgentError, ConfigError, ModelError
from .models import LLM
from .models.judge import judge
from .models.simulated_user import next_user_message
from .reports import summarize
from .scenarios import Expect, Scenario


@dataclass
class RunOptions:
    target: str | None = None
    repeat: int | None = None
    concurrency: int | None = None
    judge: bool = True
    label: str = ""


async def run_scenarios(
    config: Config,
    scenarios: list[Scenario],
    options: RunOptions,
    on_result: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run every scenario `repeat` times and return the run record (described in `roadtest guide triage`)."""
    target = options.target or DEFAULT_TARGET
    agent = build_agent(config.agent_for(target), config.directory)
    llm = LLM(config.models)
    repeat = options.repeat or config.run.repeat
    concurrency = options.concurrency or config.run.concurrency
    if repeat < 1 or concurrency < 1:
        raise ConfigError("repeat and concurrency must be at least 1")
    limit = asyncio.Semaphore(concurrency)

    needs_model = [s for s in scenarios if (s.needs_judge and options.judge) or (s.needs_generator and options.judge)]
    if needs_model and not llm.configured:
        raise ConfigError(
            f"{len(needs_model)} of {len(scenarios)} scenarios use a rubric or a simulated user, which need a model. "
            f"{llm.missing_key_message} To run only the code checks, add --no-judge."
        )

    started = datetime.now(timezone.utc)
    clock = time.perf_counter()

    async def one(scenario: Scenario) -> dict[str, Any]:
        async def attempt(index: int) -> dict[str, Any]:
            async with limit:
                return await _attempt(scenario, index, agent, llm, config, options.judge)

        attempts = await asyncio.gather(*(attempt(index) for index in range(repeat)))
        result = _scenario_result(scenario, list(attempts))
        if on_result:
            on_result(result)
        return result

    try:
        results = await asyncio.gather(*(one(scenario) for scenario in scenarios))
    finally:
        await agent.aclose()
        await llm.aclose()

    label = "".join(character if character.isalnum() else "-" for character in options.label.lower()).strip("-")
    run_id = "-".join(part for part in (started.astimezone().strftime("%Y%m%d-%H%M%S"), target, label) if part)
    run = {
        "roadtest": __version__,
        "id": run_id,
        "label": options.label,
        "target": target,
        "started": started.isoformat(timespec="seconds"),
        "seconds": round(time.perf_counter() - clock, 2),
        "agent": agent.describe(),
        "models": {"judge": config.models.judge.model, "generator": config.models.generator.model},
        "judge_enabled": options.judge,
        "repeat": repeat,
        "usage": llm.usage,
        "scenarios": list(results),
    }
    run.update(summarize(run["scenarios"]))
    return run


async def _attempt(
    scenario: Scenario, index: int, agent: Agent, llm: LLM, config: Config, judge_enabled: bool
) -> dict[str, Any]:
    if scenario.simulate and not judge_enabled:
        return {
            "index": index,
            "status": SKIP,
            "seconds": 0.0,
            "error": None,
            "note": "needs the generator model to play the user; skipped by --no-judge",
            "turns": [],
            "checks": [],
        }

    session = agent.new_session(scenario.id, scenario.vars)
    turns: list[dict[str, Any]] = []
    pending: list[tuple[list[CheckResult], asyncio.Task[CheckResult]]] = []
    conversation_checks: list[CheckResult] = []
    error: str | None = None
    clock = time.perf_counter()

    def grade(expect: Expect, conversation: list[dict[str, str]], reply: str | None, tool_calls: list[ToolCall] | None,
              into: list[CheckResult]) -> None:
        for criterion in expect.rubric:
            if judge_enabled:
                task = asyncio.create_task(judge(llm, config.models.judge, criterion, conversation, reply, tool_calls))
                pending.append((into, task))
            else:
                into.append(CheckResult(criterion.name, "judge", SKIP, "not graded (--no-judge)", criterion.criterion))

    async def take_turn(message: str, expect: Expect) -> None:
        before = list(session.messages)
        turn_clock = time.perf_counter()
        reply = await agent.send(message, session)
        seconds = time.perf_counter() - turn_clock
        asked = [*before, {"role": "user", "content": message}]
        session.messages = [*asked, {"role": "assistant", "content": reply.text}]
        checks = run_code_checks(expect, reply.text, reply.tool_calls, seconds)
        context = CheckContext(reply.text, reply.tool_calls, seconds, list(session.messages), scenario.id, scenario.vars)
        checks.extend(run_custom_checks(expect, context, config.directory))
        turns.append(
            {
                "user": message,
                "reply": reply.text,
                "tool_calls": None if reply.tool_calls is None else [_plain(asdict(call)) for call in reply.tool_calls],
                "seconds": round(seconds, 3),
                "checks": checks,
            }
        )
        grade(expect, asked, reply.text, reply.tool_calls, checks)

    try:
        if scenario.simulate:
            await _simulated_conversation(scenario, session, llm, config, take_turn)
        else:
            for turn in scenario.turns:
                await take_turn(turn.user, turn.expect)
    except AgentError as problem:
        error = str(problem)
    except ModelError as problem:
        error = f"the simulated user failed: {problem}"

    if error is None and not scenario.expect.is_empty:
        every_call = _all_tool_calls(turns)
        last_reply = turns[-1]["reply"] if turns else ""
        total = sum(turn["seconds"] for turn in turns)
        conversation_checks.extend(run_code_checks(scenario.expect, last_reply, every_call, total))
        context = CheckContext(last_reply, every_call, total, list(session.messages), scenario.id, scenario.vars)
        conversation_checks.extend(run_custom_checks(scenario.expect, context, config.directory))
        grade(scenario.expect, list(session.messages), None, every_call, conversation_checks)

    for into, task in pending:
        into.append(await task)

    every_check = [check for turn in turns for check in turn["checks"]] + conversation_checks
    for turn in turns:
        turn["checks"] = [check.to_dict() for check in turn["checks"]]
    return {
        "index": index,
        "status": _status(every_check, error),
        "seconds": round(time.perf_counter() - clock, 2),
        "error": error,
        "note": "",
        "turns": turns,
        "checks": [check.to_dict() for check in conversation_checks],
    }


async def _simulated_conversation(scenario: Scenario, session: Session, llm: LLM, config: Config, take_turn: Any) -> None:
    simulate = scenario.simulate
    assert simulate is not None
    message = simulate.opening
    if not message:
        message, _ = await next_user_message(llm, config.models.generator, simulate, [])
        if not message:
            raise ModelError("the generator model returned an empty first message")
    for turn_number in range(simulate.max_turns):
        await take_turn(message, Expect())
        if turn_number == simulate.max_turns - 1:
            break
        message, done = await next_user_message(llm, config.models.generator, simulate, list(session.messages))
        if done:
            break


def _plain(value: Any) -> Any:
    """`value` as plain JSON data. A Python adapter may report arguments such as dates or decimals,
    and the run record has to be writable to run.json and the HTML report whatever they hold."""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _all_tool_calls(turns: list[dict[str, Any]]) -> list[ToolCall] | None:
    """Every tool call in the conversation, or None when no turn reported any tool information."""
    reported = [turn["tool_calls"] for turn in turns if turn["tool_calls"] is not None]
    if not reported:
        return None
    return [ToolCall(call["name"], call.get("arguments")) for calls in reported for call in calls]


def _status(checks: list[CheckResult], error: str | None) -> str:
    """fail beats error: a real failure is the more useful thing to report."""
    statuses = {check.status for check in checks}
    if FAIL in statuses:
        return FAIL
    if error or ERROR in statuses:
        return ERROR
    if PASS in statuses:
        return PASS
    return SKIP


def _scenario_result(scenario: Scenario, attempts: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = [attempt["status"] for attempt in attempts]
    if all(status == PASS for status in statuses):
        status = PASS
    elif FAIL in statuses:
        status = FAIL
    elif ERROR in statuses:
        status = ERROR
    else:
        status = SKIP
    return {
        "id": scenario.id,
        "title": scenario.title,
        "file": scenario.file,
        "tags": scenario.tags,
        "tier": scenario.tier,
        "why": scenario.why,
        "status": status,
        "passed_attempts": statuses.count(PASS),
        "seconds": round(sum(attempt["seconds"] for attempt in attempts) / max(len(attempts), 1), 2),
        "attempts": attempts,
    }
