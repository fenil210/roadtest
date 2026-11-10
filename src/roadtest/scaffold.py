"""`roadtest init`: the files a project starts with, and the skill that teaches coding agents roadtest."""

from __future__ import annotations

import shutil
from pathlib import Path

from .errors import RoadtestError

SKILL_NAME = "roadtest"
DEFAULT_URL = "http://localhost:8000/chat"

CONFIG_TEMPLATE = """\
# How roadtest reaches your agent. Every option is explained in `roadtest guide setup`.
version: 1

agent:
  type: http
  url: http://localhost:8000/chat
  # headers:
  #   Authorization: "Bearer ${AGENT_API_KEY}"   # ${VAR} comes from your shell or a .env file
  body:
    message: "{{message}}"
    session_id: "{{session_id}}"                 # one id per scenario, so its turns share a conversation
  response:
    text: reply                                  # where the answer is in the JSON your agent returns
    # tool_calls: "tool_calls[*].name"           # lets scenarios check which tools were called

# The same agent in other places. Use one with `roadtest run --target staging`.
# targets:
#   staging:
#     url: https://staging.example.com/chat

models:
  judge: gpt-6-luna          # decides pass or fail for each rubric line
  generator: gpt-6.1-terra   # plays the user in simulated conversations
"""

EXAMPLE_SCENARIOS = """\
# Two starter scenarios that suit most conversational agents. Replace them with your own:
# ask your coding agent to "write roadtest scenarios for this agent", or see `roadtest guide scenarios`.

scenarios:
  - id: says-what-it-can-do
    title: The agent can say what it helps with
    user: What can you help me with?
    expect:
      rubric:
        - Names at least one concrete thing it can help with.

  - id: follow-up-uses-context
    title: A follow-up is answered using the previous turn
    turns:
      - user: My name is Sam.
      - user: What is my name?
        expect:
          contains: Sam
"""

GITIGNORE = "results/\n.env\n"

SKILL_TARGETS = {
    "agents": (".agents/skills", "Codex, Cursor, Copilot and other agents"),
    "claude": (".claude/skills", "Claude Code"),
}


def skill_source() -> Path:
    """The skill folder: inside the installed package, or at the repo root in a source checkout."""
    packaged = Path(__file__).parent / "skill"
    if (packaged / "SKILL.md").is_file():
        return packaged
    checkout = Path(__file__).resolve().parents[2] / "skills" / SKILL_NAME
    if (checkout / "SKILL.md").is_file():
        return checkout
    raise RoadtestError("the roadtest skill files are missing from this installation. Reinstall roadtest.")


def guide_topics() -> dict[str, Path]:
    """Guide name to file. The guides are the skill's reference pages, so both always agree."""
    return {path.stem: path for path in sorted((skill_source() / "references").glob("*.md"))}


def read_guide(topic: str) -> str:
    topics = guide_topics()
    if topic not in topics:
        raise RoadtestError(f"no guide named `{topic}`. Guides: {', '.join(topics)}")
    return topics[topic].read_text(encoding="utf-8")


def guide_summary(path: Path) -> str:
    """The first line of a guide after its heading, used as its one-line description."""
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return ""


def init_project(
    root: Path, directory: str = "evals", skills: str = "auto", force: bool = False, url: str | None = None
) -> list[tuple[str, str, str]]:
    """Create the starter files. Returns (what happened, path, note) for each one.

    Existing config and scenario files are kept unless `force` is set. Skill files are always
    refreshed, because they belong to roadtest and should match the installed version.
    """
    evals = root / directory
    written: list[tuple[str, str, str]] = []

    def write(path: Path, content: str) -> None:
        relative = path.relative_to(root).as_posix()
        if path.exists() and not force:
            written.append(("kept", relative, "already there"))
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(("created", relative, ""))

    write(evals / "roadtest.yaml", CONFIG_TEMPLATE.replace(DEFAULT_URL, url) if url else CONFIG_TEMPLATE)
    scenarios = evals / "scenarios"
    if scenarios.is_dir() and any(scenarios.rglob("*.y*ml")) and not force:
        written.append(("kept", scenarios.relative_to(root).as_posix() + "/", "already has scenarios"))
    else:
        write(scenarios / "example.yaml", EXAMPLE_SCENARIOS)
    write(evals / ".gitignore", GITIGNORE)

    for target in _skill_targets(root, skills):
        folder, audience = SKILL_TARGETS[target]
        destination = root / folder / SKILL_NAME
        existed = destination.exists()
        if existed:
            shutil.rmtree(destination)
        shutil.copytree(skill_source(), destination)
        written.append(("updated" if existed else "created", f"{folder}/{SKILL_NAME}/", f"skill for {audience}"))
    return written


def _skill_targets(root: Path, skills: str) -> list[str]:
    if skills == "none":
        return []
    if skills == "all":
        return ["agents", "claude"]
    if skills in SKILL_TARGETS:
        return [skills]
    targets = ["agents"]
    if (root / ".claude").exists() or (root / "CLAUDE.md").exists() or (Path.home() / ".claude").exists():
        targets.append("claude")
    return targets
