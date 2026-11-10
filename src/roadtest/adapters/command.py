"""The command adapter: runs a program once per turn, with the turn as JSON on stdin."""

from __future__ import annotations

import asyncio
import json
import shlex

from ..errors import AgentError
from .base import Agent, Exchange, Reply, Session, shorten, to_reply
from .template import render

class CommandAgent(Agent):
    """Runs a command per turn. The turn goes in as JSON on stdin; the answer comes back on stdout."""

    def describe(self) -> dict[str, str]:
        run = self.spec.run
        return {"type": "command", "run": run if isinstance(run, str) else " ".join(run or [])}

    async def send(self, message: str, session: Session) -> Reply:
        variables = session.variables(message)
        run = self.spec.run
        arguments = [str(render(part, variables)) for part in (shlex.split(run) if isinstance(run, str) else list(run or []))]
        payload = json.dumps(variables, ensure_ascii=False)
        self.last_exchange = Exchange(request={"run": arguments, "stdin": variables})
        try:
            process = await asyncio.create_subprocess_exec(
                *arguments,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            raise AgentError(f"command not found: {arguments[0]}") from None
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(payload.encode("utf-8")), timeout=self.spec.timeout)
        except asyncio.TimeoutError:
            process.kill()
            raise AgentError(f"`{arguments[0]}` did not finish within {self.spec.timeout:g}s") from None
        output = stdout.decode("utf-8", errors="replace").strip()
        self.last_exchange.body = output
        if process.returncode != 0:
            detail = shorten(stderr.decode("utf-8", errors="replace").strip() or output, 300)
            raise AgentError(f"`{arguments[0]}` exited with code {process.returncode}: {detail}")
        if output.startswith("{"):
            try:
                reply = to_reply(json.loads(output), "the command's JSON output")
            except json.JSONDecodeError:
                reply = Reply(text=output, raw=output)
        else:
            reply = Reply(text=output, raw=output)
        if not reply.text:
            raise AgentError(f"`{arguments[0]}` printed nothing")
        if reply.session_id:
            session.id = reply.session_id
        return reply
