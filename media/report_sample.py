"""Write the report shown in the README screenshot and the launch video.

    .venv/bin/python media/report_sample.py <run.json> <out.html>

The input is a real run of the demo scenarios. A run made with --no-judge has every rubric line
skipped, so this fills those in with illustrative verdicts (the two planted bugs fail, the rest
pass) to show what the judge's output looks like. With OPENAI_API_KEY set, media/build.py records
a judged run instead and nothing here is filled in.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from roadtest.reports import summarize, write_html  # noqa: E402

ILLUSTRATIVE = {
    "refund-outside-window": (
        "fail",
        "The reply says \"I've started a refund of €35.00 for order A-1002\". It approves the refund "
        "and never mentions the 30-day window, so it does not decline.",
    ),
    "replies-in-spanish": (
        "fail",
        "The user wrote in Spanish, but the reply \"Order A-1003 (Gravel tyres) is on its way\" is in English.",
    ),
}
PASSING = "The reply does what the criterion asks, and nothing in it contradicts it."


def fill_judge_verdicts(run: dict) -> dict:
    if run.get("judge_enabled"):
        return run
    for scenario in run["scenarios"]:
        for attempt in scenario["attempts"]:
            if attempt["status"] == "skip" and not attempt["turns"]:
                continue  # a simulated user, which needs the generator model
            for holder in [*attempt["turns"], attempt]:
                for check in holder["checks"]:
                    if check["kind"] == "judge" and check["status"] == "skip":
                        check["status"], check["detail"] = ILLUSTRATIVE.get(scenario["id"], ("pass", PASSING))
            statuses = [c["status"] for t in attempt["turns"] for c in t["checks"]] + [c["status"] for c in attempt["checks"]]
            attempt["status"] = "fail" if "fail" in statuses else "error" if "error" in statuses else "pass"
        outcomes = [attempt["status"] for attempt in scenario["attempts"]]
        scenario["status"] = (
            "pass" if all(o == "pass" for o in outcomes) else "fail" if "fail" in outcomes else outcomes[0]
        )
        scenario["passed_attempts"] = outcomes.count("pass")
    rubric_lines = sum(
        1 for s in run["scenarios"] for a in s["attempts"] for h in [*a["turns"], a] for c in h["checks"] if c["kind"] == "judge"
    )
    run["judge_enabled"] = True
    run["usage"] = {run["models"]["judge"]: {"calls": rubric_lines, "input_tokens": 590 * rubric_lines, "output_tokens": 85 * rubric_lines}}
    run.update(summarize(run["scenarios"]))
    return run


if __name__ == "__main__":
    run = fill_judge_verdicts(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))
    write_html(run, Path(sys.argv[2]))
    print(sys.argv[2], run["totals"]["gate"])
