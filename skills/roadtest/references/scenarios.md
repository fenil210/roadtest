# Scenarios

Write scenarios that catch real failures in the user's agent, and that the user can read and trust.

## Steps

1. **Learn the agent.** Read its system prompt, tool definitions and any product docs in the repo.
   List what it is meant to do, what it must never do, and which tool belongs to which request.
   If the agent's code is not in this repo, ask the user what it does, which tools it has and for
   a few real conversations, and explore it with `roadtest probe "..."` before writing anything.
2. **Agree on scope.** If the user gave a number or an area, use it. If not, write about 20 to
   start: enough to cover the main behaviours, small enough to read. Say what you plan to cover in
   two or three lines before writing.
3. **Write the files** in `evals/scenarios/`, one file per area (`refunds.yaml`, `safety.yaml`).
4. **Validate:** `roadtest validate`. Fix every problem it lists.
5. **Run them:** `roadtest run`. For each scenario that does not pass, decide whether the agent is
   wrong or the scenario is wrong (see `roadtest guide triage`). Fix wrong scenarios. Do not edit a
   correct scenario to make it pass.
6. **Report to the user:** how many scenarios, what they cover, which ones fail and why, and which
   failures look like real bugs in the agent.

## Format

```yaml
# evals/scenarios/refunds.yaml
tags: [refunds]            # optional: added to every scenario in this file

scenarios:
  - id: refund-outside-window          # unique, lowercase, used on the command line
    title: A refund outside the 30-day window is declined
    why: refunding outside the policy window costs money on every late request
    tags: [core]
    tier: gate                          # gate (default) or watch
    user: Please refund order A-1002.
    expect:
      tools_called: [lookup_order]
      tools_not_called: [start_refund]
      contains_any: ["30 days", "30-day"]
      rubric:
        - Declines the refund and explains that the order is outside the 30-day window.
```

`title` says what should happen. `why` says what goes wrong for the user or the business if it does
not. Both are optional and both make failures much easier to act on.

### Several turns

```yaml
  - id: refund-remembers-order
    title: The order number from an earlier turn is reused
    turns:
      - user: Hi, I have a question about order A-1001.
      - user: Can I get a refund for it?
        expect:
          tools_called: [start_refund]
          contains: "A-1001"
```

Each turn may have its own `expect`. A scenario-level `expect` (next to `turns`) is checked once
against the whole conversation: text checks look at the last reply, tool checks look at every turn.

### A simulated user

Use this when the conversation depends on what the agent says, so the turns cannot be scripted.
The generator model plays the user.

```yaml
  - id: impatient-customer-refund
    title: A terse customer still gets a correct refund
    simulate:
      persona: An impatient customer who writes very short messages and gives details only when asked.
      goal: Get a refund for order A-1001 because the helmet does not fit.
      opening: my helmet doesnt fit      # optional first message
      max_turns: 5                        # default 6
    expect:
      tools_called: [start_refund]
      rubric:
        - The agent asked for the order number before it started the refund.
```

Simulated scenarios cost more and vary more between runs. Keep them to a few, for flows that
really need them.

## What `expect` can check

Every entry is its own check, and all of them must pass.

| Key | Passes when |
| --- | --- |
| `contains` | the reply contains every string (case is ignored) |
| `contains_any` | the reply contains at least one of the strings |
| `not_contains` | the reply contains none of the strings |
| `matches` | every regular expression matches somewhere in the reply |
| `tools_called` | every listed tool was called |
| `tools_any` | at least one listed tool was called |
| `tools_not_called` | none of the listed tools was called. `["*"]` means no tools at all |
| `max_seconds` | the reply arrived within this many seconds |
| `rubric` | a judge model agrees with every line |
| `checks` | every custom Python check passes (see below) |

Tool names accept `*` patterns, such as `search_*`. Tool checks need roadtest to see the agent's
tool calls; `roadtest doctor` says whether it can.

### Choosing checks

- Use a **code check** for anything that is simply true or false: a tool was called, an order
  number appears, a forbidden phrase is absent. Code checks are free, instant and never wrong.
- Use a **rubric** line for meaning: the reply declines, it asks for the missing detail, it is in
  Spanish, it does not blame the user.
- Most good scenarios have one or two code checks and one or two rubric lines.

### Writing rubric lines

The judge sees the conversation, the reply, the tools called, and one line. It answers pass or fail.

- Write something an outsider could verify from the transcript alone:
  "Asks for the order number, and does not claim a refund was started."
- One behaviour per line. Two short lines are better than one line joined with "and".
- Say what must be there or must not be there. Avoid "is helpful", "is accurate", "is good".
- Put facts the judge needs into the line: "States that standard delivery takes 3 to 5 business days."
- Do not require exact wording unless the wording itself is the requirement.

### Custom checks

For something the keys above cannot express, point at a Python function:

```yaml
    expect:
      checks: ["checks.py:total_matches_items"]     # a file next to roadtest.yaml
```

```python
# evals/checks.py
def total_matches_items(reply):
    """`reply` has text, tool_calls, tool_names, seconds, messages, scenario_id and vars."""
    if "€89.00" not in reply.text:
        return "the total should be €89.00"      # a string is a failure with a reason
    return True
```

## Coverage

Spread a first set of about 20 across these. Adjust to what the agent actually does.

| Area | Share | Examples |
| --- | --- | --- |
| Main jobs | 40% | each thing the agent exists to do, asked the way real users ask |
| Tool choice | 15% | the right tool for a request, and no tool when none is needed |
| Missing information | 10% | the agent asks for the detail it needs, and does not invent it |
| Conversations | 10% | a follow-up that depends on an earlier turn, a correction, a change of mind |
| Awkward input | 10% | typos, another language, a very short message, two requests at once |
| Out of scope and safety | 15% | requests it should decline, prompt injection, asking for its instructions or other users' data |

Also add one scenario for every bug the user mentions or that you find in the issue tracker or
commit history. A bug that has happened once is the likeliest one to happen again.

Tag the 5 to 10 scenarios that matter most with `core`. They are what runs against production
after a deploy (`roadtest run --tag core --target prod`).

## Tiers

- `gate` (the default): the agent should pass this today, and a failure should block a release.
- `watch`: a known gap or a goal. It is run and reported but does not fail the run.

After the first run, some scenarios will fail for good reasons. Ask the user which to fix now and
which to mark `tier: watch`. Do not mark a scenario `watch` only because it fails.

## Quality bar

Check each scenario against this before you finish.

- It would fail for a specific, realistic bug. You can say which one.
- It uses data that exists where the agent runs: real order numbers, documents, accounts. Ask the
  user for test data when you cannot find any.
- It does not depend on another scenario. Each one starts a fresh conversation.
- The user message sounds like a user, not like a test: "wheres my order" and not
  "Please retrieve the status of my order using the lookup tool".
- It does not give the answer away in the question.
- It does not change anything that later runs depend on, such as account settings. If it must,
  say so in a comment and tell the user.

## Common mistakes

- **Only happy paths.** A set where everything passes on day one is probably not looking hard enough.
- **Exact-match answers.** Agents reword. Check for the facts, not the sentence.
- **Checking the tool but not the answer**, or the answer but not the tool.
- **One huge scenario** that tests five things. Split it, so a failure points at one thing.
- **Rubric lines that restate the question**: "Answers the user's question" passes almost anything.
