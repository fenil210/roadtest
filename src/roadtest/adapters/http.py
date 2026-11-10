"""The HTTP adapter: JSON, plain text, server-sent events and JSON lines."""

from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path
from typing import Any

import httpx

from ..config import AgentSpec
from ..errors import AgentError
from .base import (
    Agent,
    Exchange,
    Reply,
    Session,
    as_text,
    describe_shape,
    guess_text,
    normalize_tool_calls,
    optional_text,
    shorten,
)
from .template import extract, render

RETRYABLE_STATUSES = {429, 502, 503, 504}

# Where streamed text and tool calls usually are when roadtest.yaml does not say. First match wins.
STREAM_TEXT_GUESSES = ("choices[0].delta.content", "delta.content", "delta.text", "delta", "text", "content", "token")
TOOL_GUESSES = ("choices[0].message.tool_calls", "tool_calls")
STREAM_TOOL_GUESSES = ("choices[0].delta.tool_calls", "tool_calls")


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
                        raise AgentError(f"HTTP {response.status_code} from {url}: {shorten(body, 300)}")
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
                raise AgentError(f"expected a JSON reply but got: {shorten(body, 200)}") from None
            return Reply(text=body.strip(), raw=body)
        return self._from_json(data)

    def _from_json(self, data: Any) -> Reply:
        spec = self.spec.response
        if spec.text:
            text = extract(data, spec.text)
            if text is None:
                raise AgentError(f"`{spec.text}` is not in the agent's reply. {describe_shape(data)}")
        else:
            path, text = guess_text(data)
            if text is None:
                raise AgentError(
                    f"could not tell where the answer is in the agent's reply. {describe_shape(data)} "
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
        return Reply(text=as_text(text), tool_calls=tool_calls, session_id=optional_text(session_id), raw=data)

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
                pieces.extend(as_text(item) for item in value if item not in (None, ""))
            elif value not in (None, ""):
                pieces.append(as_text(value))
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
        return Reply(text=text, tool_calls=tool_calls, session_id=optional_text(session_id), raw=events)


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
