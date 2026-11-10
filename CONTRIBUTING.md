# Contributing

Thanks for helping. Issues and pull requests are both welcome.

## Set up

```bash
git clone https://github.com/fenil210/roadtest
cd roadtest
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
roadtest demo
```

## Where things are

| Path | What it holds |
| --- | --- |
| `src/roadtest/agents.py` | The adapters: HTTP, Python function, command |
| `src/roadtest/scenarios.py`, `config.py` | The file formats and their validation |
| `src/roadtest/checks.py`, `judge.py`, `simulate.py` | Code checks, the judge, the simulated user |
| `src/roadtest/runner.py`, `results.py`, `report.py` | Running, saving, comparing and showing runs |
| `src/roadtest/demo/` | The bundled demo agent and its scenarios |
| `skills/roadtest/` | The skill for coding assistants. Its `references/` pages are also what `roadtest guide` prints |
| `examples/` | Small runnable projects, one per way of connecting an agent |

## Ground rules

- **Keep the surface small.** A new option needs a use case that the existing ones cannot serve.
  If a custom check or a Python adapter already covers it, document that instead.
- **Error messages are part of the product.** Say what is wrong, where, and what to do next.
  People and coding assistants both act on them.
- **Tests exercise real behaviour.** Run the real runner against a real local agent (the demo agent
  or one from `examples/`). Do not mock roadtest's own modules, and do not fake the model API.
  `tests/test_live_models.py` makes real model calls and runs only when `OPENAI_API_KEY` is set.
- **When behaviour changes, change the guide.** `skills/roadtest/references/` is the documentation
  that coding assistants follow, so it must match the code.

## Releasing

Set `__version__` in `src/roadtest/__init__.py` (the package reads its version from there) and
`version` in `.claude-plugin/plugin.json`, add a `CHANGELOG.md` entry, push, then publish a GitHub
release tagged `v` plus that version, for example `v0.1.1`. The `release` workflow checks that the
tag matches, builds the package and uploads it to PyPI.
