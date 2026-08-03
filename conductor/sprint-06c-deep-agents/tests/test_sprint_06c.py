"""
Tests for Sprint 6c -- Deep Agents port.

Test requirements (§83 Deep Agents, sprint-6c spec):
  1. create_deep_agent() wiring: model=, system_prompt=, tools=, middleware= all bound (RULE-DA01)
  2. SetupStateMiddleware blocks out-of-sequence tool calls (RULE-DA02, RULE-STM01)
  3. SetupStateMiddleware advances state correctly across full read->validate->write sequence
  4. token_cost event is written to the log at run end (RULE-DA03)
  5. graph.py is gone (Deep Agents owns graph topology)
  6. skills.py: make_skills_middleware() returns SkillsMiddleware (progressive disclosure, not manifest)
  7. agent.py has no graph import (ceiling finding: explicit graph rewiring impossible)
  8. Tool schemas unchanged from sprint-6a (RULE-T01 through T05 carried forward)
  9. Connector tool executor produces expected shapes (parity with sprint-6a)
  10. AGENTS.md exists in src/ (MemoryMiddleware loads it -- replaces custom BeforeAgentMiddleware)

Sprint 6c ceiling findings tested:
  - No HITL_TOOLS constant (interrupt_on is tool-level only in Deep Agents)
  - No build_graph() (graph topology hidden)
  - SetupStateMachine state threaded externally through middleware (not in TypedDict state)
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from src.agent import (
    MODEL,
    SetupStateMiddleware,
    _pydantic_from_schema,
    _build_langchain_tools,
    _extract_final_answer,
    _count_input_tokens,
)

BOM_PATH = Path(__file__).parents[1] / "agent-bom.yaml"
from src.skills import make_skills_middleware
from src.state import SetupState, SetupStateMachine
from src.tools import ToolExecutor, TOOL_SCHEMAS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_logger():
    logger = MagicMock()
    logger._write = MagicMock()
    return logger


def _tool_schema_names() -> set[str]:
    return {s["name"] for s in TOOL_SCHEMAS}


# ---------------------------------------------------------------------------
# 0 — Eval dataset structural checks (carried forward from sprint-6a/6b)
# ---------------------------------------------------------------------------

class TestEvalDatasetStructural:

    EVAL_DATASET = Path(__file__).parents[2] / "evals" / "datasets" / "conductor-v2.yaml"

    def test_setup_sm_tools_have_schemas(self):
        """All tools tracked by the STM must have schemas in TOOL_SCHEMAS."""
        stm_tools = {"read_connector_config", "validate_credentials", "write_connector_config"}
        schema_names = _tool_schema_names()
        for tool_name in stm_tools:
            assert tool_name in schema_names, (
                f"Tool {tool_name!r} is STM-gated but missing from TOOL_SCHEMAS"
            )

    def test_hitl_tool_in_schema(self):
        """write_connector_config must have a schema (it's callable, just interrupted)."""
        assert "write_connector_config" in _tool_schema_names()

    def test_load_skill_not_in_tool_schemas(self):
        """load_skill is appended separately; having it in TOOL_SCHEMAS would double-bind it."""
        assert "load_skill" not in _tool_schema_names()

    def test_eval_dataset_exists_and_has_cases(self):
        """Eval dataset is present and non-empty."""
        if not self.EVAL_DATASET.exists():
            pytest.skip("eval dataset not present in this environment")
        data = yaml.safe_load(self.EVAL_DATASET.read_text())
        cases = data.get("cases", data) if isinstance(data, dict) else data
        assert len(cases) > 0, "Eval dataset is empty"


# ---------------------------------------------------------------------------
# 1 — RULE-DA01: create_deep_agent wiring
# ---------------------------------------------------------------------------

class TestDeepAgentWiring:
    """RULE-DA01: agent.py uses create_deep_agent() with explicit model=, system_prompt=, tools=."""

    def test_no_graph_py_in_src(self):
        """Sprint 6b ceiling: graph.py must not exist in src/ -- Deep Agents owns topology."""
        src_dir = Path(__file__).parent.parent / "src"
        assert not (src_dir / "graph.py").exists(), (
            "graph.py found in src/ -- sprint-6c should not have an explicit graph"
        )

    def test_no_build_graph_import_in_agent(self):
        """Sprint 6b ceiling: agent.py must not import build_graph."""
        agent_path = Path(__file__).parent.parent / "src" / "agent.py"
        content = agent_path.read_text()
        assert "build_graph" not in content, "agent.py still imports build_graph"

    def test_create_deep_agent_call_in_agent(self):
        """RULE-DA01: agent.py calls create_deep_agent()."""
        agent_path = Path(__file__).parent.parent / "src" / "agent.py"
        content = agent_path.read_text()
        assert "create_deep_agent" in content

    def test_system_prompt_from_build_system_prompt(self):
        """RULE-DA01 + RULE-P01: system_prompt= is built via build_system_prompt(), not hardcoded."""
        agent_path = Path(__file__).parent.parent / "src" / "agent.py"
        content = agent_path.read_text()
        assert "build_system_prompt" in content
        # Must not pass soul.md content as a string literal
        assert "system_prompt=" not in content.split("build_system_prompt")[0].split("\n")[-1]

    def test_agents_md_exists_in_src(self):
        """AGENTS.md must be present -- MemoryMiddleware sources point at it."""
        agents_md = Path(__file__).parent.parent / "src" / "AGENTS.md"
        assert agents_md.exists(), "AGENTS.md not found -- MemoryMiddleware source missing"
        content = agents_md.read_text()
        assert len(content) > 100, "AGENTS.md is empty"


# ---------------------------------------------------------------------------
# 2 & 3 — RULE-DA02: SetupStateMiddleware
# ---------------------------------------------------------------------------

class TestSetupStateMiddleware:
    """RULE-DA02: SetupStateMiddleware enforces read->validate->write via wrap_tool_call."""

    @pytest.fixture
    def middleware(self):
        sm = SetupStateMachine()
        logger = _make_mock_logger()
        return SetupStateMiddleware(sm, logger), sm, logger

    def test_allows_read_from_idle(self, middleware):
        """read_connector_config is the first allowed step (IDLE state)."""
        mw, sm, log = middleware
        call = MagicMock()
        call.name = "read_connector_config"
        next_fn = MagicMock(return_value={"content": "config data"})
        result = mw.wrap_tool_call(call, next_fn)
        next_fn.assert_called_once()
        assert result == {"content": "config data"}

    def test_blocks_validate_before_read(self, middleware):
        """validate_credentials is blocked from IDLE (step 1 not done)."""
        mw, sm, log = middleware
        call = MagicMock()
        call.name = "validate_credentials"
        next_fn = MagicMock()
        result = mw.wrap_tool_call(call, next_fn)
        next_fn.assert_not_called()
        assert "blocked" in result["content"].lower() or "SetupStateMachine" in result["content"]
        log._write.assert_called()
        write_call = log._write.call_args[0][0]
        assert write_call["event"] == "tool_call"
        assert write_call["status"] == "error"
        assert write_call.get("stm_blocked") is True

    def test_blocks_write_before_validate(self, middleware):
        """write_connector_config is blocked until validate step completes."""
        mw, sm, log = middleware
        # Advance to READ state
        sm.advance("read_connector_config")
        # write_connector_config requires VALIDATE state, not READ
        call = MagicMock()
        call.name = "write_connector_config"
        next_fn = MagicMock()
        result = mw.wrap_tool_call(call, next_fn)
        next_fn.assert_not_called()
        assert "blocked" in result["content"].lower()

    def test_full_sequence_all_allowed(self, middleware):
        """Full read -> validate -> write sequence passes through middleware."""
        mw, sm, log = middleware
        for tool_name in ["read_connector_config", "validate_credentials", "write_connector_config"]:
            call = MagicMock()
            call.name = tool_name
            next_fn = MagicMock(return_value={"content": f"{tool_name} result"})
            result = mw.wrap_tool_call(call, next_fn)
            next_fn.assert_called_once()
            assert result["content"] == f"{tool_name} result"

    def test_advances_sm_state_on_success(self, middleware):
        """After a successful tool call, SM state advances correctly."""
        mw, sm, log = middleware
        assert sm.state == SetupState.IDLE
        call = MagicMock()
        call.name = "read_connector_config"
        mw.wrap_tool_call(call, MagicMock(return_value={}))
        assert sm.state == SetupState.READ

    def test_non_sequence_tool_always_allowed(self, middleware):
        """Tools not in the SM gate are always allowed regardless of state."""
        mw, sm, log = middleware
        call = MagicMock()
        call.name = "search_knowledge_base"
        next_fn = MagicMock(return_value={"content": "results"})
        result = mw.wrap_tool_call(call, next_fn)
        next_fn.assert_called_once()

    def test_wrap_tool_call_logs_stm_advance(self, middleware):
        """Successful tool calls log the stm_advance event (observable behavior)."""
        mw, sm, log = middleware
        call = MagicMock()
        call.name = "read_connector_config"
        mw.wrap_tool_call(call, MagicMock(return_value={}))
        write_calls = [c[0][0] for c in log._write.call_args_list]
        tool_events = [c for c in write_calls if c.get("event") == "tool_call"]
        assert len(tool_events) == 1
        assert tool_events[0]["tool_name"] == "read_connector_config"
        assert tool_events[0]["status"] == "success"
        assert "duration_ms" in tool_events[0]

    def test_hitl_tools_dict_not_set(self):
        """RULE-DA04: _HITL_TOOLS is a dict (interrupt_on= config), not a set (sprint-6 pattern)."""
        import src.agent as agent_module
        assert hasattr(agent_module, "_HITL_TOOLS"), "_HITL_TOOLS dict missing from agent.py"
        assert isinstance(agent_module._HITL_TOOLS, dict), "_HITL_TOOLS must be a dict (interrupt_on= shape)"
        # write_connector_config is the Setup mode write gate
        assert "write_connector_config" in agent_module._HITL_TOOLS

    def test_checkpointer_module_level(self):
        """RULE-DA04: _CHECKPOINTER is a MemorySaver instance -- required for interrupt/resume."""
        import src.agent as agent_module
        from langgraph.checkpoint.memory import MemorySaver
        assert hasattr(agent_module, "_CHECKPOINTER")
        assert isinstance(agent_module._CHECKPOINTER, MemorySaver)


# ---------------------------------------------------------------------------
# 4 — RULE-DA03: token_cost event logged at run end
# ---------------------------------------------------------------------------

class TestTokenCostLogging:
    """RULE-DA03: token cost per query type logged at run end for Lab 11 baseline."""

    def test_count_input_tokens_string_content(self):
        """_count_input_tokens handles string content messages."""
        from langchain_core.messages import AIMessage, HumanMessage
        messages = [
            HumanMessage(content="Hello, how do I set up Snowflake?"),
            AIMessage(content="Here are the steps..."),
        ]
        token_count = _count_input_tokens(messages)
        assert token_count > 0
        assert isinstance(token_count, int)

    def test_count_input_tokens_list_content(self):
        """_count_input_tokens handles list-of-blocks content (tool use messages)."""
        from langchain_core.messages import AIMessage
        msg = AIMessage(content=[{"type": "text", "text": "I'll look that up for you."}])
        count = _count_input_tokens([msg])
        assert count > 0

    def test_count_input_tokens_empty(self):
        """_count_input_tokens returns 0 for empty message list."""
        assert _count_input_tokens([]) == 0

    def test_agent_py_logs_token_cost_event(self):
        """RULE-DA03: agent.py contains token_cost event write call."""
        agent_path = Path(__file__).parent.parent / "src" / "agent.py"
        content = agent_path.read_text()
        assert '"token_cost"' in content or "'token_cost'" in content

    def test_agent_py_logs_query_type(self):
        """RULE-DA03: token_cost log includes query_type field (Setup vs Troubleshooting baseline)."""
        agent_path = Path(__file__).parent.parent / "src" / "agent.py"
        content = agent_path.read_text()
        assert "query_type" in content


# ---------------------------------------------------------------------------
# 5 — graph.py is gone (redundant with test 1 above, explicit check)
# ---------------------------------------------------------------------------

def test_graph_module_not_importable():
    """Sprint 6b ceiling: src.graph must not be importable."""
    import importlib
    try:
        importlib.import_module("src.graph")
        pytest.fail("src.graph imported successfully -- should have been deleted in sprint-6c")
    except ModuleNotFoundError:
        pass  # expected


# ---------------------------------------------------------------------------
# 6 — skills.py: SkillsMiddleware via make_skills_middleware() (progressive disclosure)
# ---------------------------------------------------------------------------

class TestSkillsAdapter:
    """Sprint 6b skills: SkillsMiddleware reads .claude/skills/ with progressive disclosure."""

    def test_load_skill_tool_not_in_skills_py(self):
        """load_skill @tool removed -- Deep Agents uses SkillsMiddleware natively."""
        import src.skills as skills_module
        assert not hasattr(skills_module, "load_skill"), (
            "load_skill @tool still present in skills.py -- should be removed in sprint-6c"
        )

    def test_get_skill_manifest_not_in_skills_py(self):
        """get_skill_manifest() replaced by make_skills_middleware() -- manifest approach was wrong."""
        import src.skills as skills_module
        assert not hasattr(skills_module, "get_skill_manifest"), (
            "get_skill_manifest still present -- replaced by make_skills_middleware() in sprint-6c"
        )

    def test_make_skills_middleware_callable(self):
        """make_skills_middleware() is the sprint-6c entry point for SkillsMiddleware."""
        assert callable(make_skills_middleware)

    def test_make_skills_middleware_returns_middleware_or_none(self):
        """make_skills_middleware() returns a SkillsMiddleware instance or None (soft failure)."""
        result = make_skills_middleware()
        if result is not None:
            from deepagents.middleware import SkillsMiddleware
            assert isinstance(result, SkillsMiddleware)


# ---------------------------------------------------------------------------
# 7 — agent.py ceiling: no graph import
# ---------------------------------------------------------------------------

def test_agent_py_has_no_graph_import():
    """Sprint 6b ceiling: agent.py must not import from graph module."""
    agent_path = Path(__file__).parent.parent / "src" / "agent.py"
    content = agent_path.read_text()
    assert "from .graph" not in content
    assert "from src.graph" not in content
    assert "import graph" not in content


# ---------------------------------------------------------------------------
# 8 — Tool schemas unchanged (RULE-T01 through T05 carried forward)
# ---------------------------------------------------------------------------

class TestToolSchemasParity:
    """Tool schemas must cover the same set of tools as sprint-6a (RULE-T01 through T05)."""

    REQUIRED_TOOLS = {
        "check_connector_status",
        "read_connector_config",
        "validate_credentials",
        "write_connector_config",
        "search_knowledge_base",
    }

    def test_required_tools_present(self):
        """All required connector tools have schema entries."""
        schema_names = _tool_schema_names()
        for name in self.REQUIRED_TOOLS:
            assert name in schema_names, f"Missing tool schema: {name!r}"

    def test_setup_sm_tools_in_schema(self):
        """SetupStateMachine-tracked tools all have schemas."""
        sm_tools = {"read_connector_config", "validate_credentials", "write_connector_config"}
        schema_names = _tool_schema_names()
        for name in sm_tools:
            assert name in schema_names

    def test_schema_has_required_fields(self):
        """Each tool schema has name, description, and input_schema."""
        for schema in TOOL_SCHEMAS:
            assert "name" in schema
            assert "description" in schema
            assert "input_schema" in schema


# ---------------------------------------------------------------------------
# 9 — Connector tool executor parity
# ---------------------------------------------------------------------------

class TestConnectorToolsParity:
    """Tool executor output shapes unchanged from sprint-6a."""

    @pytest.fixture
    def executor(self):
        return ToolExecutor()

    def test_check_connector_status(self, executor):
        """check_connector_status returns expected shape."""
        result = executor.execute("check_connector_status", {"connector_id": "snowflake-prod"})
        assert result["connector_id"] == "snowflake-prod"
        assert result["status"] == "live"
        assert "check_duration_ms" in result

    def test_read_connector_config_shape(self, executor):
        """read_connector_config returns typed config shape."""
        result = executor.execute("read_connector_config", {"connector_id": "snowflake-prod"})
        assert "config" in result
        assert result["config"]["connector_id"] == "snowflake-prod"
        assert "read_at" in result

    def test_validate_credentials_empty_fails(self, executor):
        """validate_credentials with empty creds returns valid=False."""
        result = executor.execute(
            "validate_credentials",
            {"connector_id": "snowflake-prod", "credentials": {}},
        )
        assert result["valid"] is False
        assert len(result["errors"]) > 0

    def test_validate_credentials_valid_fields(self, executor):
        """validate_credentials with required fields returns valid=True."""
        result = executor.execute(
            "validate_credentials",
            {"connector_id": "snowflake-prod", "credentials": {"username": "admin", "password": "s3cr3t"}},
        )
        assert result["valid"] is True

    def test_write_connector_config_written(self, executor):
        """write_connector_config executes without error (STM gate is in middleware, not tool)."""
        result = executor.execute(
            "write_connector_config",
            {"connector_id": "snowflake-prod", "config_patch": {"host": "new.example.com"}},
        )
        assert result["written"] is True


# ---------------------------------------------------------------------------
# 10 — _build_langchain_tools wraps TOOL_SCHEMAS
# ---------------------------------------------------------------------------

class TestLangChainToolsBinding:
    """_build_langchain_tools produces one LangChain StructuredTool per schema."""

    @pytest.fixture
    def lc_tools(self):
        executor = ToolExecutor()
        return _build_langchain_tools(executor)

    def test_produces_one_tool_per_schema(self, lc_tools):
        """One LangChain tool produced per TOOL_SCHEMAS entry."""
        assert len(lc_tools) == len(TOOL_SCHEMAS)

    def test_tool_names_match_schemas(self, lc_tools):
        """LangChain tool names match schema names."""
        lc_names = {t.name for t in lc_tools}
        schema_names = _tool_schema_names()
        assert lc_names == schema_names

    def test_tools_are_callable(self, lc_tools):
        """Each tool has .invoke() method."""
        for tool in lc_tools:
            assert hasattr(tool, "invoke")
            assert callable(tool.invoke)


# ---------------------------------------------------------------------------
# _extract_final_answer
# ---------------------------------------------------------------------------

class TestExtractFinalAnswer:
    """_extract_final_answer returns the last AI text from message list."""

    def test_returns_last_ai_message(self):
        from langchain_core.messages import AIMessage, HumanMessage
        messages = [
            HumanMessage(content="question"),
            AIMessage(content="first answer"),
            AIMessage(content="final answer"),
        ]
        assert _extract_final_answer(messages) == "final answer"

    def test_skips_empty_ai_messages(self):
        from langchain_core.messages import AIMessage, HumanMessage
        messages = [
            HumanMessage(content="question"),
            AIMessage(content="real answer"),
            AIMessage(content=""),
        ]
        assert _extract_final_answer(messages) == "real answer"

    def test_returns_none_on_no_ai_messages(self):
        from langchain_core.messages import HumanMessage
        assert _extract_final_answer([HumanMessage(content="hi")]) is None

    def test_handles_list_content(self):
        from langchain_core.messages import AIMessage
        msg = AIMessage(content=[{"type": "text", "text": "list-based answer"}])
        assert _extract_final_answer([msg]) == "list-based answer"


# ---------------------------------------------------------------------------
# _pydantic_from_schema
# ---------------------------------------------------------------------------

class TestPydanticFromSchema:
    """_pydantic_from_schema builds correct Pydantic model from JSON Schema."""

    def test_required_fields_are_required(self):
        schema = {
            "type": "object",
            "properties": {"connector_id": {"type": "string"}},
            "required": ["connector_id"],
        }
        Model = _pydantic_from_schema("TestModel", schema)
        with pytest.raises(Exception):
            Model()  # connector_id is required -- should fail

    def test_optional_fields_have_defaults(self):
        schema = {
            "type": "object",
            "properties": {"top_k": {"type": "integer", "default": 5}},
            "required": [],
        }
        Model = _pydantic_from_schema("TestModel", schema)
        m = Model()
        assert m.top_k == 5

    def test_empty_schema_produces_model(self):
        schema = {"type": "object", "properties": {}}
        Model = _pydantic_from_schema("EmptyModel", schema)
        m = Model()
        assert m is not None


# ---------------------------------------------------------------------------
# BOM consistency
# ---------------------------------------------------------------------------

class TestBomConsistency:

    def test_bom_model_matches_agent_constant(self):
        """BOM model.id must match the MODEL constant in agent.py (enforces the pin is real)."""
        bom = yaml.safe_load(BOM_PATH.read_text())
        assert bom["model"]["id"] == MODEL, (
            f"BOM model.id {bom['model']['id']!r} != agent.MODEL {MODEL!r}. "
            "Update MODEL in agent.py or agent-bom.yaml to re-sync."
        )

    def test_bom_all_source_files_exist(self):
        """Every file listed in agent-bom.yaml must exist on disk."""
        bom = yaml.safe_load(BOM_PATH.read_text())
        sprint_root = BOM_PATH.parent
        missing = [
            entry["file"]
            for entry in bom.get("tools", [])
            if not (sprint_root / entry["file"]).exists()
        ]
        assert not missing, f"BOM references missing files: {missing}"
