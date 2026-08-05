"""
before_tool_callback / after_tool_callback for Conductor -- Sprint 6d (Google ADK).

RULE-ADK02: every LlmAgent in this sprint wires before_tool_callback=; all path and
approval validation happens here, not inside individual tool functions in tools.py.

Two independent checks, either can block a call:
  1. Path allowlist -- any arg ending in path/file/filename must resolve inside
     CONDUCTOR_ALLOWED_ROOT. This is what the five-way benchmark's Repo Triage
     scenario exercises (a fake secret file that must not be read).
  2. Approval gate -- write_connector_config requires tool_context.state["approved"]
     to already be True. ADK has no interrupt_on=/checkpointer pause-and-resume
     primitive at this level (see results.md ceiling finding); the workaround is
     pre-authorization -- the caller sets approved=True in the initial session
     state before the run starts, instead of the run pausing mid-flight the way
     Deep Agents' interrupt_on= did in Lab 6c.

after_tool_callback pairs with before_tool_callback to log every tool call uniformly
(RULE-O01/O02), the ADK equivalent of Lab 6c's SetupStateMiddleware.wrap_tool_call --
one enforcement+logging layer wired on every agent instead of relying on each tool
function to self-log.

ToolCallGuard is instantiated once per run() call -- its start-time map is per-run
state, not a module-level global, so nothing leaks or collides across a process that
handles many runs sequentially (e.g. eval/runner.py iterating a whole dataset).
"""

import os
import time
from typing import Any


ALLOWED_ROOT = os.path.abspath(os.environ.get("CONDUCTOR_ALLOWED_ROOT", os.getcwd()))
_PATH_ARG_SUFFIXES = ("path", "file", "filename")
_APPROVAL_GATED_TOOLS = {"write_connector_config"}


def _blocked_path(args: dict[str, Any]) -> str | None:
    for key, value in args.items():
        if not key.lower().endswith(_PATH_ARG_SUFFIXES) or not isinstance(value, str):
            continue
        candidate = os.path.abspath(os.path.join(ALLOWED_ROOT, value))
        if not (candidate == ALLOWED_ROOT or candidate.startswith(ALLOWED_ROOT + os.sep)):
            return f"{value!r} resolves outside the allowed directory ({ALLOWED_ROOT})."
        basename = os.path.basename(value).lower()
        if "secret" in basename or basename == ".env":
            return f"refusing to read a suspected secret file ({value})."
    return None


class ToolCallGuard:
    """Per-run before/after_tool_callback pair. One instance per agent.py run() call."""

    def __init__(self, structured_logger):
        self._logger = structured_logger
        self._start_times: dict[str, float] = {}

    def before_tool_callback(self, tool, args: dict[str, Any], tool_context) -> dict | None:
        self._start_times[tool_context.function_call_id] = time.monotonic()

        reason = _blocked_path(args)
        if reason is None and tool.name in _APPROVAL_GATED_TOOLS and not tool_context.state.get("approved"):
            reason = (
                f"{tool.name} blocked: human approval required. Pass approved=True "
                "to run() (main.py --approve) before this call."
            )

        if reason is not None:
            block = {"error": True, "error_code": "TOOL_CALL_BLOCKED", "message": reason}
            self._logger.log_tool_call(
                step_id=tool_context.function_call_id, parent_step_id="run",
                tool_name=tool.name, tool_input=args, tool_output=block,
                duration_ms=0.0, status="error", error=reason,
            )
            self._start_times.pop(tool_context.function_call_id, None)
            return block

        return None

    def after_tool_callback(self, tool, args: dict[str, Any], tool_context, tool_response) -> None:
        t0 = self._start_times.pop(tool_context.function_call_id, None)
        duration_ms = round((time.monotonic() - t0) * 1000, 1) if t0 is not None else 0.0
        self._logger.log_tool_call(
            step_id=tool_context.function_call_id, parent_step_id="run",
            tool_name=tool.name, tool_input=args, tool_output=tool_response,
            duration_ms=duration_ms, status="success",
        )
        return None  # never modify the response
