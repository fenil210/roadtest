"""Adapters: how roadtest reaches the agent under test, over HTTP, as a Python function or as a command."""

from __future__ import annotations

from pathlib import Path

from ..config import AgentSpec
from .base import Agent, Exchange, Reply, Session, ToolCall
from .command import CommandAgent
from .http import HttpAgent
from .python import PythonAgent, load_callable

__all__ = [
    "Agent",
    "CommandAgent",
    "Exchange",
    "HttpAgent",
    "PythonAgent",
    "Reply",
    "Session",
    "ToolCall",
    "build_agent",
    "load_callable",
]


def build_agent(spec: AgentSpec, base_dir: Path) -> Agent:
    return {"http": HttpAgent, "python": PythonAgent, "command": CommandAgent}[spec.type](spec, base_dir)
