"""
Conductor agent harness -- Sprint 6a.

Sprint 6a changes from Sprint 6:
  - ClaudeSDKClient replaced by LangGraph CompiledGraph (RULE-LG01/02/03, RULE-SDK01 scope)
  - PreToolUse/PermissionRequest hooks replaced by pre_tool_check node + interrupt() (RULE-STM01)
  - In-process MCP server (build_mcp_server) removed; tools bound via LangGraph ToolNode
  - langchain-anthropic ChatAnthropic replaces raw Anthropic SDK in the graph
  - SQLite checkpointer for state persistence (RULE-LG02); SessionStore/CheckpointStore still
    used for cross-session message history injection into the initial SystemMessage
  - Session messages reconstructed from prior CheckpointStore/SessionStore for continuity
  - load_skill @tool added (LangChain progressive disclosure, no startup token cost)

BOM drift check and _format_history_context carried forward from Sprint 6 unchanged.
"""

import hashlib
import os
import time
import uuid

import yaml
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool
from pydantic import create_model

from .graph import build_graph, ConductorState
from .logger import StructuredLogger, TraceDepth
from .memory import make_memory_store
from .prompt import build_system_prompt
from .secrets import make_secret_store
from .skills import load_skill
from .state import (
    RunState, RunStatus, StepRecord,
    AgentState, CheckpointStore, SessionStore,
)
from .tools import ToolExecutor, TOOL_SCHEMAS

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"), override=False)
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", "..", ".env"), override=False)

_BOM_PATH = os.path.join(os.path.dirname(__file__), "..", "agent-bom.yaml")


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
    """
    Wrap ToolExecutor methods as LangChain StructuredTools for ToolNode binding.
    load_skill is appended last (LangChain @tool decorator, no executor backing).
    """
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

    lc_tools.append(load_skill)
    return lc_tools


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
) -> tuple[RunState, StructuredLogger]:
    """
    Run the Conductor agent for a single user message via LangGraph.

    mode: selects behavioral guidance block (troubleshooting | setup | onboarding | qa).
    In Setup mode, pre_tool_check node enforces SetupStateMachine + interrupt() for HITL.

    thread_id = session_id (RULE-LG02: unique thread per session, no shared state).
    """
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

    # Cross-session continuity: inject prior turns into initial SystemMessage
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

    graph, checkpointer = build_graph(
        tools=lc_tools,
        structured_logger=structured_logger,
        db_path=db_path,
    )

    # thread_id = session_id: RULE-LG02 (unique thread_id per session)
    config = {"configurable": {"thread_id": session_id}}

    # ponytail: check for existing checkpoint before building initial_state.
    # On resume, pass only the new HumanMessage — the prior SystemMessage is already
    # in the checkpointed messages; appending another one causes an API error.
    existing_checkpoint = graph.get_state(config)
    if existing_checkpoint and existing_checkpoint.values:
        invoke_input: dict = {"messages": [HumanMessage(content=user_message)], "status": "running"}
    else:
        invoke_input = {
            "messages": [
                SystemMessage(content=system_prompt),
                HumanMessage(content=user_message),
            ],
            "iteration_count": 0,
            "status": "running",
            "mode": mode,
            "session_id": session_id,
            "user_id": user_id,
            "setup_sm_state": "idle",
            "hitl_pending": False,
        }

    t_run_start = time.monotonic()
    structured_logger.log_run_start(user_message=user_message)
    structured_logger._write({
        "event": "session_context",
        "step_id": "init",
        "session_id": session_id,
        "user_id": user_id,
        "mode": mode,
        "has_history": bool(history_context),
        "setup_sm_state": "idle",
    })

    try:
        final_graph_state = graph.invoke(invoke_input, config=config)
        run_status = final_graph_state.get("status", "completed")
        final_answer = _extract_final_answer(final_graph_state)

        state.final_answer = final_answer
        try:
            state.status = RunStatus.COMPLETED if run_status == "running" else RunStatus(run_status)
        except ValueError:
            state.status = RunStatus.COMPLETED
        state.step_count = final_graph_state.get("iteration_count", 1)
        state.steps.append(StepRecord(
            step_id="graph-run",
            tool_name="LangGraph",
            status="success" if state.status == RunStatus.COMPLETED else "error",
            duration_ms=round((time.monotonic() - t_run_start) * 1000, 1),
        ))

        # Persist messages for cross-session continuity (mirrors Sprint 6 stop_hook behavior)
        messages_for_persist = [
            {"role": "user" if isinstance(m, HumanMessage) else "assistant", "content": getattr(m, "content", "")}
            for m in final_graph_state.get("messages", [])
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

    except Exception as exc:
        structured_logger._write({
            "event": "graph_error",
            "step_id": "run",
            "error": str(exc),
        })
        state.status = RunStatus.ERROR
        state.final_answer = None
        final_answer = None
        run_status = "error"

    finally:
        try:
            checkpointer.conn.close()  # we own this connection (created in build_graph)
        except Exception:
            pass

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
        total_steps=8,  # MAX_TURNS from graph.py
        status=state.status.value,
    )
    checkpoints.save(agent_state)

    return state, structured_logger


def _extract_final_answer(graph_state: dict) -> str | None:
    """Extract the last non-empty AI text response from the graph's message list."""
    messages = graph_state.get("messages", [])
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
