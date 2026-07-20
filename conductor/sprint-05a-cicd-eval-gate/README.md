# Lab 5a — CI/CD for Agent Evals

## What I wanted to test

Whether a pass-rate eval gate can catch prompt regressions that unit tests miss - a change that drops the pass rate below threshold is blocked while a change that maintains or improves quality passes - and whether the gate itself avoids false negatives (passing when a threshold is breached).

## Why this matters

Conductor's behavior lives in two places: code and prompts. Unit tests cover the code side well. But a prompt rewrite that silently narrows troubleshooting scope, or a model bump that changes how Conductor handles ambiguous questions, won't show up in any unit test. Without an eval gate, every prompt change is ship and hope. This lab wires the eval dataset from Lab 1 and the harness from Lab 5 into a pipeline that blocks regressions automatically.

## What I'm Building

- **`scripts/check_eval_gate.py`** - reads an eval report JSON, checks overall pass rate >= threshold AND each required category independently, exits 0/1. Runs standalone without GitHub Actions. Serves Troubleshooting + Q&A modes where nuanced behavior is hardest to test deterministically.
- **`scripts/regression_check.py`** - compares current report to a stored baseline JSON, fires if drop exceeds `max_regression`, prints the specific cases that regressed. Serves all modes.
- **`scripts/run_eval_sample.py`** - runs a 10-20 case subset from the Lab 5 eval dataset and produces the report JSON that the gate scripts consume. Serves all modes.
- **`scripts/install_hooks.sh`** - installs a `pre-commit` git hook that runs `pytest tests/` before every commit. Plain git hook, no framework. Fast, deterministic, no LLM calls.
- **`.github/workflows/agent-eval.yml`** - tiered pipeline (unit tests every commit, eval sample on PRs, full suite on merge to main) that calls the scripts above. Workflow dispatched manually only - scripts are the primary interface and testable without GitHub.

## Design principle

Scripts are the primary interface. The workflow calls the same scripts. If a gate can't be validated locally, it doesn't belong in this lab.

## Success Criteria

1. `check_eval_gate.py` exits 1 when overall pass rate < threshold
2. `check_eval_gate.py` exits 1 when any required category < its per-category threshold, even if overall passes (the masking case)
3. `check_eval_gate.py` exits 0 when all thresholds are met
4. `regression_check.py` exits 1 when pass rate drops > `max_regression`
5. `regression_check.py` exits 0 when drop is exactly at `max_regression` (boundary: 89% → 86% with `max_regression=0.03` passes)
6. `regression_check.py` names the specific cases that regressed in its output
7. Intentional prompt break (empty troubleshooting prompt) → gate catches it, exits 1
8. Pre-commit hook blocks a commit when unit tests fail; passes when they pass

## What Failed

- Float precision in `regression_check.py` boundary condition (caught by unit test, fixed before running)
- `install_hooks.sh` missing `mkdir -p` for hooks directory (caught during manual inspection, fixed immediately)

## What I Learned

- Write a test for every documented boundary condition. Float arithmetic does not behave the way your mental model expects at exact values.
- Scripts-first design makes CI gates locally debuggable. When a gate can't be run without GitHub Actions, every debug cycle costs a CI push.
- Per-category thresholds are not optional polish — they are the only protection against aggregate masking. A single number can hide a critical category failure.
- The regression checker's value is in naming cases, not reporting deltas. A named case is actionable; a percentage drop is not.

## Metrics
| Metric | Target | Actual |
|--------|--------|--------|
| Gate script: fail on threshold breach | exits 1 | exits 1 |
| Gate script: pass when all thresholds met | exits 0 | exits 0 |
| Boundary case (exactly at max_regression) | exits 0 | exits 0 |
| Category masking case | exits 1 | exits 1 |
| Regression checker: names regressed cases | printed to stdout | 3 cases named with inputs |
| Pre-commit hook: blocks failing tests | commit rejected | fires, 17 passed in 0.02s |
| All tests deterministic (no LLM calls) | < 1s | 0.02s |
| Gate script: fail on threshold breach | exits 1 | |
| Gate script: pass when all thresholds met | exits 0 | |
| Boundary case (exactly at max_regression) | exits 0 | |
| Category masking case | exits 1 | |
| Regression checker: names regressed cases | printed to stdout | |
| Pre-commit hook blocks failing tests | commit rejected | |
| Full eval sample run time | < 5 min | |

## Failure Indicators

- Gate exits 0 when a threshold is breached (false negative - the gate is broken)
- Regression checker fires on a pass rate improvement
- Scripts require GitHub Actions to run - must work standalone

## Out of Scope

- Live GitHub Actions triggered run (workflow is scaffolded but not exercised via actual CI)
- Remote baseline storage (S3/GCS) - local JSON file only
- Full 80-case suite in CI - eval sample only (10-20 cases)
- `pre-commit` Python framework or husky - plain `.git/hooks/pre-commit` only

## Evidence to Collect

- Terminal output: gate failing on intentional prompt break (exit 1, reason printed)
- Terminal output: gate passing on clean eval (exit 0)
- Terminal output: regression checker firing on > 3% drop, listing regressed cases
- Terminal output: regression checker passing at exactly the 3% boundary
- Terminal output: category-level failure when overall passes (masking scenario)
- `results/` - JSON fixtures for fail, pass, regression, and boundary cases

## How to Run

### Install dependencies (shared venv)

```bash
cd conductor/sprint-05a-cicd-eval-gate
UV_PROJECT_ENVIRONMENT=../.venv uv sync --extra dev
```

### Install the pre-commit hook

```bash
bash scripts/install_hooks.sh
```

### Run unit tests

```bash
UV_PROJECT_ENVIRONMENT=../.venv uv run pytest tests/ -v
```

### Run the eval gate against a report

```bash
# Check a report against an 85% overall threshold, 80% for troubleshooting
UV_PROJECT_ENVIRONMENT=../.venv uv run python scripts/check_eval_gate.py \
  --report results/fixture-pass.json \
  --threshold 0.85 \
  --categories troubleshooting:0.80 knowledge_qa:0.80

# Trigger a fail (use the all-fail fixture)
UV_PROJECT_ENVIRONMENT=../.venv uv run python scripts/check_eval_gate.py \
  --report results/fixture-fail.json \
  --threshold 0.85
```

### Run the regression checker

```bash
# Save a baseline
UV_PROJECT_ENVIRONMENT=../.venv uv run python scripts/regression_check.py \
  --current results/fixture-baseline.json \
  --save-baseline results/my-baseline.json

# Check for regression (will fire — regression fixture drops ~8.6%)
UV_PROJECT_ENVIRONMENT=../.venv uv run python scripts/regression_check.py \
  --current results/fixture-regression.json \
  --baseline results/my-baseline.json \
  --max-regression 0.03

# Check boundary case (89% → 86%, max_regression=0.03 — passes)
UV_PROJECT_ENVIRONMENT=../.venv uv run python scripts/regression_check.py \
  --current results/fixture-pass.json \
  --baseline results/fixture-baseline.json \
  --max-regression 0.03
```

### Run an eval sample (requires Lab 5 harness + .env with credentials)

```bash
cp ../../.env .env  # or set LLM_GATEWAY_URL + ANTHROPIC_API_KEY

UV_PROJECT_ENVIRONMENT=../.venv uv run python scripts/run_eval_sample.py \
  --dataset ../../evals/datasets/conductor-v2.yaml \
  --sample 15 \
  --output results/sample-run.json \
  --memory-provider inmemory
```

### Required environment variables

See `.env.example` in the repo root. Key variables:
- `LLM_GATEWAY_URL` — gateway base URL
- `ANTHROPIC_API_KEY` — gateway API key

## What Actually Happened

The gate scripts and regression checker worked exactly as designed. All 6 evidence scenarios produced the correct exit codes. Two bugs surfaced during build and manual inspection — both caught before any CI run:

1. Float precision: `0.89 - 0.86 = -0.030000000000000002` caused the boundary case to fire incorrectly. Fixed with an epsilon tolerance.
2. `install_hooks.sh` assumed `.git/hooks/` existed. It doesn't on this repo. Fixed with `mkdir -p`.

The most useful output from the lab was Evidence 3 — the category masking demo. Running the gate against a report where overall is 91.4% but one category is 70% makes the design argument for per-category thresholds immediately visible in a way no explanation can.

## Next lab

Lab 6 — Claude Agent SDK, HITL patterns, and skills.
