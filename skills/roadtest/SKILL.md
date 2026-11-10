---
name: roadtest
description: Set up and run evals for an AI agent with roadtest. Use when the user asks to set up roadtest, connect an agent or endpoint for evals, write, generate or add eval scenarios or test cases for an agent, run agent evals, explain or fix failing scenarios, compare eval runs, or add agent evals to CI. Also use when the repo has evals/roadtest.yaml and the user changes the agent's prompt, tools or model and wants to check nothing broke.
---

# roadtest

roadtest runs scenario-based evals against an AI agent. A scenario is a message (or a whole
conversation) plus a description of what a good reply looks like. roadtest sends it to the real
agent, checks the reply with code checks and a judge model, and exits non-zero when a must-pass
scenario fails.

You do four jobs for the user. Read the page for the job before you start it.

| Job | When | Read |
| --- | --- | --- |
| Connect the agent | there is no `evals/roadtest.yaml`, or `roadtest doctor` fails | `references/setup.md` |
| Write scenarios | the user asks for scenarios, test cases or evals | `references/scenarios.md` |
| Triage a run | scenarios failed and the user asks why, or asks you to fix them | `references/triage.md` |
| Add it to CI | the user wants evals on pull requests or after deploys | `references/ci.md` |

The same pages print with `roadtest guide setup`, `roadtest guide scenarios`, `roadtest guide triage`
and `roadtest guide ci`. Use that when the files are not next to this one.

## If roadtest is not installed

```bash
pip install roadtest        # or: uv add --dev roadtest
roadtest init               # creates evals/roadtest.yaml, a starter scenario file and this skill
```

## Rules

- **Let the CLI check your work.** After editing scenarios run `roadtest validate`. After editing
  `roadtest.yaml` run `roadtest doctor`. Fix what they report before you do anything else.
- **Do not guess how the agent behaves.** Read its code first: the system prompt, the tool
  definitions and the route or function that serves it. Ask the user about anything you cannot find.
- **Keep secrets out of files.** Write `${AGENT_API_KEY}` in `roadtest.yaml` and tell the user which
  variables to put in `.env`. Never paste a key or token into a file or a command.
- **Do not weaken a scenario to make it pass.** A failing scenario means the agent is wrong (report
  it) or the scenario is wrong (fix it and say what was wrong with it).
- **Running costs money and time.** A run calls the user's agent, and rubric lines call the OpenAI
  API. A handful of scenarios is fine. Before a run of more than about 50 scenarios, or any run
  against a production target, say what it will do and wait for a go-ahead.

## Commands

```bash
roadtest doctor                    # is everything connected?
roadtest probe "a message"         # one message: the request, the raw reply, what roadtest read
roadtest validate                  # are the scenario files valid? lists them
roadtest run                       # run everything
roadtest run refund-* --repeat 3   # some scenarios, three times each
roadtest run --tag core --target staging
roadtest run --no-judge            # code checks only, no model calls
roadtest report                    # show the last run again
roadtest compare                   # what changed since the baseline (or the previous run)
roadtest baseline                  # pin the last run as the baseline
```

Exit codes: `0` every gate scenario passed, `1` a gate scenario failed or errored, `2` the config or
scenario files are invalid.

Results are written to `evals/results/<run id>/`: `run.json` (everything, including each reply and
each judge's reasoning), `summary.md` and `report.html`.
