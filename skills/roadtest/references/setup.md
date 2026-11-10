# Setup

Connect roadtest to an agent, so that `roadtest doctor` prints "Ready".

## Steps

1. **Find how the agent is called.** Search the repo for the route or function that takes a user
   message and returns the agent's answer. Write down:
   - the URL, method and auth header, or the Python function
   - the request body: which field holds the message, and how a conversation is identified
   - the response: which field holds the answer, whether it streams, and whether tool calls are in it
   If the agent's code is not in this repo, ask the user for the endpoint and an example request,
   then let `roadtest probe` show you the response shape.
2. **Pick an adapter.**
   - `http`: the agent has an HTTP endpoint. Prefer this, because it tests what users actually reach.
   - `python`: the agent is a function or object in this repo and there is no server, or the
     protocol is something else (WebSocket, gRPC, an SDK). You write a small adapter function.
   - `command`: the agent is a program in another language with no HTTP endpoint.
3. **Write `evals/roadtest.yaml`** using the reference below. Run `roadtest init` first if the file
   does not exist.
4. **Probe it:** `roadtest probe "a realistic first message"`. This prints the request that was
   sent, the raw reply and what roadtest read from it. Adjust the config until the answer is right.
5. **Check memory:** `roadtest probe "My name is Sam." "What is my name?"` sends both messages as
   one conversation. If the second reply does not know the name, the session wiring is wrong.
6. **Run `roadtest doctor`.** Fix every line marked ✗.

Tell the user which environment variables they need to set, and where (`.env` in the project root
or in `evals/`; both are loaded automatically and neither should be committed).

## roadtest.yaml

```yaml
version: 1

agent:
  type: http                      # http (default) | python | command
  url: http://localhost:8000/chat
  method: POST                    # default POST. With GET the body is sent as query parameters
  headers:
    Authorization: "Bearer ${AGENT_API_KEY}"
  body:                           # the JSON body. Default: {"message": "{{message}}"}
    message: "{{message}}"
    session_id: "{{session_id}}"
  response:
    format: auto                  # auto | json | text | sse | jsonl. auto goes by Content-Type
    text: reply                   # path to the answer
    tool_calls: "tool_calls[*].name"   # path to the tools that were called (optional)
    session_id: conversation.id   # path to an id the server issues (optional)
    join: concat                  # for streams: concat the pieces (default) or keep the last
  timeout: 120                    # seconds per turn
  retries: 2                      # on connection errors and HTTP 429, 502, 503, 504

targets:                          # optional: the same agent elsewhere
  staging:
    url: https://staging.example.com/chat
  prod:
    url: https://api.example.com/chat
    headers:
      Authorization: "Bearer ${PROD_AGENT_API_KEY}"

models:
  judge: gpt-6-luna               # decides pass or fail for each rubric line
  generator: gpt-6.1-terra        # plays the user in simulated conversations
  # base_url: https://api.openai.com/v1    # any OpenAI-compatible Chat Completions endpoint
  # api_key_env: OPENAI_API_KEY

run:
  concurrency: 4                  # scenarios in flight at once
  repeat: 1                       # runs per scenario; a scenario passes only if every run passes
```

A target overrides only the keys it sets and inherits the rest from `agent`. Select one with
`roadtest run --target staging`. Environment variables are read when a target is used, so a missing
production token does not break a local run.

A model can be a mapping when it needs tuning:

```yaml
models:
  judge:
    model: gpt-6-luna
    reasoning_effort: low         # sent as-is; leave it out for servers that reject it
    params: {}                    # extra fields for the request body
```

### Placeholders

Use these in `url`, `headers` and `body`.

| Placeholder | Value |
| --- | --- |
| `{{message}}` | the user message for this turn |
| `{{messages}}` | the whole conversation including this message, as `[{role, content}, ...]` |
| `{{history}}` | the earlier turns only |
| `{{session_id}}` | the conversation id (see Sessions) |
| `{{scenario_id}}` | the id of the scenario being run |
| `{{vars.name}}` | a value from the scenario's `vars:` |
| `${NAME}` | an environment variable. `${NAME:-fallback}` gives a default |

A value that is exactly one placeholder keeps its type: `messages: "{{messages}}"` sends a list.
A key whose placeholder is empty is left out of the body.

### Response paths

Paths are dotted, with `[0]` for an index and `[*]` for every item:
`reply`, `data.answer`, `choices[0].message.content`, `steps[*].tool`.

When `text` is not set, roadtest looks in the usual places (`choices[0].message.content`, `output`,
`answer`, `response`, `reply`, `text`, `message`, `content`, `result`). `roadtest probe` says where it
found the answer. Pin that path in `text` once you know it.

`tool_calls` may point at a list of names or a list of objects. For objects roadtest reads the name
from `name`, `tool`, `tool_name` or `function.name`, and the arguments from `arguments`, `args`,
`input` or `parameters`.

### Sessions

Scenarios with several turns need every turn to land in the same conversation. Pick the pattern the
agent's API uses:

- **The client chooses the id.** Put `{{session_id}}` in the body, a header or the URL. roadtest
  generates a fresh id for each scenario run.
- **The server issues the id.** Set `response.session_id` to its path. `{{session_id}}` is then empty
  on the first turn, so the key is left out, and carries the server's id from the second turn on.
- **The API is stateless.** Send the whole conversation every turn: `messages: "{{messages}}"`.

### Streaming

With `format: sse` (or `auto` and a `text/event-stream` reply) roadtest reads every `data:` event,
applies `text` to each one and joins the pieces. For newline-delimited JSON use `format: jsonl`.
If each event repeats the whole answer so far, set `join: last`.

## Recipes

**An OpenAI-compatible endpoint**

```yaml
agent:
  url: http://localhost:8000/v1/chat/completions
  headers:
    Authorization: "Bearer ${AGENT_API_KEY}"
  body:
    model: my-agent
    messages: "{{messages}}"
  response:
    text: choices[0].message.content
    tool_calls: "choices[0].message.tool_calls[*]"
```

**A streaming endpoint (server-sent events)**

```yaml
agent:
  url: http://localhost:8000/chat/stream
  body:
    message: "{{message}}"
    thread_id: "{{session_id}}"
  response:
    format: sse
    text: delta            # the field that holds each piece of text
    tool_calls: tool.name  # read from the events that have it
```

**A server-issued conversation id**

```yaml
agent:
  url: http://localhost:8000/conversations
  body:
    input: "{{message}}"
    conversation_id: "{{session_id}}"
  response:
    text: output
    session_id: conversation_id
```

**A Python adapter** for an agent with no HTTP endpoint, or a protocol roadtest does not speak:

```yaml
agent:
  type: python
  entry: adapter.py:call        # a file next to roadtest.yaml, or package.module:function
```

```python
# evals/adapter.py
from my_app.agent import build_agent

agent = build_agent()

def call(message, session):
    """Called once per turn. `session.id` is the same for every turn of a scenario."""
    result = agent.run(message, thread_id=session.id)
    return {
        "text": result.final_output,
        "tool_calls": [{"name": step.tool, "arguments": step.args} for step in result.steps],
    }
```

The function may be sync or async. It takes `(message)` or `(message, session)`. `session` has `id`,
`messages` (earlier turns), `vars` (from the scenario) and `state` (a dict that is yours to use, for
example to keep a per-conversation agent object). Return a string, or a dict with `text` and
optionally `tool_calls` and `session_id`. Leave `tool_calls` out when you cannot report them; return
an empty list when the agent called none.

**A command** in any language. Each turn runs the command once with JSON on stdin (`message`,
`messages`, `session_id`, `vars`). It prints the answer, or JSON with `text` and `tool_calls`.

```yaml
agent:
  type: command
  run: [node, agent.js]
```

## Tool calls

Scenarios can check which tools the agent called (`tools_called`, `tools_not_called`). That needs
the tool calls in the reply. If the endpoint does not return them, the usual options are:

- return them from the endpoint, perhaps only when a debug header or flag is set
- use a `python` adapter that calls the agent in-process and reads its steps

If neither is possible, leave `tool_calls` unset. Rubric and text checks still work, and scenarios
that use tool checks are reported as errors that say why.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| `could not connect to ...` | Start the agent, or fix `url`. |
| `HTTP 401` or `403` | The auth header is wrong or the variable is not set. Check `.env`. |
| `HTTP 422` or `400` | The body shape is wrong. Compare `body` with the route's request model. |
| `could not tell where the answer is` | Run `roadtest probe --raw` and set `response.text`. |
| `the stream had N events but no answer text` | Set `response.text` to the field inside each event. |
| The second probe message has no memory | Fix the session wiring (see Sessions). |
| `environment variable X is not set` | Add it to `.env` or export it. |
| `model ... was not found` | Change `models:` to a model the key can use. |
