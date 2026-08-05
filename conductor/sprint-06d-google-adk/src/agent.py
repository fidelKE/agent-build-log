"""
Conductor agent harness -- Sprint 6d (Google ADK).

Sprint 6d changes from Sprint 6c (Deep Agents):
  - LangGraph/Deep Agents replaced by Google ADK: LlmAgent (troubleshooting/qa leaf),
    a Workflow graph for setup/onboarding (RULE-ADK01/ADK03)
  - workflow.py owns the Setup/Onboarding topology -- structural, not prompt-injected.
    Built on google.adk.Workflow(edges=[...]) with plain LlmAgent nodes, not
    SequentialAgent/ParallelAgent -- both are @deprecated in installed
    google-adk==2.6.1 in favor of this Workflow graph engine. Confirmed live (not from
    docs, which don't cover the new engine yet) that build_node() clones a plain
    LlmAgent directly into a graph node, and a chain tuple (START, a, b, c) parses
    into strict pairwise edges -- the exact structural guarantee RULE-ADK03 needs,
    now inspectable directly on workflow.graph.edges.
  - Runner takes node=root_agent for setup/onboarding (Workflow is a BaseNode, not a
    BaseAgent) vs agent=root_agent for the plain LlmAgent path; run_async()'s call
    shape is identical either way.
  - SetupStateMiddleware removed. Its job (block write before validate) is now done
    structurally by the Workflow graph: SetupConfigureAgent's tools= list literally
    does not contain write_connector_config's sibling steps, and there is no edge
    from START or SetupReadAgent directly to it -- skipping is architecturally
    impossible, not merely disallowed by a runtime check. SetupStateMachine (state.py)
    is intentionally left unwired this lab -- see results.md.
  - skills.py wires google.adk.tools.skill_toolset.SkillToolset onto the
    troubleshooting/qa LlmAgent -- a real, undocumented-in-the-public-docs ADK
    feature confirmed live: list_skills/load_skill/load_skill_resource/
    search_skills/run_skill_script, same progressive-disclosure shape as every
    prior lab's mechanism (Claude Skills API, load_skill tool, SkillsMiddleware).
    Loads this series' shared SKILL.md unmodified except for one incompatible
    field patched in memory (allowed-tools: YAML list -> comma string; ADK's
    Frontmatter.allowed_tools is str-only) -- see skills.py docstring.
  - before_tool_callback / after_tool_callback (callback.py) replace wrap_tool_call as
    the uniform tool-call enforcement + logging layer (RULE-ADK02, RULE-O01/O02)
  - HITL: no interrupt_on=/checkpointer pause-and-resume primitive at this level in
    ADK. Workaround is pre-authorization -- approved=True is set in initial session
    state before the run starts (main.py --approve), not a mid-run pause/resume like
    Lab 6c's Command(resume=...). See results.md ceiling finding.
  - BuiltInPlanner A/B: use_planner=True wraps the troubleshooting/qa LlmAgent with
    Gemini's native thinking via planner=BuiltInPlanner(...)
  - Token cost logged from event.usage_metadata when Gemini returns it (real counts,
    not the char/4 approximation Deep Agents needed) -- falls back to the
    approximation if no event carried usage_metadata.
  - No separate GOOGLE_API_KEY: Gemini is one of ~100 models already reachable
    through the existing LLM_GATEWAY_URL/ANTHROPIC_API_KEY (confirmed live -- this
    gateway is a multi-provider proxy, not Anthropic-only). GatewayGemini below is
    ADK's own documented extension point for this (subclass Gemini, override the
    api_client cached_property) -- not a workaround, the API is built for exactly
    this case.
"""

import hashlib
import os
import time
import uuid
from functools import cached_property
from pathlib import Path

import yaml
from dotenv import load_dotenv
from google.adk.models import Gemini
from google.genai import Client as GenAIClient
from google.genai.types import HttpOptions

from .callback import ToolCallGuard
from .logger import StructuredLogger, TraceDepth
from .memory import make_memory_store
from .prompt import build_system_prompt
from .secrets import make_secret_store
from .skills import make_skills_toolset
from .state import RunState, RunStatus, StepRecord, AgentState, CheckpointStore, SessionStore
from .tools import ToolExecutor, build_adk_tool_functions
from .workflow import build_setup_workflow, build_onboarding_workflow

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"), override=False)
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", "..", ".env"), override=False)

# MODEL constant: single pin point for the model used by every LlmAgent/workflow.
# gemini-pro-latest for Standard-tier parity with claude-sonnet-4-5 (the model prior
# labs pinned) -- README's own failure indicator warns that mismatched tiers would
# make the five-way benchmark's quality delta a model story, not a framework story.
# BOM model.id must match this value (enforced by TestBomConsistency).
MODEL = os.environ.get("CONDUCTOR_MODEL", "gemini-pro-latest")


class GatewayGemini(Gemini):
    """Routes Gemini calls through LLM_GATEWAY_URL instead of Google AI Studio directly.

    ADK's documented extension point for this (Gemini's own docstring): subclass and
    override the api_client cached_property. Every LlmAgent/workflow node gets a
    fresh instance via _make_model() -- api_client is lazily cached per-instance, so
    nothing is shared/mutable across agents.
    """

    @cached_property
    def api_client(self) -> GenAIClient:
        return GenAIClient(
            api_key=os.environ["ANTHROPIC_API_KEY"],
            http_options=HttpOptions(base_url=os.environ["LLM_GATEWAY_URL"]),
        )


def _make_model() -> GatewayGemini:
    return GatewayGemini(model=MODEL)

# RULE-A01: hard iteration cap. RunConfig(max_llm_calls=) is ADK's own primitive for
# this -- no hand-rolled counter needed (same run_limit=8 convention as Labs 6b/6c).
MAX_ITERATIONS = 8

_BOM_PATH = os.path.join(os.path.dirname(__file__), "..", "agent-bom.yaml")

# Tools available to the plain troubleshooting/qa LlmAgent (RULE-ADK01 leaf).
# Setup and onboarding tools are scoped per sub-agent in workflow.py instead.
_GENERAL_TOOL_NAMES = [
    "notes_search", "search_knowledge_base", "search_memory", "add_memory",
    "delete_memory", "check_connector_status",
]


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
    approved: bool = False,
    use_planner: bool = False,
) -> tuple[RunState, StructuredLogger]:
    """
    Run the Conductor agent for a single user message via Google ADK.

    mode: troubleshooting/qa run a single ReAct LlmAgent; setup runs the
    SequentialAgent workflow (RULE-ADK03); onboarding runs the ParallelAgent workflow.

    approved: pre-authorizes write_connector_config for this run (Setup mode HITL
    workaround -- see module docstring and results.md).

    use_planner: wraps the troubleshooting/qa LlmAgent with BuiltInPlanner for the
    A/B comparison in results.md. Ignored for setup/onboarding (workflow agents don't
    reason step-by-step the way the ReAct leaf does).
    """
    from google.adk.agents.run_config import RunConfig
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types as genai_types

    session_id = session_id or str(uuid.uuid4())
    task_id = task_id or "default"
    user_id = user_id or session_id
    catalog_base_url = catalog_base_url or os.environ.get("CATALOG_BASE_URL", "")

    secret_store = make_secret_store(prefer_vault=prefer_vault)
    checkpoints = CheckpointStore(db_path=db_path)
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

    executor = ToolExecutor(
        secret_store=secret_store,
        memory_store=memory_store,
        catalog_base_url=catalog_base_url,
        structured_logger=structured_logger,
    )
    tool_functions = build_adk_tool_functions(executor)
    guard = ToolCallGuard(structured_logger)
    before_tool_callback = guard.before_tool_callback
    after_tool_callback = guard.after_tool_callback

    structured_logger.log_run_start(user_message=user_message)
    structured_logger._write({
        "event": "session_context",
        "step_id": "init",
        "session_id": session_id,
        "user_id": user_id,
        "mode": mode,
        "has_history": bool(history_context),
        # SetupStateMachine unwired this lab -- the Workflow graph enforces step order
        # structurally instead (RULE-ADK03). Non-None so the cross-lab smoke test's
        # shape check still holds; the value documents *why* there's no state to report.
        "setup_sm_state": "n/a (structural Workflow graph enforcement, RULE-ADK03)",
        "framework": "google-adk",
    })

    session_service = InMemorySessionService()
    initial_state = {"user_id": user_id, "approved": approved}
    await session_service.create_session(
        app_name="conductor", user_id=user_id, session_id=session_id, state=initial_state,
    )

    is_workflow = mode in ("setup", "onboarding")
    if mode == "setup":
        root_agent = build_setup_workflow(_make_model, tool_functions, before_tool_callback, after_tool_callback)
    elif mode == "onboarding":
        root_agent = build_onboarding_workflow(_make_model, tool_functions, before_tool_callback, after_tool_callback)
    else:
        from google.adk.agents.llm_agent import LlmAgent
        planner = None
        if use_planner:
            from google.adk.planners import BuiltInPlanner
            planner = BuiltInPlanner(
                thinking_config=genai_types.ThinkingConfig(include_thoughts=True, thinking_budget=1024)
            )
        system_prompt = build_system_prompt(user_id=user_id, mode=mode, history_context=history_context)
        agent_tools = [tool_functions[name] for name in _GENERAL_TOOL_NAMES]
        skills_toolset = make_skills_toolset()
        if skills_toolset is not None:
            agent_tools.append(skills_toolset)
        root_agent = LlmAgent(
            name="ConductorAgent",
            model=_make_model(),
            instruction=system_prompt,
            tools=agent_tools,
            before_tool_callback=before_tool_callback,
            after_tool_callback=after_tool_callback,
            planner=planner,
            output_key="final_result",
        )

    # Workflow (setup/onboarding) is a BaseNode, not a BaseAgent -- Runner's node=
    # parameter is its dedicated entry point; run_async()'s call shape is identical
    # either way, so nothing below this line depends on which path was taken.
    if is_workflow:
        runner = Runner(node=root_agent, app_name="conductor", session_service=session_service)
    else:
        runner = Runner(agent=root_agent, app_name="conductor", session_service=session_service)

    t_run_start = time.monotonic()
    final_answer: str | None = None
    tool_call_count = 0
    input_tokens = 0
    output_tokens = 0
    real_tokens = False

    run_config = RunConfig(max_llm_calls=MAX_ITERATIONS)

    try:
        content = genai_types.Content(role="user", parts=[genai_types.Part(text=user_message)])
        t_llm_start = time.monotonic()
        async for event in runner.run_async(
            user_id=user_id, session_id=session_id, new_message=content, run_config=run_config,
        ):
            usage = getattr(event, "usage_metadata", None)
            if usage is not None:
                real_tokens = True
                input_tokens += getattr(usage, "prompt_token_count", 0) or 0
                output_tokens += getattr(usage, "candidates_token_count", 0) or 0
            parts = getattr(event.content, "parts", None) if getattr(event, "content", None) else None
            for part in parts or []:
                if getattr(part, "function_call", None):
                    tool_call_count += 1
        llm_duration_ms = round((time.monotonic() - t_llm_start) * 1000, 1)

        session = await session_service.get_session(app_name="conductor", user_id=user_id, session_id=session_id)
        session_state = session.state or {}

        if mode == "onboarding":
            parts_out = [
                session_state.get("onboarding_status_result"),
                session_state.get("onboarding_catalog_result"),
                session_state.get("onboarding_memory_result"),
            ]
            final_answer = "\n".join(p for p in parts_out if p) or None
        else:
            final_answer = session_state.get("final_result")

        if not real_tokens:
            # ponytail: char/4 approximation, same fallback Deep Agents needed (RULE-DA03 lineage)
            input_tokens = len(user_message) // 4
            output_tokens = len(final_answer or "") // 4

        state.status = RunStatus.COMPLETED
        state.step_count = tool_call_count
        state.final_answer = final_answer

        # RULE-O01: log_llm_call() writes gen_ai.usage.input_tokens/output_tokens --
        # eval/runner.py's _extract_token_metrics() reads exactly these keys, unchanged
        # across every 6x lab, so this run stays comparable without a per-lab edit.
        structured_logger.log_llm_call(
            step_id="run", parent_step_id=None, input_messages=[],
            output_text=final_answer or "", input_tokens=input_tokens,
            output_tokens=output_tokens, duration_ms=llm_duration_ms, status="success",
        )
        structured_logger._write({
            "event": "token_cost",
            "step_id": "post-run",
            "query_type": mode,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "real_tokens": real_tokens,
            "tool_call_count": tool_call_count,
        })

        # Persist messages for cross-session continuity (RULE-STO03/STO04)
        messages_for_persist = list(prior_messages) + [
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": final_answer or ""},
        ]
        checkpoints.save_messages(session_id, task_id, messages_for_persist)
        sessions.save(session_id, task_id, messages_for_persist)
        structured_logger._write({
            "event": "session_saved",
            "step_id": "post-run",
            "session_id": session_id,
            "message_count": len(messages_for_persist),
        })

        state.steps.append(StepRecord(
            step_id="adk-run",
            tool_name="GoogleADK",
            status="success",
            duration_ms=round((time.monotonic() - t_run_start) * 1000, 1),
        ))

    except Exception as exc:
        if "max_llm_calls" in str(exc).lower() or type(exc).__name__ == "LlmCallsLimitExceededError":
            structured_logger._write({
                "event": "limit_reached", "step_id": "run", "max_llm_calls": MAX_ITERATIONS,
            })
            state.status = RunStatus.LIMIT_REACHED
        else:
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

    checkpoints.save(AgentState(
        session_id=session_id,
        task_id=task_id,
        current_step=state.step_count,
        total_steps=8,
        status=state.status.value,
    ))

    return state, structured_logger
