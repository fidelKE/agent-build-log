"""
LangGraph graph definition for Conductor -- Sprint 6a.

Graph topology (all modes):

  START
    |
  call_llm  <-----------+
    |                   |
    | [tool call?]      |
    v                   |
  pre_tool_check        | (back-edge after tool runs)
    |                   |
    | [allowed?]        |
    v                   |
  run_tools  ----------->+
    |
    | [limit or abort?]
    v
  error_node
    |
    v
  END

Setup mode injects an interrupt() inside pre_tool_check before write_connector_config.
That node calls interrupt(approval_request) and resumes with the human's decision dict.

RULE-LG01: All session state lives in ConductorState (TypedDict).
RULE-LG02: SQLite checkpointer + unique thread_id per session.
RULE-LG03: iteration_count in state; conditional edge routes to error_node at MAX_TURNS.
"""

import json
import sqlite3
import time
from typing import Annotated, Literal, TypedDict

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AnyMessage, AIMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.types import interrupt

from .logger import StructuredLogger
from .state import SetupStateMachine

MAX_TURNS = 8
MODEL = "claude-haiku-4-5-20251001"

# write_connector_config always requires human approval before execution
HITL_TOOLS: frozenset[str] = frozenset({"write_connector_config"})

# Tools that have sequencing constraints managed by SetupStateMachine
SETUP_SM_TOOLS: frozenset[str] = frozenset({
    "read_connector_config",
    "validate_credentials",
    "write_connector_config",
})


# ---------------------------------------------------------------------------
# State (RULE-LG01: all session state in TypedDict)
# ---------------------------------------------------------------------------

class ConductorState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    iteration_count: int
    status: str          # "running" | "completed" | "limit_reached" | "aborted" | "error"
    mode: str
    session_id: str
    user_id: str
    setup_sm_state: str  # serialized SetupStateMachine.state.value; nodes reconstruct from it
    hitl_pending: bool   # True while interrupt() is active; set by graph internals


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

def build_graph(
    tools: list[BaseTool],
    structured_logger: StructuredLogger,
    db_path: str = "conductor_checkpoints.db",
) -> tuple:
    """
    Compile the Conductor LangGraph graph with SQLite checkpointing.

    Returns (compiled_graph, checkpointer) so the caller can close the checkpointer.
    """
    llm = ChatAnthropic(model=MODEL).bind_tools(tools)

    tool_map: dict[str, BaseTool] = {t.name: t for t in tools}

    # SQLite checkpointer -- RULE-LG02
    # from_conn_string is a context manager in v3+; own the conn directly instead
    _conn = sqlite3.connect(db_path, check_same_thread=False)
    checkpointer = SqliteSaver(_conn)

    # Reconstruct SetupStateMachine per call from serialized state in ConductorState.
    # A fresh SM is created in each node that needs it; state propagated via the dict.
    from .state import SetupState

    def _sm_from_state(state_value: str) -> SetupStateMachine:
        sm = SetupStateMachine()
        try:
            sm.state = SetupState(state_value)
        except ValueError:
            pass  # unknown value stays at IDLE
        return sm

    # ------------------------------------------------------------------
    # Nodes
    # ------------------------------------------------------------------

    def call_llm(state: ConductorState) -> dict:
        """Call the LLM with the current message history."""
        t0 = time.monotonic()
        count = state["iteration_count"] + 1

        # RULE-LG03: check limit before the call, not after
        if count > MAX_TURNS:
            structured_logger._write({
                "event": "iteration_limit",
                "step_id": f"llm-{count}",
                "iteration_count": count,
                "max_turns": MAX_TURNS,
            })
            return {"iteration_count": count, "status": "limit_reached"}

        response = llm.invoke(state["messages"])
        duration_ms = round((time.monotonic() - t0) * 1000, 1)

        # O01 compliance: log the LLM call with available token info
        usage = getattr(response, "usage_metadata", None) or {}
        structured_logger._write({
            "event": "llm_call",
            "step_id": f"llm-{count}",
            "model": MODEL,
            "gen_ai.usage.input_tokens": usage.get("input_tokens", 0),
            "gen_ai.usage.output_tokens": usage.get("output_tokens", 0),
            "duration_ms": duration_ms,
            "status": "success",
            "iteration_count": count,
        })

        return {"messages": [response], "iteration_count": count}

    def pre_tool_check(state: ConductorState) -> dict:
        """
        Gate every tool call:
        1. SetupStateMachine sequence check (Setup mode, RULE-STM01).
        2. interrupt() approval gate for write_connector_config (HITL, Setup mode).

        Returns a state update dict (with denial ToolMessage on deny, empty dict on pass).
        """
        last = state["messages"][-1]
        if not isinstance(last, AIMessage) or not last.tool_calls:
            return {}

        mode = state.get("mode", "troubleshooting")
        sm = _sm_from_state(state.get("setup_sm_state", "idle"))

        for tc in last.tool_calls:
            tool_name = tc["name"]

            # Setup state machine sequence check (RULE-STM01)
            if mode == "setup" and tool_name in SETUP_SM_TOOLS:
                if not sm.is_allowed(tool_name):
                    structured_logger._write({
                        "event": "setup_sm_deny",
                        "step_id": tc["id"],
                        "tool_name": tool_name,
                        "setup_sm_state": sm.state.value,
                    })
                    # Inject a ToolMessage denial so the model can explain to the user
                    denial = ToolMessage(
                        tool_call_id=tc["id"],
                        content=json.dumps({
                            "error": f"Setup sequence violation: {tool_name!r} cannot run from "
                                     f"state {sm.state.value!r}. Complete the preceding steps first."
                        }),
                    )
                    return {
                        "messages": [denial],
                        "status": "running",
                    }

            # HITL approval gate for write_connector_config (RULE-STM01, Setup mode)
            if mode == "setup" and tool_name in HITL_TOOLS:
                approval_request = {
                    "tool_name": tool_name,
                    "connector_id": tc["args"].get("connector_id", "unknown"),
                    "fields": list(tc["args"].get("config_patch", {}).keys()),
                }
                structured_logger._write({
                    "event": "hitl_interrupt",
                    "step_id": tc["id"],
                    "tool_name": tool_name,
                    "connector_id": approval_request["connector_id"],
                })

                # Pause, checkpoint, wait for human input (RULE-LG01/LG02)
                human_input = interrupt(approval_request)

                approved = (
                    human_input.get("decision") == "allow"
                    if isinstance(human_input, dict)
                    else str(human_input).strip().lower() == "y"
                )
                structured_logger._write({
                    "event": "hitl_decision",
                    "step_id": tc["id"],
                    "approved": approved,
                })

                if not approved:
                    denial = ToolMessage(
                        tool_call_id=tc["id"],
                        content=json.dumps({"error": "User rejected the configuration write."}),
                    )
                    return {"messages": [denial], "status": "aborted"}

        return {}

    def run_tools(state: ConductorState) -> dict:
        """Execute all tool calls in the last AIMessage and advance SetupStateMachine."""
        last = state["messages"][-1]
        if not isinstance(last, AIMessage) or not last.tool_calls:
            return {}

        mode = state.get("mode", "troubleshooting")
        sm = _sm_from_state(state.get("setup_sm_state", "idle"))
        results: list[ToolMessage] = []

        for tc in last.tool_calls:
            tool_name = tc["name"]
            t0 = time.monotonic()
            try:
                tool = tool_map.get(tool_name)
                if tool is None:
                    raise ValueError(f"Unknown tool: {tool_name!r}")
                output = tool.invoke(tc["args"])
                status = "success"
                output_str = json.dumps(output) if not isinstance(output, str) else output
            except Exception as exc:
                status = "error"
                output_str = json.dumps({"error": str(exc)})

            duration_ms = round((time.monotonic() - t0) * 1000, 1)

            # O02 compliance
            structured_logger._write({
                "event": "tool_call",
                "step_id": tc["id"],
                "tool.name": tool_name,
                "duration_ms": duration_ms,
                "status": status,
            })

            results.append(ToolMessage(tool_call_id=tc["id"], content=output_str))

            # Advance SetupStateMachine after successful tool call (RULE-STM01)
            if mode == "setup" and tool_name in SETUP_SM_TOOLS and status == "success":
                advanced = sm.advance(tool_name)
                if advanced:
                    structured_logger._write({
                        "event": "setup_sm_advanced",
                        "step_id": tc["id"],
                        "tool_name": tool_name,
                        "new_state": sm.state.value,
                    })

        return {
            "messages": results,
            "setup_sm_state": sm.state.value,
        }

    def error_node(state: ConductorState) -> dict:
        """Terminal node for limit_reached or aborted status."""
        structured_logger._write({
            "event": "run_end",
            "step_id": "error_node",
            "status": state.get("status", "error"),
            "iteration_count": state.get("iteration_count", 0),
        })
        return {"status": state.get("status", "error")}

    # ------------------------------------------------------------------
    # Routing functions
    # ------------------------------------------------------------------

    def route_after_llm(state: ConductorState) -> Literal["pre_tool_check", "error_node", END]:
        if state.get("status") in ("limit_reached", "aborted", "error"):
            return "error_node"
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            return "pre_tool_check"
        return END

    def route_after_check(state: ConductorState) -> Literal["run_tools", "call_llm", "error_node"]:
        """If pre_tool_check injected a denial ToolMessage, skip to call_llm (let model explain)."""
        if state.get("status") in ("aborted", "error"):
            return "error_node"
        last = state["messages"][-1]
        # A ToolMessage here means pre_tool_check injected a denial
        if isinstance(last, ToolMessage):
            return "call_llm"
        return "run_tools"

    # ------------------------------------------------------------------
    # Compile
    # ------------------------------------------------------------------

    builder = StateGraph(ConductorState)
    builder.add_node("call_llm", call_llm)
    builder.add_node("pre_tool_check", pre_tool_check)
    builder.add_node("run_tools", run_tools)
    builder.add_node("error_node", error_node)

    builder.add_edge(START, "call_llm")
    builder.add_conditional_edges("call_llm", route_after_llm)
    builder.add_conditional_edges("pre_tool_check", route_after_check)
    builder.add_edge("run_tools", "call_llm")  # back-edge: the ReAct cycle (RULE-LG01)
    builder.add_edge("error_node", END)

    compiled = builder.compile(checkpointer=checkpointer)
    return compiled, checkpointer
