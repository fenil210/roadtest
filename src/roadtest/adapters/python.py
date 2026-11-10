"""The Python adapter: calls a function in this process."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import sys
from pathlib import Path
from typing import Any, Callable

from ..config import AgentSpec
from ..errors import AgentError, ConfigError
from .base import Agent, Exchange, Reply, Session, shorten, to_reply

class PythonAgent(Agent):
    """Calls `function(message)` or `function(message, session)` in this process."""

    def __init__(self, spec: AgentSpec, base_dir: Path):
        super().__init__(spec, base_dir)
        self._function = load_callable(str(spec.entry), base_dir)
        parameters = [
            parameter
            for parameter in inspect.signature(self._function).parameters.values()
            if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
        ]
        self._takes_session = len(parameters) >= 2

    def describe(self) -> dict[str, str]:
        return {"type": "python", "entry": str(self.spec.entry)}

    async def send(self, message: str, session: Session) -> Reply:
        arguments = (message, session) if self._takes_session else (message,)
        self.last_exchange = Exchange(request={"call": str(self.spec.entry), "message": message})
        try:
            if inspect.iscoroutinefunction(self._function):
                pending = self._function(*arguments)
            else:
                pending = asyncio.to_thread(self._function, *arguments)
            result = await asyncio.wait_for(pending, timeout=self.spec.timeout)
        except asyncio.TimeoutError:
            raise AgentError(f"{self.spec.entry} did not return within {self.spec.timeout:g}s") from None
        except AgentError:
            raise
        except Exception as error:  # the user's code can raise anything
            raise AgentError(f"{self.spec.entry} raised {type(error).__name__}: {error}") from error
        reply = to_reply(result, f"{self.spec.entry}")
        self.last_exchange.body = shorten(repr(result), 4000)
        if reply.session_id:
            session.id = reply.session_id
        return reply


def load_callable(entry: str, base_dir: Path) -> Callable[..., Any]:
    """Resolve `path/to/file.py:function` or `package.module:function`."""
    target, _, name = entry.rpartition(":")
    if not target or not name:
        raise ConfigError(f"`{entry}` must look like `path/to/file.py:function` or `package.module:function`")
    working_dir = Path.cwd()
    for folder in (str(working_dir), str(base_dir)):
        if folder not in sys.path:
            sys.path.insert(0, folder)
    if target.endswith(".py"):
        candidates = [Path(target)] if Path(target).is_absolute() else [base_dir / target, working_dir / target]
        path = next((candidate for candidate in candidates if candidate.is_file()), None)
        if path is None:
            raise ConfigError(f"cannot find {target} (looked in {base_dir} and {working_dir})")
        module_name = f"_roadtest_{abs(hash(str(path.resolve())))}"
        if module_name in sys.modules:
            module = sys.modules[module_name]
        else:
            specification = importlib.util.spec_from_file_location(module_name, path)
            if specification is None or specification.loader is None:
                raise ConfigError(f"cannot import {path}")
            module = importlib.util.module_from_spec(specification)
            sys.modules[module_name] = module
            try:
                specification.loader.exec_module(module)
            except Exception as error:
                del sys.modules[module_name]
                raise ConfigError(f"importing {path} failed: {type(error).__name__}: {error}") from error
    else:
        try:
            module = importlib.import_module(target)
        except Exception as error:
            raise ConfigError(f"importing `{target}` failed: {type(error).__name__}: {error}") from error
    function = getattr(module, name, None)
    if not callable(function):
        raise ConfigError(f"`{name}` is not a function in {target}")
    return function
