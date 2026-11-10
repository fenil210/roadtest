# command

An agent that is **a program**, connected as a **command**. It could be written in any language.

The agent is the counter assistant of a made-up tool hire shop. roadtest starts the program once for
every turn, writes the turn to its stdin as JSON, and reads the answer from its stdout. The program
keeps nothing between turns, so it works out which tool the customer means from the earlier turns
that roadtest sends. It is rule-based, so it needs no model and no API key.

What this shows in `evals/roadtest.yaml`:

- `type: command` with `run: [python3, agent.py]`. The command runs in the folder where you start
  roadtest, so start it from this folder
- stdin carries `message`, `messages`, `history`, `session_id`, `scenario_id` and `vars`
- stdout is JSON with `text` and `tool_calls` (a list of `{"name", "arguments"}`). A program that only
  prints the answer as plain text works too, but then roadtest cannot see its tool calls

`reserve-remembers-tool` is the scenario that proves `history` arrives: the second turn says
"reserve it" and never names the tool.

## Run it

There is nothing to start. From this folder:

```bash
roadtest probe "Do you have a pressure washer?" "Great, reserve it for 2 days please."
roadtest validate
roadtest run --no-judge          # code checks only, no model calls
roadtest run                     # also grades the rubric line (needs OPENAI_API_KEY)
```
