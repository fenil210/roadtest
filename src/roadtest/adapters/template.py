"""`{{name}}` templates for requests and dotted paths for reading responses."""

from __future__ import annotations

import json
import re
from typing import Any

from ..errors import ConfigError

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][\w.]*)\s*\}\}")
_PATH_TOKEN = re.compile(r"([^.\[\]]+)|\[(\*|-?\d+)\]")
_MISSING = object()


def render(template: Any, variables: dict[str, Any]) -> Any:
    """Fill `{{name}}` placeholders in a JSON-like template.

    A string that is exactly one placeholder keeps the variable's type, so `"{{messages}}"`
    becomes the list of messages. A placeholder inside a longer string is replaced with text.
    A dict key whose value is a lone placeholder that resolves to nothing is left out, so an id
    the server has not issued yet is omitted rather than sent as null.
    """
    if isinstance(template, dict):
        rendered = {}
        for key, value in template.items():
            result = render(value, variables)
            if result is None and isinstance(value, str) and _PLACEHOLDER.fullmatch(value.strip()):
                continue
            rendered[key] = result
        return rendered
    if isinstance(template, list):
        return [render(item, variables) for item in template]
    if not isinstance(template, str):
        return template
    whole = _PLACEHOLDER.fullmatch(template.strip())
    if whole:
        return _variable(whole.group(1), variables)
    return _PLACEHOLDER.sub(lambda match: _as_text(_variable(match.group(1), variables)), template)


def placeholders(template: Any) -> set[str]:
    """Every placeholder name used anywhere in a template."""
    if isinstance(template, dict):
        return set().union(*(placeholders(value) for value in template.values())) if template else set()
    if isinstance(template, list):
        return set().union(*(placeholders(item) for item in template)) if template else set()
    if isinstance(template, str):
        return set(_PLACEHOLDER.findall(template))
    return set()


def _variable(name: str, variables: dict[str, Any]) -> Any:
    current: Any = variables
    for part in name.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            known = ", ".join(sorted(variables))
            raise ConfigError(f"unknown placeholder {{{{{name}}}}}. Available: {known}")
    return current


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def parse_path(path: str) -> list[str | int]:
    """Turn `choices[0].message.content` or `steps[*].tool` into a list of steps."""
    text = path.strip()
    if text.startswith("$"):
        text = text[1:].lstrip(".")
    steps: list[str | int] = []
    position = 0
    while position < len(text):
        if text[position] == ".":
            position += 1
            continue
        match = _PATH_TOKEN.match(text, position)
        if not match:
            raise ConfigError(f"cannot read the path `{path}` near `{text[position:]}`")
        key, index = match.groups()
        if key is not None:
            steps.append(key)
        elif index == "*":
            steps.append("*")
        else:
            steps.append(int(index))
        position = match.end()
    if not steps:
        raise ConfigError("a response path cannot be empty")
    return steps


def extract(data: Any, path: str) -> Any:
    """Read `path` from parsed JSON. Returns None when it is absent.

    A path with `[*]` returns a list of every match (possibly empty).
    """
    steps = parse_path(path)
    nodes = [data]
    for step in steps:
        found = []
        for node in nodes:
            if step == "*":
                if isinstance(node, list):
                    found.extend(node)
                elif isinstance(node, dict):
                    found.extend(node.values())
            elif isinstance(step, int):
                if isinstance(node, list) and -len(node) <= step < len(node):
                    found.append(node[step])
            elif isinstance(node, dict) and step in node:
                found.append(node[step])
        nodes = found
    if "*" in steps:
        return nodes
    return nodes[0] if nodes else None
