"""The front desk of a made-up vet clinic, as plain Python: no server, no model, no API key.

`Concierge` is one conversation. It remembers the pet's species and name from earlier messages,
and says in each `Turn` what it answered and which tools it used to write the answer. It is
rule-based, so the same message always gets the same answer.

    from agent import Concierge
    desk = Concierge()
    desk.say("Hi, I have a dog called Biscuit.")
    desk.say("Can I see a vet tomorrow?").text   # Booked: Biscuit sees Dr Okafor tomorrow at 09:30.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# The first free slot tomorrow, and the vet who sees each kind of pet.
FIRST_SLOT = {"dog": ("Dr Okafor", "09:30"), "cat": ("Dr Lind", "10:15"), "rabbit": ("Dr Lind", "11:00")}
URGENT_WORDS = ("chocolate", "poison", "lily", "bleeding", "collapsed", "not breathing", "seizure")
EMERGENCY_LINE = "01632 960321"
SCOPE = "I can book appointments for dogs, cats and rabbits at Paws Row Vets, and answer questions about vaccinations."

_SPECIES = re.compile(r"\b(dog|cat|rabbit)s?\b", re.IGNORECASE)
_OTHER_ANIMAL = re.compile(r"\b(snake|hamster|parrot|lizard|horse|goldfish|tortoise)\b", re.IGNORECASE)
_NAME = re.compile(r"\b(?:called|named)\s+([A-Z][a-z]+)")
_WANTS_APPOINTMENT = ("appointment", "book", "see a vet", "see the vet", "see someone")


@dataclass
class Turn:
    text: str
    tools: list[dict[str, Any]] = field(default_factory=list)


class Concierge:
    """One conversation with the front desk."""

    def __init__(self) -> None:
        self.species: str | None = None
        self.name: str | None = None

    def say(self, message: str) -> Turn:
        text = message.lower()
        if any(word in text for word in URGENT_WORDS):
            return Turn(f"That sounds urgent. Please call our emergency line on {EMERGENCY_LINE} now, and come in straight away.")
        found = _SPECIES.search(message)
        other = _OTHER_ANIMAL.search(message)
        if other and not found:
            return Turn(f"Sorry, we only see dogs, cats and rabbits, so we can't help with a {other.group(1).lower()}.")
        if found:
            self.species = found.group(1).lower()
        named = _NAME.search(message)
        if named:
            self.name = named.group(1)

        if any(phrase in text for phrase in _WANTS_APPOINTMENT):
            return self._book()
        if "vaccin" in text or "booster" in text:
            return Turn("Dogs and cats need a booster every 12 months, and rabbits every 6 months. Bring the vaccination card with you.")
        if found or named:
            who = self.name or f"your {self.species}"
            return Turn(f"Thanks, I have {who}. Would you like an appointment?")
        if re.search(r"\b(hello|hi|hey)\b", text):
            return Turn(f"Hello! {SCOPE}")
        return Turn(f"That is outside what I can do. {SCOPE}")

    def _book(self) -> Turn:
        if self.species is None and self.name is None:
            return Turn("Happy to. What kind of pet is it, and what is their name?")
        if self.species is None:
            return Turn(f"Happy to. What kind of pet is {self.name}?")
        if self.name is None:
            return Turn(f"Happy to. What is your {self.species}'s name?")
        vet, slot = FIRST_SLOT[self.species]
        tools = [
            {"name": "find_slots", "arguments": {"species": self.species}},
            {"name": "book_appointment", "arguments": {"pet": self.name, "vet": vet, "time": slot}},
        ]
        return Turn(f"Booked: {self.name} sees {vet} tomorrow at {slot}.", tools)
