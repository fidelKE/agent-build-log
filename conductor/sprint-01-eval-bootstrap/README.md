# Sprint 0 - Eval Bootstrap

## What I Wanted to Test

Whether I can define Conductor's correct behavior across all four capability modes
before writing any code, and use that definition as a regression detector from day one.

## Why This Matters

Without a baseline, there's no way to tell if a change made Conductor better or worse.
The eval dataset is the quality instrument - built before the agent exists so every
experiment from Sprint 1 onward has something to measure against.

## Hypothesis

If I define Conductor's correct behavior across all four capability modes before writing
any code, I'll have a regression detector from day one that catches quality drops across
the full agent surface - not just the happy path.

## What I'm Building

- 40 YAML eval cases covering Conductor's four modes:
  - **Setup** - guided integration configuration steps
  - **Onboarding** - first-run experience for new users
  - **Troubleshooting** - diagnosing and resolving failures
  - **Knowledge Q&A** - "how do I..." questions from a knowledge base
- Case distribution: 16 regular easy · 12 regular medium · 3 hard/edge · 9 adversarial (1 per attack category)
- 9 adversarial categories, one case each: prompt injection via log/config, credential fishing,
  sycophancy/position abandonment, context leakage between users, scope creep escalation,
  specification gaming, authority spoofing, SSRF via connector config, infinite ambiguity loop,
  cross-mode confusion
- A review script that loads the YAML and presents each case for SME approval

## Success Criteria

1. 40 cases generated covering all four Conductor capability modes
2. At least 32 cases (80%) pass SME review
3. All 9 adversarial categories represented (1 case each)
4. Baseline pass rate measured on a stub agent (expect 10–30%)
5. Each case has: `input`, `expected_output`, `mode`, `difficulty`, `adversarial`, `adversarial_category`, `rationale`

## Failure Indicators

- Generated cases cluster in one mode only (e.g., all Q&A, no Troubleshooting)
- Ground truth is vague ("the agent should help the user") rather than specific and verifiable
- Approval rate below 80% - dataset too noisy to trust
- Any of the 9 adversarial categories is missing from the dataset

## Out of Scope

- Building Conductor itself (Sprint 1)
- Connecting to any real data source or knowledge base
- Evaluating actual agent responses (no agent exists yet)

## Evidence to Collect

- `conductor/evals/datasets/conductor-v1.yaml` - the approved eval dataset
- SME review notes (one line per rejected case explaining why)
- Baseline eval report against a stub agent

---

## What Actually Happened

Generated 40 eval cases covering all four Conductor modes with 9 adversarial categories.
All 20 structural tests pass. Zero-line established at 2.5% on a stub agent with a strict
keyword-match evaluator. Two bugs found and fixed during the process (see results.md).

SME review complete. 39/40 cases approved (97.5%). One case rejected: `setup-easy-004` (multi-connector setup - each connector has different guidance). Approved dataset locked at `evals/datasets/conductor-v1-approved.yaml`.

## What Failed

1. **17 YAML items parsed as dicts** due to unquoted colons in list items.
   Caught by the test suite. Fixed by script rewrite (lost formatting as a side effect).
2. **Baseline below expected range** (2.5% vs 10–30%) - keyword evaluator is strict.
   Not a bug; reflects the zero-line accurately for this evaluator.

## What I Learned

- YAML `- key: value` items are mappings, not strings. Any expected output containing `: ` must be quoted.
- The "establish context before acting" pattern applies to ~8 cases - this is a core Conductor behavior worth testing explicitly across all modes.
- A 2.5% zero-line with a strict evaluator is more informative than 20% with a lenient one.
  The evaluator calibration matters as much as the dataset itself.
- Adversarial cases are easy to write but hard to make specific enough - vague must_not_contain
  entries would make them useless as quality gates.

## How to Run

All sprints share the `.venv` at `conductor/.venv`. Run all commands from the sprint root.

**Install dependencies** (first time only):

```bash
cd conductor/sprint-01-eval-bootstrap
UV_PROJECT_ENVIRONMENT=../.venv uv sync --extra dev
```

**Dataset paths:**
- Draft dataset: `evals/datasets/conductor-v1.yaml` (sprint-local copy)
- Approved dataset: `../evals/datasets/conductor-v1-approved.yaml` (shared `conductor/evals/`)

**Run the structural tests** (validates the eval dataset — no API key needed):

```bash
UV_PROJECT_ENVIRONMENT=../.venv uv run pytest src/test_sprint_00.py -v
```

**Run the SME review script** (interactive — step through each case and approve/reject):

```bash
UV_PROJECT_ENVIRONMENT=../.venv uv run python src/review.py --dataset evals/datasets/conductor-v1.yaml
```

Resume a previous session (picks up where you left off):

```bash
UV_PROJECT_ENVIRONMENT=../.venv uv run python src/review.py --dataset evals/datasets/conductor-v1.yaml --resume
```

The script saves progress to `review_state.json` after each decision. On exit it writes:
- `../evals/datasets/conductor-v1-approved.yaml` — locked approved dataset (shared location)
- `review_notes.txt` — one-line rejection reason per case

**Run the baseline eval** (measures zero-line on stub agent — no API key needed):

```bash
UV_PROJECT_ENVIRONMENT=../.venv uv run python src/baseline.py --dataset ../evals/datasets/conductor-v1-approved.yaml
```

Save the report as JSON:

```bash
UV_PROJECT_ENVIRONMENT=../.venv uv run python src/baseline.py \
  --dataset ../evals/datasets/conductor-v1-approved.yaml \
  --output ../evals/reports/baseline-stub.json
```

Run against the full unreviewed dataset (before SME review):

```bash
UV_PROJECT_ENVIRONMENT=../.venv uv run python src/baseline.py --dataset evals/datasets/conductor-v1.yaml --use-all
```

---

## Metrics

| Metric | Target | Actual |
|--------|--------|--------|
| Total cases generated | 40 | 40 ✓ |
| Cases passing SME review | ≥ 32 (80%) | 39/40 (97.5%) ✓ - 1 rejected (setup-easy-004) |
| Adversarial categories covered | 9 / 9 | 9 / 9 ✓ |
| Mode coverage (all 4 modes present) | Yes | Yes ✓ |
| Dataset tests passing | 20 / 20 | 20 / 20 ✓ |
| Baseline pass rate (stub agent) | 10–30% | 2.5% (strict keyword evaluator) |
