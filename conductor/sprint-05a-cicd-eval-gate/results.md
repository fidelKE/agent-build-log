# Lab 5a — Results

## Test Run

```
============================= test session starts ==============================
platform darwin -- Python 3.13.7, pytest-9.1.1, pluggy-1.6.0
collected 17 items

tests/test_eval_gate.py::TestCheckEvalGateOverall::test_passes_when_all_pass PASSED
tests/test_eval_gate.py::TestCheckEvalGateOverall::test_fails_when_all_fail PASSED
tests/test_eval_gate.py::TestCheckEvalGateOverall::test_fails_when_below_threshold PASSED
tests/test_eval_gate.py::TestCheckEvalGateOverall::test_fails_when_threshold_not_met PASSED
tests/test_eval_gate.py::TestCheckEvalGateOverall::test_empty_results_fails PASSED
tests/test_eval_gate.py::TestCheckEvalGateCategoryMasking::test_fails_when_category_below_threshold PASSED
tests/test_eval_gate.py::TestCheckEvalGateCategoryMasking::test_passes_when_all_categories_meet_threshold PASSED
tests/test_eval_gate.py::TestCheckEvalGateCategoryMasking::test_category_threshold_overrides_overall_for_that_category PASSED
tests/test_eval_gate.py::TestCheckEvalGateBoundary::test_exactly_at_threshold_passes PASSED
tests/test_eval_gate.py::TestCheckEvalGateBoundary::test_one_below_threshold_fails PASSED
tests/test_eval_gate.py::TestRegressionCheckPass::test_no_regression_when_same PASSED
tests/test_eval_gate.py::TestRegressionCheckPass::test_no_regression_when_improved PASSED
tests/test_eval_gate.py::TestRegressionCheckPass::test_passes_at_exact_boundary PASSED
tests/test_eval_gate.py::TestRegressionCheckFail::test_fires_when_drop_exceeds_max PASSED
tests/test_eval_gate.py::TestRegressionCheckFail::test_fires_just_above_boundary PASSED
tests/test_eval_gate.py::TestRegressionCaseDetail::test_identifies_regressed_cases PASSED
tests/test_eval_gate.py::TestRegressionCaseDetail::test_no_false_regressions_on_pass PASSED

============================== 17 passed in 0.02s ==============================
```

Passed: 17 / 17

## Evidence Artifacts

### Evidence 1 - Gate FAIL (intentional break — all outputs empty)
```
$ python scripts/check_eval_gate.py --report results/fixture-fail.json --threshold 0.85 --categories troubleshooting:0.80 knowledge_qa:0.80
Overall:  0.0% (threshold: 85.0%)  [FAIL]

Per-category breakdown:
  knowledge_qa                   0.0%  (0/2)  threshold: 80.0%  [FAIL]
  onboarding                     0.0%  (0/2)  threshold: 85.0%  [FAIL]
  setup                          0.0%  (0/3)  threshold: 85.0%  [FAIL]
  troubleshooting                0.0%  (0/3)  threshold: 80.0%  [FAIL]

GATE: FAIL — one or more thresholds not met
exit: 1
```

### Evidence 2 - Gate PASS (clean run)
```
$ python scripts/check_eval_gate.py --report results/fixture-pass.json --threshold 0.85 --categories troubleshooting:0.80 knowledge_qa:0.80
Overall:  100.0% (threshold: 85.0%)  [PASS]

Per-category breakdown:
  knowledge_qa                   100.0%  (2/2)  threshold: 80.0%  [PASS]
  onboarding                     100.0%  (2/2)  threshold: 85.0%  [PASS]
  setup                          100.0%  (3/3)  threshold: 85.0%  [PASS]
  troubleshooting                100.0%  (3/3)  threshold: 80.0%  [PASS]

GATE: PASS — all thresholds met
exit: 0
```

### Evidence 3 - Category masking (overall 91.4% PASS, troubleshooting 70% FAIL)
```
$ python scripts/check_eval_gate.py --report results/fixture-regression.json --threshold 0.75 --categories troubleshooting:0.90
Overall:  91.4% (threshold: 75.0%)  [PASS]

Per-category breakdown:
  knowledge_qa                   100.0%  (9/9)  threshold: 75.0%  [PASS]
  onboarding                     100.0%  (7/7)  threshold: 75.0%  [PASS]
  setup                          100.0%  (9/9)  threshold: 75.0%  [PASS]
  troubleshooting                70.0%  (7/10)  threshold: 90.0%  [FAIL]

GATE: FAIL — one or more thresholds not met
exit: 1
```

### Evidence 4 - Regression fires, names regressed cases
```
$ python scripts/regression_check.py --current results/fixture-regression.json --baseline results/fixture-baseline.json --max-regression 0.03
Baseline: 100.0%  |  Current: 91.4%  |  Delta: -8.6%
Max allowed regression: 3.0%

Regressed cases (3):
  - troubleshooting-001: Getting 403 on BigQuery connector
  - troubleshooting-002: Connection times out after 30s
  - troubleshooting-003: Sync completed but no data appeared

REGRESSION: pass rate dropped 8.6%, exceeds max allowed 3.0%
exit: 1
```

### Evidence 5 - Boundary case (89% to 86%, max_regression=0.03 - PASSES)
```
Baseline: 89.0%  |  Current: 86.0%  |  Delta: -3.0%
Max allowed regression: 3.0%

REGRESSION CHECK: PASS — drop of 3.0% is within allowed 3.0%
exit: 0
```

### Evidence 6 - Pre-commit hook (all tests pass in 0.02s)
```
$ bash .git/hooks/pre-commit
pre-commit: running unit tests...
................. [100%]
17 passed in 0.02s
pre-commit: all tests passed
exit: 0
```

## Metrics

| Metric | Target | Actual |
|--------|--------|--------|
| Gate script: fail on threshold breach | exits 1 | exits 1 |
| Gate script: pass when all thresholds met | exits 0 | exits 0 |
| Boundary case (exactly at max_regression) | exits 0 | exits 0 |
| Category masking case | exits 1 | exits 1 |
| Regression checker: names regressed cases | printed to stdout | 3 cases named with inputs |
| Pre-commit hook blocks failing tests | commit rejected | fires, 17 passed in 0.02s |
| All tests deterministic (no LLM calls) | < 1s | 0.02s |

## Failures and Fixes

1. **Float precision in regression boundary** - `0.89 - 0.86 = -0.030000000000000002` in IEEE 754.
   The naive `delta < -max_regression` condition fired on an exact 3% drop, which should pass.
   Fixed: `delta < -(max_regression + 1e-9)`. Caught by `test_passes_at_exact_boundary`.

2. **install_hooks.sh: missing hooks directory** - `.git/hooks/` does not exist by default in
   this repo. Script failed with "No such file or directory" on first run.
   Fixed: added `mkdir -p "$HOOKS_DIR"` before writing the hook file.

## Standards Compliance

Part A: sprint-05a (all active rules)
- RULE-CI01: PASS
- RULE-CI02: PASS
- RULE-CI03: PASS
- RULE-CI04: PASS
- All prior rules: N/A (no agent loop, prompt, memory, storage, or logging code this sprint)

Part B: previous sprints (CI01-CI04 only)
- Sprints 1-5: N/A — no scripts/ or .github/ directories in prior sprints

VIOLATIONS FOUND: none

## Blog-worthy findings

1. **Float precision breaks the boundary case** - The exact 3% boundary test caught a real IEEE
   754 bug. Write a test for every documented boundary condition — floats do not behave the way
   your mental model expects.

2. **Category masking is the best teaching moment** - Evidence 3 shows this live: overall 91.4%
   passes, but troubleshooting 70% fails the gate. Screenshot this. It is the clearest
   illustration of why per-category thresholds exist.

3. **Scripts-first is the inversion worth explaining** - The workflow is 60 lines and calls the
   scripts. The scripts are the product. Every gate check CI runs is also runnable in 3 seconds
   locally. Contrast with the common pattern of baking logic into workflow YAML.

4. **The missing hooks directory** - `install_hooks.sh` failed on first run because `.git/hooks/`
   did not exist. Fix is `mkdir -p`. Worth one sentence in the blog: real install scripts fail
   on edge cases tutorials skip.

5. **Regression output is a signal, not a number** - The checker names the specific inputs that
   regressed. "Pass rate dropped 8.6%" is a metric. "These three troubleshooting cases broke"
   is actionable.

## What I Would Do Differently

- **Baseline storage**: local JSON file works for a single developer but breaks in CI across
  branches (each branch has its own baseline). Next step: store baseline as a versioned CI
  artifact or in a dedicated branch. Missing prerequisite: a CI environment that actually runs
  the workflow (deferred to when the repo has active CI).

- **Eval sample run**: `run_eval_sample.py` delegates to the Lab 5 harness via subprocess.
  This works but couples sprint-05a to sprint-05's directory structure. A cleaner design would
  extract the eval runner into a shared `conductor/eval/` package imported by both sprints.
  Missing prerequisite: the series decision on whether shared packages are allowed
  (currently forbidden by the no-cross-sprint-imports rule).
