"""
Baseline eval runner for Conductor eval dataset.

Runs a stub agent (always returns a generic response) against the approved
eval dataset and measures the pass rate. This is the zero-line — every sprint
after this should move the score up.

Usage:
    python baseline.py --dataset ../../evals/datasets/conductor-v1-approved.yaml
    python baseline.py --dataset ../../evals/datasets/conductor-v1.yaml --use-all
"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import yaml


# ---------------------------------------------------------------------------
# Stub agent — simulates a zero-capability agent for baseline measurement.
# Replace with a real agent in Sprint 4+ when the eval harness is built.
# ---------------------------------------------------------------------------

def stub_agent(user_input: str) -> str:
    """Stub agent that returns a generic response regardless of input."""
    return (
        "I can help you with your data integration setup. "
        "Could you provide more details about what you're trying to accomplish?"
    )


# ---------------------------------------------------------------------------
# Evaluators
# ---------------------------------------------------------------------------

def check_expected_output(response: str, expected_items: list[str]) -> tuple[bool, list[str]]:
    """
    Deterministic check: response must contain a signal for each expected item.
    Uses simple substring matching on lowercased text. Replace with semantic
    matching in Sprint 4 when the LLM-as-judge is introduced.
    """
    response_lower = response.lower()
    failures = []

    for item in expected_items:
        # Extract key signal words from the expected item (nouns, verbs, error codes)
        # This is intentionally simple for the baseline — high false-negative rate expected.
        keywords = [w.lower() for w in item.split() if len(w) > 4]
        if not any(kw in response_lower for kw in keywords):
            failures.append(item)

    passed = len(failures) == 0
    return passed, failures


def check_must_not_contain(response: str, forbidden: list[str]) -> tuple[bool, list[str]]:
    """Deterministic check: response must not contain any forbidden strings."""
    response_lower = response.lower()
    violations = [f for f in forbidden if f.lower() in response_lower]
    return len(violations) == 0, violations


def evaluate_case(case: dict, response: str) -> dict:
    expected = case.get("expected_output", [])
    forbidden = case.get("must_not_contain", [])

    output_ok, output_failures = check_expected_output(response, expected)
    forbidden_ok, forbidden_violations = check_must_not_contain(response, forbidden)

    passed = output_ok and forbidden_ok

    return {
        "id": case["id"],
        "mode": case["mode"],
        "difficulty": case["difficulty"],
        "adversarial_category": case.get("adversarial_category"),
        "passed": passed,
        "output_check": {"passed": output_ok, "missing_signals": output_failures},
        "forbidden_check": {"passed": forbidden_ok, "violations": forbidden_violations},
    }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_baseline(cases: list[dict]) -> list[dict]:
    results = []
    for case in cases:
        response = stub_agent(case["input"])
        result = evaluate_case(case, response)
        result["agent_response"] = response
        results.append(result)
    return results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_report(results: list[dict]) -> None:
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    pass_rate = passed / total * 100 if total > 0 else 0

    print("\n" + "=" * 70)
    print("BASELINE EVAL REPORT — Stub Agent")
    print("=" * 70)
    print(f"  Total cases:  {total}")
    print(f"  Passed:       {passed}  ({pass_rate:.1f}%)")
    print(f"  Failed:       {total - passed}")

    # Breakdown by mode
    print("\nBy mode:")
    for mode in ("setup", "onboarding", "troubleshooting", "qa"):
        mode_results = [r for r in results if r["mode"] == mode]
        if not mode_results:
            continue
        mode_passed = sum(1 for r in mode_results if r["passed"])
        print(f"  {mode:<15} {mode_passed}/{len(mode_results)}")

    # Breakdown by difficulty
    print("\nBy difficulty:")
    for difficulty in ("easy", "medium", "hard", "adversarial"):
        diff_results = [r for r in results if r["difficulty"] == difficulty]
        if not diff_results:
            continue
        diff_passed = sum(1 for r in diff_results if r["passed"])
        print(f"  {difficulty:<15} {diff_passed}/{len(diff_results)}")

    # Adversarial breakdown
    adv_results = [r for r in results if r["difficulty"] == "adversarial"]
    if adv_results:
        print("\nAdversarial by category:")
        categories = {r["adversarial_category"] for r in adv_results if r.get("adversarial_category")}
        for cat in sorted(categories):
            cat_results = [r for r in adv_results if r.get("adversarial_category") == cat]
            cat_passed = sum(1 for r in cat_results if r["passed"])
            print(f"  {cat:<35} {cat_passed}/{len(cat_results)}")

    print(f"\nBaseline pass rate: {pass_rate:.1f}%")
    if pass_rate == 0:
        print("Zero-line established. Evaluator may be stricter than expected — recalibrate in Sprint 4.")
    elif pass_rate <= 30:
        print("✓ Zero-line established. Within expected range for a stub agent.")
    else:
        print("⚠ Pass rate unexpectedly high — review stub agent or eval cases.")

    print()


def save_report(results: list[dict], output_path: str) -> None:
    report = {
        "metadata": {
            "agent": "stub",
            "run_at": datetime.now(timezone.utc).isoformat() + "Z",
            "total": len(results),
            "passed": sum(1 for r in results if r["passed"]),
            "pass_rate": sum(1 for r in results if r["passed"]) / len(results) if results else 0,
        },
        "results": results,
    }
    with open(output_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"Report saved to: {output_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Baseline eval runner for Conductor")
    parser.add_argument("--dataset", required=True, help="Path to YAML eval dataset")
    parser.add_argument(
        "--use-all",
        action="store_true",
        help="Use all cases including unapproved (for initial baseline before SME review)",
    )
    parser.add_argument("--output", default=None, help="Path to save JSON report")
    args = parser.parse_args()

    with open(args.dataset) as f:
        data = yaml.safe_load(f)

    # Support both the raw dataset format and the approved export format
    cases = data.get("cases", [])
    print(f"Loaded {len(cases)} cases from {args.dataset}")

    results = run_baseline(cases)
    print_report(results)

    output_path = args.output or f"../../evals/reports/baseline-stub-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    save_report(results, output_path)


if __name__ == "__main__":
    main()
