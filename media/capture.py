"""Record a command's real output, with timings and colours, for the terminal scenes.

    python media/capture.py demo "roadtest demo" -- python -m roadtest demo --out /tmp/x

writes media/captures/demo.js, which a scene loads with a <script> tag. The first argument is the
capture's name, the second is the command line to show on screen, and everything after `--` is the
command that is actually run.
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SGR = re.compile(r"\x1b\[([0-9;]*)m")
OTHER_ESCAPES = re.compile(r"\x1b\[[0-9;?]*[A-Za-ln-z]")
SPINNER = re.compile(r"^[\u2800-\u28ff]\s")
COLOURS = {31: "red", 32: "green", 33: "yellow", 34: "blue", 35: "magenta", 36: "cyan", 37: "white"}


def to_html(line: str, state: dict[str, object]) -> str:
    """One output line as HTML spans. `state` carries bold/dim/colour across escape codes."""
    pieces: list[str] = []
    position = 0

    def emit(text: str) -> None:
        if not text:
            return
        classes = [name for name in ("bold", "dim") if state.get(name)]
        if state.get("colour"):
            classes.append(str(state["colour"]))
        escaped = html.escape(text, quote=False)
        pieces.append(f'<span class="{" ".join(classes)}">{escaped}</span>' if classes else escaped)

    for match in SGR.finditer(line):
        emit(line[position : match.start()])
        position = match.end()
        for code in (int(part) if part else 0 for part in match.group(1).split(";")):
            if code == 0:
                state.clear()
            elif code == 1:
                state["bold"] = True
            elif code == 2:
                state["dim"] = True
            elif code == 22:
                state.pop("bold", None)
                state.pop("dim", None)
            elif code in COLOURS or code - 60 in COLOURS:
                state["colour"] = COLOURS.get(code) or COLOURS[code - 60]
            elif code == 39:
                state.pop("colour", None)
    emit(line[position:])
    return "".join(pieces)


def main() -> None:
    name, shown = sys.argv[1], sys.argv[2]
    command = sys.argv[sys.argv.index("--") + 1 :]
    environment = {
        **os.environ,
        "FORCE_COLOR": "1",
        "TERM": "xterm-256color",
        "COLUMNS": os.environ.get("COLUMNS", "96"),
        "PYTHONUNBUFFERED": "1",
    }
    environment.pop("COLORTERM", None)
    environment.pop("NO_COLOR", None)
    home = str(Path.home())
    started = time.perf_counter()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=environment, text=True)
    lines = []
    state: dict[str, object] = {}
    assert process.stdout is not None
    for raw in process.stdout:
        text = OTHER_ESCAPES.sub("", raw.rstrip("\n")).replace(home, "~")
        entry = {"t": round(time.perf_counter() - started, 3), "html": to_html(text, state)}
        if SPINNER.match(SGR.sub("", text)):
            entry["status"] = True  # a spinner frame: a real terminal overwrites it in place
        elif not text.strip() and lines and lines[-1].get("status"):
            continue  # what is left of an erased spinner line; a real terminal shows nothing here
        lines.append(entry)
    process.wait()
    capture = {"command": shown, "exit": process.returncode, "seconds": round(time.perf_counter() - started, 3), "lines": lines}
    target = Path(__file__).parent / "captures" / f"{name}.js"
    target.write_text(
        "window.CAPTURES = window.CAPTURES || {};\n"
        f"window.CAPTURES[{json.dumps(name)}] = {json.dumps(capture, ensure_ascii=False)};\n",
        encoding="utf-8",
    )
    print(f"{target}: {len(lines)} lines, {capture['seconds']}s, exit {process.returncode}")


if __name__ == "__main__":
    main()
