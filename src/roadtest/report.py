"""Showing a run: terminal output, a Markdown summary for pull requests, and the HTML report."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.table import Table

ICONS = {"pass": "✓", "fail": "✗", "error": "!", "skip": "–"}
STYLES = {"pass": "green", "fail": "red", "error": "yellow", "skip": "dim"}
WORDS = {"pass": "passed", "fail": "failed", "error": "error", "skip": "skipped"}
TEMPLATE = Path(__file__).parent / "templates" / "report.html"
RUN_PLACEHOLDER = "__ROADTEST_RUN__"


def result_line(result: dict[str, Any], width: int = 44) -> str:
    """One line per finished scenario, printed while the run is in progress."""
    status = result["status"]
    attempts = len(result["attempts"])
    parts = [f"  [{STYLES[status]}]{ICONS[status]}[/] {escape(result['id']):<{width}}"]
    if result["tier"] == "watch":
        parts.append("[dim]watch[/]")
    if attempts > 1:
        parts.append(f"[{STYLES[status]}]{result['passed_attempts']}/{attempts}[/]")
    parts.append(f"[dim]{result['seconds']:.1f}s[/]")
    return "  ".join(parts)


def problems(result: dict[str, Any], limit: int = 220) -> list[str]:
    """Why a scenario did not pass: one line per failed or errored check, from its first bad attempt."""
    attempt = next((a for a in result["attempts"] if a["status"] in ("fail", "error")), None)
    if attempt is None:
        return []
    lines = []
    many_turns = len(attempt["turns"]) > 1
    for number, turn in enumerate(attempt["turns"], start=1):
        for check in turn["checks"]:
            if check["status"] in ("fail", "error"):
                prefix = f"turn {number} · " if many_turns else ""
                lines.append(prefix + _check_text(check, limit))
    for check in attempt["checks"]:
        if check["status"] in ("fail", "error"):
            lines.append("conversation · " + _check_text(check, limit))
    if attempt["error"]:
        lines.append(_shorten(attempt["error"], limit))
    return lines


def print_summary(console: Console, run: dict[str, Any], folder: Path | None = None) -> None:
    totals = run["totals"]
    console.print()
    console.rule(
        f"[bold]roadtest[/] · target {escape(run['target'])} · {totals['all']['total']} scenarios · {run['seconds']:.1f}s",
        style="dim",
    )
    console.print()
    for tier in ("gate", "watch"):
        counts = totals[tier]
        if not counts["total"] and not counts["skipped"]:
            continue
        rate = f"{100 * counts['passed'] / counts['total']:.0f}%" if counts["total"] else "–"
        extras = [f"[red]{counts['failed']} failed[/]"] if counts["failed"] else []
        if counts["errors"]:
            extras.append(f"[yellow]{counts['errors']} errors[/]")
        if counts["skipped"]:
            extras.append(f"[dim]{counts['skipped']} skipped[/]")
        console.print(f"  [bold]{tier:<6}[/] {counts['passed']}/{counts['total']} passed  [dim]{rate:>4}[/]   " + "  ".join(extras))

    not_passing = [s for s in run["scenarios"] if s["status"] in ("fail", "error")]
    if not_passing:
        console.print()
        for result in sorted(not_passing, key=lambda s: (s["tier"] != "gate", s["id"])):
            status = result["status"]
            tier = "  [dim]watch[/]" if result["tier"] == "watch" else ""
            title = f"  [dim]{escape(result['title'])}[/]" if result["title"] else ""
            console.print(f"  [{STYLES[status]}]{ICONS[status]} {escape(result['id'])}[/]{tier}{title}")
            for line in problems(result):
                console.print(f"      {escape(line)}", highlight=False)

    console.print()
    if run["usage"]:
        for model, usage in run["usage"].items():
            console.print(
                f"  [dim]model  {escape(model)} · {usage['calls']} calls · "
                f"{_tokens(usage['input_tokens'])} in · {_tokens(usage['output_tokens'])} out[/]"
            )
    if folder is not None:
        console.print(f"  [dim]report[/] {_relative(folder / 'report.html')}")
    console.print()
    gate = totals["gate"]
    if run["ok"]:
        console.print(f"  [bold green]✓ Gate passed[/]  [dim]{gate['passed']}/{gate['total']} gate scenarios[/]")
    else:
        bad = gate["failed"] + gate["errors"]
        console.print(f"  [bold red]✗ Gate failed[/]  [dim]{bad} of {gate['total']} gate scenarios did not pass[/]")
    console.print()


def markdown_summary(run: dict[str, Any]) -> str:
    """A short summary for a pull request comment or a CI job summary."""
    totals = run["totals"]
    gate = totals["gate"]
    verdict = "✅ Gate passed" if run["ok"] else "❌ Gate failed"
    lines = [
        f"## roadtest: {verdict}",
        "",
        f"Target `{run['target']}` · {totals['all']['total']} scenarios · {run['seconds']:.0f}s · run `{run['id']}`",
        "",
        "| Tier | Passed | Failed | Errors | Skipped |",
        "| --- | --- | --- | --- | --- |",
    ]
    for tier in ("gate", "watch"):
        counts = totals[tier]
        if counts["total"] or counts["skipped"]:
            lines.append(
                f"| {tier} | {counts['passed']} of {counts['total']} | {counts['failed']} | {counts['errors']} | {counts['skipped']} |"
            )
    not_passing = [s for s in run["scenarios"] if s["status"] in ("fail", "error")]
    if not_passing:
        lines += ["", "### Did not pass", ""]
        for result in sorted(not_passing, key=lambda s: (s["tier"] != "gate", s["id"])):
            title = f": {result['title']}" if result["title"] else ""
            lines.append(f"- **{result['id']}** ({result['tier']}, {WORDS[result['status']]}){title}")
            lines.extend(f"  - {line}" for line in problems(result, 400))
    if len(run["tags"]) > 1:
        lines += ["", "### By tag", "", "| Tag | Passed |", "| --- | --- |"]
        lines += [f"| {tag} | {counts['passed']} of {counts['total']} |" for tag, counts in run["tags"].items()]
    return "\n".join(lines) + "\n"


def write_html(run: dict[str, Any], path: Path) -> None:
    """The report is one self-contained file: the template with the run embedded as JSON."""
    payload = json.dumps(run, ensure_ascii=False).replace("</", "<\\/")
    path.write_text(TEMPLATE.read_text(encoding="utf-8").replace(RUN_PLACEHOLDER, payload), encoding="utf-8")


def print_comparison(console: Console, comparison: dict[str, Any]) -> None:
    base, head = comparison["base"], comparison["head"]
    console.print()
    console.print(f"  [dim]from[/] {escape(base['id'])}  {_rate(base)}")
    console.print(f"  [dim]to  [/] {escape(head['id'])}  {_rate(head)}")
    sections = (
        ("regressions", "Regressions (passed before, not now)", "red"),
        ("fixed", "Fixed (now passing)", "green"),
        ("still_failing", "Still not passing", "yellow"),
        ("added", "New scenarios", "cyan"),
    )
    for key, heading, style in sections:
        items = comparison[key]
        if not items:
            continue
        console.print()
        console.print(f"  [bold {style}]{heading}: {len(items)}[/]")
        table = Table(box=None, show_header=False, padding=(0, 2, 0, 4))
        for item in items:
            change = " → ".join(part for part in (item["before"], item["after"]) if part)
            table.add_row(escape(item["id"]), f"[dim]{item['tier']}[/]", f"[dim]{change}[/]")
        console.print(table)
    if comparison["removed"]:
        console.print()
        console.print(f"  [dim]Not in the later run: {len(comparison['removed'])} scenarios[/]")
    console.print()
    if not comparison["regressions"]:
        console.print("  [bold green]✓ No regressions[/]")
    elif comparison["ok"]:
        console.print("  [bold yellow]! Regressions only in watch scenarios[/]")
    else:
        console.print("  [bold red]✗ Gate scenarios regressed[/]")
    console.print()


def _rate(run: dict[str, Any]) -> str:
    gate = (run.get("totals") or {}).get("gate")
    return f"[dim]gate {gate['passed']}/{gate['total']}[/]" if gate else ""


def _check_text(check: dict[str, str], limit: int) -> str:
    label = f"judge: {check['name']}" if check["kind"] == "judge" else check["name"]
    marker = " (error)" if check["status"] == "error" else ""
    detail = f": {_shorten(check['detail'], limit)}" if check["detail"] else ""
    return f"{label}{marker}{detail}"


def _shorten(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tokens(count: int) -> str:
    return f"{count / 1000:.1f}k tokens" if count >= 1000 else f"{count} tokens"


def _relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        home = str(Path.home())
        text = str(path)
        return "~" + text[len(home) :] if text == home or text.startswith(home + os.sep) else text
