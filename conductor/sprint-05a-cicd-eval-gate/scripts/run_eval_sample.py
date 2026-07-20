"""
Eval sample runner for Conductor CI pipeline.

Runs a random subset of cases from a YAML eval dataset through the sprint-05a
eval harness, then scores them with the judge. Produces the report JSON consumed
by check_eval_gate.py and regression_check.py.

Runs standalone without GitHub Actions (RULE-CI03).
Dataset path is always a CLI argument — never hardcoded (RULE-EVL01).

Usage:
    UV_PROJECT_ENVIRONMENT=../.venv uv run python scripts/run_eval_sample.py \\
        --dataset ../evals/datasets/conductor-v2.yaml \\
        --sample 15 \\
        --output results/sample-run.json \\
        --memory-provider inmemory

    # Run all cases (full suite mode)
    UV_PROJECT_ENVIRONMENT=../.venv uv run python scripts/run_eval_sample.py \\
        --dataset ../evals/datasets/conductor-v2.yaml \\
        --output results/full-run.json \\
        --memory-provider inmemory
"""

import argparse
import json
import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path

# This sprint's own root — eval/ and src/ live here
SPRINT_ROOT = Path(__file__).parent.parent


def _assert_harness_exists() -> None:
    runner = SPRINT_ROOT / "eval" / "runner.py"
    judge = SPRINT_ROOT / "eval" / "judge.py"
    if not runner.exists() or not judge.exists():
        print(
            f"ERROR: eval harness not found at {SPRINT_ROOT}/eval/\n"
            "       Expected eval/runner.py and eval/judge.py in this sprint.",
            file=sys.stderr,
        )
        sys.exit(2)


def _sample_dataset(dataset_path: str, n: int | None, seed: int | None) -> str:
    """Return path to a temp YAML file with a random sample of cases."""
    import yaml

    with open(dataset_path) as f:
        data = yaml.safe_load(f)

    cases = data.get("cases", [])
    if n is None or n >= len(cases):
        return dataset_path  # use original, no sampling needed

    rng = random.Random(seed)
    sampled = rng.sample(cases, n)

    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, prefix="conductor-sample-"
    )
    sample_data = {**data, "cases": sampled}
    yaml.dump(sample_data, tmp)
    tmp.close()
    return tmp.name


def _uv_run(cmd: list[str], cwd: Path) -> int:
    env = os.environ.copy()
    env["UV_PROJECT_ENVIRONMENT"] = str(SPRINT_ROOT / ".." / ".venv")
    result = subprocess.run(
        ["uv", "run"] + cmd,
        cwd=str(cwd),
        env=env,
    )
    return result.returncode


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run an eval sample and produce a scored report JSON."
    )
    parser.add_argument("--dataset", required=True, help="Path to YAML eval dataset")
    parser.add_argument(
        "--sample",
        type=int,
        default=None,
        metavar="N",
        help="Number of cases to sample (default: all cases)",
    )
    parser.add_argument("--seed", type=int, default=42, help="Random seed for sampling (default: 42)")
    parser.add_argument("--output", required=True, help="Path for the final scored report JSON")
    parser.add_argument(
        "--memory-provider",
        default="inmemory",
        choices=["inmemory", "redis", "qdrant", "mem0"],
        help="Memory provider to use (default: inmemory)",
    )
    args = parser.parse_args()

    _assert_harness_exists()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Step 1: sample the dataset if requested
    dataset_to_use = _sample_dataset(args.dataset, args.sample, args.seed)
    sampled = dataset_to_use != args.dataset
    if sampled:
        print(f"Sampled {args.sample} cases from {args.dataset}")

    # Step 2: run the agent over sampled cases → raw output
    raw_output = str(output_path.with_suffix(".raw.json"))
    print(f"\n[1/2] Running eval harness ({args.sample or 'all'} cases)...")
    rc = _uv_run(
        [
            "python", "-m", "eval.runner",
            "--dataset", os.path.abspath(dataset_to_use),
            "--memory-provider", args.memory_provider,
            "--output", os.path.abspath(raw_output),
        ],
        cwd=SPRINT_ROOT,
    )
    if rc != 0:
        print(f"ERROR: eval runner exited {rc}", file=sys.stderr)
        sys.exit(rc)

    # Step 3: score with judge → final scored report
    print(f"\n[2/2] Scoring with judge...")
    rc = _uv_run(
        [
            "python", "-m", "eval.judge",
            "--input", os.path.abspath(raw_output),
            "--output", os.path.abspath(str(output_path)),
        ],
        cwd=SPRINT_ROOT,
    )
    if rc != 0:
        print(f"ERROR: judge exited {rc}", file=sys.stderr)
        sys.exit(rc)

    # Clean up temp sample file
    if sampled:
        Path(dataset_to_use).unlink(missing_ok=True)

    # Print a quick summary
    with open(output_path) as f:
        report = json.load(f)
    results = report.get("results", [])
    passed = sum(1 for r in results if r.get("final_pass") is True)
    total = len(results)
    rate = passed / total if total > 0 else 0.0
    print(f"\nSample complete: {passed}/{total} passed ({rate:.1%})")
    print(f"Report written to: {output_path}")


if __name__ == "__main__":
    main()
