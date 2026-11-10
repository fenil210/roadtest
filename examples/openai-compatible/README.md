# openai-compatible

An agent behind an **OpenAI-style `/v1/chat/completions` endpoint**, which is **stateless**.

The agent takes table reservations for a made-up bistro. Like the OpenAI API, it keeps no
conversation between requests: every request carries the whole conversation in `messages`, and the
agent works out the party size and the day from the guest's earlier messages. It reports the tools it
used in `choices[0].message.tool_calls`. It is rule-based, so it needs no model and no API key.

What this shows in `evals/roadtest.yaml`:

- `messages: "{{messages}}"` in the body: roadtest sends the whole conversation, including the new
  message, as a list. There is no session id to manage
- `response.text: choices[0].message.content`
- `response.tool_calls: "choices[0].message.tool_calls[*]"`: tool calls come as `{"function": {"name",
  "arguments"}}` objects, and the arguments arrive as a JSON string that roadtest parses

`booking-uses-earlier-turns` is the scenario that proves the history is sent: the party size and the
day are given in the first turn and only the time is given in the second.

## Run it

```bash
python3 agent.py                 # http://127.0.0.1:18803/v1/chat/completions  (--port to change it)
```

In another terminal, from this folder:

```bash
roadtest probe "Do you have a table for 4 on Friday?" "Book the 8:30pm one please."
roadtest validate
roadtest run --no-judge          # code checks only, no model calls
roadtest run                     # also grades the rubric line (needs OPENAI_API_KEY)
```
