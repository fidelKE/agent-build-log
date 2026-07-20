"""
LLM-as-judge for Conductor eval — Sprint 4.

Deterministic checks run in runner.py before this file is called (RULE-EVL02).
Cases that already failed deterministic checks are marked FAIL here without
calling the LLM — the judge is only invoked for cases that passed det checks.

The judge evaluates the agent's output against the case's expected_output criteria
using a structured rubric. It returns PASS or FAIL with a one-sentence reason.

Usage:
  UV_PROJECT_ENVIRONMENT=../.venv uv run python -m eval.judge \\
      --results results/run-generic.json \\
      --output results/run-generic-judged.json
"""

import argparse
import json
import os
import time
from pathlib import Path

import anthropic
from dotenv import load_dotenv

JUDGE_MODEL = "claude-haiku-4-5-20251001"

JUDGE_SYSTEM = """You are an objective evaluator for an AI agent called Conductor.
Conductor helps users with data integration: setting up connectors, troubleshooting
failures, onboarding, and answering technical questions.

You will be given:
- The user's question
- The agent's response
- Expected output criteria (what a good response must include)

Return a JSON object with exactly two fields:
  "verdict": "PASS" or "FAIL"
  "reason": one sentence explaining the verdict

PASS if the response satisfies the key_decision criterion and addresses the user's need.
FAIL if the response is factually wrong, hallucinates, refuses when it should help,
or violates a must_not_contain constraint that was missed by deterministic checks.
Be strict. A vague answer that doesn't commit to actionable steps is a FAIL."""


def _build_judge_prompt(case: dict, output: str) -> str:
    expected = case.get("expected_output", {})
    # Dataset v1: expected_output is a plain list of strings
    # Dataset v2 (Atlan): expected_output is a dict with must_include / key_decision
    if isinstance(expected, list):
        must_include = expected
        key_decision = ""
    else:
        must_include = expected.get("must_include", [])
        key_decision = expected.get("key_decision", "")

    lines = [
        f"USER QUESTION: {case['input']}",
        "",
        f"AGENT RESPONSE:\n{output}",
        "",
        "EVALUATION CRITERIA:",
    ]
    if key_decision:
        lines.append(f"  Key decision (primary check): {key_decision}")
    if must_include:
        lines.append("  Must include:")
        for item in must_include:
            lines.append(f"    - {item}")
    lines.append("")
    lines.append("Return JSON only: {\"verdict\": \"PASS\" or \"FAIL\", \"reason\": \"...\"}")
    return "\n".join(lines)


def _judge_case(case: dict, output: str, client: anthropic.Anthropic) -> tuple[str, str]:
    """Call LLM judge. Returns (verdict, reason)."""
    prompt = _build_judge_prompt(case, output)
    try:
        response = client.messages.create(
            model=JUDGE_MODEL,
            max_tokens=256,
            system=JUDGE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        # Extract JSON from response
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(text[start:end])
            verdict = data.get("verdict", "FAIL").upper()
            reason = data.get("reason", "No reason given")
            if verdict not in ("PASS", "FAIL"):
                verdict = "FAIL"
            return verdict, reason
        return "FAIL", f"Judge returned unparseable response: {text[:100]}"
    except Exception as exc:
        return "FAIL", f"Judge error: {exc}"


def judge_results(results_path: str, output_path: str) -> dict:
    with open(results_path) as f:
        data = json.load(f)

    load_dotenv()
    client = anthropic.Anthropic(base_url=os.environ["LLM_GATEWAY_URL"])

    # Rebuild case lookup from dataset for judge prompts
    dataset_path = data.get("dataset", "")
    cases_by_id: dict[str, dict] = {}
    if dataset_path and Path(dataset_path).exists():
        import yaml
        with open(dataset_path) as f:
            ds = yaml.safe_load(f)
        for c in ds.get("cases", []):
            cases_by_id[c["id"]] = c

    results = data["results"]
    total = len(results)
    judged = 0

    for i, result in enumerate(results, 1):
        case_id = result["id"]

        # Already failed deterministic check — skip judge (RULE-EVL02)
        if not result["deterministic_pass"]:
            result["judge_verdict"] = "SKIP"
            result["judge_reason"] = "failed deterministic check"
            result["final_pass"] = False
            continue

        if result.get("error"):
            result["judge_verdict"] = "FAIL"
            result["judge_reason"] = f"agent error: {result['error']}"
            result["final_pass"] = False
            continue

        case = cases_by_id.get(case_id, {"input": result["input"], "expected_output": {}})
        print(f"  [{i}/{total}] judging {case_id} ...", end=" ", flush=True)
        verdict, reason = _judge_case(case, result["output"], client)
        result["judge_verdict"] = verdict
        result["judge_reason"] = reason
        result["final_pass"] = (verdict == "PASS")
        print(verdict)
        judged += 1

    data["judged_at"] = time.time()

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(data, f, indent=2)

    passed = sum(1 for r in results if r.get("final_pass"))
    print(f"\nJudged {judged} cases. {passed}/{total} passed.")
    print(f"Judged results written to {output_path}")
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description="Conductor eval judge")
    parser.add_argument("--results", required=True,
                        help="Raw results JSON from eval/runner.py")
    parser.add_argument("--output", required=True,
                        help="Output path for judged results JSON")
    args = parser.parse_args()
    judge_results(args.results, args.output)


if __name__ == "__main__":
    main()
