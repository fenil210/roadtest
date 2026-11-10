"""Scenario files: the shorthand, file-level defaults, error reporting and selection."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from roadtest.errors import ScenarioError
from roadtest.scenarios import load_scenarios, select


def write_files(folder: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dedent(text), encoding="utf-8")
    return folder


def test_the_user_shorthand_is_a_single_turn_carrying_its_expectations(tmp_path: Path) -> None:
    # `user:` + `expect:` is what almost every scenario looks like. If the expectations were dropped
    # while turning it into a turn, those scenarios would pass without checking anything.
    write_files(
        tmp_path,
        {
            "s.yaml": """
                scenarios:
                  - id: quick
                    user: Where is my order?
                    expect:
                      contains: A-1003
                      tools_called: lookup_order
            """
        },
    )
    (scenario,) = load_scenarios(tmp_path)
    (turn,) = scenario.turns
    assert turn.user == "Where is my order?"
    assert turn.expect.contains == ["A-1003"]
    assert turn.expect.tools_called == ["lookup_order"]


def test_file_level_tags_and_tier_apply_unless_a_scenario_says_otherwise(tmp_path: Path) -> None:
    # A file of known gaps marked `tier: watch` must not block releases; one scenario can still opt back in.
    write_files(
        tmp_path,
        {
            "s.yaml": """
                tags: [language]
                tier: watch
                scenarios:
                  - id: inherits
                    tags: spanish
                    user: Hola
                    expect: {contains: hola}
                  - id: opts-back-in
                    tier: gate
                    user: Bonjour
                    expect: {contains: bonjour}
            """
        },
    )
    found = {scenario.id: scenario for scenario in load_scenarios(tmp_path)}
    assert (found["inherits"].tier, found["inherits"].tags) == ("watch", ["language", "spanish"])
    assert (found["opts-back-in"].tier, found["opts-back-in"].tags) == ("gate", ["language"])


def test_every_problem_in_the_scenario_files_is_reported_at_once(tmp_path: Path) -> None:
    # Fixing one error per run, with a model or a person waiting on each run, is where scenario writing stalls.
    write_files(
        tmp_path,
        {
            "a.yaml": """
                scenarios:
                  - id: bad-regex
                    user: hi
                    expect:
                      matches: "([unclosed"
                  - id: no-expectations
                    user: hi
                  - id: dup
                    user: hi
                    expect: {contains: x}
            """,
            "b.yaml": """
                scenarios:
                  - id: dup
                    user: hi
                    expect: {contains: x}
                  - id: typo
                    user: hi
                    colour: red
                    expect: {contains: x}
            """,
        },
    )
    with pytest.raises(ScenarioError) as error:
        load_scenarios(tmp_path)
    problems = error.value.problems
    assert len(problems) == 4
    expected = [
        "a.yaml: bad-regex: ",
        "`([unclosed` is not a valid regular expression",
        "a.yaml: no-expectations: has no `expect`, so it could never fail",
        "b.yaml: typo: colour: unknown key",
        "b.yaml: id `dup` is already used in a.yaml",
    ]
    text = "\n".join(problems)
    for fragment in expected:
        assert fragment in text, fragment


def test_scenarios_are_selected_by_id_pattern_tag_tier_and_file_name(tmp_path: Path) -> None:
    write_files(
        tmp_path,
        {
            "refunds.yaml": """
                tags: [refunds]
                scenarios:
                  - id: refund-late
                    tags: [core]
                    user: a
                    expect: {contains: a}
                  - id: refund-early
                    tier: watch
                    user: a
                    expect: {contains: a}
            """,
            "shipping/rates.yaml": """
                scenarios:
                  - id: shipping-rates
                    tags: [core]
                    user: a
                    expect: {contains: a}
            """,
        },
    )
    everything = load_scenarios(tmp_path)

    def ids(chosen: list) -> list[str]:
        return sorted(scenario.id for scenario in chosen)

    assert ids(select(everything, ["refund-*"])) == ["refund-early", "refund-late"]
    assert ids(select(everything, ["shipping-rates"])) == ["shipping-rates"]
    assert ids(select(everything, ["rates.yaml"])) == ["shipping-rates"]
    assert ids(select(everything, ["refunds.yaml"])) == ["refund-early", "refund-late"]
    assert ids(select(everything, tags=["core"])) == ["refund-late", "shipping-rates"]
    assert ids(select(everything, tier="watch")) == ["refund-early"]
    assert ids(select(everything, ["refund-*"], tags=["core"])) == ["refund-late"]
    assert select(everything, ["no-such-scenario"]) == []
