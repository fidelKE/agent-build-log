"""
StructuredLogger — reusable observability layer for Conductor.

Every event is a JSON object written to a file sink (one line per event).
Schema is versioned — consumers check SCHEMA_VERSION before parsing.

OTel-compatible field names used throughout:
  gen_ai.usage.input_tokens, gen_ai.usage.output_tokens
  run_id    → maps to OTel trace_id
  step_id   → maps to OTel span_id
  parent_step_id → maps to OTel parent_span_id

Parallel / fan-out tracing:
  parent_step_id + dispatch_index make worker identity stable regardless of
  completion order. See docs/decisions/observability-trace-model.md.

trace_depth controls how deep to log workers:
  "boundary"  — input + output + status at the worker boundary only (default, production)
  "full"      — all internal steps within a worker (debugging / development)

Secret redaction happens at the logger layer — never at call sites.
Add patterns to _REDACT_PATTERNS to extend.
"""

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


SCHEMA_VERSION = "1.0"

# Patterns whose matched values are replaced with [REDACTED] in all log output.
# Extend this list as new credential shapes are introduced.
_REDACT_PATTERNS: list[re.Pattern] = [
    re.compile(r'(?i)(password|passwd|secret|token|api[_-]?key|credential|private[_-]?key)\s*[=:]\s*\S+'),
    re.compile(r'(?i)"(password|passwd|secret|token|api[_-]?key|credential)"\s*:\s*"[^"]*"'),
    re.compile(r'eyJ[A-Za-z0-9_-]{10,}'),   # JWT bearer tokens
]


class TraceDepth(str, Enum):
    BOUNDARY = "boundary"
    FULL = "full"


@dataclass
class StructuredLogger:
    """
    Write structured JSON events for one agent run.

    Usage:
        logger = StructuredLogger(run_id="...", sink_dir="logs/")
        logger.log_run_start(user_message="...")
        logger.log_llm_call(step_id="step-1", ...)
        logger.log_tool_call(step_id="step-2", ...)
        logger.log_run_end(...)
    """

    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    sink_dir: str = "logs"
    trace_depth: TraceDepth = TraceDepth.BOUNDARY
    agent_id: str = "conductor-v1"

    def __post_init__(self) -> None:
        os.makedirs(self.sink_dir, exist_ok=True)
        self._sink_path = os.path.join(self.sink_dir, f"{self.run_id}.jsonl")

    # ------------------------------------------------------------------
    # Public event methods
    # ------------------------------------------------------------------

    def log_run_start(self, user_message: str) -> None:
        self._write({
            "event": "run_start",
            "step_id": "run",
            "agent_id": self.agent_id,
            "user_message": self._redact(user_message),
        })

    def log_llm_call(
        self,
        step_id: str,
        parent_step_id: str | None,
        input_messages: list[dict],
        output_text: str,
        input_tokens: int,
        output_tokens: int,
        duration_ms: float,
        status: Literal["success", "error"] = "success",
        error: str | None = None,
    ) -> None:
        event: dict[str, Any] = {
            "event": "llm_call",
            "step_id": step_id,
            "parent_step_id": parent_step_id,
            "gen_ai.usage.input_tokens": input_tokens,
            "gen_ai.usage.output_tokens": output_tokens,
            "duration_ms": duration_ms,
            "status": status,
            "output": self._redact(output_text),
        }
        if self.trace_depth == TraceDepth.FULL:
            event["input_messages"] = self._redact_obj(input_messages)
        if error:
            event["error"] = error
        self._write(event)

    def log_tool_call(
        self,
        step_id: str,
        parent_step_id: str | None,
        tool_name: str,
        tool_input: dict,
        tool_output: Any,
        duration_ms: float,
        status: Literal["success", "error", "empty"] = "success",
        dispatch_index: int | None = None,
        error: str | None = None,
    ) -> None:
        event: dict[str, Any] = {
            "event": "tool_call",
            "step_id": step_id,
            "parent_step_id": parent_step_id,
            "tool.name": tool_name,
            "input": self._redact_obj(tool_input),
            "output": self._redact_obj(tool_output),
            "duration_ms": duration_ms,
            "status": status,
        }
        if dispatch_index is not None:
            event["dispatch_index"] = dispatch_index
        if error:
            event["error"] = error
        self._write(event)

    def log_fan_out(
        self,
        step_id: str,
        parent_step_id: str | None,
        worker_count: int,
        workers_dispatched: list[dict],
        merge_strategy: str,
    ) -> None:
        self._write({
            "event": "fan_out",
            "step_id": step_id,
            "parent_step_id": parent_step_id,
            "step_type": "fan_out",
            "worker_count": worker_count,
            "workers_dispatched": self._redact_obj(workers_dispatched),
            "merge_strategy": merge_strategy,
            "dispatched_at": self._now(),
        })

    def log_fan_out_complete(
        self,
        step_id: str,
        workers_returned: list[dict],
        selected: list[int],
    ) -> None:
        self._write({
            "event": "fan_out_complete",
            "step_id": step_id,
            "workers_returned": self._redact_obj(workers_returned),
            "selected": selected,
        })

    def log_run_end(
        self,
        status: str,
        final_answer: str | None,
        total_steps: int,
        total_duration_ms: float,
    ) -> None:
        self._write({
            "event": "run_end",
            "step_id": "run",
            "status": status,
            "final_answer": self._redact(final_answer or ""),
            "total_steps": total_steps,
            "total_duration_ms": total_duration_ms,
        })

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write(self, payload: dict) -> None:
        record = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "ts": self._now(),
            **payload,
        }
        with open(self._sink_path, "a") as f:
            f.write(json.dumps(record) + "\n")

    def _now(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int((time.time() % 1) * 1000):03d}Z"

    def _redact(self, value: str) -> str:
        for pattern in _REDACT_PATTERNS:
            value = pattern.sub("[REDACTED]", value)
        return value

    def _redact_obj(self, obj: Any) -> Any:
        """Recursively redact strings inside dicts and lists.
        SDK objects (e.g. ToolUseBlock) are converted via model_dump() first."""
        if hasattr(obj, "model_dump"):
            return self._redact_obj(obj.model_dump())
        if isinstance(obj, str):
            return self._redact(obj)
        if isinstance(obj, dict):
            return {k: self._redact_obj(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._redact_obj(item) for item in obj]
        return obj
