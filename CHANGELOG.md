# Changelog

## 0.1.1

- Clearer wording in the README, the command-line help and the files `roadtest init` writes.
- The release workflow stops with a clear message when the release tag and the package version differ.

## 0.1.0

First release.

- Adapters for HTTP endpoints (JSON, plain text, server-sent events, JSON lines), Python functions and commands.
- Scenarios in YAML: single turn, several turns, or a simulated user.
- Code checks, rubric lines graded by a judge model, and custom Python checks.
- `gate` and `watch` tiers, repeated runs, named targets.
- Terminal, Markdown and HTML reports; run comparison and a committed baseline.
- A skill for coding assistants, installed by `roadtest init`, by `npx skills add`, or as a Claude Code plugin.
- `roadtest demo`: a full run against a bundled agent with no setup.
