"""
Eval report generator for Conductor — Sprint 4.

Reads one or more judged results JSON files and produces:
  - Pass rate by dataset (never averaged across datasets — RULE-EVL01)
  - Pass rate by mode and difficulty
  - Per-mode token cost averages (baseline for mode router — RULE-EVL03)
  - Provider comparison table (when multiple result files supplied)
  - Retrieval mechanics table (search latency, result counts, tool call counts)
  - Isolation and TTL check results

Dataset health report (A10):
  Pass --health <dataset.yaml> to check coverage rate, freshness, and tag distribution.
  Freshness: % of cases with created_date within 6 months of today.
  Tag distribution target: 40% easy / 30% medium / 20% hard / 10% adversarial.

Usage (single dataset):
  UV_PROJECT_ENVIRONMENT=../.venv uv run python -m eval.report \\
      --results results/run-generic-judged.json \\
      --label "generic"

Usage (provider comparison):
  UV_PROJECT_ENVIRONMENT=../.venv uv run python -m eval.report \\
      --results results/run-generic-redis-judged.json:redis-nocontext \\
                results/run-alice-redis-judged.json:redis-alice \\
                results/run-alice-qdrant-judged.json:qdrant-alice \\
                results/run-alice-mem0-judged.json:mem0-alice

Usage (dataset health):
  UV_PROJECT_ENVIRONMENT=../.venv uv run python -m eval.report \\
      --health ../../evals/datasets/conductor-v1-approved.yaml
"""

import argparse
import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import yaml


def _pass_rate(results: list[dict]) -> float:
    if not results:
        return 0.0
    passed = sum(1 for r in results if r.get("final_pass"))
    return round(passed / len(results) * 100, 1)


def _avg(values: list[float]) -> float:
    non_zero = [v for v in values if v]
    return round(sum(non_zero) / len(non_zero), 1) if non_zero else 0.0


def generate_report(judged_data: dict, label: str = "") -> str:
    results = judged_data["results"]
    dataset = judged_data.get("dataset", "unknown")
    provider = judged_data.get("memory_provider", "unknown")
    fixture_user = judged_data.get("fixture_user") or "per-case"
    total = len(results)
    passed = sum(1 for r in results if r.get("final_pass"))

    lines = []
    tag = f" [{label}]" if label else ""
    lines.append(f"## Eval Report{tag}")
    lines.append(f"Dataset:      {dataset}")
    lines.append(f"Provider:     {provider}")
    lines.append(f"Fixture user: {fixture_user}")
    lines.append(f"Cases:        {total}")
    lines.append(f"Passed:       {passed}/{total} ({_pass_rate(results)}%)")
    lines.append("")

    # Isolation and TTL results (fixture runs only)
    isolation = judged_data.get("isolation_result")
    if isolation:
        iso_status = "PASS" if isolation["passed"] else "FAIL"
        lines.append(f"Isolation check: {iso_status} — {isolation['results_found']} results found for probe user '{isolation['probe_user']}'")
    ttl = judged_data.get("ttl_result")
    if ttl:
        ttl_val = ttl.get("ttl_respected")
        ttl_status = "PASS" if ttl_val else ("FAIL" if ttl_val is False else f"N/A ({ttl.get('note', '')})")
        lines.append(f"TTL check:       {ttl_status}")
    if isolation or ttl:
        lines.append("")

    # Seed summary
    seed = judged_data.get("seed_summary")
    if seed:
        verified = "OK" if seed.get("verified") else "WARN: not immediately retrievable"
        lines.append(f"Seed summary: {seed['seeded']} memories seeded, verification: {verified}")
        lines.append("")

    # Pass rate by mode
    by_mode: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_mode[r.get("mode", "unknown")].append(r)

    lines.append("### Pass rate by mode")
    lines.append(f"{'Mode':<20} {'Cases':>6} {'Pass':>6} {'Pass%':>7} {'Avg input_tok':>14} {'Avg output_tok':>15}")
    lines.append("-" * 70)
    for mode in sorted(by_mode):
        mode_results = by_mode[mode]
        pr = _pass_rate(mode_results)
        avg_in = _avg([r["input_tokens"] for r in mode_results])
        avg_out = _avg([r["output_tokens"] for r in mode_results])
        n_pass = sum(1 for r in mode_results if r.get("final_pass"))
        lines.append(f"{mode:<20} {len(mode_results):>6} {n_pass:>6} {pr:>6}% {avg_in:>14} {avg_out:>15}")
    lines.append("")

    # Pass rate by difficulty
    by_diff: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_diff[r.get("difficulty", "unknown")].append(r)

    lines.append("### Pass rate by difficulty")
    for diff in ["easy", "medium", "hard", "unknown"]:
        if diff not in by_diff:
            continue
        dr = by_diff[diff]
        lines.append(f"  {diff:<10} {_pass_rate(dr):>5}%  ({sum(1 for r in dr if r.get('final_pass'))}/{len(dr)})")
    lines.append("")

    # Token cost summary (RULE-EVL03)
    lines.append("### Token cost baseline (per mode averages)")
    lines.append("These numbers inform the mode router decision in a later sprint.")
    lines.append(f"{'Mode':<20} {'Avg input_tokens':>18} {'Avg output_tokens':>19} {'Avg duration_ms':>16}")
    lines.append("-" * 75)
    for mode in sorted(by_mode):
        mode_results = by_mode[mode]
        avg_in = _avg([r["input_tokens"] for r in mode_results])
        avg_out = _avg([r["output_tokens"] for r in mode_results])
        avg_dur = _avg([r["duration_ms"] for r in mode_results])
        lines.append(f"{mode:<20} {avg_in:>18} {avg_out:>19} {avg_dur:>16}")
    lines.append("")

    # Retrieval mechanics (only meaningful when memory tools were called)
    total_sm_calls = sum(r.get("search_memory_calls", 0) for r in results)
    if total_sm_calls > 0:
        lines.append("### Retrieval mechanics")
        avg_sm = _avg([r.get("search_memory_calls", 0) for r in results])
        avg_sm_ms = _avg([r.get("search_memory_avg_ms", 0.0) for r in results if r.get("search_memory_calls", 0) > 0])
        avg_sm_results = _avg([r.get("search_results_returned", 0) for r in results if r.get("search_memory_calls", 0) > 0])
        avg_am = _avg([r.get("add_memory_calls", 0) for r in results])
        avg_llm = _avg([r.get("llm_call_count", 0) for r in results])
        lines.append(f"  search_memory calls/case:   {avg_sm}")
        lines.append(f"  search_memory avg latency:  {avg_sm_ms} ms")
        lines.append(f"  search results returned:    {avg_sm_results} avg/search")
        lines.append(f"  add_memory calls/case:      {avg_am}")
        lines.append(f"  LLM calls/case:             {avg_llm}")
        lines.append("")

    # Failures
    failures = [r for r in results if not r.get("final_pass")]
    if failures:
        lines.append(f"### Failures ({len(failures)})")
        for r in failures[:10]:
            det = "" if r["deterministic_pass"] else " [det]"
            verdict = r.get("judge_verdict", "?")
            reason = r.get("judge_reason", "")[:80]
            lines.append(f"  {r['id']:<35} {verdict}{det}  {reason}")
        if len(failures) > 10:
            lines.append(f"  ... and {len(failures) - 10} more")

    return "\n".join(lines)


def provider_comparison(entries: list[tuple[str, dict]]) -> str:
    """
    Table 1: Quality + cost per provider.
    Table 2: Retrieval mechanics per provider.
    """
    lines = ["## Provider comparison — Quality + Cost"]
    lines.append(
        f"{'Provider':<22} {'Cases':>6} {'Pass%':>7} {'Avg in_tok':>11} "
        f"{'Avg out_tok':>12} {'Avg ms':>8} {'Fixture user':<20}"
    )
    lines.append("-" * 92)
    for label, data in entries:
        results = data["results"]
        pr = _pass_rate(results)
        avg_in = _avg([r["input_tokens"] for r in results])
        avg_out = _avg([r["output_tokens"] for r in results])
        avg_ms = _avg([r["duration_ms"] for r in results])
        fixture_user = data.get("fixture_user") or "none"
        lines.append(
            f"{label:<22} {len(results):>6} {pr:>6}% {avg_in:>11} "
            f"{avg_out:>12} {avg_ms:>8} {fixture_user:<20}"
        )

    lines.append("")
    lines.append("## Provider comparison — Retrieval Mechanics")
    has_retrieval = any(
        sum(r.get("search_memory_calls", 0) for r in data["results"]) > 0
        for _, data in entries
    )
    if not has_retrieval:
        lines.append("  (no search_memory calls recorded — run with --seed-memories to populate)")
        return "\n".join(lines)

    lines.append(
        f"{'Provider':<22} {'search/case':>12} {'avg ms/search':>14} "
        f"{'results/search':>15} {'add/case':>10} {'LLM/case':>10} {'Isolation':>10} {'TTL':>6}"
    )
    lines.append("-" * 105)
    for label, data in entries:
        results = data["results"]
        sm_calls = [r.get("search_memory_calls", 0) for r in results]
        sm_ms = [r.get("search_memory_avg_ms", 0.0) for r in results if r.get("search_memory_calls", 0) > 0]
        sm_res = [r.get("search_results_returned", 0) for r in results if r.get("search_memory_calls", 0) > 0]
        am_calls = [r.get("add_memory_calls", 0) for r in results]
        llm_calls = [r.get("llm_call_count", 0) for r in results]

        iso = data.get("isolation_result")
        iso_str = "PASS" if iso and iso["passed"] else ("FAIL" if iso else "n/a")

        ttl = data.get("ttl_result")
        ttl_val = ttl.get("ttl_respected") if ttl else None
        ttl_str = "PASS" if ttl_val else ("FAIL" if ttl_val is False else "n/a")

        lines.append(
            f"{label:<22} {_avg(sm_calls):>12} {_avg(sm_ms):>14} "
            f"{_avg(sm_res):>15} {_avg(am_calls):>10} {_avg(llm_calls):>10} {iso_str:>10} {ttl_str:>6}"
        )

    return "\n".join(lines)


_FRESHNESS_WINDOW_DAYS = 180  # 6 months
_DIFFICULTY_TARGETS = {"easy": 0.40, "medium": 0.30, "hard": 0.20, "adversarial": 0.10}


def dataset_health(dataset_path: str) -> str:
    """
    A10: Dataset health report.
    Reports coverage rate, freshness (% with created_date within 6 months),
    and tag distribution vs. target (40/30/20/10 easy/medium/hard/adversarial).
    """
    p = Path(dataset_path)
    data = yaml.safe_load(p.read_text())
    cases = data.get("cases", [])
    total = len(cases)
    if not total:
        return f"## Dataset health: {p.name}\nNo cases found."

    lines = [f"## Dataset health: {p.name}"]
    lines.append(f"Total cases: {total}")
    lines.append("")

    # Coverage rate: % of cases that have all required fields
    required_fields = {"id", "mode", "difficulty", "input", "expected_output", "must_not_contain"}
    complete = sum(1 for c in cases if required_fields.issubset(c.keys()))
    coverage_pct = round(complete / total * 100, 1)
    status = "OK" if coverage_pct == 100.0 else "WARN"
    lines.append(f"Coverage rate: {complete}/{total} ({coverage_pct}%)  [{status}]")
    if complete < total:
        missing = [c["id"] for c in cases if not required_fields.issubset(c.keys())]
        lines.append(f"  Missing required fields: {missing}")
    lines.append("")

    # Freshness: % of cases with created_date within 6 months
    cutoff = date.today() - timedelta(days=_FRESHNESS_WINDOW_DAYS)
    with_date = [c for c in cases if c.get("created_date")]
    fresh = [
        c for c in with_date
        if date.fromisoformat(str(c["created_date"])) >= cutoff
    ]
    if with_date:
        fresh_pct = round(len(fresh) / total * 100, 1)
        stale_pct = round((len(with_date) - len(fresh)) / total * 100, 1)
        no_date_pct = round((total - len(with_date)) / total * 100, 1)
        fresh_status = "OK" if fresh_pct >= 80.0 else "WARN"
        lines.append(f"Freshness (within {_FRESHNESS_WINDOW_DAYS}d): {len(fresh)}/{total} ({fresh_pct}%)  [{fresh_status}]")
        if stale_pct > 0:
            lines.append(f"  Stale (older than cutoff {cutoff}): {stale_pct}%")
        if no_date_pct > 0:
            lines.append(f"  No created_date: {no_date_pct}%  (backfill with created_date: YYYY-MM-DD)")
    else:
        lines.append(f"Freshness: no created_date fields found — backfill needed  [WARN]")
    lines.append("")

    # Tag distribution: difficulty breakdown vs. target
    by_diff: dict[str, int] = defaultdict(int)
    for c in cases:
        d = c.get("difficulty", "unknown")
        by_diff[d] += 1

    lines.append("Tag distribution (difficulty):")
    lines.append(f"  {'Tag':<14} {'Count':>6} {'Actual%':>8} {'Target%':>8} {'Status':>8}")
    lines.append("  " + "-" * 48)
    for tag in ["easy", "medium", "hard", "adversarial", "unknown"]:
        count = by_diff.get(tag, 0)
        if count == 0 and tag == "unknown":
            continue
        actual_pct = round(count / total * 100, 1)
        target_pct = round(_DIFFICULTY_TARGETS.get(tag, 0.0) * 100, 1)
        if tag in _DIFFICULTY_TARGETS:
            gap = abs(actual_pct - target_pct)
            tag_status = "OK" if gap <= 10.0 else "WARN"
        else:
            tag_status = "NOTE"
        lines.append(f"  {tag:<14} {count:>6} {actual_pct:>7}% {target_pct:>7}% {tag_status:>8}")
    lines.append("")

    # Mode coverage: which modes are represented
    by_mode: dict[str, int] = defaultdict(int)
    for c in cases:
        by_mode[c.get("mode", "unknown")] += 1
    lines.append("Mode distribution:")
    for mode, count in sorted(by_mode.items()):
        lines.append(f"  {mode:<20} {count:>4} cases ({round(count/total*100, 1)}%)")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Conductor eval report")
    parser.add_argument(
        "--results", nargs="+",
        help="Judged results JSON file(s). Append :label to name each (e.g. run.json:redis-alice)."
    )
    parser.add_argument("--health", default=None,
                        help="Path to a YAML dataset file — print health report and exit.")
    parser.add_argument("--output", default=None,
                        help="Write report to this file (prints to stdout if omitted)")
    args = parser.parse_args()

    if args.health:
        print(dataset_health(args.health))
        return

    if not args.results:
        parser.error("--results is required unless --health is used")

    entries = []
    for spec in args.results:
        if ":" in spec:
            path, label = spec.rsplit(":", 1)
        else:
            path, label = spec, Path(spec).stem
        with open(path) as f:
            data = json.load(f)
        entries.append((label, data))

    sections = []
    for label, data in entries:
        sections.append(generate_report(data, label=label))

    if len(entries) > 1:
        sections.append(provider_comparison(entries))

    report = "\n\n".join(sections)

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(report)
        print(f"Report written to {args.output}")
    else:
        print(report)


if __name__ == "__main__":
    main()
