"""Connects roadtest to the clinic desk. roadtest calls `call` once per turn."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so `agent.py` can be imported

from agent import Concierge  # noqa: E402


def call(message, session):
    """`session.state` is a dict that belongs to one conversation, so each scenario gets its own desk."""
    if "desk" not in session.state:
        session.state["desk"] = Concierge()
    turn = session.state["desk"].say(message)
    return {"text": turn.text, "tool_calls": turn.tools}
