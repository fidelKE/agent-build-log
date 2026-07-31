"""
Conductor agent harness -- Sprint 6b.

Sprint 6b changes from Sprint 6a:
  - graph.py deleted: ConductorState TypedDict, node definitions (call_llm, pre_tool_check,
    run_tools, error_node), conditional edges, graph.compile(checkpointer=) all gone (~130 lines).
  - create_agent() harness replaces CompiledGraph (RULE-LC01)
  - ConductorContext dataclass replaces ConductorState TypedDict (context_schema=)
  - InMemorySaver replaces SQLite checkpointer (sufficient for sprint comparison)
  - SetupStateMachine enforced via @wrap_tool_call stm_gate (RULE-LC02)
  - HumanInTheLoopMiddleware replaces pre_tool_check node + interrupt()
  - ModelCallLimitMiddleware(run_limit=8) replaces hand-rolled loop counter
  - Token cost per query type logged at run end (RULE-LC03)

Capability gap: ConductorContext.stm_state is per-invocation, not persisted across run()
calls. In 6a it lived in LangGraph state (persisted via SQLite checkpointer). In 6b,
InMemorySaver is fresh per run() call so stm_state resets each turn. Multi-turn Setup
flows would need stm_state stored externally (SessionStore). Eval cases are single-turn
so this is not a blocker for this sprint.
"""

import hashlib
import os
import sys
import time
import uuid
from dataclasses import dataclass

import yaml
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.middleware import (
    HumanInTheLoopMiddleware,
    ModelCallLimitMiddleware,
    ToolRetryMiddleware,
    wrap_tool_call,
)
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import create_model

from .logger import StructuredLogger, TraceDepth
from .memory import make_memory_store
from .prompt import build_system_prompt
from .secrets import make_secret_store
from .skills import load_skill
from .state import (
    AgentState,
    CheckpointStore,
    RunState,
    RunStatus,
    SessionStore,
    SetupState,
    SetupStateMachine,
)
from .tools import TOOL_SCHEMAS, ToolExecutor

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"), override=False)
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", "..", ".env"), override=False)

_BOM_PATH = os.path.join(os.path.dirname(__file__), "..", "agent-bom.yaml")

MODEL: str = os.environ.get("CONDUCTOR_MODEL", "claude-haiku-4-5-20251001")

_JSON_TYPE_MAP = {
    "string": str,
    "integer": int,
    "boolean": bool,
    "number": float,
    "object": dict,
    "array": list,
}


@dataclass
class ConductorContext:
    """
    Per-run data for Conductor -- replaces ConductorState TypedDict fields.

    Passed at invoke() time via context=; accessible in tools and middleware via
    request.runtime.context. Message history is persisted by the checkpointer.
    This context is not -- it resets per run() call (see module docstring).
    """
    user_id: str
    stm_state: str = "idle"  # serialized SetupState; mutated by stm_gate within an invocation


@wrap_tool_call
def stm_gate(request, handler):
    """
    SetupStateMachine enforcement (RULE-LC02).

    Replaces the pre_tool_check node + interrupt() pattern from Sprint 6a.
    Blocks disallowed tool calls by returning an error ToolMessage without calling
    handler() -- so ToolRetryMiddleware does not retry a blocked call.
    Advances STM state after each successful gated tool call.
    """
    tool_name = request.tool_call["name"]
    ctx = request.runtime.context
    if ctx is None:
        return handler(request)

    stm = SetupStateMachine()
    try:
        stm.state = SetupState(ctx.stm_state)
    except ValueError:
        stm.state = SetupState.IDLE

    if not stm.is_allowed(tool_name):
        return ToolMessage(
            content=(
                f"Cannot call '{tool_name}' in state '{stm.state.value}'. "
                "Complete required prior steps first."
            ),
            tool_call_id=request.tool_call["id"],
        )

    result = handler(request)
    stm.advance(tool_name)
    ctx.stm_state = stm.state.value  # mutate in-place; persists within this invocation
    return result


def _check_prompt_hash() -> str | None:
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
                f"ABOM DRIFT: soul.md changed. "
                f"Expected {registered_hash[:16]}, got {actual_hash[:16]}. "
                "Run bom_validator.py."
            )
    except Exception:
        pass
    return None


def _format_history_context(messages: list[dict]) -> str:
    if not messages:
        return ""
    lines = []
    for msg in messages[-10:]:  # ponytail: cap at 10 turns to bound token cost
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        if content:
            lines.append(f"{role.upper()}: {str(content)[:500]}")
    return "\n".join(lines)


def _pydantic_from_schema(name: str, input_schema: dict):
    props = input_schema.get("properties", {})
    required = set(input_schema.get("required", []))
    fields = {}
    for field_name, spec in props.items():
        typ = _JSON_TYPE_MAP.get(spec.get("type", "string"), str)
        fields[field_name] = (typ, ...) if field_name in required else (typ, spec.get("default", None))
    return create_model(name, **fields)


def _build_langchain_tools(executor: ToolExecutor) -> list:
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

        lc_tools.append(StructuredTool.from_function(
            func=_make_invoke(name),
            name=name,
            description=description,
            args_schema=pydantic_model,
        ))
    lc_tools.append(load_skill)
    return lc_tools


def _extract_final_answer(result: dict) -> str | None:
    for msg in reversed(result.get("messages", [])):
        if isinstance(msg, AIMessage) and msg.content:
            content = msg.content
            if isinstance(content, list):
                text = " ".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
                if text.strip():
                    return text
            elif str(content).strip():
                return str(content)
    return None


def _extract_token_usage(result: dict) -> dict:
    """Sum token usage across all AIMessages (RULE-LC03)."""
    total_in = total_out = 0
    for msg in result.get("messages", []):
        if isinstance(msg, AIMessage):
            usage = getattr(msg, "usage_metadata", None)
            if usage:
                total_in += usage.get("input_tokens", 0)
                total_out += usage.get("output_tokens", 0)
    return {"input_tokens": total_in, "output_tokens": total_out}


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
    Run Conductor for a single user message via create_agent() (RULE-LC01).

    mode selects the behavioral guidance block in the system prompt.
    In Setup mode, stm_gate enforces read -> validate -> write via @wrap_tool_call.
    HumanInTheLoopMiddleware pauses before write_connector_config for HITL approval.
    thread_id = session_id scopes conversation history in InMemorySaver.
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
    structured_logger = StructuredLogger(
        run_id=state.run_id, sink_dir=log_dir, trace_depth=trace_depth
    )

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

    model = ChatAnthropic(
        model=MODEL,
        base_url=os.environ.get("LLM_GATEWAY_URL"),
    )

    # RULE-LC01: create_agent() with ChatAnthropic, soul.md as system_prompt,
    # context_schema=ConductorContext, checkpointer required for HITL
    agent = create_agent(
        model=model,
        tools=lc_tools,
        system_prompt=system_prompt,
        context_schema=ConductorContext,
        checkpointer=InMemorySaver(),
        middleware=[
            ModelCallLimitMiddleware(run_limit=8, exit_behavior="end"),
            HumanInTheLoopMiddleware(
                interrupt_on={
                    "write_connector_config": {
                        "allowed_decisions": ["approve", "reject"],
                    }
                }
            ),
            ToolRetryMiddleware(max_retries=2, on_failure="continue"),
            stm_gate,  # RULE-LC02: blocks disallowed STM transitions before retry
        ],
    )

    config = {"configurable": {"thread_id": session_id}}
    context = ConductorContext(user_id=user_id, stm_state="idle")

    t_run_start = time.monotonic()
    structured_logger.log_run_start(user_message=user_message)
    structured_logger._write({
        "event": "session_context",
        "step_id": "init",
        "session_id": session_id,
        "user_id": user_id,
        "mode": mode,
        "has_history": bool(history_context),
        "setup_sm_state": context.stm_state,
    })

    result = None
    try:
        t_llm_start = time.monotonic()
        result = agent.invoke(
            {"messages": [HumanMessage(content=user_message)]},
            config=config,
            context=context,
        )
        structured_logger._write({
            "event": "llm_call",
            "step_id": "run",
            "model": MODEL,
            "duration_ms": round((time.monotonic() - t_llm_start) * 1000, 1),
        })

        # HITL: HumanInTheLoopMiddleware interrupted before write_connector_config
        interrupts = result.get("__interrupt__", [])
        if interrupts:
            interrupt_info = interrupts[0] if isinstance(interrupts, list) else interrupts
            structured_logger._write({
                "event": "hitl_interrupt",
                "step_id": "hitl",
                "session_id": session_id,
                "interrupt": str(interrupt_info),
            })

            decision = "reject"
            if sys.stdin.isatty():
                print(f"\n[HITL] Agent wants to write connector config.")
                print(f"Details: {interrupt_info}")
                raw = input("Decision [approve/reject] (default: reject): ").strip()
                if raw in ("approve", "reject"):
                    decision = raw

            structured_logger._write({
                "event": "hitl_decision",
                "step_id": "hitl",
                "decision": decision,
            })
            t_llm_resume = time.monotonic()
            result = agent.invoke(Command(resume=decision), config=config, context=context)
            structured_logger._write({
                "event": "llm_call",
                "step_id": "run-resume",
                "model": MODEL,
                "duration_ms": round((time.monotonic() - t_llm_resume) * 1000, 1),
            })

        state.status = RunStatus.COMPLETED
        state.final_answer = _extract_final_answer(result)

        # RULE-LC03: log token cost labelled by query type
        token_usage = _extract_token_usage(result)
        structured_logger._write({
            "event": "token_cost",
            "step_id": "post-run",
            "query_type": mode,
            "input_tokens": token_usage["input_tokens"],
            "output_tokens": token_usage["output_tokens"],
            "total_tokens": token_usage["input_tokens"] + token_usage["output_tokens"],
        })

        messages_for_persist = [
            {
                "role": "user" if isinstance(m, HumanMessage) else "assistant",
                "content": getattr(m, "content", ""),
            }
            for m in result.get("messages", [])
            if isinstance(m, (HumanMessage, AIMessage)) and getattr(m, "content", "")
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
            "event": "agent_error",
            "step_id": "run",
            "error": str(exc),
        })
        state.status = RunStatus.ERROR
        state.final_answer = None

    structured_logger.log_run_end(
        status=state.status.value,
        final_answer=state.final_answer,
        total_steps=state.step_count,
        total_duration_ms=round((time.monotonic() - t_run_start) * 1000, 1),
    )

    checkpoints.save(AgentState(
        session_id=session_id,
        task_id=task_id,
        current_step=state.step_count,
        total_steps=8,
        status=state.status.value,
    ))

    return state, structured_logger
