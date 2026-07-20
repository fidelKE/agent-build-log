"""
Regression checker for Conductor CI pipeline.

Compares a current eval report against a stored baseline. Fires (exits 1) if the
overall pass rate drops by more than max_regression. Always prints which specific
cases regressed so the failure is actionable (RULE-CI02).

Runs standalone without GitHub Actions (RULE-CI03).

Usage:
    # Check for regression (blocks if drop > 3%)
    python scripts/regression_check.py \\
        --current results/run-scored.json \\
        --baseline results/baseline.json \\
        --max-regression 0.03

    # Save current report as the new baseline
    python scripts/regression_check.py \\
        --current results/run-scored.json \\
        --save-baseline results/baseline.json
"""

import argparse
import json
import shutil
import sys
from pathlib import Path


def _load(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        sys.exit(2)
    with open(p) as f:
        return json.load(f)


def _pass_rate(results: list[dict]) -> float:
    if not results:
        return 0.0
    return sum(1 for r in results if r.get("final_pass") is True) / len(results)


def _index_by_id(results: list[dict]) -> dict[str, dict]:
    return {r["id"]: r for r in results if "id" in r}


def check_regression(
    current: dict,
    baseline: dict,
    max_regression: float,
) -> bool:
    current_results = current.get("results", [])
    baseline_results = baseline.get("results", [])

    current_rate = _pass_rate(current_results)
    baseline_rate = _pass_rate(baseline_results)
    delta = current_rate - baseline_rate

    print(f"Baseline: {baseline_rate:.1%}  |  Current: {current_rate:.1%}  |  Delta: {delta:+.1%}")
    print(f"Max allowed regression: {max_regression:.1%}")
    print()

    # Find cases that regressed: passed in baseline, failing now
    baseline_idx = _index_by_id(baseline_results)
    regressed = []
    improved = []

    for result in current_results:
        case_id = result.get("id")
        if not case_id:
            continue
        baseline_case = baseline_idx.get(case_id)
        if baseline_case is None:
            continue

        was_pass = baseline_case.get("final_pass") is True
        now_pass = result.get("final_pass") is True

        if was_pass and not now_pass:
            regressed.append(result)
        elif not was_pass and now_pass:
            improved.append(result)

    if improved:
        print(f"Improved cases ({len(improved)}):")
        for r in improved:
            print(f"  + {r['id']}")
        print()

    if regressed:
        print(f"Regressed cases ({len(regressed)}):")
        for r in regressed:
            snippet = r.get("input", "")[:80].replace("\n", " ")
            print(f"  - {r['id']}: {snippet}")
        print()

    # The boundary condition: drop must EXCEED max_regression to fire.
    # Use a small epsilon to handle float precision (0.89 - 0.86 = -0.030000000000000002).
    # A drop of exactly max_regression should pass, not fire.
    _EPSILON = 1e-9
    if delta < -(max_regression + _EPSILON):
        print(
            f"REGRESSION: pass rate dropped {-delta:.1%}, exceeds max allowed {max_regression:.1%}"
        )
        return False

    if delta >= 0:
        print("REGRESSION CHECK: PASS — pass rate held or improved")
    else:
        print(
            f"REGRESSION CHECK: PASS — drop of {-delta:.1%} is within allowed {max_regression:.1%}"
        )
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare current eval report to baseline and detect regressions."
    )
    parser.add_argument("--current", required=True, help="Path to current eval report JSON")
    parser.add_argument(
        "--baseline",
        help="Path to baseline eval report JSON (required unless --save-baseline)",
    )
    parser.add_argument(
        "--max-regression",
        type=float,
        default=0.03,
        help="Maximum allowed pass rate drop before firing (default: 0.03 = 3%%)",
    )
    parser.add_argument(
        "--save-baseline",
        metavar="PATH",
        help="Copy current report to PATH and exit 0 (use to promote a passing run as new baseline)",
    )
    args = parser.parse_args()

    if args.save_baseline:
        shutil.copy2(args.current, args.save_baseline)
        print(f"Baseline saved: {args.save_baseline}")
        sys.exit(0)

    if not args.baseline:
        print("ERROR: --baseline is required (or use --save-baseline to create one)", file=sys.stderr)
        sys.exit(2)

    current = _load(args.current)
    baseline = _load(args.baseline)

    passed = check_regression(current, baseline, args.max_regression)
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
