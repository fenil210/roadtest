# http-streaming

A streaming agent (server-sent events) where **the server issues the conversation id**.

The agent gives train times for a made-up railway. It streams its answer a few words at a time,
sends an event for each tool it calls, and creates the conversation itself: the first request has
no id, the first event carries the new one, and later requests must send it back. An id the server
did not issue gets a 404. It is rule-based, so it needs no model and no API key.

What this shows in `evals/roadtest.yaml`:

- `response.format: sse` with `text: delta`: roadtest reads `delta` from every event and joins the pieces
- `response.tool_calls: tool`: tool calls are read from the events that have one
- `response.session_id: conversation_id`: `{{session_id}}` is empty on the first turn, so the key is
  left out of the body, and it carries the server's id from the second turn on

`route-remembered` is the scenario that proves the id is carried over: the route is given in the
first turn and the second turn only asks "Is it on time?".

## Run it

```bash
python3 agent.py                 # http://127.0.0.1:18802/chat/stream  (--port to change it)
```

In another terminal, from this folder:

```bash
roadtest probe --raw "I'm going from Leeds to York." "When is the next one?"
roadtest validate
roadtest run --no-judge          # code checks only, no model calls
roadtest run                     # also grades the rubric line (needs OPENAI_API_KEY)
```
