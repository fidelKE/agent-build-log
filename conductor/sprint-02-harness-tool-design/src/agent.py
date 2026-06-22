"""
Conductor agent harness — Sprint 1.

Implements a ReAct loop (Reason + Act) over the tool registry.
Pattern choice: ReAct — Conductor's Troubleshooting mode requires dynamic,
observation-dependent branching (next step depends on what the previous tool returned).

Loop contract:
  1. Send messages + tool schemas to the model
  2. If model returns tool_use → dispatch to registry → append result → loop
  3. If model returns text with no tool_use → final answer, exit
  4. If step count hits MAX_ITERATIONS → exit with limit_reached status

Conductor mode: Troubleshooting (dynamic branching) and Q&A (single-tool lookup).
"""

import json
import os
import time

import anthropic
from dotenv import load_dotenv

from state import RunState, RunStatus, StepRecord
from tools import TOOL_REGISTRY, ToolError, SCHEMA_VERSION

# Load .env from the sprint directory (one level up from src/)
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

MAX_ITERATIONS = 8
MODEL = "claude-haiku-4-5-20251001"

SYSTEM_PROMPT = """You are Conductor, a technical co-pilot for data integration.

You help users with:
- Setting up connectors and integrations step by step
- Troubleshooting connection failures and errors
- Answering how-to questions from the knowledge base

Rules:
- Use the notes_search tool to look up relevant guidance before answering
- If you cannot find relevant information, say so clearly — do not invent answers
- Never ask for credentials or passwords
- Keep answers concise and actionable
- If the question is outside data integration, say you can only help with integration topics
"""


_TOOL_USE_KEYS = {"id", "type", "name", "input"}
_TEXT_KEYS = {"type", "text"}


def _normalize_block(block: dict) -> dict:
    """Strip gateway-injected fields the API rejects on replay."""
    if block.get("type") == "tool_use":
        return {k: v for k, v in block.items() if k in _TOOL_USE_KEYS}
    if block.get("type") == "text":
        return {k: v for k, v in block.items() if k in _TEXT_KEYS}
    return block


def _log(run: RunState, event: str, data: dict) -> None:
    """Emit a structured JSON log line to stdout."""
    print(json.dumps({"run_id": run.run_id, "step_id": run.step_count, "event": event, **data}))


def run(user_message: str) -> RunState:
    """
    Run the agent loop for a single user message.

    Returns the completed RunState with all steps recorded.
    """
    client = anthropic.Anthropic(
        api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        base_url=os.environ.get("LLM_GATEWAY_URL", "https://api.anthropic.com"),
    )
    state = RunState()
    messages: list[dict] = [{"role": "user", "content": user_message}]
    tools = [{k: v for k, v in entry["schema"].items() if k != "version"} for entry in TOOL_REGISTRY.values()]

    _log(state, "run_start", {"message": user_message})

    while state.step_count < MAX_ITERATIONS:
        if state.step_count >= MAX_ITERATIONS:
            break

        t_start = time.monotonic()

        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
        )

        duration_ms = round((time.monotonic() - t_start) * 1000, 1)

        # No tool call → final answer
        if response.stop_reason == "end_turn":
            answer = next(
                (block.text for block in response.content if hasattr(block, "text")),
                "",
            )
            state.record_step(StepRecord(
                step=state.step_count + 1,
                tool_name=None,
                tool_input=None,
                tool_output=answer,
                duration_ms=duration_ms,
                status="no_tool",
            ))
            state.final_answer = answer
            state.status = RunStatus.COMPLETED
            _log(state, "final_answer", {"answer": answer, "duration_ms": duration_ms})
            break

        # Tool call → dispatch all tool_use blocks (RULE-A03, RULE-A04)
        if response.stop_reason == "tool_use":
            tool_blocks = [b for b in response.content if b.type == "tool_use"]
            tool_results = []

            for tool_block in tool_blocks:
                tool_name = tool_block.name
                tool_input = tool_block.input
                tool_use_id = tool_block.id

                _log(state, "tool_call", {"tool": tool_name, "input": tool_input})

                if tool_name not in TOOL_REGISTRY:
                    tool_result = {"error": True, "error_code": "UNKNOWN_TOOL", "message": f"Tool '{tool_name}' not registered", "retryable": False}
                    tool_duration_ms = 0.0
                else:
                    # A2: Dispatch-time schema version check (RULE-T04).
                    # If the call carries a schema_version that doesn't match the
                    # registered version, reject before execution. Mismatch means
                    # the model was given a stale schema — retrying with the same
                    # input won't fix it, so retryable=False.
                    call_version = tool_input.get("schema_version") if isinstance(tool_input, dict) else None
                    registered_version = TOOL_REGISTRY[tool_name]["schema"].get("version")
                    if call_version is not None and call_version != registered_version:
                        tool_result = ToolError(
                            error_code="SCHEMA_MISMATCH",
                            message=(
                                f"Tool '{tool_name}' schema version mismatch: "
                                f"call carries '{call_version}', registered is '{registered_version}'"
                            ),
                            retryable=False,
                        ).to_dict()
                        tool_duration_ms = 0.0
                    else:
                        t_tool = time.monotonic()
                        tool_result = TOOL_REGISTRY[tool_name]["fn"](tool_input)
                        tool_duration_ms = round((time.monotonic() - t_tool) * 1000, 1)

                is_error = bool(tool_result.get("error"))
                is_retryable = tool_result.get("retryable", True) if is_error else True
                status = "error" if is_error else "success"
                state.record_step(StepRecord(
                    step=state.step_count,
                    tool_name=tool_name,
                    tool_input=tool_input,
                    tool_output=tool_result,
                    duration_ms=tool_duration_ms,
                    status=status,
                ))
                _log(state, "tool_result", {"tool": tool_name, "status": status, "result": tool_result, "duration_ms": tool_duration_ms, "retryable": is_retryable})

                # Annotate non-retryable errors in the tool result content so the model
                # knows retrying with the same input will not succeed. The model reads
                # this as part of its context and should escalate or rephrase rather than retry.
                content = tool_result.copy()
                if is_error and not is_retryable:
                    content["_hint"] = "This error is not retryable. Do not retry with the same input."
                tool_results.append({"type": "tool_result", "tool_use_id": tool_use_id, "content": json.dumps(content)})

            # Serialize response.content to plain dicts before appending (RULE-A04)
            serialised_content = [
                _normalize_block(b.model_dump() if hasattr(b, "model_dump") else b)
                for b in response.content
            ]
            messages.append({"role": "assistant", "content": serialised_content})
            messages.append({"role": "user", "content": tool_results})
            continue

        # Unexpected stop reason
        state.status = RunStatus.ERROR
        _log(state, "unexpected_stop", {"stop_reason": response.stop_reason})
        break

    if state.status == RunStatus.RUNNING:
        state.status = RunStatus.LIMIT_REACHED
        _log(state, "limit_reached", {"max_iterations": MAX_ITERATIONS})

    return state


if __name__ == "__main__":
    import sys
    query = " ".join(sys.argv[1:]) or "How do I connect Snowflake?"
    result = run(query)
    print("\n---")
    print(f"Status : {result.status}")
    print(f"Steps  : {result.step_count}")
    print(f"Answer : {result.final_answer}")
