"""Table reservations for a made-up bistro, behind an OpenAI-style chat completions endpoint.

The endpoint is stateless, like the OpenAI API: every request carries the whole conversation in
`messages`, and the agent works out party size and day from the earlier user turns. It is
rule-based: no model, no API key, and the same answer every time.

    python3 agent.py --port 18803

    POST /v1/chat/completions
    {"model": "bistro-agent", "messages": [{"role": "user", "content": "A table for 4 on Friday?"}]}

    200  {"choices": [{"message": {"role": "assistant", "content": "...", "tool_calls": [...]}}]}
"""

from __future__ import annotations

import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

DEFAULT_PORT = 18803

# Free tables by day. Monday is not listed because the bistro is closed; Saturday is full.
FREE_TIMES = {
    "tuesday": ["18:00", "19:30", "21:00"],
    "wednesday": ["18:00", "19:30", "21:00"],
    "thursday": ["18:00", "19:30", "21:00"],
    "friday": ["18:00", "20:30"],
    "saturday": [],
    "sunday": ["12:30", "14:00", "18:00"],
}
MAX_PARTY = 8
PHONE = "01632 960123"
SCOPE = "I can help you book a table at Harbour Bistro, and answer questions about the menu."
ASK_BOTH = "How many people will be dining, and on which day?"

_DAY = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.IGNORECASE)
_PARTY = re.compile(r"(?:table for|party of|\bfor)\s+(\d{1,2})\b(?!:)|\b(\d{1,2})\s+(?:people|guests|of us)", re.IGNORECASE)
_TIME = re.compile(r"\b(\d{1,2}):(\d{2})\s*(am|pm)?|\b(\d{1,2})\s*(am|pm)\b", re.IGNORECASE)


def respond(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """One turn: the answer and the tools that were used to write it."""
    users = [m["content"] for m in messages if m.get("role") == "user" and isinstance(m.get("content"), str)]
    if not users:
        return f"Hello! {SCOPE}", []
    latest = users[-1]
    text = latest.lower()
    words = set(re.findall(r"[a-z']+", text))
    tools: list[dict[str, Any]] = []

    def call(name: str, **arguments: Any) -> None:
        tools.append({"name": name, "arguments": arguments})

    if words & {"menu", "vegan", "vegetarian", "allergy", "allergies", "gluten"}:
        return "Our menu changes weekly. There is always a vegan main, and we can adapt most dishes for allergies.", tools
    if not (words & {"table", "book", "booking", "reserve", "reservation", "availability", "available"}):
        if words & {"thanks", "thank", "bye"}:
            return "You're welcome. See you soon!", tools
        if words & {"hello", "hi", "hey"}:
            return f"Hello! {SCOPE}", tools
        return f"That is outside what I can do. {SCOPE}", tools

    party = _latest(users, _party)
    day = _latest(users, _day)
    if party is None or day is None:
        if party is None and day is None:
            return ASK_BOTH, tools
        return ("How many people will be dining?" if party is None else "Which day would you like?"), tools
    if party > MAX_PARTY:
        return f"For parties of more than {MAX_PARTY}, please call us on {PHONE}.", tools
    if day == "monday":
        return "We are closed on Mondays. We are open Tuesday to Sunday.", tools

    name = day.title()
    free = FREE_TIMES[day]
    wanted = _time(latest)
    call("check_availability", day=day, party_size=party)
    if not free:
        return f"Sorry, we are fully booked on {name}.", tools
    if wanted is None:
        return f"On {name} we have a table for {party} at {_joined(free)}.", tools
    if wanted not in free:
        return f"We don't have a table at {wanted} on {name}. The free times are {_joined(free)}.", tools
    call("book_table", day=day, party_size=party, time=wanted)
    return f"Booked: a table for {party} on {name} at {wanted}. We will hold it for 15 minutes after that.", tools


def _latest(users: list[str], read: Any) -> Any:
    """The most recent value `read` finds in the user's messages, newest message first."""
    for message in reversed(users):
        value = read(message)
        if value is not None:
            return value
    return None


def _day(message: str) -> str | None:
    found = _DAY.search(message)
    return found.group(1).lower() if found else None


def _party(message: str) -> int | None:
    found = _PARTY.search(message)
    return int(found.group(1) or found.group(2)) if found else None


def _time(message: str) -> str | None:
    found = _TIME.search(message)
    if not found:
        return None
    clock_hour, minute, clock_period, bare_hour, bare_period = found.groups()
    hour = int(clock_hour or bare_hour)
    period = (clock_period or bare_period or "").lower()
    if period == "pm" and hour < 12:
        hour += 12
    elif period == "am" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute or '00'}"


def _joined(times: list[str]) -> str:
    return times[0] if len(times) == 1 else ", ".join(times[:-1]) + " and " + times[-1]


def completion(text: str, tools: list[dict[str, Any]]) -> dict[str, Any]:
    """The reply in the shape of an OpenAI chat completion."""
    message: dict[str, Any] = {"role": "assistant", "content": text}
    if tools:
        message["tool_calls"] = [
            {
                "id": f"call_{number}",
                "type": "function",
                "function": {"name": tool["name"], "arguments": json.dumps(tool["arguments"])},
            }
            for number, tool in enumerate(tools, start=1)
        ]
    return {
        "id": "chatcmpl-bistro",
        "object": "chat.completion",
        "model": "bistro-agent",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
    }


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - the name is set by BaseHTTPRequestHandler
        if self.path.rstrip("/") != "/v1/chat/completions":
            self._send(404, {"error": {"message": "POST /v1/chat/completions"}})
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
            messages = body["messages"]
            if not isinstance(messages, list) or not all(isinstance(item, dict) for item in messages):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            self._send(400, {"error": {"message": 'send JSON like {"model": "...", "messages": [{"role": "user", "content": "..."}]}'}})
            return
        self._send(200, completion(*respond(messages)))

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *arguments: Any) -> None:
        return None


def start(port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    """Start the agent on a background thread. Port 0 picks a free port. Returns (server, completions URL)."""
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the bistro reservations agent.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    arguments = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", arguments.port), _Handler)
    print(f"Bistro agent on http://127.0.0.1:{arguments.port}/v1/chat/completions  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
