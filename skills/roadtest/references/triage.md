# Triage

Work out why scenarios did not pass, and what to do about each one.

## Get the facts

```bash
roadtest report                       # the last run: which scenarios did not pass, and the failed checks
roadtest report --markdown            # the same as Markdown
roadtest compare                      # what changed since the baseline, or since the previous run
```

For the full detail read `evals/results/<run id>/run.json`. For each scenario it holds every
attempt, and for each turn the user message, the agent's reply, its tool calls and every check:

```text
scenarios[].id, .status (pass | fail | error | skip), .tier, .passed_attempts
scenarios[].attempts[].error                     an agent or model failure, when there was one
scenarios[].attempts[].turns[].user, .reply, .tool_calls, .seconds
scenarios[].attempts[].turns[].checks[]          name, kind (code | judge | custom), status, detail
scenarios[].attempts[].checks[]                  checks on the whole conversation
```

For a judge check, `detail` is the judge's reasoning and `criterion` is the rubric line. Read the
reply and the reasoning together before you decide anything.

## Decide what each failure is

| It is | How you can tell | What to do |
| --- | --- | --- |
| **A bug in the agent** | The reply really does break the expectation. | Report it to the user with the reply as evidence. Fix the agent only if they ask. |
| **A wrong scenario** | The reply is fine. The expectation was too strict, out of date, or assumed data that does not exist. | Fix the scenario, and say what was wrong with it. |
| **A judge mistake** | The reply meets the rubric line, and the judge's reasoning misreads it or adds a requirement. | Rewrite the line so it is specific and checkable. Move anything objective into a code check. |
| **Flaky** | It passes on some runs and fails on others. | Run it with `--repeat 5`. An agent that fails 1 time in 5 has a real bug that shows up 20% of the time. |
| **An error** (status `error`) | The agent could not be reached, timed out, or the judge call failed. Nothing was graded. | Fix the cause and run again. It says nothing about the agent's quality. |

Do not change a scenario until you have read the agent's reply. "The check failed" is not a reason
to edit the check.

## Confirm before you conclude

Run the scenario again before calling something a bug or a fix:

```bash
roadtest run refund-outside-window --repeat 3
```

A scenario passes only when every repeat passes, so three clean runs is real evidence and one is not.

## Report to the user

Group the failures by cause, most serious first. For each one give the scenario id, what the agent
did in one sentence, and what you recommend. Keep agent bugs separate from scenario and judge
fixes you have already made. Say plainly which fixes you have re-run and which you have not.

## After the agent changes

When the user changes a prompt, a tool or the model:

```bash
roadtest run            # run everything
roadtest compare        # regressions are scenarios that passed before and do not now
```

When a run is the new normal, pin it with `roadtest baseline` and commit `evals/baseline.json`.
