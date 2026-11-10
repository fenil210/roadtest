"""The `roadtest` command."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import webbrowser
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markup import escape

from . import __version__, scaffold
from .agents import AgentError, build_agent
from .config import DEFAULT_TARGET, Config, load_config, load_env_file
from .errors import ModelError, RoadtestError, ScenarioError
from .llm import LLM
from .report import markdown_summary, print_comparison, print_summary, result_line, write_html
from .results import compare_runs, find_run, save_baseline, save_run
from .runner import RunOptions, run_scenarios
from .scenarios import Scenario, load_scenarios, select

console = Console(highlight=False)
errors = Console(stderr=True, highlight=False)

COMMANDS = (
    ("init", "Add roadtest to this project (config, starter scenarios, coding-agent skill)"),
    ("demo", "See a full run against a bundled demo agent. Needs no setup"),
    ("doctor", "Check that the config, the agent and the models all work"),
    ("probe", "Send one message to your agent and show exactly what came back"),
    ("validate", "Check the scenario files without running them"),
    ("run", "Run scenarios against your agent"),
    ("report", "Show a past run again"),
    ("compare", "Show what changed between two runs"),
    ("baseline", "Pin a run as the baseline to compare against"),
    ("guide", "Print a guide (for you or your coding agent)"),
)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    if not arguments.command:
        _welcome()
        return 0
    try:
        return arguments.handler(arguments)
    except ScenarioError as error:
        _print_problems(error.problems)
        return 2
    except RoadtestError as error:
        errors.print(f"[bold red]error[/] {escape(str(error))}")
        return 2
    except KeyboardInterrupt:
        errors.print("\ninterrupted")
        return 130


# ── commands ────────────────────────────────────────────────────────────────────────────────


def _init(arguments: argparse.Namespace) -> int:
    root = Path.cwd()
    written = scaffold.init_project(root, arguments.dir, arguments.skills, arguments.force, arguments.url)
    console.print()
    for action, path, note in written:
        style = "green" if action != "kept" else "dim"
        console.print(f"  [{style}]{action:<8}[/] {escape(path)}" + (f"  [dim]{escape(note)}[/]" if note else ""))
    console.print()
    console.print("  [bold]Next[/]")
    console.print('  1. Ask your coding agent: [cyan]"Set up roadtest for my agent, then write 20 scenarios."[/]')
    console.print(f"     [dim]or edit {arguments.dir}/roadtest.yaml yourself (`roadtest guide setup`)[/]")
    console.print("  2. [cyan]roadtest doctor[/]   [dim]check the connection[/]")
    console.print("  3. [cyan]roadtest run[/]      [dim]run the scenarios[/]")
    console.print()
    return 0


def _guide(arguments: argparse.Namespace) -> int:
    topics = scaffold.guide_topics()
    if not arguments.topic:
        console.print()
        for name, path in topics.items():
            console.print(f"  [cyan]roadtest guide {name:<10}[/] [dim]{escape(scaffold.guide_summary(path))}[/]")
        console.print()
        return 0
    sys.stdout.write(scaffold.read_guide(arguments.topic).rstrip() + "\n")
    return 0


def _validate(arguments: argparse.Namespace) -> int:
    config = load_config(arguments.config)
    scenarios = load_scenarios(config.scenarios_dir)  # invalid files exit 2, the same as `run`
    _print_inventory(scenarios, config)
    return 0


def _doctor(arguments: argparse.Namespace) -> int:
    healthy = True

    def ok(text: str) -> None:
        console.print(f"  [green]✓[/] {text}")

    def bad(text: str, fix: str = "") -> None:
        nonlocal healthy
        healthy = False
        console.print(f"  [red]✗[/] {text}")
        if fix:
            console.print(f"    [dim]{escape(fix)}[/]")

    def warn(text: str, note: str = "") -> None:
        console.print(f"  [yellow]![/] {text}")
        if note:
            console.print(f"    [dim]{escape(note)}[/]")

    console.print()
    config = load_config(arguments.config)
    ok(f"config      {escape(_relative(config.path))}")

    scenarios: list[Scenario] = []
    try:
        scenarios = load_scenarios(config.scenarios_dir)
        ok(f"scenarios   {len(scenarios)} in {escape(_relative(config.scenarios_dir))}")
    except ScenarioError as error:
        bad(f"scenarios   {len(error.problems)} problem(s)", "run `roadtest validate` to see them")

    target = arguments.target or DEFAULT_TARGET
    reply = None
    try:
        agent = build_agent(config.agent_for(target), config.directory)

        async def hello() -> Any:
            try:
                return await agent.send("Hello", agent.new_session("doctor"))
            finally:
                await agent.aclose()

        reply = asyncio.run(hello())
        where = agent.describe().get("url") or agent.describe().get("entry") or agent.describe().get("run", "")
        ok(f"agent       replied from {escape(where)}  [dim]“{escape(_shorten(reply.text, 60))}”[/]")
    except RoadtestError as error:
        bad(f"agent       {escape(str(error))}", "run `roadtest probe` to see the request and the raw reply")

    if reply is not None:
        uses_tools = sum(scenario.uses_tools for scenario in scenarios)
        if reply.tool_calls is not None:
            ok("tool calls  visible to roadtest")
        elif uses_tools:
            bad(
                f"tool calls  not visible, but {uses_tools} scenario(s) check them",
                "set `agent.response.tool_calls` in roadtest.yaml (see `roadtest guide setup`)",
            )
        else:
            warn("tool calls  not visible", "fine unless you want `tools_called` checks; see `roadtest guide setup`")

    need_judge = sum(scenario.needs_judge for scenario in scenarios)
    need_generator = sum(scenario.needs_generator for scenario in scenarios)
    llm = LLM(config.models)
    if not llm.configured:
        if need_judge or need_generator:
            bad(f"models      {escape(llm.missing_key_message)}")
        else:
            warn("models      no API key set", "only needed for `rubric` checks and simulated users")
    else:
        async def check_models() -> list[tuple[str, str, str | None]]:
            found = []
            try:
                for role, model in (("judge", config.models.judge), ("generator", config.models.generator)):
                    try:
                        await llm.check(model)
                        found.append((role, model.model, None))
                    except ModelError as error:
                        found.append((role, model.model, str(error)))
            finally:
                await llm.aclose()
            return found

        for role, model, problem in asyncio.run(check_models()):
            needed = need_judge if role == "judge" else need_generator
            if problem is None:
                ok(f"{role:<11} {escape(model)}")
            elif needed:
                bad(f"{role:<11} {escape(problem)}")
            else:
                warn(f"{role:<11} {escape(problem)}", "not used by the current scenarios")

    console.print()
    if healthy:
        console.print("  [bold green]Ready.[/] Run [cyan]roadtest run[/].")
    else:
        console.print("  [bold red]Not ready.[/] Fix the lines marked ✗ and run [cyan]roadtest doctor[/] again.")
    console.print()
    return 0 if healthy else 1


def _probe(arguments: argparse.Namespace) -> int:
    config = load_config(arguments.config)
    agent = build_agent(config.agent_for(arguments.target), config.directory)
    messages = arguments.message or ["Hello"]
    limit = None if arguments.raw else 1500

    async def conversation() -> int:
        session = agent.new_session("probe")
        try:
            for number, message in enumerate(messages, start=1):
                console.print()
                if len(messages) > 1:
                    console.rule(f"turn {number}", style="dim")
                failure = None
                try:
                    reply = await agent.send(message, session)
                except AgentError as error:
                    reply, failure = None, error
                exchange = agent.last_exchange
                console.print("  [bold]request[/]")
                console.print(_indent(json.dumps(exchange.request, indent=2, ensure_ascii=False, default=str), limit), markup=False)
                if exchange.status is not None:
                    console.print(f"\n  [bold]response[/] [dim]HTTP {exchange.status} · {escape(exchange.content_type)}[/]")
                else:
                    console.print("\n  [bold]response[/]")
                console.print(_indent(_pretty(exchange.body), limit), markup=False)
                if failure is not None:
                    console.print(f"\n  [bold red]✗[/] {escape(str(failure))}\n")
                    return 1
                session.messages += [{"role": "user", "content": message}, {"role": "assistant", "content": reply.text}]
                console.print("\n  [bold]what roadtest read[/]")
                console.print(f"  [green]answer[/]      {escape(_shorten(reply.text, 400))}")
                if reply.tool_calls is None:
                    console.print("  [yellow]tool calls[/]  not visible [dim](set agent.response.tool_calls to check tools)[/]")
                else:
                    names = ", ".join(call.name for call in reply.tool_calls) or "none this turn"
                    console.print(f"  [green]tool calls[/]  {escape(names)}")
                console.print(f"  [green]session id[/]  {escape(str(session.id))}")
                for note in exchange.notes:
                    console.print(f"  [dim]note: {escape(note)}[/]")
            console.print()
            return 0
        finally:
            await agent.aclose()

    return asyncio.run(conversation())


def _run(arguments: argparse.Namespace) -> int:
    config = load_config(arguments.config)
    scenarios = select(load_scenarios(config.scenarios_dir), arguments.selectors, arguments.tag, arguments.tier)
    if not scenarios:
        errors.print("[bold red]error[/] no scenarios match. Run `roadtest validate` to list them.")
        return 2
    options = RunOptions(arguments.target, arguments.repeat, arguments.concurrency, not arguments.no_judge, arguments.label or "")
    run, folder = _execute(config, scenarios, options)
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as handle:
            handle.write(markdown_summary(run) + "\n")
    if arguments.open:
        webbrowser.open((folder / "report.html").resolve().as_uri())
    return 0 if run["ok"] else 1


def _report(arguments: argparse.Namespace) -> int:
    config = load_config(arguments.config)
    run, folder = find_run(config, arguments.run)
    if "seconds" not in run:
        raise RoadtestError("the baseline holds only pass/fail per scenario; `roadtest report` needs a full run")
    if folder is not None:
        write_html(run, folder / "report.html")
    if arguments.markdown:
        sys.stdout.write(markdown_summary(run))
    else:
        print_summary(console, run, folder)
    if arguments.open and folder is not None:
        webbrowser.open((folder / "report.html").resolve().as_uri())
    return 0


def _compare(arguments: argparse.Namespace) -> int:
    config = load_config(arguments.config)
    base_reference = arguments.base or ("baseline" if config.baseline_path.is_file() else "previous")
    base, _ = find_run(config, base_reference)
    head, _ = find_run(config, arguments.head)
    comparison = compare_runs(base, head)
    print_comparison(console, comparison)
    return 0 if comparison["ok"] else 1


def _baseline(arguments: argparse.Namespace) -> int:
    config = load_config(arguments.config)
    run, _ = find_run(config, arguments.run)
    path = save_baseline(config, run)
    gate = run["totals"]["gate"]
    console.print()
    console.print(f"  [green]✓[/] baseline saved to {escape(_relative(path))}  [dim]run {escape(run['id'])}, gate {gate['passed']}/{gate['total']}[/]")
    console.print("    [dim]Commit this file. `roadtest compare` then shows what changed since it.[/]")
    console.print()
    return 0


def _demo(arguments: argparse.Namespace) -> int:
    from .demo import agent as demo_agent

    load_env_file(Path.cwd() / ".env")
    server, url = demo_agent.start()
    results = Path(arguments.out).resolve() if arguments.out else Path(tempfile.mkdtemp(prefix="roadtest-demo-"))
    demo_dir = Path(demo_agent.__file__).parent
    config = Config.model_validate(
        {
            "agent": {
                "url": url,
                "body": {"message": "{{message}}", "session_id": "{{session_id}}"},
                "response": {"text": "reply", "tool_calls": "tool_calls[*]"},
            },
            "results": str(results),
        }
    )
    config.path = demo_dir / "roadtest.yaml"
    scenarios = load_scenarios(config.scenarios_dir)
    judge = LLM(config.models).configured and not arguments.no_judge

    console.print()
    console.print("  [bold]roadtest demo[/]  a support agent for a made-up bike shop, with two bugs planted in it")
    if not judge:
        console.print("  [dim]No OPENAI_API_KEY found: code checks run, rubric lines and the simulated user are skipped.[/]")
    try:
        _, folder = _execute(config, scenarios, RunOptions(judge=judge), heading=False)
    finally:
        server.shutdown()
    console.print(f"  [dim]scenarios[/] {escape(_relative(config.scenarios_dir / 'support.yaml'))}")
    console.print("  [bold]Next[/]  run [cyan]roadtest init[/] in your own project to test your agent.")
    console.print()
    if arguments.open:
        webbrowser.open((folder / "report.html").resolve().as_uri())
    return 0


# ── helpers ─────────────────────────────────────────────────────────────────────────────────


def _execute(config: Config, scenarios: list[Scenario], options: RunOptions, heading: bool = True) -> tuple[dict[str, Any], Path]:
    """Run, print one line per scenario as it finishes, save the run and print the summary."""
    repeat = options.repeat or config.run.repeat
    console.print()
    if heading:
        times = f" × {repeat}" if repeat > 1 else ""
        console.print(f"  [bold]roadtest[/]  {len(scenarios)} scenarios{times} · target {escape(options.target or DEFAULT_TARGET)}")
        console.print()
    width = min(max(len(scenario.id) for scenario in scenarios), 48)
    done = 0

    def finished(result: dict[str, Any]) -> None:
        nonlocal done
        done += 1
        console.print(result_line(result, width))
        status.update(f"  [dim]running… {done}/{len(scenarios)}[/]")

    with console.status(f"  [dim]running… 0/{len(scenarios)}[/]", spinner="dots") as status:
        run = asyncio.run(run_scenarios(config, scenarios, options, finished))
    folder = save_run(config, run)
    print_summary(console, run, folder)
    return run, folder


def _print_inventory(scenarios: list[Scenario], config: Config) -> None:
    files: dict[str, int] = {}
    tags: dict[str, int] = {}
    for scenario in scenarios:
        files[scenario.file] = files.get(scenario.file, 0) + 1
        for tag in scenario.tags:
            tags[tag] = tags.get(tag, 0) + 1
    gate = sum(scenario.tier == "gate" for scenario in scenarios)
    judged = sum(scenario.needs_judge for scenario in scenarios)
    simulated = sum(scenario.needs_generator for scenario in scenarios)
    console.print()
    console.print(f"  [green]✓[/] {len(scenarios)} scenarios are valid  [dim]{escape(_relative(config.scenarios_dir))}[/]")
    console.print()
    for name, count in files.items():
        console.print(f"    {escape(name):<40} {count}")
    console.print()
    console.print(f"    [dim]tiers[/]   gate {gate} · watch {len(scenarios) - gate}")
    console.print(f"    [dim]checks[/]  {judged} use a rubric · {simulated} use a simulated user")
    if tags:
        listed = " · ".join(f"{tag} {count}" for tag, count in sorted(tags.items(), key=lambda item: (-item[1], item[0])))
        console.print(f"    [dim]tags[/]    {escape(listed)}")
    console.print()


def _print_problems(problems: list[str]) -> None:
    errors.print()
    for problem in problems:
        for line in problem.splitlines():
            errors.print(f"  [red]✗[/] {escape(line)}")
    errors.print()
    errors.print(f"  [bold red]{len(problems)} problem(s) in the scenario files.[/] See `roadtest guide scenarios` for the format.")
    errors.print()


def _welcome() -> None:
    console.print()
    console.print(f"  [bold]roadtest[/] {__version__}  [dim]road-test your AI agent before it ships[/]")
    console.print()
    for name, description in COMMANDS:
        console.print(f"  [cyan]roadtest {name:<9}[/] {description}")
    console.print()
    console.print("  [dim]New here? Try[/] [cyan]roadtest demo[/][dim], then[/] [cyan]roadtest init[/] [dim]in your project.[/]")
    console.print()


def _relative(path: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        home = str(Path.home())
        text = str(path)
        return "~" + text[len(home) :] if text == home or text.startswith(home + os.sep) else text


def _shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _pretty(body: str) -> str:
    try:
        return json.dumps(json.loads(body), indent=2, ensure_ascii=False)
    except (ValueError, TypeError):
        return body


def _indent(text: str, limit: int | None) -> str:
    if limit is not None and len(text) > limit:
        text = text[:limit] + f"\n… {len(text) - limit} more characters (use --raw to see everything)"
    return "\n".join("    " + line for line in text.splitlines()) or "    (empty)"


def _count(text: str) -> int:
    """A whole number of 1 or more. A zero or negative repeat would run nothing and still report a pass."""
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"`{text}` is not a whole number of 1 or more")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="roadtest",
        description="Road-test your AI agent before it ships.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"roadtest {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="command")
    descriptions = dict(COMMANDS)

    def add(name: str, handler: Any, uses_config: bool = True) -> argparse.ArgumentParser:
        command = commands.add_parser(name, help=descriptions[name], description=descriptions[name] + ".")
        command.set_defaults(handler=handler)
        if uses_config:
            command.add_argument("--config", type=Path, metavar="PATH", help="path to roadtest.yaml (default: found automatically)")
        return command

    init = add("init", _init, uses_config=False)
    init.add_argument("--url", help="your agent's endpoint, written into roadtest.yaml")
    init.add_argument("--dir", default="evals", help="folder for the config and scenarios (default: evals)")
    init.add_argument(
        "--skills",
        choices=("auto", "all", "agents", "claude", "none"),
        default="auto",
        help="which coding agents get the roadtest skill (default: auto)",
    )
    init.add_argument("--force", action="store_true", help="overwrite existing config and starter scenarios")

    demo = add("demo", _demo, uses_config=False)
    demo.add_argument("--no-judge", action="store_true", help="run only the code checks")
    demo.add_argument("--out", metavar="DIR", help="where to write the results (default: a temporary folder)")
    demo.add_argument("--open", action="store_true", help="open the HTML report when done")

    doctor = add("doctor", _doctor)
    doctor.add_argument("--target", help="which target to check (default: the `agent:` section)")

    probe = add("probe", _probe)
    probe.add_argument("message", nargs="*", help="what to send; several messages are sent as one conversation")
    probe.add_argument("--target", help="which target to send to")
    probe.add_argument("--raw", action="store_true", help="show the whole response instead of the first part")

    add("validate", _validate)

    run = add("run", _run)
    run.add_argument("selectors", nargs="*", metavar="SCENARIO", help="scenario ids, id patterns like `refund-*`, or file names")
    run.add_argument("--tag", action="append", metavar="TAG", help="only scenarios with this tag (repeatable)")
    run.add_argument("--tier", choices=("gate", "watch"), help="only scenarios in this tier")
    run.add_argument("--target", help="which target to run against (default: the `agent:` section)")
    run.add_argument("--repeat", type=_count, metavar="N", help="run each scenario N times; it passes only if every run passes")
    run.add_argument("--concurrency", type=_count, metavar="N", help="how many scenarios run at once (default: 4)")
    run.add_argument("--no-judge", action="store_true", help="skip rubric checks and simulated users (no model calls)")
    run.add_argument("--label", help="a name added to the run id, such as a branch or version")
    run.add_argument("--open", action="store_true", help="open the HTML report when done")

    report = add("report", _report)
    report.add_argument("run", nargs="?", default="latest", help="latest (default), previous, a run id or a path")
    report.add_argument("--markdown", action="store_true", help="print the Markdown summary")
    report.add_argument("--open", action="store_true", help="open the HTML report")

    compare = add("compare", _compare)
    compare.add_argument("base", nargs="?", help="the earlier run (default: baseline if there is one, else previous)")
    compare.add_argument("head", nargs="?", default="latest", help="the later run (default: latest)")

    baseline = add("baseline", _baseline)
    baseline.add_argument("run", nargs="?", default="latest", help="which run to pin (default: latest)")

    guide = add("guide", _guide, uses_config=False)
    guide.add_argument("topic", nargs="?", help="setup, scenarios, triage or ci")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
