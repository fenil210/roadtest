"""Errors roadtest raises on purpose. The CLI prints them without a traceback."""

from __future__ import annotations


class RoadtestError(Exception):
    """Base class: something the user can fix (config, scenarios, environment)."""


class ConfigError(RoadtestError):
    """roadtest.yaml is missing, invalid, or refers to something that is not set."""


class ScenarioError(RoadtestError):
    """One or more scenario files are invalid. `problems` lists every issue found."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("\n".join(problems))


class AgentError(RoadtestError):
    """The agent under test could not be reached or its reply could not be read."""


class ModelError(RoadtestError):
    """A judge or generator model call failed (bad key, unknown model, refusal, bad JSON)."""
