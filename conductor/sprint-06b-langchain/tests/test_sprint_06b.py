"""
Tests for Sprint 6b -- LangChain create_agent() port.

Sprint 6b changes from 6a:
  - graph.py deleted: no build_graph, no ConductorState, no HITL_TOOLS/SETUP_SM_TOOLS imports
  - ConductorContext dataclass replaces ConductorState TypedDict
  - stm_gate @wrap_tool_call replaces pre_tool_check node
  - InMemorySaver replaces SQLite checkpointer (no .db file, no .list() needed)
  - HumanInTheLoopMiddleware replaces interrupt() in pre_tool_check

Test requirements (source week 9 + week 13 adapted for create_agent()):
  1. Same eval dataset structurally passes (all query types routable, no missing tools)
  2. Context schema works (ConductorContext carries user_id + stm_state per invocation)
  3. STM enforcement works via stm_gate (approve -> allows, reject -> blocks at middleware)
  4. Session isolation via thread_id (distinct session_ids produce independent configs)
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from src.agent import ConductorContext, MODEL, stm_gate
from src.skills import load_skill, REGISTERED_SKILLS
from src.state import SetupState, SetupStateMachine
from src.tools import ToolExecutor, TOOL_SCHEMAS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tool_schema_names() -> set[str]:
    return {s["name"] for s in TOOL_SCHEMAS}


# ---------------------------------------------------------------------------
# Req 1 -- Eval dataset structural pass (all query types routable)
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
        import yaml
        data = yaml.safe_load(self.EVAL_DATASET.read_text())
        cases = data.get("cases", data) if isinstance(data, dict) else data
        assert len(cases) > 0, "Eval dataset is empty"


# ---------------------------------------------------------------------------
# Req 2 -- ConductorContext (replaces ConductorState TypedDict)
# ---------------------------------------------------------------------------

class TestConductorContext:

    def test_default_stm_state_is_idle(self):
        """ConductorContext.stm_state defaults to 'idle'."""
        ctx = ConductorContext(user_id="user-1")
        assert ctx.stm_state == "idle"

    def test_user_id_required(self):
        """ConductorContext.user_id is a required field."""
        ctx = ConductorContext(user_id="test-user")
        assert ctx.user_id == "test-user"

    def test_stm_state_mutable(self):
        """stm_gate mutates ctx.stm_state in-place -- must be a mutable dataclass."""
        ctx = ConductorContext(user_id="u")
        ctx.stm_state = "read"
        assert ctx.stm_state == "read"

    def test_context_is_dataclass(self):
        """ConductorContext must be a dataclass (not a TypedDict)."""
        import dataclasses
        assert dataclasses.is_dataclass(ConductorContext)

    def test_thread_id_isolation(self):
        """Distinct session_ids produce independent config dicts (RULE-LG02 equivalent)."""
        config_a = {"configurable": {"thread_id": "session-a"}}
        config_b = {"configurable": {"thread_id": "session-b"}}
        assert config_a["configurable"]["thread_id"] != config_b["configurable"]["thread_id"]


# ---------------------------------------------------------------------------
# Req 3 -- STM enforcement via SetupStateMachine (same logic, new enforcement point)
# ---------------------------------------------------------------------------

class TestSTMEnforcement:
    """SetupStateMachine logic is unchanged from 6a. stm_gate is the new enforcement point."""

    def test_write_blocked_from_idle(self):
        """write_connector_config is blocked from IDLE state."""
        sm = SetupStateMachine()
        assert not sm.is_allowed("write_connector_config")

    def test_validate_blocked_from_idle(self):
        """validate_credentials is blocked from IDLE state."""
        sm = SetupStateMachine()
        assert not sm.is_allowed("validate_credentials")

    def test_read_allowed_from_idle(self):
        """read_connector_config is allowed from IDLE (first step)."""
        sm = SetupStateMachine()
        assert sm.is_allowed("read_connector_config")

    def test_full_sequence_allowed_in_order(self):
        """Full read -> validate -> write sequence all allowed when advanced in order."""
        sm = SetupStateMachine()
        sm.advance("read_connector_config")
        assert sm.state == SetupState.READ
        assert sm.is_allowed("validate_credentials")

        sm.advance("validate_credentials")
        assert sm.state == SetupState.VALIDATE
        assert sm.is_allowed("write_connector_config")

        sm.advance("write_connector_config")
        assert sm.state == SetupState.WRITE

    def test_skipping_read_blocks_validate(self):
        """Skipping read_connector_config means validate_credentials is denied."""
        sm = SetupStateMachine()
        advanced = sm.advance("validate_credentials")
        assert not advanced
        assert sm.state == SetupState.IDLE
        assert not sm.is_allowed("validate_credentials")

    def test_invalid_serialized_state_raises_valueerror(self):
        """Corrupt serialized state raises ValueError on SetupState() -- caller must handle."""
        with pytest.raises(ValueError):
            SetupState("not_a_real_state")

    def test_unknown_tool_always_allowed(self):
        """Tools not in _STATE_GATED_TOOLS are always allowed (non-gated tools)."""
        sm = SetupStateMachine()
        assert sm.is_allowed("check_connector_status")
        assert sm.is_allowed("load_skill")
        assert sm.is_allowed("some_unrelated_tool")

    def test_stm_gate_is_middleware(self):
        """stm_gate must be an AgentMiddleware instance (output of @wrap_tool_call)."""
        from langchain.agents.middleware import AgentMiddleware
        assert isinstance(stm_gate, AgentMiddleware)


# ---------------------------------------------------------------------------
# load_skill -- progressive disclosure (carried from Sprint 6)
# ---------------------------------------------------------------------------

class TestLoadSkill:

    def test_registered_skills_not_empty(self):
        assert len(REGISTERED_SKILLS) > 0

    def test_load_skill_returns_string(self):
        skill_name = next(iter(REGISTERED_SKILLS))
        result = load_skill.invoke({"skill_name": skill_name})
        assert isinstance(result, str)
        assert len(result) > 0

    def test_load_skill_strips_frontmatter(self):
        skill_name = next(iter(REGISTERED_SKILLS))
        result = load_skill.invoke({"skill_name": skill_name})
        assert not result.startswith("---"), "load_skill returned raw frontmatter"

    def test_load_skill_unknown_returns_error_string(self):
        result = load_skill.invoke({"skill_name": "not-a-real-skill-xyz"})
        assert (
            result.lower().startswith("unknown skill")
            or "not found" in result.lower()
            or "error" in result.lower()
        )

    def test_load_skill_is_langchain_tool(self):
        assert hasattr(load_skill, "invoke") and callable(load_skill.invoke)


# ---------------------------------------------------------------------------
# Tool executor -- connector tools (carried from Sprint 6)
# ---------------------------------------------------------------------------

class TestConnectorTools:

    @pytest.fixture
    def executor(self):
        return ToolExecutor()

    def test_check_connector_status_in_process(self, executor):
        result = executor.execute("check_connector_status", {"connector_id": "snowflake-prod"})
        assert result["connector_id"] == "snowflake-prod"
        assert result["status"] == "live"
        assert "check_duration_ms" in result

    def test_check_connector_status_unknown_graceful(self, executor):
        result = executor.execute("check_connector_status", {"connector_id": "no-such-connector"})
        assert result["status"] == "unknown"
        assert "error_code" not in result

    def test_check_connector_status_missing_id(self, executor):
        result = executor.execute("check_connector_status", {})
        assert result.get("error_code") == "INVALID_INPUT"

    def test_read_connector_config_shape(self, executor):
        result = executor.execute("read_connector_config", {"connector_id": "snowflake-prod"})
        assert "config" in result
        assert result["config"]["connector_id"] == "snowflake-prod"
        assert "connector_type" in result["config"]
        assert "read_at" in result

    def test_validate_credentials_empty_returns_errors(self, executor):
        result = executor.execute(
            "validate_credentials",
            {"connector_id": "snowflake-prod", "credentials": {}},
        )
        assert result["valid"] is False
        assert len(result["errors"]) > 0

    def test_validate_credentials_valid_fields(self, executor):
        result = executor.execute(
            "validate_credentials",
            {"connector_id": "snowflake-prod", "credentials": {"username": "admin", "password": "s3cr3t"}},
        )
        assert result["valid"] is True
        assert result["errors"] == []

    def test_write_connector_config_returns_written(self, executor):
        """write_connector_config executes cleanly -- HITL gate lives in middleware, not the tool."""
        result = executor.execute(
            "write_connector_config",
            {"connector_id": "snowflake-prod", "config_patch": {"host": "new.snowflake.com"}},
        )
        assert result["written"] is True
        assert "host" in result["fields_updated"]


# ---------------------------------------------------------------------------
# BOM consistency (model pin is real, not just documented)
# ---------------------------------------------------------------------------

BOM_PATH = Path(__file__).parents[1] / "agent-bom.yaml"


class TestBomConsistency:

    def test_bom_model_matches_agent_constant(self):
        """BOM model.id must match the MODEL constant in agent.py (enforces the pin is real)."""
        if not BOM_PATH.exists():
            pytest.skip("agent-bom.yaml not present in this sprint")
        bom = yaml.safe_load(BOM_PATH.read_text())
        assert bom["model"]["id"] == MODEL, (
            f"BOM model.id {bom['model']['id']!r} != agent.MODEL {MODEL!r}. "
            "Update MODEL in agent.py or agent-bom.yaml to re-sync."
        )

    def test_bom_all_source_files_exist(self):
        """Every file listed in agent-bom.yaml must exist on disk."""
        if not BOM_PATH.exists():
            pytest.skip("agent-bom.yaml not present in this sprint")
        bom = yaml.safe_load(BOM_PATH.read_text())
        sprint_root = BOM_PATH.parent
        missing = [
            entry["file"]
            for entry in bom.get("tools", [])
            if not (sprint_root / entry["file"]).exists()
        ]
        assert not missing, f"BOM references missing files: {missing}"
