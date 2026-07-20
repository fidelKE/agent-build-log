"""
Eval runner for Conductor — Sprint 4.

Feeds YAML eval cases to the agent, collects outputs and token costs.
Dataset path is always a CLI argument — never hardcoded (RULE-EVL01).
Deterministic checks run before the LLM judge (RULE-EVL02).
Token cost logged per case, aggregated per mode (RULE-EVL03).

Fixture-based provider benchmark:
  --seed-memories   path to evals/fixtures/memory-sessions.yaml
  --fixture-user    user_id from the fixture to seed before running cases
  --ttl-test        verify TTL memory expires (sleeps 2s after seeding)

Usage (baseline):
  UV_PROJECT_ENVIRONMENT=../.venv uv run python -m eval.runner \\
      --dataset ../../evals/datasets/conductor-v1-approved.yaml \\
      --memory-provider inmemory \\
      --output results/run-generic.json

Usage (with fixture context):
  UV_PROJECT_ENVIRONMENT=../.venv uv run python -m eval.runner \\
      --dataset ../../evals/datasets/conductor-v1-approved.yaml \\
      --memory-provider redis \\
      --seed-memories ../../evals/fixtures/memory-sessions.yaml \\
      --fixture-user eval-fixture-alice \\
      --output results/run-redis-alice.json
"""

import argparse
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
    search_memory_calls: int = 0
    search_memory_avg_ms: float = 0.0
    search_results_returned: int = 0
    add_memory_calls: int = 0
    deterministic_pass: bool
    deterministic_reason: str
    judge_verdict: Optional[str] = None
    judge_reason: Optional[str] = None
    final_pass: Optional[bool] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Fixture: seed / cleanup / isolation
# ---------------------------------------------------------------------------

def _load_fixture(fixture_path: str) -> dict:
    with open(fixture_path) as f:
        return yaml.safe_load(f)


def _get_fixture_user(fixture: dict, user_id: str) -> dict | None:
    for user in fixture.get("users", []):
        if user["user_id"] == user_id:
            return user
    return None


def _seed_memories(store, fixture_path: str, user_id: str, ttl_test: bool = False) -> dict:
    """
    Seed fixture memories for user_id into the store.
    Cleans up first to guarantee a fresh starting point (idempotent).
    Returns a summary: {seeded: int, ttl_seeded: int, verified: bool}
    """
    fixture = _load_fixture(fixture_path)
    user = _get_fixture_user(fixture, user_id)
    if not user:
        raise ValueError(f"Fixture user '{user_id}' not found in {fixture_path}")

    # Clean first — removes any leftovers from previous runs
    _cleanup_memories(store, user_id)

    seeded = 0
    ttl_seeded = 0
    ttl_memory_content = None

    for mem in user.get("memories", []):
        content = mem["content"]
        metadata = mem.get("metadata", {})
        ttl = mem.get("ttl_seconds")

        if ttl is not None:
            ttl_memory_content = content
            ttl_seeded += 1
            # Only seed TTL memory when explicitly testing TTL behaviour
            if ttl_test:
                _seed_one(store, content, user_id, metadata, ttl)
        else:
            _seed_one(store, content, user_id, metadata, ttl=None)
            seeded += 1

    # Verify: immediately query back to confirm at least one memory is retrievable
    verified = False
    if seeded > 0:
        probe = store.search("connector", user_id=user_id, limit=1)
        verified = len(probe) > 0

    return {
        "user_id": user_id,
        "seeded": seeded,
        "ttl_seeded": ttl_seeded,
        "verified": verified,
    }


def _seed_one(store, content: str, user_id: str, metadata: dict, ttl: int | None) -> str:
    """Seed a single memory. TTL is passed as metadata for providers that support it."""
    if ttl is not None:
        metadata = {**metadata, "_ttl_seconds": ttl}
    return store.add(content, user_id=user_id, metadata=metadata)


def _cleanup_memories(store, user_id: str) -> int:
    """Delete all memories for user_id. Returns count deleted."""
    entries = store.get_all(user_id=user_id)
    deleted = 0
    for entry in entries:
        if store.delete(entry["id"], user_id=user_id):
            deleted += 1
    return deleted


def _isolation_check(store, seeded_users: list[str], probe_user: str) -> dict:
    """
    Verify probe_user cannot retrieve any memories from seeded_users' namespaces.
    Uses three probe queries to maximise chance of a false positive leaking through.
    Returns {passed: bool, probe_user: str, results_found: int, queries_tried: int}
    """
    probe_queries = [
        "connector setup authentication",
        "error timeout sync failed",
        "BigQuery Snowflake Redshift",
    ]
    total_found = 0
    for q in probe_queries:
        results = store.search(q, user_id=probe_user, limit=10)
        total_found += len(results)

    return {
        "passed": total_found == 0,
        "probe_user": probe_user,
        "results_found": total_found,
        "queries_tried": len(probe_queries),
        "seeded_users": seeded_users,
    }


def _ttl_check(store, user_id: str, ttl_content: str, sleep_seconds: float = 2.0) -> dict:
    """
    Verify a TTL-marked memory is not retrievable after expiry.
    Only Redis natively supports TTL; other providers ignore it (expected).
    Returns {provider: str, ttl_respected: bool | None}
    """
    provider = getattr(store, "provider_name", "unknown")
    if provider != "redis":
        return {"provider": provider, "ttl_respected": None, "note": "TTL not supported"}

    time.sleep(sleep_seconds)
    results = store.search(ttl_content[:20], user_id=user_id, limit=5)
    found = any(ttl_content[:20].lower() in r["content"].lower() for r in results)
    return {
        "provider": provider,
        "ttl_respected": not found,
        "sleep_seconds": sleep_seconds,
    }


# ---------------------------------------------------------------------------
# Dataset loading and deterministic check
# ---------------------------------------------------------------------------

def _load_dataset(path: str) -> tuple[dict, list[dict]]:
    with open(path) as f:
        data = yaml.safe_load(f)
    metadata = data.get("metadata", {})
    cases = data.get("cases", [])
    return metadata, cases


def _deterministic_check(case: dict, output: str) -> tuple[bool, str]:
    """
    Check must_not_contain items against the output.
    Returns (passed, reason). A fail here skips the LLM judge (RULE-EVL02).
    """
    must_not = case.get("must_not_contain", [])
    output_lower = output.lower()
    for forbidden in must_not:
        if forbidden.lower() in output_lower:
            return False, f"must_not_contain violation: '{forbidden}' found in output"
    return True, "ok"


# ---------------------------------------------------------------------------
# Retrieval metrics extracted from trace
# ---------------------------------------------------------------------------

def _extract_retrieval_metrics(slogger) -> dict:
    """
    Parse the trace log for memory tool calls and latency.
    Returns counts and timing for search_memory, add_memory, llm_call.
    """
    metrics = {
        "llm_call_count": 0,
        "search_memory_calls": 0,
        "search_memory_total_ms": 0.0,
        "search_results_returned": 0,
        "add_memory_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
    }
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
                ev = event.get("event", "")

                if ev == "llm_call":
                    metrics["llm_call_count"] += 1
                    metrics["input_tokens"] += (
                        event.get("gen_ai.usage.input_tokens")
                        or event.get("input_tokens")
                        or 0
                    )
                    metrics["output_tokens"] += (
                        event.get("gen_ai.usage.output_tokens")
                        or event.get("output_tokens")
                        or 0
                    )

                elif ev == "tool_call" and event.get("tool.name") == "search_memory":
                    metrics["search_memory_calls"] += 1
                    duration = event.get("duration_ms", 0.0)
                    metrics["search_memory_total_ms"] += duration or 0.0
                    output = event.get("output", {})
                    if isinstance(output, dict):
                        metrics["search_results_returned"] += output.get("total_found", 0)

                elif ev == "tool_call" and event.get("tool.name") == "add_memory":
                    metrics["add_memory_calls"] += 1

    except Exception:
        pass

    return metrics


# ---------------------------------------------------------------------------
# Single case runner
# ---------------------------------------------------------------------------

def _run_case(
    case: dict,
    memory_provider: str,
    catalog_base_url: str,
    fixture_user: str | None = None,
) -> dict:
    """Run a single eval case against the agent. Returns CaseResult dict."""
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from src.agent import run
    from src.logger import TraceDepth

    os.environ["MEMORY_PROVIDER"] = memory_provider

    # Use fixture_user if provided so the agent searches seeded memories
    user_id = fixture_user or case.get("user_id", f"eval-user-{case['id']}")

    t0 = time.monotonic()
    try:
        state, slogger = run(
            user_message=case["input"],
            session_id=f"eval-{case['id']}",
            task_id="eval",
            user_id=user_id,
            log_dir=os.path.join(os.path.dirname(__file__), "..", "logs", "eval"),
            trace_depth=TraceDepth.BOUNDARY,
            prefer_vault=False,
            catalog_base_url=catalog_base_url,
        )
        duration_ms = round((time.monotonic() - t0) * 1000, 1)
        output = state.final_answer or ""
        metrics = _extract_retrieval_metrics(slogger)
        error = None
    except Exception as exc:
        duration_ms = round((time.monotonic() - t0) * 1000, 1)
        output = ""
        metrics = {
            "llm_call_count": 0,
            "search_memory_calls": 0,
            "search_memory_total_ms": 0.0,
            "search_results_returned": 0,
            "add_memory_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        }
        error = str(exc)

    det_passed, det_reason = _deterministic_check(case, output)

    sm_calls = metrics["search_memory_calls"]
    sm_avg_ms = round(
        metrics["search_memory_total_ms"] / sm_calls if sm_calls > 0 else 0.0, 1
    )

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
        search_memory_calls=sm_calls,
        search_memory_avg_ms=sm_avg_ms,
        search_results_returned=metrics["search_results_returned"],
        add_memory_calls=metrics["add_memory_calls"],
        deterministic_pass=det_passed,
        deterministic_reason=det_reason,
        judge_verdict=None,
        judge_reason=None,
        final_pass=None if det_passed else False,
        error=error,
    ).model_dump()


# ---------------------------------------------------------------------------
# Dataset runner
# ---------------------------------------------------------------------------

def run_dataset(
    dataset_path: str,
    memory_provider: str = "inmemory",
    catalog_base_url: str = "",
    max_cases: int | None = None,
    fixture_path: str | None = None,
    fixture_user: str | None = None,
    ttl_test: bool = False,
) -> dict:
    """
    Run all cases in a dataset. Returns raw results dict.
    Pass rates are computed separately in report.py (RULE-EVL01 — never averaged).

    When fixture_path + fixture_user are provided:
      1. Build the memory store for this provider
      2. Cleanup any existing memories for fixture_user
      3. Seed fixture memories
      4. Run isolation check (charlie sees nothing)
      5. Run all eval cases as fixture_user
      6. Cleanup after run
    """
    sys.path.insert(0, str(Path(dataset_path).parent.parent.parent))

    metadata, cases = _load_dataset(dataset_path)
    if max_cases:
        cases = cases[:max_cases]

    seed_summary = None
    isolation_result = None
    ttl_result = None

    if fixture_path and fixture_user:
        from src.memory import make_memory_store
        store = make_memory_store(provider=memory_provider)

        print(f"Seeding fixture memories for {fixture_user} into {memory_provider}...")
        seed_summary = _seed_memories(store, fixture_path, fixture_user, ttl_test=ttl_test)
        verified = "OK" if seed_summary["verified"] else "WARN: seed not immediately retrievable"
        print(f"  Seeded {seed_summary['seeded']} memories. Verification: {verified}")

        # Isolation check: charlie should see nothing after alice+bob are seeded
        print("Running isolation check (eval-fixture-charlie)...")
        isolation_result = _isolation_check(
            store,
            seeded_users=[fixture_user],
            probe_user="eval-fixture-charlie",
        )
        iso_status = "PASS" if isolation_result["passed"] else "FAIL"
        print(f"  Isolation: {iso_status} — {isolation_result['results_found']} results found for probe user")

        if ttl_test and seed_summary.get("ttl_seeded", 0) > 0:
            print("Running TTL check (sleeping 2s)...")
            ttl_result = _ttl_check(store, fixture_user, "TTL test", sleep_seconds=2.0)
            ttl_status = ttl_result.get("ttl_respected")
            print(f"  TTL: {'PASS' if ttl_status else 'FAIL' if ttl_status is False else 'N/A (not supported)'}")

    print(f"\nRunning {len(cases)} cases from {dataset_path} (provider: {memory_provider}, fixture_user: {fixture_user or 'per-case'})")
    results = []
    for i, case in enumerate(cases, 1):
        print(f"  [{i}/{len(cases)}] {case['id']} ...", end=" ", flush=True)
        result = _run_case(
            case,
            memory_provider=memory_provider,
            catalog_base_url=catalog_base_url,
            fixture_user=fixture_user,
        )
        status = "FAIL (det)" if not result["deterministic_pass"] else "ok"
        if result["error"]:
            status = f"ERROR: {result['error'][:40]}"
        print(status)
        results.append(result)

    # Cleanup after run to leave providers in clean state
    if fixture_path and fixture_user:
        from src.memory import make_memory_store
        store = make_memory_store(provider=memory_provider)
        cleaned = _cleanup_memories(store, fixture_user)
        print(f"\nCleanup: removed {cleaned} memories for {fixture_user}")

    return {
        "dataset": dataset_path,
        "dataset_metadata": metadata,
        "memory_provider": memory_provider,
        "fixture_user": fixture_user,
        "total_cases": len(cases),
        "results": results,
        "run_timestamp": time.time(),
        "seed_summary": seed_summary,
        "isolation_result": isolation_result,
        "ttl_result": ttl_result,
    }


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Conductor eval runner")
    parser.add_argument("--dataset", required=True,
                        help="Path to YAML eval dataset (RULE-EVL01: never hardcoded)")
    parser.add_argument("--memory-provider", default="inmemory",
                        choices=["redis", "qdrant", "mem0", "mem0-server", "inmemory"],
                        help="Memory provider to use for this run")
    parser.add_argument("--output", default="results/run.json",
                        help="Output path for raw results JSON")
    parser.add_argument("--catalog-base-url", default="",
                        help="Base URL for data catalog (leave empty to skip catalog tool)")
    parser.add_argument("--max-cases", type=int, default=None,
                        help="Limit number of cases (for quick smoke tests)")
    parser.add_argument("--seed-memories", default=None,
                        help="Path to fixture file (evals/fixtures/memory-sessions.yaml)")
    parser.add_argument("--fixture-user", default=None,
                        help="user_id from fixture to seed before running cases")
    parser.add_argument("--ttl-test", action="store_true",
                        help="Seed TTL memory and verify it expires (Redis only)")
    args = parser.parse_args()

    if args.seed_memories and not args.fixture_user:
        parser.error("--fixture-user required when --seed-memories is provided")

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)

    raw = run_dataset(
        dataset_path=args.dataset,
        memory_provider=args.memory_provider,
        catalog_base_url=args.catalog_base_url,
        max_cases=args.max_cases,
        fixture_path=args.seed_memories,
        fixture_user=args.fixture_user,
        ttl_test=args.ttl_test,
    )

    with open(args.output, "w") as f:
        json.dump(raw, f, indent=2)
    print(f"\nRaw results written to {args.output}")
    print("Run eval/judge.py to score and eval/report.py to summarise.")


if __name__ == "__main__":
    main()
