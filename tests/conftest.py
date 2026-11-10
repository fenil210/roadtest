"""Shared helpers. Everything here is real roadtest code talking to real (tiny) agents.

The only stand-ins are agents under test: the bundled demo agent, the agents in `examples/`, and
`scripted_server`, a stdlib HTTP server whose replies a test decides. Nothing in roadtest itself
is replaced, and nothing pretends to be the OpenAI API.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from textwrap import dedent
from types import ModuleType
from typing import Any, Callable, Iterator

from roadtest.config import Config, load_config
from roadtest.runner import RunOptions, run_scenarios
from roadtest.scenarios import load_scenarios, select

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def load_example(name: str) -> ModuleType:
    """Import `examples/<name>/agent.py` as a module (the folder names are not importable)."""
    path = EXAMPLES / name / "agent.py"
    specification = importlib.util.spec_from_file_location(f"example_{name.replace('-', '_')}", path)
    assert specification and specification.loader
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


@contextmanager
def example_config(name: str, url: str | None = None) -> Iterator[Config]:
    """The example's own `roadtest.yaml`, pointed at its agent on a free port (or at `url`, when given)."""
    config = load_config(EXAMPLES / name / "evals" / "roadtest.yaml")
    server = None
    if config.agent.get("type", "http") == "http":
        if url is None:
            server, url = load_example(name).start(0)
        config.agent["url"] = url
    try:
        yield config
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()


def run_config(config: Config, *selectors: str, judge: bool = False, **options: Any) -> dict[str, Any]:
    """Run the config's scenarios (or only the selected ones) and return the run record."""
    scenarios = select(load_scenarios(config.scenarios_dir), list(selectors) or None)
    return asyncio.run(run_scenarios(config, scenarios, RunOptions(judge=judge, **options)))


def statuses(run: dict[str, Any]) -> dict[str, str]:
    return {scenario["id"]: scenario["status"] for scenario in run["scenarios"]}


def turn_of(run: dict[str, Any], scenario_id: str, number: int = 0, attempt: int = 0) -> dict[str, Any]:
    scenario = next(item for item in run["scenarios"] if item["id"] == scenario_id)
    return scenario["attempts"][attempt]["turns"][number]


def write_project(root: Path, scenarios: str, agent: str = "", adapter: str | None = None) -> Path:
    """A roadtest project under `root`: evals/roadtest.yaml, one scenario file, optionally an adapter.

    `agent` is the body of the `agent:` section. Without it the project uses a Python adapter.
    Returns the path of roadtest.yaml.
    """
    evals = root / "evals"
    (evals / "scenarios").mkdir(parents=True)
    if adapter is not None:
        (evals / "adapter.py").write_text(dedent(adapter), encoding="utf-8")
    section = dedent(agent) if agent else "type: python\nentry: adapter.py:call\n"
    indented = "".join(f"  {line}\n" if line.strip() else "\n" for line in section.splitlines())
    (evals / "roadtest.yaml").write_text(f"version: 1\nagent:\n{indented}", encoding="utf-8")
    (evals / "scenarios" / "s.yaml").write_text(dedent(scenarios), encoding="utf-8")
    return evals / "roadtest.yaml"


# A reply a scripted server sends: (status, content type, body, seconds to wait first).
ScriptedReply = tuple[int, str, str, float]


def json_reply(payload: Any, status: int = 200, delay: float = 0.0) -> ScriptedReply:
    return status, "application/json", json.dumps(payload), delay


@contextmanager
def scripted_server(script: Callable[[int, dict[str, Any]], ScriptedReply]) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """An HTTP server whose reply to each POST is decided by `script(request_number, json_body)`.

    Yields (url, the JSON bodies received so far).
    """
    received: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
            received.append(body)
            status, content_type, payload, delay = script(len(received), body)
            time.sleep(delay)
            data = payload.encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("content-type", content_type)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the client gave up (a timeout test)

        def log_message(self, *arguments: Any) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/chat", received
    finally:
        server.shutdown()
        server.server_close()


def unused_port() -> int:
    """A local port nothing is listening on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]
