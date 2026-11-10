# CI

Run roadtest on pull requests, after deploys and on a schedule.

## What to run where

| When | Command | Why |
| --- | --- | --- |
| Every pull request | `roadtest run` | Blocks a merge that breaks a gate scenario. |
| After a deploy | `roadtest run --tag core --target prod` | A short check that production behaves. |
| Nightly | `roadtest run --repeat 3` | Finds flaky behaviour that one run misses. |

`roadtest run` exits `0` when every gate scenario passed, `1` when one failed or errored, and `2`
when the config or scenarios are invalid. A CI step needs nothing more than the command.

## GitHub Actions: pull requests

This starts the agent inside the job and tests it. Adjust the start command and the health URL.

```yaml
# .github/workflows/evals.yml
name: evals
on:
  pull_request:
  workflow_dispatch:

jobs:
  roadtest:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r requirements.txt roadtest

      - name: Start the agent
        run: |
          uvicorn app.main:app --port 8000 &
          for i in $(seq 1 30); do curl -sf http://localhost:8000/health && break; sleep 1; done
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}

      - name: Run evals
        run: roadtest run --label "pr-${{ github.event.number }}"
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}

      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: roadtest-report
          path: evals/results/
```

The pass and fail summary appears on the job's summary page with no extra step, because roadtest
writes it to `GITHUB_STEP_SUMMARY` when that variable is set. The HTML report is in the uploaded
artifact.

To test a deployed preview or staging environment instead of starting the agent, drop the start
step and add `--target staging`.

## GitHub Actions: after a deploy and nightly

```yaml
# .github/workflows/evals-prod.yml
name: evals-prod
on:
  workflow_dispatch:
  schedule:
    - cron: "0 3 * * *"          # nightly; also call this workflow at the end of your deploy

jobs:
  roadtest:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install roadtest
      - run: roadtest run --tag core --target prod --repeat 3
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
          PROD_AGENT_API_KEY: ${{ secrets.PROD_AGENT_API_KEY }}
```

Keep the production set small and safe: scenarios tagged `core` that only read, and a dedicated
test account. Ask the user before adding any scenario that writes to production data.

## Secrets

Every `${NAME}` in `roadtest.yaml` must be set in the job's `env`. Add them as repository secrets.
`OPENAI_API_KEY` is needed for rubric lines and simulated users. Without it, `roadtest run --no-judge`
runs the code checks only.

## Regressions against a baseline

```bash
roadtest run
roadtest baseline         # writes evals/baseline.json; commit it
```

Later, in CI or locally:

```bash
roadtest run
roadtest compare          # exits 1 when a gate scenario that passed in the baseline no longer passes
```

## Other CI systems

Any system works: install the package, set the environment variables, run `roadtest run`, and keep
`evals/results/` as an artifact. `evals/results/<run id>/summary.md` is ready to post as a comment.
