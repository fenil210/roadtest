"""The `roadtest` command: exit codes that CI relies on, init, baselines and doctor.

These call `roadtest.cli.main([...])` exactly as the console script does, in a throwaway project
whose agent is a small Python function.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from roadtest import cli

from conftest import unused_port, write_project

SAYS_BLUE = "def call(message):\n    return 'blue'\n"


@pytest.fixture(autouse=True)
def quiet_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OPENAI_API_KEY", "GITHUB_STEP_SUMMARY", "ROADTEST_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli.console, "width", 400)  # long temporary paths must not wrap in the output
    monkeypatch.setattr(cli.errors, "width", 400)


@pytest.mark.parametrize(
    "scenarios, adapter, expected_code, expected_output",
    [
        pytest.param(
            "scenarios:\n  - id: says-green\n    user: colour?\n    expect: {contains: green}\n",
            SAYS_BLUE,
            1,
            "Gate failed",
            id="a-gate-scenario-fails",
        ),
        pytest.param(
            "scenarios:\n  - id: says-green\n    user: colour?\n    expect: {contains: green}\n",
            "def call(message):\n    raise RuntimeError('agent is down')\n",
            1,
            "agent is down",
            id="a-gate-scenario-errors",
        ),
        pytest.param(
            "scenarios:\n"
            "  - id: says-blue\n    user: colour?\n    expect: {contains: blue}\n"
            "  - id: says-green\n    tier: watch\n    user: colour?\n    expect: {contains: green}\n",
            SAYS_BLUE,
            0,
            "Gate passed",
            id="only-a-watch-scenario-fails",
        ),
        pytest.param(
            "scenarios:\n  - id: bad\n    user: colour?\n    expect: {matches: '(unclosed'}\n",
            SAYS_BLUE,
            2,
            "not a valid regular expression",
            id="the-scenario-files-are-invalid",
        ),
    ],
)
def test_run_exits_with_the_code_that_ci_acts_on(
    scenarios: str, adapter: str, expected_code: int, expected_output: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    write_project(tmp_path, scenarios, adapter=adapter)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["run", "--no-judge"]) == expected_code
    output = capsys.readouterr()
    assert expected_output in output.out + output.err


@pytest.mark.parametrize("repeat", ["0", "-1"])
def test_a_repeat_that_would_run_nothing_is_refused_not_reported_as_a_pass(
    repeat: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    # A script computing `--repeat $N` with N = -1 used to get a green gate for a scenario that never ran.
    write_project(tmp_path, "scenarios:\n  - id: says-green\n    user: colour?\n    expect: {contains: green}\n", adapter=SAYS_BLUE)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as stopped:
        cli.main(["run", "--no-judge", "--repeat", repeat])
    assert stopped.value.code == 2
    assert "--repeat" in capsys.readouterr().err


def test_init_creates_a_project_that_validates_and_installs_the_skill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init", "--skills", "all", "--url", "http://localhost:9000/ask"]) == 0

    assert "url: http://localhost:9000/ask" in (tmp_path / "evals" / "roadtest.yaml").read_text(encoding="utf-8")
    assert (tmp_path / "evals" / "scenarios" / "example.yaml").is_file()
    assert (tmp_path / ".agents" / "skills" / "roadtest" / "SKILL.md").is_file()
    assert (tmp_path / ".claude" / "skills" / "roadtest" / "references" / "setup.md").is_file()
    # The first thing a new user does next is validate: the starter scenarios must be valid.
    assert cli.main(["validate"]) == 0


def test_init_never_overwrites_a_config_or_scenarios_that_already_exist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Re-running init (to refresh the skill, say) must not wipe a configured agent or written scenarios.
    config_path = write_project(tmp_path, "scenarios:\n  - id: mine\n    user: hi\n    expect: {contains: hi}\n")
    config_path.write_text("version: 1\nagent:\n  url: http://my-agent.example/chat\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init", "--skills", "none"]) == 0

    assert config_path.read_text(encoding="utf-8") == "version: 1\nagent:\n  url: http://my-agent.example/chat\n"
    assert sorted(path.name for path in (tmp_path / "evals" / "scenarios").iterdir()) == ["s.yaml"]


def test_compare_against_a_baseline_flags_a_scenario_that_stopped_passing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    # The release check: pin a good run, change the agent, and `compare` must say it got worse.
    write_project(
        tmp_path,
        "scenarios:\n  - id: says-blue\n    user: colour?\n    expect: {contains: blue}\n",
        adapter="""
            from pathlib import Path

            def call(message):
                return Path(__file__).with_name("answer.txt").read_text().strip()
        """,
    )
    answer = tmp_path / "evals" / "answer.txt"
    monkeypatch.chdir(tmp_path)

    answer.write_text("blue")
    assert cli.main(["run", "--no-judge"]) == 0
    assert cli.main(["baseline"]) == 0
    assert (tmp_path / "evals" / "baseline.json").is_file()
    assert cli.main(["compare"]) == 0  # nothing changed since the baseline

    answer.write_text("red")
    assert cli.main(["run", "--no-judge"]) == 1
    capsys.readouterr()
    assert cli.main(["compare"]) == 1
    assert "Gate scenarios regressed" in capsys.readouterr().out


def test_doctor_says_ready_for_a_working_agent_and_not_ready_for_one_that_is_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    scenario = "scenarios:\n  - id: says-blue\n    user: colour?\n    expect: {contains: blue}\n"
    working = write_project(tmp_path / "working", scenario, adapter=SAYS_BLUE)
    down = write_project(tmp_path / "down", scenario, agent=f"url: http://127.0.0.1:{unused_port()}/chat\nretries: 0\n")
    monkeypatch.chdir(tmp_path)

    assert cli.main(["doctor", "--config", str(working)]) == 0
    assert "Ready." in capsys.readouterr().out
    assert cli.main(["doctor", "--config", str(down)]) == 1
    assert "Not ready." in capsys.readouterr().out


def test_paths_outside_the_working_folder_start_with_a_tilde_only_inside_the_home_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    # `/…/annabel/…` is not inside `/…/ann`; printing it as `~abel/…` sends people to a path that does not exist.
    home = tmp_path / "ann"
    neighbour = tmp_path / "annabel"
    scenario = "scenarios:\n  - id: says-blue\n    user: colour?\n    expect: {contains: blue}\n"
    inside = write_project(home / "work", scenario, adapter=SAYS_BLUE)
    outside = write_project(neighbour / "work", scenario, adapter=SAYS_BLUE)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(elsewhere)

    cli.main(["doctor", "--config", str(inside)])
    assert "~/work/evals/roadtest.yaml" in capsys.readouterr().out
    cli.main(["doctor", "--config", str(outside)])
    shown = capsys.readouterr().out
    assert str(outside) in shown
    assert "~abel" not in shown
