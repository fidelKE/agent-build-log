"""
Conductor agent harness -- Sprint 6.

Sprint 6 changes from Sprint 5a:
  - while-True loop replaced with ClaudeSDKClient (RULE-SDK01)
  - Deterministic safety via PreToolUse / PermissionRequest hooks (RULE-SDK02)
  - SetupStateMachine state enforcement in PreToolUse and advanced in PostToolUse (RULE-STM01)
  - In-process MCP server via build_mcp_server(); tools named mcp__conductor__<name>
  - setting_sources=["project"] + cwd=<repo root> loads the shared conductor-troubleshoot-connector
    skill from .claude/skills/ at repo root (RULE-SKL01) -- same file 6a/6b/6c/6d read from
  - Session persistence adapted: prior messages formatted as history_context injected into
    system prompt (SDK subprocess manages internal conversation; Stop hook saves to Redis/SQLite)
  - Qdrant-only memory provider (Redis memory provider + Mem0 dropped)

Hook contract (RULE-SDK02):
  PreToolUse  -- deny via hookSpecificOutput.permissionDecision="deny"; allow by returning {}
  PermissionRequest -- {"decision": "allow"} or {"decision": "deny", "reason": ...}
  PostToolUse -- advance SetupStateMachine; return {}
  Stop        -- save messages to Redis + SQLite

HookMatcher: matcher= MUST be set on PreToolUse/PermissionRequest hooks.
PostToolUse with an empty matcher is allowed for cross-cutting observability (RULE-SDK02).
"""

import hashlib
import os
import time
import uuid
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .logger import StructuredLogger, TraceDepth
from .memory import make_memory_store
from .prompt import build_system_prompt
from .secrets import make_secret_store
from .state import (
    RunState, RunStatus, StepRecord,
    AgentState, CheckpointStore, SessionStore,
    SetupStateMachine,
)
from .tools import build_mcp_server

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"), override=False)
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", "..", ".env"), override=False)

MAX_TURNS = 8
MODEL = "claude-haiku-4-5-20251001"

# Bash patterns always blocked regardless of mode (RULE-SDK02)
BLOCKED_BASH_PATTERNS = [
    "rm -rf",
    "chmod 777",
    "| bash",   # catches curl <url> | bash, wget <url> | bash, and variations
    "| sh",     # same pattern for sh
    "eval ",
    "sudo ",
    "dd if=",
    ":(){:|:&};:",
]

# Tools that require human approval before execution (RULE-SDK02, RULE-STM01)
HITL_TOOLS: frozenset[str] = frozenset({"mcp__conductor__write_connector_config"})

_BOM_PATH = os.path.join(os.path.dirname(__file__), "..", "agent-bom.yaml")

# Repo root -- cwd for ClaudeAgentOptions so setting_sources=["project"] resolves
# .claude/skills/ at the shared root, same directory 6a/6b/6c/6d read from (RULE-SKL01).
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _check_prompt_hash() -> str | None:
    """Runtime drift check -- returns warning string on soul.md hash mismatch, else None."""
    try:
        bom_path = os.path.abspath(_BOM_PATH)
        if not os.path.exists(bom_path):
            return None
        with open(bom_path) as f:
            bom = yaml.safe_load(f)
        prompt_entry = bom.get("prompt", {})
        registered_hash = prompt_entry.get("sha256")
        prompt_file = os.path.join(os.path.dirname(bom_path), prompt_entry.get("file", ""))
        if not registered_hash or not os.path.exists(prompt_file):
            return None
        actual_hash = hashlib.sha256(open(prompt_file, "rb").read()).hexdigest()
        if actual_hash != registered_hash:
            return (
                f"ABOM DRIFT: soul.md hash mismatch. "
                f"Expected {registered_hash[:16]}, got {actual_hash[:16]}. "
                "Run bom_validator.py."
            )
    except Exception:
        pass  # BOM check must never crash the agent
    return None


def _format_history_context(messages: list[dict]) -> str:
    """
    Format prior session messages as plain text for system prompt injection.

    The SDK subprocess manages its own internal conversation state for the current
    session. Cross-session continuity is provided by injecting prior turns here
    rather than via Anthropic messages API re-injection.
    """
    if not messages:
        return ""
    lines = []
    for msg in messages[-20:]:  # ponytail: cap at 20 turns to bound token cost
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, list):
            text_parts = [
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            content = " ".join(text_parts)
        if content:
            lines.append(f"{role.upper()}: {str(content)[:500]}")  # ponytail: truncate per turn
    return "\n".join(lines)


async def run(
    user_message: str,
    session_id: str | None = None,
    task_id: str | None = None,
    user_id: str | None = None,
    mode: str = "troubleshooting",
    log_dir: str = "logs",
    trace_depth: TraceDepth = TraceDepth.BOUNDARY,
    prefer_vault: bool = True,
    catalog_base_url: str | None = None,
    restart: bool = False,
) -> tuple[RunState, StructuredLogger]:
    """
    Run the Conductor agent for a single user message via ClaudeSDKClient.

    mode: selects which behavioral guidance block is active (troubleshooting | setup |
          onboarding | qa). In Setup mode, the SetupStateMachine enforces the
          read->validate->write sequence via hooks, not prompts.
    """
    from claude_agent_sdk import query as sdk_query, ClaudeAgentOptions, HookMatcher, ResultMessage  # type: ignore[import]

    session_id = session_id or str(uuid.uuid4())
    task_id = task_id or "default"
    user_id = user_id or session_id
    catalog_base_url = catalog_base_url or os.environ.get("CATALOG_BASE_URL", "")

    secret_store = make_secret_store(prefer_vault=prefer_vault)
    checkpoints = CheckpointStore()
    sessions = SessionStore()
    memory_store = make_memory_store()

    state = RunState()
    structured_logger = StructuredLogger(run_id=state.run_id, sink_dir=log_dir,
                                         trace_depth=trace_depth)

    bom_warning = _check_prompt_hash()
    if bom_warning:
        structured_logger._write({"event": "abom_drift_warning", "step_id": "init",
                                   "message": bom_warning})

    mcp_server = build_mcp_server(
        secret_store=secret_store,
        memory_store=memory_store,
        catalog_base_url=catalog_base_url,
        logger_inst=structured_logger,
    )

    if restart:
        checkpoints.reset(session_id, task_id)
        sessions.delete(session_id, task_id)
        structured_logger._write({"event": "checkpoint_cleared", "step_id": "init",
                                  "session_id": session_id, "task_id": task_id})

    # Load prior session for cross-session continuity
    prior_messages: list[dict] = (
        sessions.load(session_id, task_id)
        or checkpoints.load_messages(session_id, task_id)
        or []
    )
    history_context = _format_history_context(prior_messages)

    system_prompt = build_system_prompt(
        user_id=user_id,
        mode=mode,
        history_context=history_context,
    )

    # --- Hook definitions ------------------------------------------------
    # All hooks close over setup_sm and structured_logger.
    # HookMatcher.matcher= is always set -- empty string fires on every tool (RULE-SDK02).

    setup_sm = SetupStateMachine()

    async def pre_bash_hook(input_data: dict, tool_use_id: str, context: dict) -> dict:
        """Block deterministic dangerous bash patterns (RULE-SDK02)."""
        command = input_data.get("tool_input", {}).get("command", "")
        for pattern in BLOCKED_BASH_PATTERNS:
            if pattern in command:
                structured_logger._write({
                    "event": "hook_deny",
                    "step_id": tool_use_id,
                    "hook": "PreToolUse/Bash",
                    "reason": f"blocked pattern: {pattern!r}",
                    "command_prefix": command[:100],
                })
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            f"Bash command contains a blocked pattern ({pattern!r}). "
                            "This pattern is not permitted in Conductor."
                        ),
                    }
                }
        return {}

    async def pre_setup_sm_hook(input_data: dict, tool_use_id: str, context: dict) -> dict:
        """Block out-of-sequence Setup mode tool calls (RULE-STM01)."""
        tool_name = input_data.get("tool_name", "")
        if not setup_sm.is_allowed(tool_name):
            structured_logger._write({
                "event": "hook_deny",
                "step_id": tool_use_id,
                "hook": "PreToolUse/SetupSM",
                "tool_name": tool_name,
                "current_state": setup_sm.state.value,
            })
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        f"Setup sequence violation: {tool_name!r} cannot be called from state "
                        f"{setup_sm.state.value!r}. Complete the preceding steps first."
                    ),
                }
            }
        return {}

    async def post_setup_sm_hook(input_data: dict, tool_use_id: str, context: dict) -> dict:
        """Advance SetupStateMachine after a successful tool call (RULE-STM01)."""
        tool_name = input_data.get("tool_name", "")
        advanced = setup_sm.advance(tool_name)
        if advanced:
            structured_logger._write({
                "event": "setup_sm_advanced",
                "step_id": tool_use_id,
                "tool_name": tool_name,
                "new_state": setup_sm.state.value,
            })
        return {}

    async def hitl_hook(input_data: dict, tool_use_id: str, context: dict) -> dict:
        """
        Human-in-the-loop approval for write_connector_config (RULE-SDK02).

        Terminal input for demo/testing. Production deployments replace this with
        an async approval channel (webhook, queue, etc.) -- see docs/decisions/hitl.md.
        """
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input", {})
        print(f"\n[HITL] Approval required for: {tool_name}")
        print(f"Connector: {tool_input.get('connector_id', 'unknown')}")
        print(f"Fields to update: {list(tool_input.get('config_patch', {}).keys())}")
        try:
            response = input("Allow this change? (y/n): ").strip().lower()
        except EOFError:
            # Non-interactive mode (CI, eval harness) -- deny by default
            response = "n"
        approved = response == "y"
        structured_logger._write({
            "event": "hitl_decision",
            "step_id": tool_use_id,
            "tool_name": tool_name,
            "approved": approved,
        })
        if approved:
            return {"decision": "allow"}
        return {"decision": "deny", "reason": "User rejected the configuration write."}

    async def stop_hook(output_data: dict, _tool_use_id: str | None, context: dict) -> None:
        """Persist conversation history on session end (RULE-STO04 compliance)."""
        messages = context.get("messages", [])
        if messages:
            checkpoints.save_messages(session_id, task_id, messages)
            sessions.save(session_id, task_id, messages)
            structured_logger._write({
                "event": "session_saved",
                "step_id": "stop",
                "session_id": session_id,
                "message_count": len(messages),
            })

    async def post_tool_log_hook(input_data: dict, tool_use_id: str, context: dict) -> dict:
        """Log every tool call result for O02 compliance. Empty matcher is intentional (RULE-SDK02)."""
        structured_logger._write({
            "event": "tool_call",
            "step_id": tool_use_id,
            "tool_name": input_data.get("tool_name", "unknown"),
            "status": "success",
        })
        return {}

    # --- SDK options -----------------------------------------------------

    options = ClaudeAgentOptions(
        model=MODEL,
        # Explicit positive allowlist (RULE-SDK01) -- model cannot call anything not listed
        allowed_tools=[
            "mcp__conductor__notes_search",
            "mcp__conductor__search_knowledge_base",
            "mcp__conductor__search_memory",
            "mcp__conductor__add_memory",
            "mcp__conductor__delete_memory",
            "mcp__conductor__check_connector_status",
            "mcp__conductor__read_connector_config",
            "mcp__conductor__validate_credentials",
            "mcp__conductor__write_connector_config",
            "Skill",  # conductor-troubleshoot-connector (RULE-SKL01)
        ],
        mcp_servers={"conductor": mcp_server},
        cwd=str(_REPO_ROOT),
        setting_sources=["project"],  # loads .claude/skills/ from repo root, not this lab folder (RULE-SKL01)
        permission_mode="dontAsk",  # all decisions via hooks, not interactive prompts (RULE-SDK01)
        max_turns=MAX_TURNS,
        # hooks must be dict[event_type, list[HookMatcher]] -- separate pre vs post
        hooks={
            "PreToolUse": [
                # Block dangerous bash patterns (RULE-SDK02)
                HookMatcher(matcher="Bash", hooks=[pre_bash_hook]),
                # Setup sequence gate for write-path tools (RULE-STM01)
                HookMatcher(matcher="mcp__conductor__validate_credentials",
                            hooks=[pre_setup_sm_hook]),
                HookMatcher(matcher="mcp__conductor__write_connector_config",
                            hooks=[pre_setup_sm_hook, hitl_hook]),
            ],
            "PostToolUse": [
                # Advance setup SM after successful tool calls (RULE-STM01)
                HookMatcher(matcher="mcp__conductor__validate_credentials",
                            hooks=[post_setup_sm_hook]),
                HookMatcher(matcher="mcp__conductor__write_connector_config",
                            hooks=[post_setup_sm_hook]),
                HookMatcher(matcher="mcp__conductor__read_connector_config",
                            hooks=[post_setup_sm_hook]),
                # O02 logging -- empty matcher fires on every tool (RULE-SDK02)
                HookMatcher(matcher="", hooks=[post_tool_log_hook]),
            ],
            "Stop": [
                # Persist session on stop (RULE-STO04)
                HookMatcher(hooks=[stop_hook]),
            ],
        },
    )

    # --- Run -------------------------------------------------------------

    t_run_start = time.monotonic()
    structured_logger.log_run_start(user_message=user_message)
    structured_logger._write({
        "event": "session_context",
        "step_id": "init",
        "session_id": session_id,
        "user_id": user_id,
        "mode": mode,
        "has_history": bool(history_context),
        "setup_sm_state": setup_sm.state.value,
    })

    result_msg: ResultMessage | None = None
    t_sdk_start = time.monotonic()
    try:
        async for message in sdk_query(prompt=user_message, options=options):
            structured_logger._write({
                "event": "sdk_message",
                "step_id": "run",
                "message_type": type(message).__name__,
            })
            if isinstance(message, ResultMessage):
                result_msg = message
        if result_msg is not None and not result_msg.is_error:
            state.final_answer = result_msg.result or ""
            state.status = RunStatus.COMPLETED
            state.step_count = result_msg.num_turns
            structured_logger._write({
                "event": "llm_call",
                "step_id": "sdk-run",
                "model": options.model,
                "input_tokens": (result_msg.usage or {}).get("input_tokens", 0),
                "output_tokens": (result_msg.usage or {}).get("output_tokens", 0),
                "duration_ms": round((time.monotonic() - t_sdk_start) * 1000, 1),
            })
        else:
            errors = (result_msg.errors if result_msg else None) or []
            state.status = RunStatus.ERROR
            state.final_answer = None
            structured_logger._write({
                "event": "sdk_error",
                "step_id": "run",
                "error": "; ".join(errors) if errors else "no result message",
            })
        state.steps.append(StepRecord(
            step_id="sdk-run",
            tool_name="sdk_query",
            status="success" if state.status == RunStatus.COMPLETED else "error",
            duration_ms=round((time.monotonic() - t_run_start) * 1000, 1),
        ))
    except Exception as exc:
        structured_logger._write({
            "event": "sdk_error",
            "step_id": "run",
            "error": str(exc),
        })
        state.status = RunStatus.ERROR
        state.final_answer = None
        raise

    structured_logger.log_run_end(
        status=state.status.value,
        final_answer=state.final_answer,
        total_steps=state.step_count,
        total_duration_ms=round((time.monotonic() - t_run_start) * 1000, 1),
    )

    agent_state = AgentState(
        session_id=session_id,
        task_id=task_id,
        current_step=state.step_count,
        total_steps=MAX_TURNS,
        status=state.status.value,
    )
    checkpoints.save(agent_state)

    return state, structured_logger
