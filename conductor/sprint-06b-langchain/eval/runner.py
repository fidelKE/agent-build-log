"""
Eval runner for Conductor — Sprint 6b (LangChain create_agent()).

Feeds YAML eval cases to the agent, collects outputs and token costs.
Dataset path is always a CLI argument — never hardcoded (RULE-EVL01).
Deterministic checks run before the LLM judge (RULE-EVL02).
Token cost logged per case, aggregated per mode (RULE-EVL03).

Usage:
  UV_PROJECT_ENVIRONMENT=../.venv uv run python -m eval.runner \\
      --dataset ../../evals/datasets/conductor-v2.yaml \\
      --output results/run-06b.raw.json
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel


class CaseResult(BaseModel):
    id: str
    mode: str
    difficulty: str
    category: str
    input: str
    output: str
    duration_ms: float
    input_tokens: int
    output_tokens: int
    llm_call_count: int = 0
    deterministic_pass: bool
    deterministic_reason: str
    judge_verdict: Optional[str] = None
    judge_reason: Optional[str] = None
    final_pass: Optional[bool] = None
    error: Optional[str] = None


def _load_dataset(path: str) -> tuple[dict, list[dict]]:
    with open(path) as f:
        data = yaml.safe_load(f)
    metadata = data.get("metadata", {})
    cases = data.get("cases", [])
    return metadata, cases


def _deterministic_check(case: dict, output: str) -> tuple[bool, str]:
    must_not = case.get("must_not_contain", [])
    output_lower = output.lower()
    for forbidden in must_not:
        if forbidden.lower() in output_lower:
            return False, f"must_not_contain violation: '{forbidden}' found in output"
    return True, "ok"


def _extract_token_metrics(slogger) -> dict:
    metrics = {"llm_call_count": 0, "input_tokens": 0, "output_tokens": 0}
    try:
        trace_path = getattr(slogger, "_sink_path", None)
        if not trace_path or not Path(trace_path).exists():
            return metrics
        with open(trace_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                if event.get("event") == "llm_call":
                    metrics["llm_call_count"] += 1
                    metrics["input_tokens"] += (
                        event.get("gen_ai.usage.input_tokens")
                        or event.get("input_tokens", 0)
                    )
                    metrics["output_tokens"] += (
                        event.get("gen_ai.usage.output_tokens")
                        or event.get("output_tokens", 0)
                    )
    except Exception:
        pass
    return metrics


def _run_case(case: dict, catalog_base_url: str) -> dict:
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from src.agent import run
    from src.logger import TraceDepth

    user_id = case.get("user_id", f"eval-user-{case['id']}")
    log_dir = os.path.join(os.path.dirname(__file__), "..", "logs", "eval")

    t0 = time.monotonic()
    try:
        state, slogger = asyncio.run(run(
            user_message=case["input"],
            session_id=f"eval-{case['id']}",
            task_id="eval",
            user_id=user_id,
            log_dir=log_dir,
            trace_depth=TraceDepth.BOUNDARY,
            prefer_vault=False,
            catalog_base_url=catalog_base_url,
        ))
        duration_ms = round((time.monotonic() - t0) * 1000, 1)
        output = state.final_answer or ""
        metrics = _extract_token_metrics(slogger)
        error = None
    except Exception as exc:
        duration_ms = round((time.monotonic() - t0) * 1000, 1)
        output = ""
        metrics = {"llm_call_count": 0, "input_tokens": 0, "output_tokens": 0}
        error = str(exc)

    det_passed, det_reason = _deterministic_check(case, output)

    return CaseResult(
        id=case["id"],
        mode=case.get("mode", "unknown"),
        difficulty=case.get("difficulty", "unknown"),
        category=case.get("category", "unknown"),
        input=case["input"],
        output=output,
        duration_ms=duration_ms,
        input_tokens=metrics["input_tokens"],
        output_tokens=metrics["output_tokens"],
        llm_call_count=metrics["llm_call_count"],
        deterministic_pass=det_passed,
        deterministic_reason=det_reason,
        judge_verdict=None,
        judge_reason=None,
        final_pass=None if det_passed else False,
        error=error,
    ).model_dump()


def run_dataset(
    dataset_path: str,
    catalog_base_url: str = "",
    max_cases: int | None = None,
) -> dict:
    metadata, cases = _load_dataset(dataset_path)
    if max_cases:
        cases = cases[:max_cases]

    print(f"\nRunning {len(cases)} cases from {dataset_path}")
    results = []
    for i, case in enumerate(cases, 1):
        print(f"  [{i}/{len(cases)}] {case['id']} ...", end=" ", flush=True)
        result = _run_case(case, catalog_base_url=catalog_base_url)
        status = "FAIL (det)" if not result["deterministic_pass"] else "ok"
        if result["error"]:
            status = f"ERROR: {result['error'][:60]}"
        print(status)
        results.append(result)

    return {
        "dataset": dataset_path,
        "dataset_metadata": metadata,
        "total_cases": len(cases),
        "results": results,
        "run_timestamp": time.time(),
    }


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Conductor eval runner")
    parser.add_argument("--dataset", required=True,
                        help="Path to YAML eval dataset (RULE-EVL01: never hardcoded)")
    parser.add_argument("--output", default="results/run.json",
                        help="Output path for raw results JSON")
    parser.add_argument("--catalog-base-url", default="",
                        help="Base URL for data catalog")
    parser.add_argument("--max-cases", type=int, default=None,
                        help="Limit number of cases (for quick smoke tests)")
    args = parser.parse_args()

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)

    raw = run_dataset(
        dataset_path=args.dataset,
        catalog_base_url=args.catalog_base_url,
        max_cases=args.max_cases,
    )

    with open(args.output, "w") as f:
        json.dump(raw, f, indent=2)
    print(f"\nRaw results written to {args.output}")
    print("Run eval/judge.py --results <output> to score.")


if __name__ == "__main__":
    main()
