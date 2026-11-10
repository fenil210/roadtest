"""Run records on disk: totals, saving, finding earlier runs, and comparing two of them."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..config import Config
from ..errors import RoadtestError

RUN_FILE = "run.json"
NOT_PASSING = ("fail", "error")


def summarize(scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    """Totals for a list of scenario results. `ok` is the gate: no gate scenario failed or errored."""

    def count(items: list[dict[str, Any]]) -> dict[str, int]:
        statuses = [item["status"] for item in items]
        return {
            "total": len(items) - statuses.count("skip"),
            "passed": statuses.count("pass"),
            "failed": statuses.count("fail"),
            "errors": statuses.count("error"),
            "skipped": statuses.count("skip"),
        }

    tags: dict[str, list[dict[str, Any]]] = {}
    for scenario in scenarios:
        for tag in scenario["tags"]:
            tags.setdefault(tag, []).append(scenario)
    gate = [scenario for scenario in scenarios if scenario["tier"] == "gate"]
    watch = [scenario for scenario in scenarios if scenario["tier"] == "watch"]
    return {
        "ok": not any(scenario["status"] in NOT_PASSING for scenario in gate),
        "totals": {"all": count(scenarios), "gate": count(gate), "watch": count(watch)},
        "tags": {tag: count(items) for tag, items in sorted(tags.items())},
    }


def save_run(config: Config, run: dict[str, Any]) -> Path:
    """Write run.json, summary.md and report.html into a new folder under the results directory."""
    from .report import markdown_summary, write_html

    folder = config.results_dir / run["id"]
    suffix = 2
    while folder.exists():
        folder = config.results_dir / f"{run['id']}-{suffix}"
        suffix += 1
    folder.mkdir(parents=True)
    run["id"] = folder.name
    (folder / RUN_FILE).write_text(json.dumps(run, indent=2, ensure_ascii=False), encoding="utf-8")
    (folder / "summary.md").write_text(markdown_summary(run), encoding="utf-8")
    write_html(run, folder / "report.html")
    return folder


def list_runs(results_dir: Path) -> list[Path]:
    """Run folders, oldest first. Folder names start with a timestamp, so name order is time order."""
    if not results_dir.is_dir():
        return []
    return sorted(folder for folder in results_dir.iterdir() if (folder / RUN_FILE).is_file())


def find_run(config: Config, reference: str = "latest") -> tuple[dict[str, Any], Path | None]:
    """Load a run by `latest`, `previous`, `baseline`, a run id (or its start), or a path."""
    if reference == "baseline":
        if not config.baseline_path.is_file():
            raise RoadtestError(f"no baseline yet. Save one with `roadtest baseline` (it writes {config.baseline_path}).")
        return _read(config.baseline_path), None
    path = Path(reference).expanduser()
    if path.is_file():
        return _read(path), path.parent
    if (path / RUN_FILE).is_file():
        return _read(path / RUN_FILE), path
    runs = list_runs(config.results_dir)
    if reference in ("latest", "previous"):
        needed = 1 if reference == "latest" else 2
        if len(runs) < needed:
            raise RoadtestError(
                f"no {reference} run in {config.results_dir}. " + ("Run `roadtest run` first." if not runs else "There is only one run so far.")
            )
        folder = runs[-needed]
        return _read(folder / RUN_FILE), folder
    matches = [folder for folder in runs if folder.name == reference] or [
        folder for folder in runs if folder.name.startswith(reference)
    ]
    if len(matches) == 1:
        return _read(matches[0] / RUN_FILE), matches[0]
    if matches:
        names = ", ".join(folder.name for folder in matches[:5])
        raise RoadtestError(f"`{reference}` matches several runs: {names}")
    raise RoadtestError(f"no run `{reference}` in {config.results_dir}. Use latest, previous, baseline, a run id or a path.")


def save_baseline(config: Config, run: dict[str, Any]) -> Path:
    """Pin a run as the baseline: a small file meant to be committed."""
    baseline = {
        "roadtest": run.get("roadtest"),
        "id": run["id"],
        "target": run.get("target"),
        "started": run.get("started"),
        "ok": run.get("ok"),
        "totals": run.get("totals"),
        "scenarios": [
            {key: scenario.get(key) for key in ("id", "title", "tier", "tags", "status")}
            for scenario in run["scenarios"]
        ],
    }
    config.baseline_path.write_text(json.dumps(baseline, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return config.baseline_path


def compare_runs(base: dict[str, Any], head: dict[str, Any]) -> dict[str, Any]:
    """What changed from `base` to `head`, by scenario id. Skipped scenarios are left out."""
    before = {scenario["id"]: scenario for scenario in base["scenarios"] if scenario["status"] != "skip"}
    after = {scenario["id"]: scenario for scenario in head["scenarios"] if scenario["status"] != "skip"}

    def passing(scenario: dict[str, Any]) -> bool:
        return scenario["status"] == "pass"

    def entry(scenario_id: str) -> dict[str, Any]:
        scenario = after.get(scenario_id) or before[scenario_id]
        return {
            "id": scenario_id,
            "title": scenario.get("title", ""),
            "tier": scenario.get("tier", "gate"),
            "before": before[scenario_id]["status"] if scenario_id in before else None,
            "after": after[scenario_id]["status"] if scenario_id in after else None,
        }

    shared = [scenario_id for scenario_id in after if scenario_id in before]
    regressions = [entry(i) for i in shared if passing(before[i]) and not passing(after[i])]
    return {
        "base": {"id": base["id"], "target": base.get("target"), "totals": base.get("totals")},
        "head": {"id": head["id"], "target": head.get("target"), "totals": head.get("totals")},
        "regressions": regressions,
        "fixed": [entry(i) for i in shared if not passing(before[i]) and passing(after[i])],
        "still_failing": [entry(i) for i in shared if not passing(before[i]) and not passing(after[i])],
        "added": [entry(i) for i in after if i not in before],
        "removed": [entry(i) for i in before if i not in after],
        "ok": not any(item["tier"] == "gate" for item in regressions),
    }


def _read(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RoadtestError(f"cannot read {path}: {error}") from None
