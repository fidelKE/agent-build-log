"""
Conductor agent harness — Sprint 3.

Changes from Sprint 1:
- SYSTEM_PROMPT replaced by prompt.py (soul + structured behavioral contract)
- inline _log() replaced by StructuredLogger (schema-versioned, OTel-aligned, secret-redacting)
- token counts captured from API response and logged per LLM call

Loop contract (unchanged from Sprint 1):
  1. Send messages + tool schemas to the model
  2. If model returns tool_use → dispatch to registry → append result → loop
  3. If model returns text with no tool_use → final answer, exit
  4. If step count hits MAX_ITERATIONS → exit with limit_reached status

Conductor mode: Troubleshooting.
"""

import json
import os
import sys
import time

import anthropic
from dotenv import load_dotenv

from state import RunState, RunStatus, StepRecord
from tools import TOOL_REGISTRY, ToolError, SCHEMA_VERSION

from logger import StructuredLogger, TraceDepth
from prompt import SYSTEM_PROMPT, SYSTEM_PROMPT_HASH

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

MAX_ITERATIONS = 8
MODEL = "claude-haiku-4-5-20251001"


_TOOL_USE_KEYS = {"id", "type", "name", "input"}
_TEXT_KEYS = {"type", "text"}


def _normalize_block(block: dict) -> dict:
    """Strip gateway-injected fields the API rejects on replay."""
    if block.get("type") == "tool_use":
        return {k: v for k, v in block.items() if k in _TOOL_USE_KEYS}
    if block.get("type") == "text":
        return {k: v for k, v in block.items() if k in _TEXT_KEYS}
    return block


def _extract_json(text: str) -> str:
    """
    Extract the first JSON object from model output.

    The output format constraint ("raw JSON only, no fences") is not 100%
    reliable — the model sometimes prepends prose or wraps output in fences.
    This extracts the JSON object directly rather than hoping the constraint holds.
    Falls back to the raw text if no JSON object is found, so downstream callers
    can still attempt a parse and surface a clear error.
    """
    text = text.strip()
    # Find first { and last } to extract the JSON object
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]
    return text


def run(
    user_message: str,
    log_dir: str = "logs",
    trace_depth: TraceDepth = TraceDepth.BOUNDARY,
) -> tuple[RunState, StructuredLogger]:
    """
    Run the agent loop for a single user message.

    Returns (RunState, StructuredLogger) so callers can inspect both the
    in-memory state and the on-disk trace.
    """
    client = anthropic.Anthropic(
        api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        base_url=os.environ.get("LLM_GATEWAY_URL", "https://api.anthropic.com"),
    )
    state = RunState()
    logger = StructuredLogger(run_id=state.run_id, sink_dir=log_dir, trace_depth=trace_depth)
    messages: list[dict] = [{"role": "user", "content": user_message}]
    tools = [{k: v for k, v in entry["schema"].items() if k != "version"} for entry in TOOL_REGISTRY.values()]
    t_run_start = time.monotonic()

    logger.log_run_start(user_message=user_message, gen_ai_system="anthropic")

    while state.step_count < MAX_ITERATIONS:
        step_id = f"step-{state.step_count + 1}"
        t_start = time.monotonic()

        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
        )

        duration_ms = round((time.monotonic() - t_start) * 1000, 1)
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens

        # No tool call → final answer
        if response.stop_reason == "end_turn":
            answer = _extract_json(next(
                (block.text for block in response.content if hasattr(block, "text")), ""
            ))
            logger.log_llm_call(
                step_id=step_id,
                parent_step_id=None,
                input_messages=messages,
                output_text=answer,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                duration_ms=duration_ms,
                status="success",
                model=MODEL,
                finish_reason=response.stop_reason,
                prompt_hash=SYSTEM_PROMPT_HASH,
                sampling_params={"max_tokens": 1024},
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
            break

        # Tool call → dispatch all tool_use blocks (RULE-A03, RULE-A04)
        if response.stop_reason == "tool_use":
            tool_blocks = [b for b in response.content if b.type == "tool_use"]

            logger.log_llm_call(
                step_id=step_id,
                parent_step_id=None,
                input_messages=messages,
                output_text=f"[tool_use: {', '.join(b.name for b in tool_blocks)}]",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                duration_ms=duration_ms,
                status="success",
                model=MODEL,
                finish_reason=response.stop_reason,
                prompt_hash=SYSTEM_PROMPT_HASH,
                sampling_params={"max_tokens": 1024},
            )

            tool_results = []
            for tool_block in tool_blocks:
                tool_name = tool_block.name
                tool_input = tool_block.input
                tool_use_id = tool_block.id
                tool_step_id = f"{step_id}.tool-{tool_use_id[:8]}"

                t_tool = time.monotonic()
                if tool_name not in TOOL_REGISTRY:
                    tool_result = ToolError(error_code="UNKNOWN_TOOL",
                                            message=f"Tool '{tool_name}' not registered",
                                            retryable=False).to_dict()
                    tool_status = "error"
                else:
                    # Dispatch-time schema version check (RULE-T04)
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
                        tool_status = "error"
                    else:
                        tool_result = TOOL_REGISTRY[tool_name]["fn"](tool_input)
                        tool_status = "error" if tool_result.get("error") else "success"
                tool_duration_ms = round((time.monotonic() - t_tool) * 1000, 1)

                is_error = bool(tool_result.get("error"))
                is_retryable = tool_result.get("retryable", True) if is_error else True

                logger.log_tool_call(
                    step_id=tool_step_id,
                    parent_step_id=step_id,
                    tool_name=tool_name,
                    tool_input=tool_input,
                    tool_output=tool_result,
                    duration_ms=tool_duration_ms,
                    status=tool_status,
                )
                state.record_step(StepRecord(
                    step=state.step_count,
                    tool_name=tool_name,
                    tool_input=tool_input,
                    tool_output=tool_result,
                    duration_ms=tool_duration_ms,
                    status=tool_status,
                ))
                content = tool_result.copy()
                if is_error and not is_retryable:
                    content["_hint"] = "This error is not retryable. Do not retry with the same input."
                tool_results.append({"type": "tool_result", "tool_use_id": tool_use_id,
                                     "content": json.dumps(content)})

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
        logger.log_run_end(
            status="error",
            final_answer=None,
            total_steps=state.step_count,
            total_duration_ms=round((time.monotonic() - t_run_start) * 1000, 1),
        )
        break

    if state.status == RunStatus.RUNNING:
        state.status = RunStatus.LIMIT_REACHED

    logger.log_run_end(
        status=state.status.value,
        final_answer=state.final_answer,
        total_steps=state.step_count,
        total_duration_ms=round((time.monotonic() - t_run_start) * 1000, 1),
    )

    return state, logger


if __name__ == "__main__":
    query = " ".join(sys.argv[1:]) or "How do I set up a Teradata connector?"
    state, logger = run(query, log_dir="logs")
    print("\n---")
    print(f"Status : {state.status}")
    print(f"Steps  : {state.step_count}")
    print(f"Answer : {state.final_answer}")
    print(f"Trace  : logs/{logger.run_id}.jsonl")
