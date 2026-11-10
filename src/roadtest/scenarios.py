"""Scenario files: what to say to the agent, and what a good reply looks like."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .config import describe_validation_error
from .errors import ScenarioError

TIERS = ("gate", "watch")
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
_LIST_FIELDS = (
    "contains",
    "contains_any",
    "not_contains",
    "matches",
    "tools_called",
    "tools_any",
    "tools_not_called",
    "checks",
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Criterion(_Strict):
    """One thing a judge model decides, pass or fail."""

    name: str
    criterion: str = Field(min_length=1)


class Expect(_Strict):
    """What must be true of a reply. Every entry is a separate check and all must pass."""

    contains: list[str] = Field(default_factory=list)
    contains_any: list[str] = Field(default_factory=list)
    not_contains: list[str] = Field(default_factory=list)
    matches: list[str] = Field(default_factory=list)
    tools_called: list[str] = Field(default_factory=list)
    tools_any: list[str] = Field(default_factory=list)
    tools_not_called: list[str] = Field(default_factory=list)
    max_seconds: float | None = Field(default=None, gt=0)
    rubric: list[Criterion] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)

    @field_validator(*_LIST_FIELDS, mode="before")
    @classmethod
    def _one_or_many(cls, value: Any) -> Any:
        return [value] if isinstance(value, str) else value

    @field_validator("matches")
    @classmethod
    def _patterns_compile(cls, patterns: list[str]) -> list[str]:
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as error:
                raise ValueError(f"`{pattern}` is not a valid regular expression: {error}") from None
        return patterns

    @field_validator("checks")
    @classmethod
    def _checks_name_a_function(cls, entries: list[str]) -> list[str]:
        for entry in entries:
            if ":" not in entry:
                raise ValueError(f"`{entry}` must look like `checks.py:function_name`")
        return entries

    @field_validator("rubric", mode="before")
    @classmethod
    def _name_the_criteria(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return value
        named, used = [], set()
        for item in value:
            if isinstance(item, str):
                item = {"criterion": item}
            if isinstance(item, dict) and "criterion" in item:
                name = str(item.get("name") or _slug(str(item["criterion"])))
                base, counter = name, 2
                while name in used:
                    name, counter = f"{base}-{counter}", counter + 1
                used.add(name)
                item = {**item, "name": name}
            named.append(item)
        return named

    @property
    def is_empty(self) -> bool:
        return not any(getattr(self, name) for name in (*_LIST_FIELDS, "rubric")) and self.max_seconds is None

    @property
    def uses_tools(self) -> bool:
        return bool(self.tools_called or self.tools_any or self.tools_not_called)


class Turn(_Strict):
    user: str = Field(min_length=1)
    expect: Expect = Field(default_factory=Expect)


class Simulate(_Strict):
    """A user played by the generator model, for conversations that cannot be scripted."""

    goal: str = Field(min_length=1)
    persona: str = ""
    opening: str | None = None
    max_turns: int = Field(default=6, ge=1, le=30)


class Scenario(_Strict):
    id: str
    title: str = ""
    tags: list[str] = Field(default_factory=list)
    tier: Literal["gate", "watch"] = "gate"
    why: str = ""
    vars: dict[str, Any] = Field(default_factory=dict)
    turns: list[Turn] = Field(default_factory=list)
    simulate: Simulate | None = None
    expect: Expect = Field(default_factory=Expect)

    # Set by the loader; not part of the file.
    file: str = Field(default="", exclude=True)

    @field_validator("id")
    @classmethod
    def _id_is_a_slug(cls, value: str) -> str:
        if not _ID.match(value):
            raise ValueError("use lowercase letters, digits, `-`, `_` or `.` (for example `refund-late-order`)")
        return value

    @field_validator("tags", mode="before")
    @classmethod
    def _one_or_many_tags(cls, value: Any) -> Any:
        return [value] if isinstance(value, str) else value

    @model_validator(mode="before")
    @classmethod
    def _single_turn_shorthand(cls, data: Any) -> Any:
        if not isinstance(data, dict) or "user" not in data:
            return data
        if "turns" in data or "simulate" in data:
            raise ValueError("use only one of `user`, `turns` or `simulate`")
        data = dict(data)
        data["turns"] = [{"user": data.pop("user"), "expect": data.pop("expect", {})}]
        return data

    @model_validator(mode="after")
    def _is_runnable(self) -> "Scenario":
        if bool(self.turns) == bool(self.simulate):
            raise ValueError("needs exactly one of `user`, `turns` or `simulate`")
        if self.simulate and self.expect.is_empty:
            raise ValueError("a `simulate` scenario needs a top-level `expect` for the whole conversation")
        if self.expect.is_empty and all(turn.expect.is_empty for turn in self.turns):
            raise ValueError("has no `expect`, so it could never fail. Add at least one expectation.")
        return self

    @property
    def expectations(self) -> list[Expect]:
        return [*(turn.expect for turn in self.turns), self.expect]

    @property
    def needs_judge(self) -> bool:
        return any(expect.rubric for expect in self.expectations)

    @property
    def needs_generator(self) -> bool:
        return self.simulate is not None

    @property
    def uses_tools(self) -> bool:
        return any(expect.uses_tools for expect in self.expectations)


def load_scenarios(directory: Path) -> list[Scenario]:
    """Every scenario under `directory`. Raises ScenarioError listing all problems at once."""
    directory = Path(directory)
    if not directory.is_dir():
        raise ScenarioError([f"{directory} does not exist. Run `roadtest init`, or set `scenarios:` in roadtest.yaml."])
    files = sorted(path for pattern in ("*.yaml", "*.yml") for path in directory.rglob(pattern))
    scenarios: list[Scenario] = []
    problems: list[str] = []
    for path in files:
        loaded, found = _load_file(path, path.relative_to(directory).as_posix())
        scenarios.extend(loaded)
        problems.extend(found)
    first_seen: dict[str, str] = {}
    for scenario in scenarios:
        if scenario.id in first_seen:
            problems.append(f"{scenario.file}: id `{scenario.id}` is already used in {first_seen[scenario.id]}")
        else:
            first_seen[scenario.id] = scenario.file
    if not files:
        problems.append(f"no scenario files in {directory}. Add one, or see `roadtest guide scenarios`.")
    if problems:
        raise ScenarioError(problems)
    return scenarios


def _load_file(path: Path, name: str) -> tuple[list[Scenario], list[str]]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        return [], [f"{name}: not valid YAML: {' '.join(str(error).split())}"]
    if data is None:
        return [], []
    if isinstance(data, list):
        data = {"scenarios": data}
    if not isinstance(data, dict) or not isinstance(data.get("scenarios"), list):
        return [], [f"{name}: expected a top-level `scenarios:` list"]
    unknown = sorted(set(data) - {"scenarios", "tags", "tier"})
    problems = [f"{name}: unknown top-level key `{key}` (allowed: scenarios, tags, tier)" for key in unknown]
    file_tags = data.get("tags") or []
    file_tags = [file_tags] if isinstance(file_tags, str) else list(file_tags)
    scenarios = []
    for position, raw in enumerate(data["scenarios"], start=1):
        label = f"{name}: scenario {position}"
        if not isinstance(raw, dict):
            problems.append(f"{label}: expected a mapping with `id`, `user` and `expect`")
            continue
        if raw.get("id"):
            label = f"{name}: {raw['id']}"
        if "tier" not in raw and data.get("tier"):
            raw = {**raw, "tier": data["tier"]}
        try:
            scenario = Scenario.model_validate(raw)
        except ValidationError as error:
            problems.append(describe_validation_error(error, label))
            continue
        scenario.file = name
        scenario.tags = list(dict.fromkeys([*file_tags, *scenario.tags]))
        scenarios.append(scenario)
    return scenarios, problems


def select(
    scenarios: list[Scenario],
    selectors: list[str] | None = None,
    tags: list[str] | None = None,
    tier: str | None = None,
) -> list[Scenario]:
    """Narrow the list. A selector is an id, an id pattern (`refund-*`) or a file name."""
    chosen = scenarios
    if selectors:
        chosen = [
            scenario
            for scenario in chosen
            if any(
                fnmatch.fnmatch(scenario.id, selector)
                or scenario.file == selector
                or scenario.file.endswith("/" + selector)
                or Path(selector).as_posix().endswith("/" + scenario.file)
                for selector in selectors
            )
        ]
    if tags:
        wanted = set(tags)
        chosen = [scenario for scenario in chosen if wanted & set(scenario.tags)]
    if tier:
        chosen = [scenario for scenario in chosen if scenario.tier == tier]
    return chosen


def _slug(text: str, words: int = 6) -> str:
    parts = re.findall(r"[a-z0-9]+", text.lower())[:words]
    return "-".join(parts) or "criterion"
