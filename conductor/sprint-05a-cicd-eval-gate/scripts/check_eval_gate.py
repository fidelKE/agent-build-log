"""
Eval gate script for Conductor CI pipeline.

Reads a scored eval report JSON (produced by eval/judge.py + eval/report.py),
checks overall pass rate against a threshold, then checks each required category
independently. Exits 0 if all gates pass, exits 1 if any gate fails.

This is the primary interface — runs standalone without GitHub Actions (RULE-CI03).
Per-category thresholds are required to prevent aggregate masking (RULE-CI01).

Usage:
    python scripts/check_eval_gate.py \\
        --report results/run-scored.json \\
        --threshold 0.85 \\
        --categories troubleshooting:0.80 safety:0.90

The --categories flag accepts name:threshold pairs. If a category appears in the
report but is not listed here, it is checked against the overall --threshold.
"""

import argparse
import json
import sys
from pathlib import Path


def _load_report(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        print(f"ERROR: report not found: {path}", file=sys.stderr)
        sys.exit(2)
    with open(p) as f:
        return json.load(f)


def _pass_rate(results: list[dict]) -> float:
    if not results:
        return 0.0
    passed = sum(1 for r in results if r.get("final_pass") is True)
    return passed / len(results)


def _group_by_category(results: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for r in results:
        cat = r.get("category", "unknown")
        groups.setdefault(cat, []).append(r)
    return groups


def check_gate(
    report: dict,
    overall_threshold: float,
    category_thresholds: dict[str, float],
) -> bool:
    results = report.get("results", [])
    if not results:
        print("ERROR: report contains no results", file=sys.stderr)
        return False

    all_passed = True

    # Overall pass rate
    overall_rate = _pass_rate(results)
    status = "PASS" if overall_rate >= overall_threshold else "FAIL"
    print(
        f"Overall:  {overall_rate:.1%} (threshold: {overall_threshold:.1%})  [{status}]"
    )
    if overall_rate < overall_threshold:
        all_passed = False

    # Per-category pass rates
    by_category = _group_by_category(results)
    if by_category:
        print()
        print("Per-category breakdown:")
        for cat, cat_results in sorted(by_category.items()):
            rate = _pass_rate(cat_results)
            threshold = category_thresholds.get(cat, overall_threshold)
            status = "PASS" if rate >= threshold else "FAIL"
            count = len(cat_results)
            passed_count = sum(1 for r in cat_results if r.get("final_pass") is True)
            print(
                f"  {cat:<30} {rate:.1%}  ({passed_count}/{count})"
                f"  threshold: {threshold:.1%}  [{status}]"
            )
            if rate < threshold:
                all_passed = False

    print()
    if all_passed:
        print("GATE: PASS — all thresholds met")
    else:
        print("GATE: FAIL — one or more thresholds not met")

    return all_passed


def _parse_category_thresholds(specs: list[str]) -> dict[str, float]:
    result: dict[str, float] = {}
    for spec in specs:
        if ":" not in spec:
            print(
                f"ERROR: --categories entry '{spec}' must be name:threshold (e.g. safety:0.90)",
                file=sys.stderr,
            )
            sys.exit(2)
        name, raw = spec.split(":", 1)
        try:
            result[name.strip()] = float(raw.strip())
        except ValueError:
            print(
                f"ERROR: threshold in '{spec}' is not a float",
                file=sys.stderr,
            )
            sys.exit(2)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check eval pass rate against quality thresholds."
    )
    parser.add_argument("--report", required=True, help="Path to scored eval report JSON")
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.85,
        help="Overall pass rate threshold (default: 0.85)",
    )
    parser.add_argument(
        "--categories",
        nargs="*",
        default=[],
        metavar="NAME:THRESHOLD",
        help="Per-category thresholds, e.g. troubleshooting:0.80 safety:0.90",
    )
    args = parser.parse_args()

    report = _load_report(args.report)
    category_thresholds = _parse_category_thresholds(args.categories)

    passed = check_gate(report, args.threshold, category_thresholds)
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
