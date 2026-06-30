"""
Conductor agent harness — Sprint 3.

Cumulative changes from previous sprints:

Sprint 1: minimal ReAct loop — tool dispatch, RunState, iteration limit.
Sprint 2: prompt.py (soul + behavioral contract), StructuredLogger (OTel-aligned,
          schema-versioned, secret-redacting), token counts per LLM call,
          _extract_json() for reliable output parsing.
Sprint 3: credential injection — API token fetched from secret store at tool call
          time, never loaded into the model's context. Two-layer state persistence:
          Redis (Layer 1) holds message history as a fast cache with 1hr TTL.
          SQLite (Layer 3) holds message history as the durable fallback + step
          progress. Redis expiry does not kill resurrection — SQLite has the messages.

Loop contract:
  1. Load messages: Redis first (fast path), SQLite fallback (durable).
     Load step progress from SQLite checkpoint.
  2. Append new user message. Send full history + tool schemas to the model.
  3. If stop_reason == tool_use → dispatch all tool_use blocks (model may return
     several) → append results → save messages to Redis + SQLite, step to SQLite → loop.
  4. If stop_reason == end_turn → final answer, mark completed, save all layers, exit.
  5. If step count hits MAX_ITERATIONS → exit with limit_reached status.
"""

import hashlib
import json
import os
import time
import uuid

import anthropic
import yaml
from dotenv import load_dotenv

from .logger import StructuredLogger, TraceDepth
from .prompt import SYSTEM_PROMPT
from .secrets import make_secret_store
from .state import RunState, RunStatus, StepRecord, AgentState, CheckpointStore, SessionStore
from .tools import TOOL_SCHEMAS, ToolExecutor, ToolError, SCHEMA_VERSION

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

MAX_ITERATIONS = 8
MODEL = "claude-haiku-4-5-20251001"


def _extract_json(text: str) -> str:
    """
    Extract the first JSON object from model output.

    The output format constraint ("raw JSON only") is not 100% reliable — the model
    sometimes prepends prose or wraps in fences. Extracts the JSON object directly.
    Falls back to raw text if no JSON found so downstream callers get a clear error.
    """
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]
    return text


def _messages_are_valid(messages: list[dict]) -> bool:
    """
    Check that every tool_use block has a matching tool_result in the next message.
    The Anthropic API rejects histories with orphaned tool_use blocks (e.g. from a
    mid-turn crash). Better to discard the checkpoint than get a 400.
    """
    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        tool_use_ids = {b["id"] for b in content if isinstance(b, dict) and b.get("type") == "tool_use"}
        if not tool_use_ids:
            continue
        # Next message must be role=user with tool_result blocks covering all ids
        if i + 1 >= len(messages):
            return False
        next_content = messages[i + 1].get("content", [])
        if not isinstance(next_content, list):
            return False
        result_ids = {b.get("tool_use_id") for b in next_content if isinstance(b, dict) and b.get("type") == "tool_result"}
        if not tool_use_ids.issubset(result_ids):
            return False
    return True


_TOOL_USE_KEYS = {"id", "type", "name", "input"}
_TEXT_KEYS = {"type", "text"}


def _normalize_block(block: dict) -> dict:
    """Strip gateway-injected fields that the API rejects on replay (e.g. 'caller')."""
    if block.get("type") == "tool_use":
        return {k: v for k, v in block.items() if k in _TOOL_USE_KEYS}
    if block.get("type") == "text":
        return {k: v for k, v in block.items() if k in _TEXT_KEYS}
    return block


_BOM_PATH = os.path.join(os.path.dirname(__file__), "..", "agent-bom.yaml")


def _check_prompt_hash() -> None:
    """
    Verify soul.md hash against agent-bom.yaml at run startup (§59.6 runtime drift check).
    Logs a warning on mismatch rather than raising, so a stale BOM doesn't hard-break CI.
    The bom_validator.py tool is the hard gate; this is the runtime signal.
    """
    try:
        bom_path = os.path.abspath(_BOM_PATH)
        if not os.path.exists(bom_path):
            return
        with open(bom_path) as f:
            bom = yaml.safe_load(f)
        prompt_entry = bom.get("prompt", {})
        registered_hash = prompt_entry.get("sha256")
        prompt_file = os.path.join(os.path.dirname(bom_path), prompt_entry.get("file", ""))
        if not registered_hash or not os.path.exists(prompt_file):
            return
        actual_hash = hashlib.sha256(open(prompt_file, "rb").read()).hexdigest()
        if actual_hash != registered_hash:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "ABOM DRIFT: soul.md hash mismatch at runtime. "
                "Expected %s, got %s. Run bom_validator.py.",
                registered_hash[:16], actual_hash[:16],
            )
    except Exception:
        pass  # BOM check must never crash the agent


def run(
    user_message: str,
    session_id: str | None = None,
    task_id: str | None = None,
    log_dir: str = "logs",
    trace_depth: TraceDepth = TraceDepth.BOUNDARY,
    prefer_vault: bool = True,
    catalog_base_url: str | None = None,
    restart: bool = False,
) -> tuple[RunState, StructuredLogger]:
    """
    Run the agent loop for a single user message.

    Args:
        user_message:    The user's input.
        session_id:      Stable identifier for this user/session (for checkpointing).
        task_id:         Task identifier within the session (for checkpointing).
        log_dir:         Directory for JSONL trace files.
        trace_depth:     How much detail to log (BOUNDARY | FULL).
        prefer_vault:    Use Vault for secrets if available; fall back to env vars.
        catalog_base_url:  Base URL for the data catalog API. Reads CATALOG_BASE_URL if None.
        restart:         If True, clear any existing checkpoint and start from step 1.

    Returns:
        (RunState, StructuredLogger) — in-memory state and on-disk trace.
    """
    session_id = session_id or str(uuid.uuid4())
    task_id = task_id or "default"
    catalog_base_url = catalog_base_url or os.environ.get("CATALOG_BASE_URL", "")

    _check_prompt_hash()

    secret_store = make_secret_store(prefer_vault=prefer_vault)
    checkpoints = CheckpointStore()
    sessions = SessionStore()

    client = anthropic.Anthropic(base_url=os.environ["LLM_GATEWAY_URL"])
    state = RunState()
    structured_logger = StructuredLogger(run_id=state.run_id, sink_dir=log_dir, trace_depth=trace_depth)
    executor = ToolExecutor(
        secret_store=secret_store,
        catalog_base_url=catalog_base_url,
        structured_logger=structured_logger,
    )

    if restart:
        checkpoints.reset(session_id, task_id)
        sessions.delete(session_id, task_id)
        structured_logger._write({"event": "checkpoint_cleared", "step_id": "init",
                                  "session_id": session_id, "task_id": task_id})

    # Load message history: Redis first (fast), SQLite fallback (durable).
    # Redis TTL expiry does not kill resurrection — SQLite holds the same messages.
    checkpoint = checkpoints.load(session_id, task_id)
    saved_messages = sessions.load(session_id, task_id)
    source = "redis"
    if not saved_messages:
        saved_messages = checkpoints.load_messages(session_id, task_id)
        source = "sqlite"

    if saved_messages and _messages_are_valid(saved_messages):
        messages: list[dict] = saved_messages
        messages.append({"role": "user", "content": user_message})
        structured_logger._write({"event": "session_resumed", "step_id": "init",
                                  "session_id": session_id, "source": source,
                                  "resumed_from_step": checkpoint.current_step if checkpoint else 0})
    else:
        if saved_messages:
            # Messages exist but are malformed (orphaned tool_use blocks from a crash)
            sessions.delete(session_id, task_id)
            checkpoints.reset(session_id, task_id)
            checkpoint = None
            structured_logger._write({"event": "session_discarded", "step_id": "init",
                                      "session_id": session_id,
                                      "reason": "malformed message history (orphaned tool_use)"})
        messages = [{"role": "user", "content": user_message}]
        structured_logger._write({"event": "session_new", "step_id": "init",
                                  "session_id": session_id})

    tools = [{k: v for k, v in s.items() if k != "version"} for s in TOOL_SCHEMAS]
    t_run_start = time.monotonic()
    structured_logger.log_run_start(user_message=user_message)

    agent_state = checkpoint or AgentState(session_id=session_id, task_id=task_id,
                                           total_steps=MAX_ITERATIONS)

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

        # Final answer
        if response.stop_reason == "end_turn":
            answer = _extract_json(
                next((b.text for b in response.content if hasattr(b, "text")), "")
            )
            structured_logger.log_llm_call(
                step_id=step_id, parent_step_id=None, input_messages=messages,
                output_text=answer, input_tokens=input_tokens,
                output_tokens=output_tokens, duration_ms=duration_ms, status="success",
            )
            state.record_step(StepRecord(
                step=state.step_count + 1, tool_name=None, tool_input=None,
                tool_output=answer, duration_ms=duration_ms, status="no_tool",
            ))
            state.final_answer = answer
            state.status = RunStatus.COMPLETED

            agent_state.current_step += 1
            agent_state.status = "completed"
            checkpoints.save(agent_state)
            checkpoints.save_messages(session_id, task_id, messages)
            sessions.save(session_id, task_id, messages)
            break

        # Tool call — handle all tool_use blocks in the response (model may request several)
        if response.stop_reason == "tool_use":
            tool_blocks = [b for b in response.content if b.type == "tool_use"]

            structured_logger.log_llm_call(
                step_id=step_id, parent_step_id=None, input_messages=messages,
                output_text=f"[tool_use: {', '.join(b.name for b in tool_blocks)}]",
                input_tokens=input_tokens, output_tokens=output_tokens,
                duration_ms=duration_ms, status="success",
            )

            tool_results = []
            for tool_block in tool_blocks:
                tool_name = tool_block.name
                tool_input = tool_block.input
                tool_use_id = tool_block.id
                tool_step_id = f"{step_id}.tool-{tool_use_id[:8]}"

                t_tool = time.monotonic()
                call_version = tool_input.get("schema_version") if isinstance(tool_input, dict) else None
                registered_version = next(
                    (s.get("version") for s in TOOL_SCHEMAS if s["name"] == tool_name), None
                )
                if call_version is not None and call_version != registered_version:
                    tool_result = ToolError(
                        error_code="SCHEMA_MISMATCH",
                        message=(f"Tool '{tool_name}' schema version mismatch: "
                                 f"call carries '{call_version}', registered is '{registered_version}'"),
                        retryable=False,
                    ).to_dict()
                    tool_status = "error"
                else:
                    try:
                        tool_result = executor.execute(tool_name, tool_input, step_id=tool_step_id)
                        tool_status = "error" if tool_result.get("error") else "success"
                    except ValueError:
                        tool_result = ToolError(error_code="UNKNOWN_TOOL",
                                                message=f"Tool '{tool_name}' not registered",
                                                retryable=False).to_dict()
                        tool_status = "error"
                tool_duration_ms = round((time.monotonic() - t_tool) * 1000, 1)

                is_error = bool(tool_result.get("error"))
                is_retryable = tool_result.get("retryable", True) if is_error else True

                structured_logger.log_tool_call(
                    step_id=tool_step_id, parent_step_id=step_id,
                    tool_name=tool_name, tool_input=tool_input,
                    tool_output=tool_result, duration_ms=tool_duration_ms, status=tool_status,
                )
                state.record_step(StepRecord(
                    step=state.step_count, tool_name=tool_name, tool_input=tool_input,
                    tool_output=tool_result, duration_ms=tool_duration_ms, status=tool_status,
                ))
                content = tool_result.copy()
                if is_error and not is_retryable:
                    content["_hint"] = "This error is not retryable. Do not retry with the same input."
                tool_results.append({"type": "tool_result", "tool_use_id": tool_use_id,
                                     "content": json.dumps(content)})

            serialised_content = [
                _normalize_block(b.model_dump() if hasattr(b, "model_dump") else b)
                for b in response.content
            ]
            messages.append({"role": "assistant", "content": serialised_content})
            messages.append({"role": "user", "content": tool_results})

            agent_state.current_step += 1
            agent_state.completed_steps.append(agent_state.current_step)
            checkpoints.save(agent_state)
            checkpoints.save_messages(session_id, task_id, messages)
            sessions.save(session_id, task_id, messages)
            continue

        # Unexpected stop reason
        state.status = RunStatus.ERROR
        break

    if state.status == RunStatus.RUNNING:
        state.status = RunStatus.LIMIT_REACHED

    structured_logger.log_run_end(
        status=state.status.value,
        final_answer=state.final_answer,
        total_steps=state.step_count,
        total_duration_ms=round((time.monotonic() - t_run_start) * 1000, 1),
    )

    return state, structured_logger


