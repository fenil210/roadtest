"""roadtest.yaml: where the agent lives, which models grade it, and how runs behave."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .errors import ConfigError

CONFIG_NAMES = ("evals/roadtest.yaml", "evals/roadtest.yml", "roadtest.yaml", "roadtest.yml")
DEFAULT_TARGET = "default"
DEFAULT_JUDGE_MODEL = "gpt-6-luna"
DEFAULT_GENERATOR_MODEL = "gpt-6.1-terra"

_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResponseSpec(_Strict):
    """How to read the agent's reply."""

    format: Literal["auto", "json", "text", "sse", "jsonl"] = "auto"
    text: str | None = None
    tool_calls: str | None = None
    session_id: str | None = None
    join: Literal["concat", "last"] = "concat"


class AgentSpec(_Strict):
    """One way of reaching the agent under test."""

    type: Literal["http", "python", "command"] = "http"
    url: str | None = None
    method: str = "POST"
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    response: ResponseSpec = Field(default_factory=ResponseSpec)
    entry: str | None = None
    run: list[str] | str | None = None
    timeout: float = Field(default=120, gt=0)
    retries: int = Field(default=2, ge=0, le=10)

    @model_validator(mode="after")
    def _has_what_its_type_needs(self) -> "AgentSpec":
        if self.type == "http" and not self.url:
            raise ValueError("an http agent needs `url`")
        if self.type == "python" and not self.entry:
            raise ValueError("a python agent needs `entry`, for example `evals/adapter.py:call`")
        if self.type == "command" and not self.run:
            raise ValueError("a command agent needs `run`, for example `[node, agent.js]`")
        return self


class ModelRef(_Strict):
    """One model. In roadtest.yaml this is a plain id, or a mapping when it needs tuning."""

    model: str
    reasoning_effort: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class ModelsSpec(_Strict):
    """The models roadtest itself uses, called through the OpenAI Chat Completions API.

    `judge` decides pass or fail for each rubric criterion. `generator` plays the simulated user.
    """

    judge: ModelRef = Field(default_factory=lambda: ModelRef(model=DEFAULT_JUDGE_MODEL))
    generator: ModelRef = Field(default_factory=lambda: ModelRef(model=DEFAULT_GENERATOR_MODEL))
    base_url: str | None = None
    api_key_env: str = "OPENAI_API_KEY"

    @field_validator("judge", "generator", mode="before")
    @classmethod
    def _accept_a_plain_id(cls, value: Any) -> Any:
        return {"model": value} if isinstance(value, str) else value


class RunSpec(_Strict):
    concurrency: int = Field(default=4, ge=1, le=64)
    repeat: int = Field(default=1, ge=1, le=50)


class Config(_Strict):
    version: Literal[1] = 1
    agent: dict[str, Any]
    targets: dict[str, dict[str, Any]] = Field(default_factory=dict)
    models: ModelsSpec = Field(default_factory=ModelsSpec)
    run: RunSpec = Field(default_factory=RunSpec)
    scenarios: str = "scenarios"
    results: str = "results"

    # Set by load_config; not part of the file.
    path: Path = Field(default=Path("roadtest.yaml"), exclude=True)

    @property
    def directory(self) -> Path:
        return self.path.parent

    @property
    def scenarios_dir(self) -> Path:
        return (self.directory / self.scenarios).resolve()

    @property
    def results_dir(self) -> Path:
        return (self.directory / self.results).resolve()

    @property
    def baseline_path(self) -> Path:
        return self.directory / "baseline.json"

    @property
    def target_names(self) -> list[str]:
        return [DEFAULT_TARGET, *self.targets]

    def agent_for(self, target: str | None = None) -> AgentSpec:
        """The agent settings for `target`, with `${ENV}` references filled in.

        A named target overrides only the keys it sets; everything else comes from `agent`.
        Environment variables are read here, not at load time, so a missing prod token does
        not stop a local run.
        """
        name = target or DEFAULT_TARGET
        merged = self.agent
        if name != DEFAULT_TARGET:
            if name not in self.targets:
                known = ", ".join(self.target_names)
                raise ConfigError(f"no target named `{name}` in {self.path}. Targets: {known}")
            merged = _merge(self.agent, self.targets[name])
        where = "agent" if name == DEFAULT_TARGET else f"targets.{name}"
        expanded = _expand_env(merged, where)
        try:
            return AgentSpec.model_validate(expanded)
        except ValidationError as error:
            raise ConfigError(describe_validation_error(error, f"{self.path}: {where}")) from None


def find_config(start: Path | None = None) -> Path:
    """The nearest roadtest.yaml, looking in `start` and then each parent folder."""
    override = os.environ.get("ROADTEST_CONFIG")
    if override:
        path = Path(override).expanduser()
        if not path.is_file():
            raise ConfigError(f"ROADTEST_CONFIG points to {path}, which does not exist")
        return path.resolve()
    here = (start or Path.cwd()).resolve()
    for folder in (here, *here.parents):
        for name in CONFIG_NAMES:
            candidate = folder / name
            if candidate.is_file():
                return candidate
    raise ConfigError("no roadtest.yaml found here or in a parent folder. Run `roadtest init` first.")


def load_config(path: Path | None = None) -> Config:
    """Read and validate roadtest.yaml, and load `.env` files next to it and in the working folder."""
    config_path = Path(path).expanduser().resolve() if path else find_config()
    if not config_path.is_file():
        raise ConfigError(f"{config_path} does not exist")
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ConfigError(f"{config_path} is not valid YAML: {error}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{config_path} must be a mapping with an `agent:` section")
    if "agent" not in data:
        raise ConfigError(f"{config_path} has no `agent:` section. See `roadtest guide setup`.")
    try:
        config = Config.model_validate(data)
    except ValidationError as error:
        raise ConfigError(describe_validation_error(error, str(config_path))) from None
    config.path = config_path
    for folder in dict.fromkeys((Path.cwd(), config_path.parent, config_path.parent.parent)):
        load_env_file(folder / ".env")
    return config


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines from a .env file without replacing variables that are already set."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key and key not in os.environ:
            os.environ[key] = value


def describe_validation_error(error: ValidationError, source: str) -> str:
    """One readable line per problem, each starting with where it is."""
    lines = []
    for issue in error.errors():
        location = ".".join(str(part) for part in issue["loc"])
        message = issue["msg"].removeprefix("Value error, ")
        if issue["type"] == "extra_forbidden":
            message = "unknown key"
        lines.append(f"{source}: {location}: {message}" if location else f"{source}: {message}")
    return "\n".join(lines)


def _merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for key, value in override.items():
            merged[key] = _merge(base.get(key), value) if key in base else value
        return merged
    return override


def _expand_env(value: Any, where: str) -> Any:
    if isinstance(value, dict):
        return {key: _expand_env(item, f"{where}.{key}") for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_env(item, f"{where}[{index}]") for index, item in enumerate(value)]
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        if name in os.environ:
            return os.environ[name]
        if default is not None:
            return default
        raise ConfigError(
            f"environment variable {name} is not set (used in {where}). "
            f"Export it or add it to a .env file."
        )

    return _ENV_REFERENCE.sub(replace, value)
