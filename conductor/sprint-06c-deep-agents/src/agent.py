"""
Conductor agent harness -- Sprint 6c.

Sprint 6c changes from Sprint 6b:
  - LangGraph CompiledGraph replaced by Deep Agents `create_deep_agent()` (RULE-DA01)
  - graph.py removed -- Deep Agents owns the graph topology
  - skills.py @tool load_skill removed -- Deep Agents SkillsMiddleware (progressive disclosure)
  - pre_tool_check node replaced by SetupStateMiddleware.wrap_tool_call (RULE-DA02/STM01)
  - Token cost logged per query type from final message list at run end (RULE-DA03)
  - AGENTS.md loaded via MemoryMiddleware (replaces custom BeforeAgentMiddleware hook)
  - SkillsMiddleware reads .claude/skills/ -- injects metadata only, full body on demand
  - ModelCallLimitMiddleware(run_limit=8) replaces hand-rolled step cap (RULE-AG02)
  - soul.md content passed as system_prompt= (RULE-P01, RULE-DA01)
  - HITL wired via interrupt_on= + MemorySaver checkpointer + Command(resume=...) (RULE-DA04)
    interrupt_on gates write_connector_config (Setup mode) -- caller gets result.interrupts,
    calls run() again with resume_decisions= to continue after human approval.

Ceiling findings documented in results.md:
  - Per-mode conditional routing: workaround (system prompt injection, topology fixed)
  - HITL interrupt+resume: workaround (tool-level via interrupt_on=, node-level needs LangGraph)
"""

import hashlib
import os
import time
import uuid
from pathlib import Path

import yaml
from dotenv import load_dotenv
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage, AIMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from pydantic import create_model

from .logger import StructuredLogger, TraceDepth
from .memory import make_memory_store
from .prompt import build_system_prompt
from .secrets import make_secret_store
from .state import (
    RunState, RunStatus, StepRecord,
    AgentState, CheckpointStore, SessionStore,
    SetupStateMachine,
)
from .skills import make_skills_middleware
from .tools import ToolExecutor, TOOL_SCHEMAS

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"), override=False)
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", "..", ".env"), override=False)

# MODEL constant: single pin point for the model used by create_deep_agent().
# BOM model.id must match this value (enforced by TestBomConsistency).
MODEL = os.environ.get("CONDUCTOR_MODEL", "claude-sonnet-4-5")

_BOM_PATH = os.path.join(os.path.dirname(__file__), "..", "agent-bom.yaml")
_SRC_DIR = Path(__file__).parent
_AGENTS_MD_PATH = _SRC_DIR / "AGENTS.md"

# Tools that require human approval before execution (RULE-DA04).
# interrupt_on= is tool-level -- the agent pauses before calling these tools
# and the caller resumes via Command(resume={"decisions": [...]}) with same thread_id.
# Currently scoped to Setup mode's write step (high-impact, irreversible action).
_HITL_TOOLS: dict[str, object] = {
    "write_connector_config": {"allowed_decisions": ["approve", "edit", "reject"]},
}

# MemorySaver is required by interrupt_on= -- persists graph state between the
# initial invoke() (pause) and the resume invoke(Command(resume=...)).
# ponytail: one saver per process, keyed by session_id via thread_id in config
_CHECKPOINTER = MemorySaver()


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
        with open(prompt_file, "rb") as fh:
            actual_hash = hashlib.sha256(fh.read()).hexdigest()
        if actual_hash != registered_hash:
            return (
                f"ABOM DRIFT: soul.md hash mismatch. "
                f"Expected {registered_hash[:16]}, got {actual_hash[:16]}. "
                "Run bom_validator.py."
            )
    except Exception:
        pass
    return None


def _format_history_context(messages: list[dict]) -> str:
    """Format prior session messages as plain text for system prompt injection."""
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


_JSON_TYPE_MAP = {"string": str, "integer": int, "boolean": bool, "number": float, "object": dict, "array": list}


def _pydantic_from_schema(name: str, input_schema: dict):
    """Build a Pydantic model class from a JSON Schema properties dict."""
    props = input_schema.get("properties", {})
    required = set(input_schema.get("required", []))
    fields = {}
    for field_name, spec in props.items():
        typ = _JSON_TYPE_MAP.get(spec.get("type", "string"), str)
        if field_name in required:
            fields[field_name] = (typ, ...)
        else:
            fields[field_name] = (typ, spec.get("default", None))
    return create_model(name, **fields)


def _build_langchain_tools(executor: ToolExecutor) -> list:
    """Wrap ToolExecutor methods as LangChain StructuredTools for Deep Agents binding."""
    lc_tools = []
    for schema in TOOL_SCHEMAS:
        name = schema["name"]
        description = schema.get("description", name)
        input_schema = schema.get("input_schema", {"type": "object", "properties": {}})
        pydantic_model = _pydantic_from_schema(name, input_schema)

        def _make_invoke(tool_name: str):
            def invoke(**kwargs):
                return executor.execute(tool_name, kwargs)
            return invoke

        lc_tool = StructuredTool.from_function(
            func=_make_invoke(name),
            name=name,
            description=description,
            args_schema=pydantic_model,
        )
        lc_tools.append(lc_tool)
    return lc_tools


def _count_input_tokens(messages: list) -> int:
    """
    Approximate input token count from the final message list.
    Deep Agents does not expose per-call token counts; we use character count / 4
    as a rough proxy for logging purposes. Records actual token budget consumed
    as evidence for the Lab 11 mode router baseline (RULE-DA03).
    """
    total_chars = 0
    for msg in messages:
        content = getattr(msg, "content", "") or ""
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total_chars += len(block.get("text", ""))
    return total_chars // 4  # ponytail: ~4 chars/token approximation


# ---------------------------------------------------------------------------
# Deep Agents middleware
# ---------------------------------------------------------------------------

class SetupStateMiddleware(AgentMiddleware):
    """
    Enforces SetupStateMachine sequence in the wrap_tool_call hook (RULE-DA02, RULE-STM01).

    In sprint-6 this was a PreToolUse hook.
    In sprint-6a this was a pre_tool_check node.
    In sprint-6b (Deep Agents) this is wrap_tool_call middleware.

    The enforcement contract is identical: write-step tools are blocked if validate
    has not completed. A prompt injection asking to skip the sequence is rejected by
    the middleware, not the model.

    Ceiling finding: SetupStateMachine state must be threaded through the middleware
    externally (via a mutable container) because Deep Agents middleware instances are
    stateless between tool calls -- there is no equivalent of LangGraph's TypedDict
    state flowing through nodes.
    """

    def __init__(self, sm: SetupStateMachine, structured_logger: StructuredLogger):
        self._sm = sm
        self._logger = structured_logger

    def wrap_tool_call(self, call, next_fn):
        import time
        tool_name = getattr(call, "name", None) or (call.get("name") if isinstance(call, dict) else None)
        tool_input = getattr(call, "input", None) or (call.get("input") if isinstance(call, dict) else None) or {}
        if tool_name and not self._sm.is_allowed(tool_name):
            blocked_msg = (f"[SetupStateMachine] Tool {tool_name!r} blocked: "
                           f"sequence requires completing the validate step first.")
            self._logger._write({
                "event": "tool_call",
                "tool_name": tool_name,
                "tool_input": tool_input,
                "tool_output": {"error": blocked_msg},
                "duration_ms": 0,
                "status": "error",
                "stm_blocked": True,
                "sm_state": self._sm.state,
            })
            # Return an error response without calling next_fn -- enforcing the sequence
            # ponytail: return dict matching Deep Agents tool result shape
            return {"content": blocked_msg, "tool_call_id": getattr(call, "id", "unknown")}
        t0 = time.monotonic()
        result = next_fn(call)
        duration_ms = int((time.monotonic() - t0) * 1000)
        if tool_name:
            self._sm.advance(tool_name)
        self._logger._write({
            "event": "tool_call",
            "tool_name": tool_name or "unknown",
            "tool_input": tool_input,
            "tool_output": result if isinstance(result, dict) else {"content": str(result)},
            "duration_ms": duration_ms,
            "status": "success",
            "sm_state": self._sm.state,
        })
        return result

    async def awrap_tool_call(self, call, next_fn):
        import time
        tool_name = getattr(call, "name", None) or (call.get("name") if isinstance(call, dict) else None)
        tool_input = getattr(call, "input", None) or (call.get("input") if isinstance(call, dict) else None) or {}
        if tool_name and not self._sm.is_allowed(tool_name):
            blocked_msg = (f"[SetupStateMachine] Tool {tool_name!r} blocked: "
                           f"sequence requires completing the validate step first.")
            self._logger._write({
                "event": "tool_call",
                "tool_name": tool_name,
                "tool_input": tool_input,
                "tool_output": {"error": blocked_msg},
                "duration_ms": 0,
                "status": "error",
                "stm_blocked": True,
                "sm_state": self._sm.state,
            })
            return {"content": blocked_msg, "tool_call_id": getattr(call, "id", "unknown")}
        t0 = time.monotonic()
        result = await next_fn(call)
        duration_ms = int((time.monotonic() - t0) * 1000)
        if tool_name:
            self._sm.advance(tool_name)
        self._logger._write({
            "event": "tool_call",
            "tool_name": tool_name or "unknown",
            "tool_input": tool_input,
            "tool_output": result if isinstance(result, dict) else {"content": str(result)},
            "duration_ms": duration_ms,
            "status": "success",
            "sm_state": self._sm.state,
        })
        return result


# ---------------------------------------------------------------------------
# Main run() entry point
# ---------------------------------------------------------------------------

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
    db_path: str = "conductor_checkpoints.db",
    resume_decisions: list[dict] | None = None,
) -> tuple[RunState, StructuredLogger]:
    """
    Run the Conductor agent for a single user message via Deep Agents.

    mode: selects behavioral guidance block (troubleshooting | setup | onboarding | qa).
    In Setup mode, SetupStateMiddleware enforces the step sequence (RULE-DA02/STM01).

    HITL (RULE-DA04): interrupt_on= gates write_connector_config in Setup mode.
    First call returns state.status=INTERRUPTED and state.interrupts with action_requests.
    Caller resumes by calling run() again with the same session_id and
    resume_decisions=[{"type": "approve"}] (or "edit"/"reject").

    Ceiling finding: per-mode conditional routing -- Deep Agents owns the graph topology.
    All four modes handled by a single agent instance with mode injected via system prompt.
    """
    # ponytail: deepagents import deferred to here -- not installed at project level
    try:
        from deepagents import create_deep_agent
    except ImportError as e:
        raise ImportError(
            "deepagents not installed. Run: "
            "UV_PROJECT_ENVIRONMENT=../.venv uv add deepagents"
        ) from e

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

    if restart:
        checkpoints.reset(session_id, task_id)
        sessions.delete(session_id, task_id)
        structured_logger._write({"event": "checkpoint_cleared", "step_id": "init",
                                  "session_id": session_id, "task_id": task_id})

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

    executor = ToolExecutor(
        secret_store=secret_store,
        memory_store=memory_store,
        catalog_base_url=catalog_base_url,
        structured_logger=structured_logger,
    )
    lc_tools = _build_langchain_tools(executor)

    from deepagents.middleware import MemoryMiddleware, SkillsMiddleware
    from langchain.agents.middleware import ModelCallLimitMiddleware
    from deepagents.backends.filesystem import FilesystemBackend

    sm = SetupStateMachine()
    sm_middleware = SetupStateMiddleware(sm, structured_logger)

    # MemoryMiddleware replaces custom BeforeAgentMiddleware -- loads AGENTS.md per-call
    # with Anthropic prompt-cache control (keeps memory block boundary cached across turns)
    memory_middleware = MemoryMiddleware(
        backend=FilesystemBackend(root_dir=str(_SRC_DIR), virtual_mode=False),
        sources=[str(_AGENTS_MD_PATH)],
        add_cache_control=True,
    )

    # SkillsMiddleware: progressive disclosure -- injects skill metadata only at session
    # start; agent reads full body via read_file when a skill applies (zero bulk token cost)
    skills_middleware = make_skills_middleware()

    # ModelCallLimitMiddleware: replaces hand-rolled step cap (RULE-AG02, run_limit=8)
    call_limit_middleware = ModelCallLimitMiddleware(run_limit=8, exit_behavior="end")

    middleware_stack = [memory_middleware, call_limit_middleware, sm_middleware]
    if skills_middleware is not None:
        middleware_stack.insert(1, skills_middleware)

    # RULE-DA01: explicit model=, system_prompt= from soul.md, tools= allowlist
    # RULE-DA02: middleware= includes SetupStateMiddleware for sequence enforcement
    # RULE-DA04: interrupt_on= gates write_connector_config; checkpointer= persists state
    #            between the pause invoke() and the resume invoke(Command(resume=...))
    agent = create_deep_agent(
        model=MODEL,
        system_prompt=system_prompt,
        tools=lc_tools,
        middleware=middleware_stack,
        interrupt_on=_HITL_TOOLS,
        checkpointer=_CHECKPOINTER,
    )

    # thread_id ties the two invoke() calls together for interrupt/resume
    hitl_config = {"configurable": {"thread_id": session_id}}

    structured_logger.log_run_start(user_message=user_message)
    structured_logger._write({
        "event": "session_context",
        "step_id": "init",
        "session_id": session_id,
        "user_id": user_id,
        "mode": mode,
        "has_history": bool(history_context),
        "resume": resume_decisions is not None,
        "framework": "deep-agents",
        "framework_version": "0.6.12",
    })

    t_run_start = time.monotonic()
    final_answer: str | None = None

    try:
        if resume_decisions is not None:
            # Resume path: caller approved/edited/rejected the HITL gate
            structured_logger._write({
                "event": "hitl_resume",
                "step_id": "resume",
                "decisions": resume_decisions,
            })
            t_llm_start = time.monotonic()
            result = agent.invoke(
                Command(resume={"decisions": resume_decisions}),
                config=hitl_config,
                version="v2",
            )
            structured_logger._write({
                "event": "llm_call",
                "step_id": "resume",
                "model": MODEL,
                "duration_ms": round((time.monotonic() - t_llm_start) * 1000, 1),
            })
        else:
            t_llm_start = time.monotonic()
            result = agent.invoke(
                {"messages": [HumanMessage(content=user_message)]},
                config=hitl_config,
                version="v2",
            )
            structured_logger._write({
                "event": "llm_call",
                "step_id": "run",
                "model": MODEL,
                "duration_ms": round((time.monotonic() - t_llm_start) * 1000, 1),
            })

        # Interrupt path: agent paused at an interrupt_on= gate
        if getattr(result, "interrupts", None):
            interrupt_value = result.interrupts[0].value
            action_requests = interrupt_value.get("action_requests", [interrupt_value])
            structured_logger._write({
                "event": "hitl_interrupt",
                "step_id": "interrupt",
                "action_requests": action_requests,
            })
            state.status = RunStatus.INTERRUPTED
            state.interrupts = action_requests
            structured_logger.log_run_end(
                status=state.status.value,
                final_answer=None,
                total_steps=state.step_count,
                total_duration_ms=round((time.monotonic() - t_run_start) * 1000, 1),
            )
            checkpoints.save(AgentState(
                session_id=session_id, task_id=task_id,
                current_step=state.step_count, total_steps=8,
                status=state.status.value,
            ))
            return state, structured_logger

        output_messages = result.get("messages", []) if isinstance(result, dict) else (
            result.value.get("messages", []) if hasattr(result, "value") else []
        )
        final_answer = _extract_final_answer(output_messages)

        state.status = RunStatus.COMPLETED
        state.step_count = len([m for m in output_messages if isinstance(m, AIMessage)])
        state.final_answer = final_answer

        # RULE-DA03: token cost log per query type for Lab 11 mode router baseline
        approx_input_tokens = _count_input_tokens(output_messages)
        structured_logger._write({
            "event": "token_cost",
            "step_id": "post-run",
            "query_type": mode,
            "approx_input_tokens": approx_input_tokens,
            "message_count": len(output_messages),
        })

        # Persist messages for cross-session continuity (RULE-STO03/STO04)
        messages_for_persist = [
            {"role": "user" if isinstance(m, HumanMessage) else "assistant",
             "content": getattr(m, "content", "")}
            for m in output_messages
            if hasattr(m, "content") and getattr(m, "content", "")
        ]
        if messages_for_persist:
            checkpoints.save_messages(session_id, task_id, messages_for_persist)
            sessions.save(session_id, task_id, messages_for_persist)
            structured_logger._write({
                "event": "session_saved",
                "step_id": "post-run",
                "session_id": session_id,
                "message_count": len(messages_for_persist),
            })

        state.steps.append(StepRecord(
            step_id="deep-agents-run",
            tool_name="DeepAgents",
            status="success",
            duration_ms=round((time.monotonic() - t_run_start) * 1000, 1),
        ))

    except Exception as exc:
        structured_logger._write({
            "event": "agent_error",
            "step_id": "run",
            "error": str(exc),
        })
        state.status = RunStatus.ERROR
        state.final_answer = None
        final_answer = None

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
        total_steps=8,
        status=state.status.value,
    )
    checkpoints.save(agent_state)

    return state, structured_logger


def _extract_final_answer(messages: list) -> str | None:
    """Extract the last non-empty AI text response from the message list."""
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            content = getattr(msg, "content", "")
            if isinstance(content, str) and content.strip():
                return content
            if isinstance(content, list):
                text = " ".join(
                    block.get("text", "") for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
                if text.strip():
                    return text
    return None
