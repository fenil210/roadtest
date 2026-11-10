# http-json

A JSON HTTP agent where **the client chooses the conversation id**.

The agent is the front desk of a made-up library. It answers questions about opening hours, the
catalogue and a member's loans, and it remembers the library card number within a conversation.
It is rule-based, so it needs no model and no API key.

What this shows in `evals/roadtest.yaml`:

- `{{session_id}}` in the request body, so every turn of a scenario lands in one conversation
- `response.text` as a nested path (`data.answer`)
- `response.tool_calls` pointing at a list of objects (`data.steps[*]`), where roadtest reads the
  tool name from `tool` and the arguments from `input`

`renew-remembers-card` is the scenario that proves memory: the card number is given in the first
turn and only used in the second.

## Run it

```bash
python3 agent.py                 # http://127.0.0.1:18801/chat  (--port to change it)
```

In another terminal, from this folder:

```bash
roadtest probe "Hi, my card number is M-2041." "What do I have on loan?"
roadtest validate
roadtest run --no-judge          # code checks only, no model calls
roadtest run                     # also grades the rubric lines (needs OPENAI_API_KEY)
```
