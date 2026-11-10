# python-function

An agent with **no server at all**, connected through a small **Python adapter**.

The agent is the front desk of a made-up vet clinic, written as a plain class (`agent.py`). It
remembers the pet's species and name within a conversation, and says which tools it used. It is
rule-based, so it needs no model and no API key.

What this shows in `evals/roadtest.yaml` and `evals/adapter.py`:

- `type: python` with `entry: adapter.py:call`: roadtest imports `evals/adapter.py` and calls
  `call(message, session)` once per turn. The function may be sync or async
- the adapter returns `{"text": ..., "tool_calls": [{"name", "arguments"}, ...]}`
- `session.state` is a dict that belongs to one conversation. The adapter keeps the clinic desk in it,
  so every scenario starts with a fresh desk and its later turns reach the same one

`books-known-pet` proves memory within a conversation. `asks-about-the-pet` proves the opposite:
it only passes when nothing leaks from one conversation into the next.

## Run it

There is nothing to start. From this folder:

```bash
roadtest probe "Hi, I have a dog called Biscuit." "Can I see a vet tomorrow?"
roadtest validate
roadtest run --no-judge          # code checks only, no model calls
roadtest run                     # also grades the rubric line (needs OPENAI_API_KEY)
```
