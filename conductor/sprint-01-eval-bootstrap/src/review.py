"""
SME review script for conductor-v1.yaml.

Loads each eval case and presents it interactively for human approval.
Outputs a reviewed dataset and a review notes file.

Usage:
    python review.py --dataset ../../evals/datasets/conductor-v1.yaml
    python review.py --dataset ../../evals/datasets/conductor-v1.yaml --resume
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import yaml


REVIEW_STATE_FILE = "review_state.json"


def load_dataset(path: str) -> list[dict]:
    with open(path) as f:
        data = yaml.safe_load(f)
    return data["cases"]


def load_state(path: str) -> dict:
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"reviewed": {}, "notes": {}}


def save_state(path: str, state: dict) -> None:
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


def display_case(case: dict, index: int, total: int) -> None:
    print("\n" + "=" * 70)
    print(f"Case {index + 1} of {total}  |  ID: {case['id']}")
    print(f"Mode: {case['mode']}  |  Difficulty: {case['difficulty']}", end="")
    if case.get("adversarial_category"):
        print(f"  |  Attack: {case['adversarial_category']}", end="")
    print()
    print("=" * 70)

    print("\nINPUT:")
    print(f"  {case['input'].strip()}")

    if case.get("context"):
        print("\nCONTEXT (previous turn):")
        print(f"  {case['context'].strip()}")

    print("\nEXPECTED OUTPUT (must include all of):")
    for item in case["expected_output"]:
        print(f"  • {item}")

    if case.get("must_not_contain"):
        print("\nMUST NOT CONTAIN:")
        for item in case["must_not_contain"]:
            print(f"  ✗ {item}")

    print("\nRATIONALE:")
    print(f"  {case['rationale'].strip()}")


def prompt_decision() -> tuple[str, str]:
    print("\n" + "-" * 70)
    print("Decision:  [a] Approve   [r] Reject   [s] Skip (review later)   [q] Quit")
    while True:
        choice = input("→ ").strip().lower()
        if choice in ("a", "r", "s", "q"):
            break
        print("Please enter a, r, s, or q.")

    note = ""
    if choice == "r":
        note = input("Rejection reason (one line): ").strip()
    elif choice == "a":
        note = input("Optional note (press Enter to skip): ").strip()

    return choice, note


def print_summary(state: dict, total: int) -> None:
    reviewed = state["reviewed"]
    approved = sum(1 for v in reviewed.values() if v == "approved")
    rejected = sum(1 for v in reviewed.values() if v == "rejected")
    skipped = sum(1 for v in reviewed.values() if v == "skipped")
    remaining = total - len(reviewed)

    print("\n" + "=" * 70)
    print("REVIEW SUMMARY")
    print("=" * 70)
    print(f"  Total cases:   {total}")
    print(f"  Approved:      {approved}  ({approved / total * 100:.0f}%)")
    print(f"  Rejected:      {rejected}")
    print(f"  Skipped:       {skipped}")
    print(f"  Not yet seen:  {remaining}")

    if len(reviewed) > 0:
        approval_rate = approved / len(reviewed) * 100
        print(f"\n  Approval rate (of reviewed): {approval_rate:.0f}%", end="")
        if approval_rate >= 80:
            print("  ✓ Meets 80% target")
        else:
            print(f"  ✗ Below 80% target ({80 - approval_rate:.0f}% gap)")

    print()


def export_approved_dataset(cases: list[dict], state: dict, output_path: str) -> None:
    approved_ids = {k for k, v in state["reviewed"].items() if v == "approved"}
    approved_cases = [c for c in cases if c["id"] in approved_ids]

    output = {
        "metadata": {
            "version": "1",
            "reviewed_at": datetime.utcnow().isoformat() + "Z",
            "total_approved": len(approved_cases),
        },
        "cases": approved_cases,
    }

    with open(output_path, "w") as f:
        yaml.dump(output, f, allow_unicode=True, sort_keys=False)

    print(f"Approved dataset written to: {output_path}")


def export_review_notes(cases: list[dict], state: dict, output_path: str) -> None:
    lines = [
        f"# SME Review Notes — conductor-v1.yaml",
        f"# Generated: {datetime.utcnow().isoformat()}Z",
        "",
    ]

    for case in cases:
        cid = case["id"]
        decision = state["reviewed"].get(cid, "not_reviewed")
        note = state["notes"].get(cid, "")
        line = f"{cid}: {decision}"
        if note:
            line += f"  — {note}"
        lines.append(line)

    with open(output_path, "w") as f:
        f.write("\n".join(lines) + "\n")

    print(f"Review notes written to: {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="SME review script for Conductor eval dataset")
    parser.add_argument("--dataset", required=True, help="Path to conductor-v1.yaml")
    parser.add_argument("--resume", action="store_true", help="Resume a previous review session")
    parser.add_argument("--state", default=REVIEW_STATE_FILE, help="Path to review state file")
    args = parser.parse_args()

    cases = load_dataset(args.dataset)
    state = load_state(args.state) if args.resume else {"reviewed": {}, "notes": {}}

    total = len(cases)
    print(f"\nLoaded {total} cases from {args.dataset}")

    if args.resume:
        done = len(state["reviewed"])
        print(f"Resuming — {done} already reviewed, {total - done} remaining")

    try:
        for i, case in enumerate(cases):
            cid = case["id"]

            if cid in state["reviewed"] and args.resume:
                continue

            display_case(case, i, total)
            choice, note = prompt_decision()

            if choice == "q":
                print("\nSession ended. Progress saved.")
                break

            decision_map = {"a": "approved", "r": "rejected", "s": "skipped"}
            state["reviewed"][cid] = decision_map[choice]
            if note:
                state["notes"][cid] = note

            save_state(args.state, state)

    except KeyboardInterrupt:
        print("\n\nInterrupted. Progress saved.")

    print_summary(state, total)

    dataset_dir = Path(args.dataset).parent
    export_approved_dataset(cases, state, str(dataset_dir / "conductor-v1-approved.yaml"))
    export_review_notes(cases, state, "review_notes.txt")


if __name__ == "__main__":
    main()
