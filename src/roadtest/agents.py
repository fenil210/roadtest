"""Adapters: the three ways roadtest can talk to an agent (HTTP, a Python function, a command)."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import json
import random
import shlex
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from .config import AgentSpec
from .errors import AgentError, ConfigError
from .template import extract, render

RETRYABLE_STATUSES = {429, 502, 503, 504}

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
STREAM_TEXT_GUESSES = ("choices[0].delta.content", "delta.content", "delta.text", "delta", "text", "content", "token")
TOOL_GUESSES = ("choices[0].message.tool_calls", "tool_calls")
STREAM_TOOL_GUESSES = ("choices[0].delta.tool_calls", "tool_calls")
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


class HttpAgent(Agent):
    def __init__(self, spec: AgentSpec, base_dir: Path):
        super().__init__(spec, base_dir)
        self.server_issues_session_ids = spec.response.session_id is not None
        timeout = httpx.Timeout(spec.timeout, connect=min(10.0, spec.timeout))
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    def describe(self) -> dict[str, str]:
        return {"type": "http", "url": str(self.spec.url).split("?", 1)[0]}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def send(self, message: str, session: Session) -> Reply:
        variables = session.variables(message)
        url = render(self.spec.url, variables)
        headers = render(self.spec.headers, variables)
        template = self.spec.body if self.spec.body is not None else {"message": "{{message}}"}
        payload = render(template, variables)
        method = self.spec.method.upper()
        if method == "GET":
            arguments: dict[str, Any] = {"params": payload}
        elif isinstance(payload, str):
            arguments = {"content": payload}
        else:
            arguments = {"json": payload}
        self.last_exchange = Exchange(request={"method": method, "url": url, "body": payload})

        for attempt in range(self.spec.retries + 1):
            last_try = attempt == self.spec.retries
            try:
                async with self._client.stream(method, url, headers=headers, **arguments) as response:
                    self.last_exchange.status = response.status_code
                    self.last_exchange.content_type = response.headers.get("content-type", "")
                    if response.status_code in RETRYABLE_STATUSES and not last_try:
                        await response.aread()
                        await asyncio.sleep(_backoff(attempt, response.headers.get("retry-after")))
                        continue
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", errors="replace")
                        self.last_exchange.body = body
                        raise AgentError(f"HTTP {response.status_code} from {url}: {_shorten(body, 300)}")
                    reply = await self._read(response)
            except httpx.ConnectError:
                if last_try:
                    raise AgentError(f"could not connect to {url}. Is the agent running?") from None
                await asyncio.sleep(_backoff(attempt))
                continue
            except httpx.TimeoutException:
                raise AgentError(f"no reply from {url} within {self.spec.timeout:g}s") from None
            except httpx.HTTPError as error:
                raise AgentError(f"request to {url} failed: {error}") from None
            if reply.session_id and self.server_issues_session_ids:
                session.id = reply.session_id
            return reply
        raise AgentError(f"no reply from {url}")  # pragma: no cover - the loop always returns or raises

    async def _read(self, response: httpx.Response) -> Reply:
        wanted = self.spec.response.format
        content_type = response.headers.get("content-type", "").lower()
        if wanted == "auto":
            if "text/event-stream" in content_type:
                wanted = "sse"
            elif "ndjson" in content_type or "jsonl" in content_type:
                wanted = "jsonl"
            elif "json" in content_type:
                wanted = "json"
            else:
                wanted = "text"
        if wanted in ("sse", "jsonl"):
            lines = [line async for line in response.aiter_lines()]
            self.last_exchange.body = "\n".join(lines)
            events = _sse_events(lines) if wanted == "sse" else _json_lines(lines)
            return self._from_events(events)
        body = (await response.aread()).decode("utf-8", errors="replace")
        self.last_exchange.body = body
        if wanted == "text" and not self.spec.response.text:
            return Reply(text=body.strip(), raw=body)
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            if wanted == "json" or self.spec.response.text:
                raise AgentError(f"expected a JSON reply but got: {_shorten(body, 200)}") from None
            return Reply(text=body.strip(), raw=body)
        return self._from_json(data)

    def _from_json(self, data: Any) -> Reply:
        spec = self.spec.response
        if spec.text:
            text = extract(data, spec.text)
            if text is None:
                raise AgentError(f"`{spec.text}` is not in the agent's reply. {_shape(data)}")
        else:
            path, text = guess_text(data)
            if text is None:
                raise AgentError(
                    f"could not tell where the answer is in the agent's reply. {_shape(data)} "
                    "Set `agent.response.text` in roadtest.yaml to its path."
                )
            self.last_exchange.notes.append(f"answer found at `{path}` (set agent.response.text to pin it)")
        if spec.tool_calls:
            found = extract(data, spec.tool_calls)
            tool_calls = normalize_tool_calls(found if isinstance(found, list) else [] if found is None else [found])
        else:
            path, found = _first_list(data, TOOL_GUESSES)
            tool_calls = normalize_tool_calls(found) if found is not None else None
            if path:
                self.last_exchange.notes.append(f"tool calls found at `{path}[*]` (set agent.response.tool_calls to pin it)")
        session_id = extract(data, spec.session_id) if spec.session_id else None
        return Reply(text=_text(text), tool_calls=tool_calls, session_id=_optional_text(session_id), raw=data)

    def _from_events(self, events: list[Any]) -> Reply:
        spec = self.spec.response
        pieces: list[str] = []
        tool_values: list[Any] = []
        saw_tool_field = bool(spec.tool_calls)
        session_id = None
        for event in events:
            if isinstance(event, str):
                if not spec.text:
                    pieces.append(event)
                continue
            if spec.text:
                value = extract(event, spec.text)
            else:
                _, value = guess_text(event, STREAM_TEXT_GUESSES)
            if isinstance(value, list):
                pieces.extend(_text(item) for item in value if item not in (None, ""))
            elif value not in (None, ""):
                pieces.append(_text(value))
            if spec.tool_calls:
                found = extract(event, spec.tool_calls)
                tool_values.extend(found if isinstance(found, list) else [] if found is None else [found])
            else:
                _, found = _first_list(event, STREAM_TOOL_GUESSES)
                if found is not None:
                    saw_tool_field = True
                    tool_values.extend(found)
            if spec.session_id and session_id is None:
                session_id = extract(event, spec.session_id)
        if not pieces:
            where = f"at `{spec.text}`" if spec.text else "in any of the usual places"
            raise AgentError(
                f"the stream had {len(events)} events but no answer text {where}. "
                "Run `roadtest probe --raw` to see the events, then set `agent.response.text`."
            )
        text = "".join(pieces) if spec.join == "concat" else pieces[-1]
        tool_calls = normalize_tool_calls(tool_values) if saw_tool_field else None
        return Reply(text=text, tool_calls=tool_calls, session_id=_optional_text(session_id), raw=events)


class PythonAgent(Agent):
    """Calls `function(message)` or `function(message, session)` in this process."""

    def __init__(self, spec: AgentSpec, base_dir: Path):
        super().__init__(spec, base_dir)
        self._function = load_callable(str(spec.entry), base_dir)
        parameters = [
            parameter
            for parameter in inspect.signature(self._function).parameters.values()
            if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
        ]
        self._takes_session = len(parameters) >= 2

    def describe(self) -> dict[str, str]:
        return {"type": "python", "entry": str(self.spec.entry)}

    async def send(self, message: str, session: Session) -> Reply:
        arguments = (message, session) if self._takes_session else (message,)
        self.last_exchange = Exchange(request={"call": str(self.spec.entry), "message": message})
        try:
            if inspect.iscoroutinefunction(self._function):
                pending = self._function(*arguments)
            else:
                pending = asyncio.to_thread(self._function, *arguments)
            result = await asyncio.wait_for(pending, timeout=self.spec.timeout)
        except asyncio.TimeoutError:
            raise AgentError(f"{self.spec.entry} did not return within {self.spec.timeout:g}s") from None
        except AgentError:
            raise
        except Exception as error:  # the user's code can raise anything
            raise AgentError(f"{self.spec.entry} raised {type(error).__name__}: {error}") from error
        reply = to_reply(result, f"{self.spec.entry}")
        self.last_exchange.body = _shorten(repr(result), 4000)
        if reply.session_id:
            session.id = reply.session_id
        return reply


class CommandAgent(Agent):
    """Runs a command per turn. The turn goes in as JSON on stdin; the answer comes back on stdout."""

    def describe(self) -> dict[str, str]:
        run = self.spec.run
        return {"type": "command", "run": run if isinstance(run, str) else " ".join(run or [])}

    async def send(self, message: str, session: Session) -> Reply:
        variables = session.variables(message)
        run = self.spec.run
        arguments = [str(render(part, variables)) for part in (shlex.split(run) if isinstance(run, str) else list(run or []))]
        payload = json.dumps(variables, ensure_ascii=False)
        self.last_exchange = Exchange(request={"run": arguments, "stdin": variables})
        try:
            process = await asyncio.create_subprocess_exec(
                *arguments,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            raise AgentError(f"command not found: {arguments[0]}") from None
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(payload.encode("utf-8")), timeout=self.spec.timeout)
        except asyncio.TimeoutError:
            process.kill()
            raise AgentError(f"`{arguments[0]}` did not finish within {self.spec.timeout:g}s") from None
        output = stdout.decode("utf-8", errors="replace").strip()
        self.last_exchange.body = output
        if process.returncode != 0:
            detail = _shorten(stderr.decode("utf-8", errors="replace").strip() or output, 300)
            raise AgentError(f"`{arguments[0]}` exited with code {process.returncode}: {detail}")
        if output.startswith("{"):
            try:
                reply = to_reply(json.loads(output), "the command's JSON output")
            except json.JSONDecodeError:
                reply = Reply(text=output, raw=output)
        else:
            reply = Reply(text=output, raw=output)
        if not reply.text:
            raise AgentError(f"`{arguments[0]}` printed nothing")
        if reply.session_id:
            session.id = reply.session_id
        return reply


def build_agent(spec: AgentSpec, base_dir: Path) -> Agent:
    return {"http": HttpAgent, "python": PythonAgent, "command": CommandAgent}[spec.type](spec, base_dir)


def load_callable(entry: str, base_dir: Path) -> Callable[..., Any]:
    """Resolve `path/to/file.py:function` or `package.module:function`."""
    target, _, name = entry.rpartition(":")
    if not target or not name:
        raise ConfigError(f"`{entry}` must look like `path/to/file.py:function` or `package.module:function`")
    working_dir = Path.cwd()
    for folder in (str(working_dir), str(base_dir)):
        if folder not in sys.path:
            sys.path.insert(0, folder)
    if target.endswith(".py"):
        candidates = [Path(target)] if Path(target).is_absolute() else [base_dir / target, working_dir / target]
        path = next((candidate for candidate in candidates if candidate.is_file()), None)
        if path is None:
            raise ConfigError(f"cannot find {target} (looked in {base_dir} and {working_dir})")
        module_name = f"_roadtest_{abs(hash(str(path.resolve())))}"
        if module_name in sys.modules:
            module = sys.modules[module_name]
        else:
            specification = importlib.util.spec_from_file_location(module_name, path)
            if specification is None or specification.loader is None:
                raise ConfigError(f"cannot import {path}")
            module = importlib.util.module_from_spec(specification)
            sys.modules[module_name] = module
            try:
                specification.loader.exec_module(module)
            except Exception as error:
                del sys.modules[module_name]
                raise ConfigError(f"importing {path} failed: {type(error).__name__}: {error}") from error
    else:
        try:
            module = importlib.import_module(target)
        except Exception as error:
            raise ConfigError(f"importing `{target}` failed: {type(error).__name__}: {error}") from error
    function = getattr(module, name, None)
    if not callable(function):
        raise ConfigError(f"`{name}` is not a function in {target}")
    return function


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
            raise AgentError(f"{source} returned a dict without a `text` key. {_shape(value)}")
        tool_calls = normalize_tool_calls(value["tool_calls"]) if value.get("tool_calls") is not None else None
        return Reply(text=_text(text), tool_calls=tool_calls, session_id=_optional_text(value.get("session_id")), raw=value)
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


def _first_list(data: Any, candidates: tuple[str, ...]) -> tuple[str | None, list[Any] | None]:
    for path in candidates:
        value = extract(data, path)
        if isinstance(value, list):
            return path, value
    return None, None


def _sse_events(lines: list[str]) -> list[Any]:
    events: list[Any] = []
    buffer: list[str] = []

    def flush() -> None:
        if not buffer:
            return
        data = "\n".join(buffer)
        buffer.clear()
        if data.strip() in ("", "[DONE]"):
            return
        try:
            events.append(json.loads(data))
        except json.JSONDecodeError:
            events.append(data)

    for line in lines:
        if not line.strip():
            flush()
        elif line.startswith("data:"):
            buffer.append(line[5:].removeprefix(" "))
    flush()
    return events


def _json_lines(lines: list[str]) -> list[Any]:
    events: list[Any] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            events.append(line)
    return events


def _backoff(attempt: int, retry_after: str | None = None) -> float:
    if retry_after:
        try:
            return min(float(retry_after), 30.0)
        except ValueError:
            pass
    return min(0.5 * (2**attempt), 8.0) + random.uniform(0, 0.25)


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "".join(value)
    return json.dumps(value, ensure_ascii=False)


def _optional_text(value: Any) -> str | None:
    return None if value in (None, "") else str(value)


def _shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _shape(data: Any) -> str:
    if isinstance(data, dict):
        return f"Top-level keys: {', '.join(map(str, list(data)[:12])) or '(none)'}."
    if isinstance(data, list):
        return f"The reply is a list of {len(data)} items."
    return f"The reply is a {type(data).__name__}."
