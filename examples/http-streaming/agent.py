"""Train times for a made-up railway, as a streaming agent that issues its own conversation ids.

The answer is streamed as server-sent events, a few words at a time. The server creates the
conversation on the first request and returns its id; later requests must send that id back.
An id the server did not issue is rejected, so the first request must not carry one.
It is rule-based: no model, no API key, and the same answer every time.

    python3 agent.py --port 18802

    POST /chat/stream  {"message": "When is the next train from Leeds to York?"}

    data: {"type": "conversation", "conversation_id": "conv-1"}
    data: {"type": "tool", "tool": {"name": "find_departures", "arguments": {...}}}
    data: {"type": "delta", "delta": "The next "}
    data: {"type": "delta", "delta": "train "}
    ...
    data: [DONE]
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

DEFAULT_PORT = 18802

DEPARTURES = {
    ("leeds", "york"): {"time": "09:15", "platform": 4, "minutes": 25, "late_by": 0},
    ("york", "leeds"): {"time": "09:32", "platform": 3, "minutes": 25, "late_by": 0},
    ("leeds", "manchester"): {"time": "09:40", "platform": 12, "minutes": 55, "late_by": 10},
}
STATIONS = ("leeds", "york", "manchester")
SCOPE = "I can only help with train times and delays on the Northline."

_ROUTE = re.compile(r"\bfrom\s+([a-z]+)\s+to\s+([a-z]+)")
_conversations: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def respond(message: str, memory: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """One turn: the answer and the tools that were called to write it."""
    text = message.lower()
    tools: list[dict[str, Any]] = []

    def call(name: str, **arguments: Any) -> None:
        tools.append({"name": name, "arguments": arguments})

    found = _ROUTE.search(text)
    if found:
        unknown = [station for station in found.groups() if station not in STATIONS]
        if unknown:
            return f"I don't know a station called {unknown[0].title()}. I cover Leeds, York and Manchester.", tools
        memory["route"] = found.groups()
    route = memory.get("route")
    asks_about_delay = any(word in text for word in ("on time", "delay", "late"))
    asks_for_next = any(word in text for word in ("next", "when", "what time", "depart"))

    if not (asks_about_delay or asks_for_next):
        if found:
            return f"Got it: {_named(route)}. I can tell you when the next train leaves or whether it is on time.", tools
        return f"{SCOPE} I can't help with that.", tools
    if not route:
        return "Where are you travelling from and to?", tools
    origin, destination = route
    train = DEPARTURES.get(route)
    if train is None:
        call("find_departures", origin=origin, destination=destination)
        return f"There are no direct trains from {_named(route)} today.", tools
    if asks_about_delay:
        call("check_disruptions", origin=origin, destination=destination)
        if train["late_by"]:
            return f"The {train['time']} from {_named(route)} is running {train['late_by']} minutes late.", tools
        return f"The {train['time']} from {_named(route)} is on time.", tools
    call("find_departures", origin=origin, destination=destination)
    return (
        f"The next train from {_named(route)} leaves at {train['time']} from platform {train['platform']}. "
        f"The journey takes {train['minutes']} minutes."
    ), tools


def _named(route: tuple[str, str]) -> str:
    return f"{route[0].title()} to {route[1].title()}"


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - the name is set by BaseHTTPRequestHandler
        if self.path.rstrip("/") != "/chat/stream":
            self._error(404, "POST /chat/stream")
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
            message = body["message"]
            if not isinstance(message, str):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            self._error(400, 'send JSON like {"message": "...", "conversation_id": "..."}')
            return
        conversation_id = body.get("conversation_id")
        with _lock:
            if conversation_id is None:
                conversation_id = f"conv-{len(_conversations) + 1}"
                _conversations[conversation_id] = {}
            elif conversation_id not in _conversations:
                self._error(404, "unknown conversation_id. Leave it out to start a new conversation.")
                return
            memory = _conversations[conversation_id]
        text, tools = respond(message, memory)

        self.send_response(200)
        self.send_header("content-type", "text/event-stream; charset=utf-8")
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        self._event({"type": "conversation", "conversation_id": conversation_id})
        for tool in tools:
            self._event({"type": "tool", "tool": tool})
        for piece in re.findall(r"\S+\s*", text):
            self._event({"type": "delta", "delta": piece})
            time.sleep(0.01)  # so the pieces really arrive one after another
        self.wfile.write(b"data: [DONE]\n\n")

    def _event(self, payload: dict[str, Any]) -> None:
        self.wfile.write(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _error(self, status: int, message: str) -> None:
        data = json.dumps({"error": message}).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *arguments: Any) -> None:
        return None


def start(port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    """Start the agent on a background thread. Port 0 picks a free port. Returns (server, stream URL)."""
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/chat/stream"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the train times agent.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    arguments = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", arguments.port), _Handler)
    print(f"Train times agent on http://127.0.0.1:{arguments.port}/chat/stream  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
