"""The two models roadtest uses: the judge, which grades rubric lines, and the generator,
which plays the user in simulated conversations."""

from .client import LLM

__all__ = ["LLM"]
