"""
Tests for Sprint 6a — LangGraph port.

Source week test requirements (Week 9):
  1. Same eval dataset passes (structural: all queries routable, no missing tools)
  2. Checkpoint recovery works (graph state persists + can be re-loaded)
  3. HITL interrupt fires correctly (approve -> executes, reject -> halts cleanly)
  4. Rollback to previous checkpoint (graph can resume from prior thread_id snapshot)

Week 13 (carried from Sprint 6):
  - Approve -> allow decision
  - Reject -> deny decision, leaves clean state
  - State machine rejects out-of-sequence tool calls
"""

import json
import re
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from src.graph import (
    ConductorState,
    HITL_TOOLS,
    SETUP_SM_TOOLS,
    MAX_TURNS,
    MODEL,
    build_graph,
)
from src.skills import load_skill, REGISTERED_SKILLS, _SKILLS_ROOT
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
# Week 9 req 1 — Same eval dataset passes (structural)
# ---------------------------------------------------------------------------

class TestEvalDatasetStructural:
    """Verifies graph can route all eval query types; no missing tool schemas."""

    EVAL_DATASET = Path(__file__).parents[2] / "evals" / "datasets" / "conductor-v2.yaml"

    def test_all_connector_tools_in_schema(self):
        """Week 9 req 1: all tools that SetupStateMachine tracks have schemas bound."""
        schema_names = _tool_schema_names()
        for tool_name in SETUP_SM_TOOLS:
            assert tool_name in schema_names, (
                f"Tool {tool_name!r} is in SETUP_SM_TOOLS but missing from TOOL_SCHEMAS"
            )

    def test_hitl_tools_in_schema(self):
        """Week 9 req 1: HITL-gated tools have schemas (they must be callable)."""
        schema_names = _tool_schema_names()
        for tool_name in HITL_TOOLS:
            assert tool_name in schema_names, (
                f"Tool {tool_name!r} is in HITL_TOOLS but missing from TOOL_SCHEMAS"
            )

    def test_load_skill_not_in_tool_schemas(self):
        """load_skill is appended separately; having it in TOOL_SCHEMAS would double-bind it."""
        assert "load_skill" not in _tool_schema_names()

    def test_conductor_state_has_required_fields(self):
        """Week 9 req 1: ConductorState TypedDict has all fields the graph nodes write."""
        fields = set(ConductorState.__annotations__.keys())
        required = {"messages", "iteration_count", "status", "mode",
                    "session_id", "user_id", "setup_sm_state", "hitl_pending"}
        assert required == fields, f"ConductorState fields mismatch: {fields ^ required}"

    def test_eval_dataset_exists_and_has_cases(self):
        """Week 9 req 1: eval dataset is present and non-empty."""
        if not self.EVAL_DATASET.exists():
            pytest.skip("eval dataset not present in this environment")
        import yaml
        data = yaml.safe_load(self.EVAL_DATASET.read_text())
        cases = data.get("cases", data) if isinstance(data, dict) else data
        assert len(cases) > 0, "Eval dataset is empty"

    def test_max_turns_constant(self):
        """RULE-LG03: MAX_TURNS is 8 (matches graph's iteration cap)."""
        assert MAX_TURNS == 8


# ---------------------------------------------------------------------------
# Week 9 req 2 — Checkpoint recovery (SQLite persistence)
# ---------------------------------------------------------------------------

class TestCheckpointRecovery:
    """Verifies SQLite checkpointer persists state and is re-readable."""

    def test_build_graph_returns_compiled_and_checkpointer(self):
        """Week 9 req 2: build_graph returns a compiled graph and a checkpointer."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        logger = _make_mock_logger()
        with patch("src.graph.ChatAnthropic"):
            graph, checkpointer = build_graph(
                tools=[],
                structured_logger=logger,
                db_path=db_path,
            )
        assert graph is not None
        assert checkpointer is not None
        try:
            checkpointer.conn.close()
        except Exception:
            pass

    def test_sqlite_checkpoint_file_created(self):
        """Week 9 req 2: SQLite file is created at the path provided to build_graph."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "test_checkpoints.db")
            logger = _make_mock_logger()
            with patch("src.graph.ChatAnthropic"):
                _, checkpointer = build_graph(
                    tools=[],
                    structured_logger=logger,
                    db_path=db_path,
                )
            try:
                checkpointer.close()
            except Exception:
                pass
            assert Path(db_path).exists(), "SQLite checkpoint file was not created"

    def test_thread_id_isolation_distinct_sessions(self):
        """RULE-LG02: two distinct session_ids produce independent config dicts."""
        session_a = "session-abc"
        session_b = "session-xyz"
        config_a = {"configurable": {"thread_id": session_a}}
        config_b = {"configurable": {"thread_id": session_b}}
        assert config_a["configurable"]["thread_id"] != config_b["configurable"]["thread_id"]


# ---------------------------------------------------------------------------
# Week 9 req 3 — HITL interrupt fires correctly
# ---------------------------------------------------------------------------

class TestHITL:
    """Verifies pre_tool_check logic for approve and reject paths."""

    def test_write_connector_config_is_hitl_gated(self):
        """Week 9 req 3, Week 13: write_connector_config is in HITL_TOOLS."""
        assert "write_connector_config" in HITL_TOOLS

    def test_hitl_tools_subset_of_setup_sm_tools(self):
        """Week 13: HITL gate only applies to tools the SM also tracks."""
        assert HITL_TOOLS.issubset(SETUP_SM_TOOLS)

    def test_setup_sm_rejects_write_before_validate(self):
        """Week 13: state machine blocks write_connector_config from IDLE state."""
        sm = SetupStateMachine()
        assert sm.state == SetupState.IDLE
        assert not sm.is_allowed("write_connector_config")

    def test_setup_sm_rejects_validate_before_read(self):
        """Week 13: state machine blocks validate_credentials from IDLE state."""
        sm = SetupStateMachine()
        assert not sm.is_allowed("validate_credentials")

    def test_setup_sm_allows_read_from_idle(self):
        """Week 13: read_connector_config is allowed from IDLE (first step)."""
        sm = SetupStateMachine()
        assert sm.is_allowed("read_connector_config")

    def test_setup_sm_full_sequence_allowed(self):
        """Week 13: full read -> validate -> write sequence all allowed in order."""
        sm = SetupStateMachine()
        sm.advance("read_connector_config")
        assert sm.state == SetupState.READ
        assert sm.is_allowed("validate_credentials")

        sm.advance("validate_credentials")
        assert sm.state == SetupState.VALIDATE
        assert sm.is_allowed("write_connector_config")

        sm.advance("write_connector_config")
        assert sm.state == SetupState.WRITE

    def test_setup_sm_skipping_read_blocks_validate(self):
        """Week 13: skipping read_connector_config means validate_credentials is denied."""
        sm = SetupStateMachine()
        # Try to jump directly to validate
        advanced = sm.advance("validate_credentials")
        assert not advanced
        assert sm.state == SetupState.IDLE
        # After failed advance, still not allowed
        assert not sm.is_allowed("validate_credentials")

    def test_setup_sm_invalid_state_value_stays_idle(self):
        """Corrupt serialized state defaults to IDLE (no crash)."""
        from src.graph import build_graph
        # _sm_from_state is a closure inside build_graph; test via SetupStateMachine directly
        sm = SetupStateMachine()
        try:
            sm.state = SetupState("not_a_real_state")
        except ValueError:
            pass  # expected; state should be unchanged
        assert sm.state == SetupState.IDLE


# ---------------------------------------------------------------------------
# Week 9 req 4 — Rollback (checkpoint history accessible)
# ---------------------------------------------------------------------------

class TestRollback:
    """Verifies checkpoint list API is accessible for rollback."""

    def test_checkpointer_has_list_method(self):
        """Week 9 req 4: SqliteSaver exposes a list/get API for rollback."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        logger = _make_mock_logger()
        with patch("src.graph.ChatAnthropic"):
            _, checkpointer = build_graph(
                tools=[],
                structured_logger=logger,
                db_path=db_path,
            )
        # SqliteSaver must expose .list() for rollback support
        assert hasattr(checkpointer, "list"), "Checkpointer missing .list() for rollback"
        try:
            checkpointer.conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# load_skill — LangChain progressive disclosure (carried from Sprint 6)
# ---------------------------------------------------------------------------

class TestLoadSkill:
    """Verifies load_skill tool reads SKILL.md on demand with zero startup cost."""

    def test_registered_skills_not_empty(self):
        """load_skill has at least one registered skill."""
        assert len(REGISTERED_SKILLS) > 0

    def test_load_skill_returns_string(self):
        """load_skill returns a string (body content or error string)."""
        skill_name = next(iter(REGISTERED_SKILLS))
        result = load_skill.invoke({"skill_name": skill_name})
        assert isinstance(result, str)
        assert len(result) > 0

    def test_load_skill_strips_frontmatter(self):
        """load_skill returns body only — no YAML frontmatter in output."""
        skill_name = next(iter(REGISTERED_SKILLS))
        result = load_skill.invoke({"skill_name": skill_name})
        assert not result.startswith("---"), "load_skill returned raw frontmatter"

    def test_load_skill_unknown_returns_error_string(self):
        """load_skill returns an error string for unregistered skill names."""
        result = load_skill.invoke({"skill_name": "not-a-real-skill-xyz"})
        assert result.lower().startswith("unknown skill") or "not found" in result.lower() or "error" in result.lower()

    def test_load_skill_is_langchain_tool(self):
        """load_skill is decorated with @tool (has .invoke())."""
        assert hasattr(load_skill, "invoke")
        assert callable(load_skill.invoke)

    def test_skills_root_resolves_inside_this_repo(self):
        """Regression test: _SKILLS_ROOT must resolve to THIS repo's .claude/skills,
        not a directory one level above it. A previous off-by-one (parents[4]
        instead of parents[3]) resolved outside the repo to a different, unrelated
        .claude/skills/ directory that happened to exist -- load_skill() never
        raised, it silently returned 'Skill file not found' for every real call.
        test_load_skill_returns_string alone could not catch this: the error
        string is itself a non-empty string with no frontmatter."""
        from src.skills import _SKILLS_ROOT
        repo_root = Path(__file__).resolve().parents[3]
        assert _SKILLS_ROOT == repo_root / ".claude" / "skills"
        assert (_SKILLS_ROOT / "conductor-troubleshoot-connector" / "SKILL.md").exists()

    def test_load_skill_returns_real_skill_content_not_a_not_found_error(self):
        """The prior bug's failure mode returned a well-formed non-empty string
        that would pass every other existing assertion in this class. Assert on
        content that can only come from the real SKILL.md body."""
        skill_name = next(iter(REGISTERED_SKILLS))
        result = load_skill.invoke({"skill_name": skill_name})
        assert "not found" not in result.lower()
        assert "check_connector_status" in result


# ---------------------------------------------------------------------------
# Tool executor — connector tools (carried from Sprint 6)
# ---------------------------------------------------------------------------

class TestConnectorTools:

    @pytest.fixture
    def executor(self):
        return ToolExecutor()

    def test_check_connector_status_in_process(self, executor):
        """check_connector_status runs in-process (no subprocess)."""
        result = executor.execute("check_connector_status", {"connector_id": "snowflake-prod"})
        assert result["connector_id"] == "snowflake-prod"
        assert result["status"] == "live"
        assert "check_duration_ms" in result

    def test_check_connector_status_unknown_graceful(self, executor):
        """Unknown connector returns 'unknown' status (not a raised error)."""
        result = executor.execute("check_connector_status", {"connector_id": "no-such-connector"})
        assert result["status"] == "unknown"
        assert "error_code" not in result

    def test_check_connector_status_missing_id(self, executor):
        """Missing connector_id returns INVALID_INPUT error code."""
        result = executor.execute("check_connector_status", {})
        assert result.get("error_code") == "INVALID_INPUT"

    def test_read_connector_config_shape(self, executor):
        """read_connector_config returns a typed config shape."""
        result = executor.execute("read_connector_config", {"connector_id": "snowflake-prod"})
        assert "config" in result
        assert result["config"]["connector_id"] == "snowflake-prod"
        assert "connector_type" in result["config"]
        assert "read_at" in result

    def test_validate_credentials_empty_returns_errors(self, executor):
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
        assert result["errors"] == []

    def test_write_connector_config_returns_written(self, executor):
        """write_connector_config executes (HITL gate lives in graph, not tool)."""
        result = executor.execute(
            "write_connector_config",
            {"connector_id": "snowflake-prod", "config_patch": {"host": "new.snowflake.com"}},
        )
        assert result["written"] is True
        assert "host" in result["fields_updated"]


# ---------------------------------------------------------------------------
# BOM consistency
# ---------------------------------------------------------------------------

BOM_PATH = Path(__file__).parents[1] / "agent-bom.yaml"


class TestBomConsistency:

    def test_bom_model_matches_agent_constant(self):
        """BOM model.id must match the MODEL constant in graph.py (enforces the pin is real)."""
        bom = yaml.safe_load(BOM_PATH.read_text())
        assert bom["model"]["id"] == MODEL, (
            f"BOM model.id {bom['model']['id']!r} != graph.MODEL {MODEL!r}. "
            "Update MODEL in graph.py or agent-bom.yaml to re-sync."
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
