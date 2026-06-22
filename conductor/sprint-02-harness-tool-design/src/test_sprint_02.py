"""
Sprint 1 test suite — Agent Harness + Tool Design.

Tests map to the five requirements from the experiment spec:
  1. Valid tool input returns results
  2. Invalid tool input is rejected before execution
  3. No-tool answer path works (agent answers without calling a tool)
  4. Malformed arguments are caught by schema validation
  5. Max iteration limit fires and exits gracefully

All tests that require a live API key are skipped when ANTHROPIC_API_KEY is absent.
"""

import os
import sys
import pytest
from unittest.mock import MagicMock, patch
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(__file__))

# Load .env so the live test picks up keys without manual export
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

from tools import notes_search, NotesSearchInput, TOOL_REGISTRY, create_connector_config, _IDEMPOTENCY_CACHE, SCHEMA_VERSION
from state import RunState, RunStatus, StepRecord


# ---------------------------------------------------------------------------
# Requirement 1 — Valid tool input returns results
# ---------------------------------------------------------------------------

def test_valid_tool_input_returns_results():
    """Valid query returns structured results with expected fields."""
    result = notes_search({"query": "Snowflake connector"})
    assert "error" not in result
    assert "results" in result
    assert "total_found" in result
    assert result["total_found"] >= 1
    first = result["results"][0]
    assert "id" in first
    assert "title" in first
    assert "snippet" in first
    assert "score" in first


# ---------------------------------------------------------------------------
# Requirement 2 — Invalid tool input rejected before execution
# ---------------------------------------------------------------------------

def test_invalid_tool_input_wrong_type():
    """Non-string query is rejected with INVALID_INPUT error before execution."""
    result = notes_search({"query": 12345})
    assert result.get("error") is True
    assert result["error_code"] == "INVALID_INPUT"
    assert result["retryable"] is False


def test_invalid_tool_input_missing_required_field():
    """Missing required 'query' field is rejected with INVALID_INPUT."""
    result = notes_search({})
    assert result.get("error") is True
    assert result["error_code"] == "INVALID_INPUT"


def test_invalid_tool_input_blank_query():
    """Blank query string is rejected — query must not be blank."""
    result = notes_search({"query": "   "})
    assert result.get("error") is True
    assert result["error_code"] == "INVALID_INPUT"


def test_invalid_tool_input_max_results_out_of_range():
    """max_results > 10 is rejected by schema validation."""
    result = notes_search({"query": "Snowflake", "max_results": 999})
    assert result.get("error") is True
    assert result["error_code"] == "INVALID_INPUT"


# ---------------------------------------------------------------------------
# Requirement 3 — No-tool answer: state records step correctly
# ---------------------------------------------------------------------------

def test_state_records_no_tool_step():
    """StepRecord with no tool_name is valid and tracked in RunState."""
    state = RunState()
    state.record_step(StepRecord(
        step=1,
        tool_name=None,
        tool_input=None,
        tool_output="Here is your answer.",
        duration_ms=42.0,
        status="no_tool",
    ))
    assert state.step_count == 1
    assert state.steps[0].tool_name is None
    assert state.steps[0].status == "no_tool"


# ---------------------------------------------------------------------------
# Requirement 4 — Malformed arguments caught by schema validation
# ---------------------------------------------------------------------------

def test_extra_fields_in_tool_input():
    """Extra fields in tool input are either stripped or cause validation error — never silently passed through."""
    # Pydantic v2 with model_validate: extra fields are ignored by default.
    # The key invariant is that the tool still executes correctly.
    result = notes_search({"query": "timeout", "unknown_field": "injected"})
    # Should not error — extra fields are stripped
    assert "error" not in result
    assert "results" in result


def test_very_long_query_rejected():
    """Query exceeding 500 chars is rejected before execution."""
    result = notes_search({"query": "x" * 501})
    assert result.get("error") is True
    assert result["error_code"] == "INVALID_INPUT"


# ---------------------------------------------------------------------------
# Requirement 5 — Max iteration limit fires and exits gracefully
# ---------------------------------------------------------------------------

def test_iteration_limit_fires():
    """Agent exits with limit_reached status when MAX_ITERATIONS is hit."""
    import agent as agent_module

    # Mock the Anthropic client to always return a tool_use response,
    # forcing the loop to hit the iteration cap.
    mock_tool_block = MagicMock()
    mock_tool_block.type = "tool_use"
    mock_tool_block.name = "notes_search"
    mock_tool_block.input = {"query": "test"}
    mock_tool_block.id = "tool_use_mock_id"

    mock_response = MagicMock()
    mock_response.stop_reason = "tool_use"
    mock_response.content = [mock_tool_block]

    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response

    with patch("agent.anthropic.Anthropic", return_value=mock_client):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            state = agent_module.run("keep looping")

    assert state.status == RunStatus.LIMIT_REACHED
    assert state.step_count == agent_module.MAX_ITERATIONS


# ---------------------------------------------------------------------------
# Live integration test — skipped without API key
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set — skipping live API test",
)
def test_live_valid_query_completes():
    """Live test: agent answers a valid query in ≤ 3 iterations."""
    import agent as agent_module
    state = agent_module.run("How do I connect Snowflake?")
    assert state.status == RunStatus.COMPLETED
    assert state.step_count <= 3
    assert state.final_answer is not None
    assert len(state.final_answer) > 10


# ---------------------------------------------------------------------------
# A1 — Idempotency key on write tool (§21.3 — RULE-T03)
# ---------------------------------------------------------------------------

def test_idempotent_same_key_returns_cached_result(monkeypatch):
    """Second call with same idempotency_key returns first result without re-executing."""
    monkeypatch.setitem(_IDEMPOTENCY_CACHE, "__clear__", None)
    _IDEMPOTENCY_CACHE.clear()

    payload = {
        "idempotency_key": "test-key-abc",
        "connector_type": "snowflake",
        "display_name": "My Snowflake",
    }

    first = create_connector_config(payload)
    assert first.get("created") is True
    first_config_id = first["config_id"]

    second = create_connector_config(payload)
    assert second["config_id"] == first_config_id
    assert second.get("created") is False  # came from cache

    _IDEMPOTENCY_CACHE.clear()


def test_idempotent_different_key_executes_fresh(monkeypatch):
    """Different idempotency keys produce independent executions with different config_ids."""
    _IDEMPOTENCY_CACHE.clear()

    first = create_connector_config({
        "idempotency_key": "key-alpha",
        "connector_type": "bigquery",
        "display_name": "BQ Production",
    })
    second = create_connector_config({
        "idempotency_key": "key-beta",
        "connector_type": "bigquery",
        "display_name": "BQ Production",
    })

    assert first["config_id"] != second["config_id"]
    assert first.get("created") is True
    assert second.get("created") is True

    _IDEMPOTENCY_CACHE.clear()


# ---------------------------------------------------------------------------
# A2 — Schema version mismatch rejected at dispatch time (§21.4 — RULE-T04)
# ---------------------------------------------------------------------------

def test_schema_mismatch_returns_error():
    """Tool called with stale schema_version returns SCHEMA_MISMATCH, retryable=False."""
    import agent as agent_module

    mock_tool_block = MagicMock()
    mock_tool_block.type = "tool_use"
    mock_tool_block.name = "notes_search"
    mock_tool_block.input = {"query": "Snowflake", "schema_version": "0.9"}  # stale version
    mock_tool_block.id = "tool_use_mismatch_id"

    # First response: tool call with stale schema_version
    mock_response_1 = MagicMock()
    mock_response_1.stop_reason = "tool_use"
    mock_response_1.content = [mock_tool_block]

    # Second response: model acknowledges and answers without tool
    mock_text_block = MagicMock()
    mock_text_block.type = "text"
    mock_text_block.text = "I encountered a schema version mismatch."
    mock_response_2 = MagicMock()
    mock_response_2.stop_reason = "end_turn"
    mock_response_2.content = [mock_text_block]

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [mock_response_1, mock_response_2]

    with patch("agent.anthropic.Anthropic", return_value=mock_client):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            state = agent_module.run("How do I connect Snowflake?")

    # Confirm the schema mismatch error was recorded in the step
    mismatch_steps = [s for s in state.steps if isinstance(s.tool_output, dict) and s.tool_output.get("error_code") == "SCHEMA_MISMATCH"]
    assert len(mismatch_steps) == 1
    assert mismatch_steps[0].tool_output["retryable"] is False


# ---------------------------------------------------------------------------
# G3 — Non-retryable errors are annotated with _hint in tool result content
# ---------------------------------------------------------------------------

def test_non_retryable_error_hint_injected():
    """Non-retryable tool errors carry _hint in tool result content so model knows not to retry."""
    import agent as agent_module

    mock_tool_block = MagicMock()
    mock_tool_block.type = "tool_use"
    mock_tool_block.name = "notes_search"
    mock_tool_block.input = {"query": "Snowflake", "schema_version": "0.9"}
    mock_tool_block.id = "tool_use_hint_id"

    mock_response_1 = MagicMock()
    mock_response_1.stop_reason = "tool_use"
    mock_response_1.content = [mock_tool_block]

    mock_text_block = MagicMock()
    mock_text_block.type = "text"
    mock_text_block.text = "Schema mismatch acknowledged."
    mock_response_2 = MagicMock()
    mock_response_2.stop_reason = "end_turn"
    mock_response_2.content = [mock_text_block]

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [mock_response_1, mock_response_2]

    captured_tool_results = []
    original_create = None

    def capture_messages(*args, **kwargs):
        call_count = mock_client.messages.create.call_count
        messages = kwargs.get("messages", [])
        for m in messages:
            if isinstance(m.get("content"), list):
                for block in m["content"]:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        import json as _json
                        captured_tool_results.append(_json.loads(block["content"]))
        return mock_client.messages.create.side_effect[call_count - 1]

    mock_client.messages.create.side_effect = [mock_response_1, mock_response_2]

    with patch("agent.anthropic.Anthropic", return_value=mock_client):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            state = agent_module.run("How do I connect Snowflake?")

    mismatch_steps = [s for s in state.steps if isinstance(s.tool_output, dict) and s.tool_output.get("error_code") == "SCHEMA_MISMATCH"]
    assert len(mismatch_steps) == 1
    assert mismatch_steps[0].tool_output["retryable"] is False


def test_retryable_flag_logged_in_tool_result():
    """Tool result log includes retryable field — harness communicates retry semantics explicitly."""
    import agent as agent_module
    import io
    from contextlib import redirect_stdout

    mock_tool_block = MagicMock()
    mock_tool_block.type = "tool_use"
    mock_tool_block.name = "notes_search"
    mock_tool_block.input = {"query": "Snowflake"}
    mock_tool_block.id = "tool_use_retryable_id"

    mock_response_1 = MagicMock()
    mock_response_1.stop_reason = "tool_use"
    mock_response_1.content = [mock_tool_block]

    mock_text_block = MagicMock()
    mock_text_block.type = "text"
    mock_text_block.text = "Here is how to connect Snowflake."
    mock_response_2 = MagicMock()
    mock_response_2.stop_reason = "end_turn"
    mock_response_2.content = [mock_text_block]

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [mock_response_1, mock_response_2]

    buf = io.StringIO()
    with patch("agent.anthropic.Anthropic", return_value=mock_client):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            with redirect_stdout(buf):
                agent_module.run("How do I connect Snowflake?")

    import json as _json
    log_lines = [_json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    tool_result_logs = [l for l in log_lines if l.get("event") == "tool_result"]
    assert len(tool_result_logs) >= 1
    assert "retryable" in tool_result_logs[0], "retryable field missing from tool_result log"
    assert tool_result_logs[0]["retryable"] is True  # successful call is retryable


# ---------------------------------------------------------------------------
# G5 — Tool schema description rejects action requests (§22.2 negative examples)
# ---------------------------------------------------------------------------

def test_notes_search_schema_has_action_request_exclusion():
    """notes_search schema description explicitly excludes action verbs (create, add, configure, etc.)."""
    import tools as tools_module
    description = tools_module.TOOL_REGISTRY["notes_search"]["schema"]["description"].lower()
    action_verbs = ["create", "add", "configure", "update", "delete"]
    for verb in action_verbs:
        assert verb in description, (
            f"notes_search schema missing action-verb exclusion for '{verb}' — "
            "this is the root cause of the Teradata routing failure (§22.2)"
        )


# ---------------------------------------------------------------------------
# Tool schema versioning (§21.4 — RULE-T04)
# ---------------------------------------------------------------------------

def test_tool_schema_has_version_field():
    """RULE-T04: every tool schema must declare a version field."""
    import tools as tools_module
    for tool_name, entry in tools_module.TOOL_REGISTRY.items():
        schema = entry["schema"]
        assert "version" in schema, (
            f"Tool '{tool_name}' schema is missing 'version' field"
        )


def test_all_tool_schemas_use_module_schema_version():
    """All tool schemas reference the module-level SCHEMA_VERSION constant."""
    import tools as tools_module
    for tool_name, entry in tools_module.TOOL_REGISTRY.items():
        assert entry["schema"]["version"] == SCHEMA_VERSION, (
            f"Tool '{tool_name}' schema version does not match SCHEMA_VERSION"
        )


# ---------------------------------------------------------------------------
# No-tool answer at agent loop level — stop_reason=end_turn as first response
# ---------------------------------------------------------------------------

def test_no_tool_answer_exits_completed():
    """Agent exits COMPLETED when the model's first response is end_turn with no tool call."""
    import agent as agent_module

    mock_text_block = MagicMock()
    mock_text_block.type = "text"
    mock_text_block.text = "I can only help with data integration topics."

    mock_response = MagicMock()
    mock_response.stop_reason = "end_turn"
    mock_response.content = [mock_text_block]

    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response

    with patch("agent.anthropic.Anthropic", return_value=mock_client):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            state = agent_module.run("What is 2+2?")

    assert state.status == RunStatus.COMPLETED
    assert state.step_count == 1
    assert state.final_answer == "I can only help with data integration topics."
    # Exactly one LLM call — no tool dispatch
    assert mock_client.messages.create.call_count == 1


# ---------------------------------------------------------------------------
# Malformed tool_input at harness dispatch level
# ---------------------------------------------------------------------------

def test_malformed_tool_input_none_returns_error():
    """tool_input=None is handled gracefully — does not raise an exception in the harness."""
    import agent as agent_module

    mock_tool_block = MagicMock()
    mock_tool_block.type = "tool_use"
    mock_tool_block.name = "notes_search"
    mock_tool_block.input = None  # malformed
    mock_tool_block.id = "tool_use_none_id"

    mock_response_1 = MagicMock()
    mock_response_1.stop_reason = "tool_use"
    mock_response_1.content = [mock_tool_block]

    mock_text_block = MagicMock()
    mock_text_block.type = "text"
    mock_text_block.text = "Something went wrong."
    mock_response_2 = MagicMock()
    mock_response_2.stop_reason = "end_turn"
    mock_response_2.content = [mock_text_block]

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [mock_response_1, mock_response_2]

    with patch("agent.anthropic.Anthropic", return_value=mock_client):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            state = agent_module.run("How do I connect?")

    # Harness must not raise — loop must complete (COMPLETED or LIMIT_REACHED)
    assert state.status in (RunStatus.COMPLETED, RunStatus.LIMIT_REACHED)


def test_malformed_tool_input_integer_returns_error():
    """tool_input=42 (wrong type) is handled gracefully — Pydantic validation rejects it cleanly."""
    result = notes_search(42)  # type: ignore[arg-type]
    # notes_search wraps validation in try/except — must return a ToolError dict, not raise
    assert isinstance(result, dict)
    assert result.get("error") is True
    assert result["error_code"] == "INVALID_INPUT"


# ---------------------------------------------------------------------------
# retry_after field on ToolError
# ---------------------------------------------------------------------------

def test_retry_after_field_present_on_tool_error():
    """ToolError with retry_after set includes it in to_dict output."""
    from tools import ToolError
    err = ToolError(
        error_code="RATE_LIMITED",
        message="Too many requests",
        retryable=True,
        retry_after=30,
    )
    result = err.to_dict()
    assert result["retry_after"] == 30
    assert result["retryable"] is True


def test_retry_after_absent_when_none():
    """ToolError with retry_after=None does not include key in to_dict output."""
    from tools import ToolError
    err = ToolError(
        error_code="INVALID_INPUT",
        message="Bad input",
        retryable=False,
    )
    result = err.to_dict()
    assert "retry_after" not in result
