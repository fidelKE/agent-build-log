"""
Skill description optimizer (Phase 3 -- Triggering evaluation).

Iterates on a skill description to maximize trigger rate on a held-out test set.
Uses a 60/40 train/test split to prevent overfitting on the training queries.
Winner is selected on test score, not train score.

Usage:
    python -m scripts.run_loop \\
        --eval-set conductor/evals/trigger-evals/troubleshoot-trigger-eval.json \\
        --skill-path conductor/sprint-06-claude-sdk-skills/.claude/skills/conductor-troubleshoot-connector \\
        --model claude-haiku-4-5-20251001 \\
        --max-iterations 5 \\
        --verbose

Algorithm per iteration:
  1. Split eval set 60/40 train/test (fixed random seed for reproducibility).
  2. Run each query in the current eval set 3 times (non-determinism -- passes if >=2/3 trigger).
  3. Compute train trigger rate and test trigger rate.
  4. If test score improved over prior best, save description as candidate winner.
  5. Ask Claude to propose an improved description based on train failures.
  6. Write proposed description to SKILL.md, repeat up to max_iterations.
  7. Restore the best description (by test score) when done.

Trigger detection uses real SDK dispatch: sdk_query with setting_sources=["project"]
and cwd pointing to the sprint folder. A trigger is confirmed when AssistantMessage
contains a ToolUseBlock with name="Skill" -- the same signal the live agent uses.

Output:
  Prints iteration-by-iteration scores. Final winner written back to SKILL.md.
  Results saved to <skill_path>/optimization-results.json.
"""

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

import anthropic


def _load_eval_set(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def _split_eval(queries: list[dict], seed: int = 42) -> tuple[list[dict], list[dict]]:
    """60/40 train/test split, fixed seed."""
    import random
    rng = random.Random(seed)
    shuffled = queries[:]
    rng.shuffle(shuffled)
    n_train = int(len(shuffled) * 0.6)
    return shuffled[:n_train], shuffled[n_train:]


def _read_skill_description(skill_path: Path) -> str:
    skill_md = skill_path / "SKILL.md"
    content = skill_md.read_text()
    m = re.search(r"^description:\s*(.+)$", content, re.MULTILINE)
    if not m:
        raise ValueError(f"No description: field found in {skill_md}")
    return m.group(1).strip()


def _write_skill_description(skill_path: Path, new_description: str) -> None:
    skill_md = skill_path / "SKILL.md"
    content = skill_md.read_text()
    updated = re.sub(
        r"^(description:\s*)(.+)$",
        lambda m: m.group(1) + new_description,
        content,
        flags=re.MULTILINE,
    )
    skill_md.write_text(updated)


async def _run_query_once(sprint_cwd: str, query: str, skill_name: str, verbose: bool) -> bool:
    """
    Returns True if the skill triggered for this query.

    Uses real SDK dispatch: sdk_query with setting_sources=["project"] and cwd
    pointing to the sprint directory so .claude/skills/ is resolved. A trigger is
    detected when AssistantMessage contains a ToolUseBlock with name="Skill".
    """
    from claude_agent_sdk import (  # type: ignore[import]
        query as sdk_query,
        ClaudeAgentOptions,
        AssistantMessage,
        ToolUseBlock,
    )

    options = ClaudeAgentOptions(
        system_prompt=(
            "You are a helpful assistant. Answer user questions directly. "
            "When you have a skill available that matches the user's request, use it."
        ),
        setting_sources=["project"],
        permission_mode="dontAsk",
        cwd=sprint_cwd,
        max_turns=3,
    )

    triggered = False
    try:
        async for msg in sdk_query(prompt=query, options=options):
            if isinstance(msg, AssistantMessage):
                for block in (msg.content or []):
                    if isinstance(block, ToolUseBlock) and block.name == "Skill":
                        triggered = True
                        break
            if triggered:
                break  # stop consuming — trigger detected, skill execution result irrelevant
    except Exception:
        pass  # SDK may raise after skill dispatch if skill has no tools; trigger state already captured

    if verbose:
        status = "TRIGGERED" if triggered else "not triggered"
        print(f"    [{status}] {query[:70]}...")
    return triggered


async def _evaluate_set(
    sprint_cwd: str,
    queries: list[dict],
    skill_name: str,
    runs_per_query: int,
    verbose: bool,
) -> tuple[float, list[dict]]:
    """
    Returns (trigger_rate, detailed_results).
    A query passes if trigger rate >= 0.5 across runs_per_query runs.
    """
    results = []
    for q in queries:
        trigger_count = 0
        for _ in range(runs_per_query):
            if await _run_query_once(sprint_cwd, q["query"], skill_name, verbose):
                trigger_count += 1
        actual_triggered = trigger_count >= (runs_per_query / 2)
        correct = actual_triggered == q["should_trigger"]
        results.append({
            "id": q["id"],
            "query": q["query"],
            "should_trigger": q["should_trigger"],
            "actual_triggered": actual_triggered,
            "trigger_count": trigger_count,
            "correct": correct,
        })
    score = sum(r["correct"] for r in results) / len(results) if results else 0.0
    return score, results


def _propose_new_description(
    client: anthropic.Anthropic,
    current_description: str,
    failures: list[dict],
    model: str,
) -> str:
    """
    Ask Claude to improve the description based on training failures.
    Hard constraint: <=500 chars (cross-provider rule, RULE-SKL01).
    """
    failure_lines = "\n".join(
        f"  - '{r['query']}' (should_trigger={r['should_trigger']}, got={r['actual_triggered']})"
        for r in failures
    )
    prompt = f"""You are optimizing a skill description to improve trigger accuracy.

Current description (must be improved):
{current_description}

Failures on training set:
{failure_lines}

Write a new description that fixes these failures. Rules:
- Must be <= 500 characters (hard limit for cross-provider compatibility)
- No workflow instructions in the description (those go in the skill body)
- Use keywords from the failing queries to help the model recognize when to trigger
- Do not add examples or lists -- plain prose only
- Return ONLY the new description text, nothing else

New description:"""

    response = client.messages.create(
        model=model,
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    proposed = "".join(b.text for b in response.content if hasattr(b, "text")).strip()
    # Hard enforce 500-char limit (RULE-SKL01)
    if len(proposed) > 500:
        proposed = proposed[:497] + "..."
    return proposed


async def run_optimization_loop(
    eval_set_path: str,
    skill_path: str,
    model: str,
    max_iterations: int,
    runs_per_query: int,
    verbose: bool,
) -> dict:
    client = anthropic.Anthropic(base_url=os.environ["LLM_GATEWAY_URL"])
    skill_dir = Path(skill_path).resolve()
    eval_data = _load_eval_set(eval_set_path)
    queries = eval_data["queries"]
    skill_name = eval_data["skill"]

    # cwd must be the sprint folder that contains .claude/skills/
    # skill_dir is .../sprint-06-claude-sdk-skills/.claude/skills/<skill-name>
    sprint_cwd = str(skill_dir.parents[2])

    train_set, test_set = _split_eval(queries)
    print(f"Eval set: {len(queries)} queries ({len(train_set)} train / {len(test_set)} test)")
    print(f"Sprint cwd: {sprint_cwd}")

    best_description = _read_skill_description(skill_dir)
    best_test_score = 0.0
    history = []

    for iteration in range(max_iterations):
        current_description = _read_skill_description(skill_dir)
        print(f"\n--- Iteration {iteration + 1}/{max_iterations} ---")
        print(f"Description ({len(current_description)} chars): {current_description[:80]}...")

        print("Evaluating train set...")
        train_score, train_results = await _evaluate_set(
            sprint_cwd, train_set, skill_name, runs_per_query, verbose
        )
        print(f"Train score: {train_score:.2%}")

        print("Evaluating test set...")
        test_score, test_results = await _evaluate_set(
            sprint_cwd, test_set, skill_name, runs_per_query, verbose
        )
        print(f"Test score: {test_score:.2%}")

        history.append({
            "iteration": iteration + 1,
            "description": current_description,
            "train_score": train_score,
            "test_score": test_score,
            "train_results": train_results,
            "test_results": test_results,
        })

        if test_score > best_test_score:
            best_test_score = test_score
            best_description = current_description
            print(f"New best test score: {best_test_score:.2%}")

        if iteration < max_iterations - 1:
            train_failures = [r for r in train_results if not r["correct"]]
            if not train_failures:
                print("No train failures -- stopping early.")
                break
            print(f"{len(train_failures)} train failures -- proposing improved description...")
            new_description = _propose_new_description(
                client, current_description, train_failures, model
            )
            _write_skill_description(skill_dir, new_description)
            print(f"Proposed ({len(new_description)} chars): {new_description[:80]}...")

    # Restore best description
    print(f"\nRestoring best description (test score: {best_test_score:.2%})")
    _write_skill_description(skill_dir, best_description)

    results = {
        "skill": skill_name,
        "model": model,
        "best_test_score": best_test_score,
        "best_description": best_description,
        "iterations": len(history),
        "history": history,
    }
    output_path = skill_dir / "optimization-results.json"
    output_path.write_text(json.dumps(results, indent=2))
    print(f"\nResults saved to {output_path}")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Skill description optimizer")
    parser.add_argument("--eval-set", required=True, help="Path to trigger eval JSON")
    parser.add_argument("--skill-path", required=True, help="Path to skill directory")
    parser.add_argument("--model", default="claude-haiku-4-5-20251001")
    parser.add_argument("--max-iterations", type=int, default=5)
    parser.add_argument("--runs-per-query", type=int, default=3,
                         help="Runs per query (pass if >=50%% trigger)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    results = asyncio.run(run_optimization_loop(
        eval_set_path=args.eval_set,
        skill_path=args.skill_path,
        model=args.model,
        max_iterations=args.max_iterations,
        runs_per_query=args.runs_per_query,
        verbose=args.verbose,
    ))
    print(f"\nFinal best test score: {results['best_test_score']:.2%}")
    print(f"Best description: {results['best_description']}")


if __name__ == "__main__":
    main()
