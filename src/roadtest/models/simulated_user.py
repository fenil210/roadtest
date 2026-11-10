"""The simulated user: the generator model plays a person with a goal, one message at a time."""

from __future__ import annotations

from ..config import ModelRef
from .client import LLM
from ..scenarios import Simulate

INSTRUCTIONS = """You play a user who is talking to an AI agent, so that the agent can be tested.

Rules:
- Write only what this user would type next. No narration, no quotation marks, no stage directions.
- Stay in character and pursue the goal. Give details when the agent asks for them or when a real user would volunteer them.
- Do not do the agent's work for it, do not hint at what a good answer looks like, and never say this is a test.
- Keep messages the length a real user would write.
- Set `done` to true when the goal has been met, or when the agent has clearly finished or refused and a real user would stop. When done, `message` can be empty."""

NEXT_MESSAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "message": {"type": "string"},
        "done": {"type": "boolean"},
    },
    "required": ["message", "done"],
    "additionalProperties": False,
}


def build_messages(simulate: Simulate, conversation: list[dict[str, str]]) -> list[dict[str, str]]:
    lines = []
    for message in conversation:
        speaker = "you" if message.get("role") == "user" else "agent"
        lines.append(f"[{speaker}]\n{message.get('content', '')}")
    transcript = "\n\n".join(lines) or "(nothing yet: you speak first)"
    persona = simulate.persona.strip() or "An ordinary user."
    prompt = (
        f"<persona>\n{persona}\n</persona>\n\n"
        f"<goal>\n{simulate.goal.strip()}\n</goal>\n\n"
        f"<conversation>\n{transcript}\n</conversation>\n\n"
        "Write the user's next message."
    )
    return [{"role": "system", "content": INSTRUCTIONS}, {"role": "user", "content": prompt}]


async def next_user_message(
    llm: LLM, model: ModelRef, simulate: Simulate, conversation: list[dict[str, str]]
) -> tuple[str, bool]:
    """The next thing the simulated user says, and whether the conversation is over."""
    reply = await llm.json(model, build_messages(simulate, conversation), "next_message", NEXT_MESSAGE_SCHEMA)
    message = str(reply["message"]).strip()
    done = reply["done"] is True or not message
    return message, done
