"""The counter assistant of a made-up tool hire shop, as a command: JSON in on stdin, JSON out on stdout.

roadtest starts this program once for every turn and writes the turn to its stdin:

    {"message": "...", "messages": [...], "history": [...], "session_id": "...", "scenario_id": "...", "vars": {}}

`history` holds the earlier turns as {"role", "content"} pairs. The program keeps nothing between
turns, so it finds the tool a customer means in `history` when the new message does not name one.
It prints one JSON object with the answer and the tools it used:

    {"text": "...", "tool_calls": [{"name": "check_stock", "arguments": {"tool": "pressure washer"}}]}

It is rule-based, so it needs no model and no API key. It could as well be written in any other
language: all roadtest needs is a command that reads JSON and prints JSON (or just the answer text).
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

# keyword in the customer's message -> (name, price per day in dollars, how many are in stock)
TOOLS = {
    "drill": ("cordless drill", 12, 3),
    "ladder": ("extension ladder", 8, 2),
    "washer": ("pressure washer", 30, 1),
    "mixer": ("concrete mixer", 45, 0),
}
HOURS = "We are open 8:00 to 17:30 on weekdays and 9:00 to 13:00 on Saturdays, and closed on Sundays."
SCOPE = "I can check stock, quote prices and reserve tools at Lend-a-Hand Tool Hire."


def respond(message: str, history: list[dict[str, Any]]) -> dict[str, Any]:
    """One turn: the answer text and the tools that were used to write it."""
    text = message.lower()
    tools: list[dict[str, Any]] = []
    words = set(re.findall(r"[a-z']+", text))

    def call(name: str, **arguments: Any) -> None:
        tools.append({"name": name, "arguments": arguments})

    named = _keyword(text)
    if words & {"open", "opening", "close", "closing", "hours"} and not named:
        return {"text": HOURS, "tool_calls": tools}
    keyword = named or _remembered(history)
    wants_reserve = bool(words & {"reserve", "hire", "rent", "book"})
    asks_price = "how much" in text or bool(words & {"price", "cost"})
    if keyword is None:
        if wants_reserve or asks_price or "stock" in words:
            return {"text": "Which tool do you need? We hire drills, ladders, pressure washers and concrete mixers.", "tool_calls": tools}
        return {"text": SCOPE, "tool_calls": tools}

    name, price, in_stock = TOOLS[keyword]
    days = _days(text)
    if asks_price and not wants_reserve:
        return {"text": f"A {name} is ${price} a day, so ${price * days} for {days} day{'s' if days != 1 else ''}.", "tool_calls": tools}

    call("check_stock", tool=name)
    if in_stock == 0:
        return {"text": f"Sorry, the {name} is not in stock. We expect one back on Thursday.", "tool_calls": tools}
    if not wants_reserve:
        return {"text": f"Yes, we have {in_stock} {name}{'s' if in_stock != 1 else ''} in stock, at ${price} a day.", "tool_calls": tools}
    call("reserve_tool", tool=name, days=days)
    return {
        "text": f"Reserved: {name} for {days} day{'s' if days != 1 else ''}, ${price * days} in total. Collect it from the counter at 8:00.",
        "tool_calls": tools,
    }


def _keyword(text: str) -> str | None:
    return next((keyword for keyword in TOOLS if keyword in text), None)


def _remembered(history: list[dict[str, Any]]) -> str | None:
    """The tool the customer last mentioned in an earlier message."""
    for turn in reversed(history):
        if turn.get("role") == "user":
            keyword = _keyword(str(turn.get("content", "")).lower())
            if keyword:
                return keyword
    return None


def _days(text: str) -> int:
    found = re.search(r"(\d+)\s*days?", text)
    if found:
        return int(found.group(1))
    return 2 if "weekend" in text else 1


def main() -> None:
    turn = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    print(json.dumps(respond(str(turn["message"]), turn.get("history") or [])))


if __name__ == "__main__":
    main()
