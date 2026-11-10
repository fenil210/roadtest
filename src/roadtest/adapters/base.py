"""What every adapter shares: the reply and session types, and reading a reply out of what the agent returned."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import AgentSpec
from ..errors import AgentError
from .template import extract

# Where the answer usually is when roadtest.yaml does not say. First match wins.
TEXT_GUESSES = (
    "choices[0].message.content",
    "output_text",
    "output",
    "answer",
    "response",
    "reply",
    "text",
    "message.content",
    "message",
    "content",
    "result",
    "data.output",
    "data.answer",
    "data.text",
)
_TOOL_NAME_KEYS = ("name", "tool", "tool_name", "function")
_TOOL_ARGUMENT_KEYS = ("arguments", "args", "input", "parameters", "tool_input")


@dataclass
class ToolCall:
    name: str
    arguments: Any = None


@dataclass
class Reply:
    """What the agent said. `tool_calls` is None when the agent does not report its tool calls."""

    text: str
    tool_calls: list[ToolCall] | None = None
    session_id: str | None = None
    raw: Any = None


@dataclass
class Session:
    """One conversation. `id` is None until the server issues it, when the server owns the ids."""

    id: str | None
    scenario_id: str = ""
    vars: dict[str, Any] = field(default_factory=dict)
    messages: list[dict[str, str]] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)

    def variables(self, message: str) -> dict[str, Any]:
        return {
            "message": message,
            "messages": [*self.messages, {"role": "user", "content": message}],
            "history": list(self.messages),
            "session_id": self.id,
            "scenario_id": self.scenario_id,
            "vars": self.vars,
        }


@dataclass
class Exchange:
    """The last request and response, kept so `roadtest probe` can show them."""

    request: dict[str, Any] = field(default_factory=dict)
    status: int | None = None
    content_type: str = ""
    body: str = ""
    notes: list[str] = field(default_factory=list)


class Agent:
    """Base adapter. Subclasses implement `send`."""

    server_issues_session_ids = False

    def __init__(self, spec: AgentSpec, base_dir: Path):
        self.spec = spec
        self.base_dir = base_dir
        self.last_exchange = Exchange()

    def new_session(self, scenario_id: str = "", variables: dict[str, Any] | None = None) -> Session:
        session_id = None if self.server_issues_session_ids else str(uuid.uuid4())
        return Session(id=session_id, scenario_id=scenario_id, vars=dict(variables or {}))

    async def send(self, message: str, session: Session) -> Reply:
        raise NotImplementedError

    async def aclose(self) -> None:
        return None

    def describe(self) -> dict[str, str]:
        return {"type": self.spec.type}


def to_reply(value: Any, source: str) -> Reply:
    """Accept the shapes an adapter may return: a string, a dict, or a Reply."""
    if isinstance(value, Reply):
        return value
    if isinstance(value, str):
        return Reply(text=value, raw=value)
    if isinstance(value, dict):
        text = value.get("text")
        if text is None:
            _, text = guess_text(value)
        if text is None:
            raise AgentError(f"{source} returned a dict without a `text` key. {describe_shape(value)}")
        tool_calls = normalize_tool_calls(value["tool_calls"]) if value.get("tool_calls") is not None else None
        return Reply(text=as_text(text), tool_calls=tool_calls, session_id=optional_text(value.get("session_id")), raw=value)
    raise AgentError(f"{source} returned {type(value).__name__}; return a string, a dict with `text`, or a roadtest.Reply")


def guess_text(data: Any, candidates: tuple[str, ...] = TEXT_GUESSES) -> tuple[str | None, str | None]:
    """The first candidate path that holds a non-empty string."""
    if isinstance(data, str):
        return "", data
    for path in candidates:
        value = extract(data, path)
        if isinstance(value, str) and value:
            return path, value
    return None, None


def normalize_tool_calls(values: list[Any] | None) -> list[ToolCall]:
    """Turn whatever the agent reports (names, or objects in a few common shapes) into ToolCalls."""
    calls = []
    for value in values or []:
        if isinstance(value, ToolCall):
            calls.append(value)
        elif isinstance(value, str):
            calls.append(ToolCall(name=value))
        elif isinstance(value, dict):
            source = value
            name = None
            for key in _TOOL_NAME_KEYS:
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate:
                    name = candidate
                    break
                if isinstance(candidate, dict) and isinstance(candidate.get("name"), str):
                    name, source = candidate["name"], candidate
                    break
            if not name:
                continue  # for example a streaming delta that only carries more arguments
            arguments = next((source[key] for key in _TOOL_ARGUMENT_KEYS if key in source), None)
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    pass
            calls.append(ToolCall(name=name, arguments=arguments))
    return calls


def as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "".join(value)
    return json.dumps(value, ensure_ascii=False)


def optional_text(value: Any) -> str | None:
    return None if value in (None, "") else str(value)


def shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe_shape(data: Any) -> str:
    if isinstance(data, dict):
        return f"Top-level keys: {', '.join(map(str, list(data)[:12])) or '(none)'}."
    if isinstance(data, list):
        return f"The reply is a list of {len(data)} items."
    return f"The reply is a {type(data).__name__}."
