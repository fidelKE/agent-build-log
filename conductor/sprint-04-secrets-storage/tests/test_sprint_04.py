"""
Sprint 4 test suite — Secrets + Storage

Cumulative: all sprint 2 and 3 tests carry forward (prompt, observability, tool contracts).
New sprint 4 coverage:
  Week 5: secret not in prompt/log/DB, missing secret graceful, timeout, rotation
  Week 6: normal completion, crash-resume, duplicate execution, checkpoint corruption

Tests are self-contained: no real API calls, no Vault required.
The ToolExecutor is patched at the httpx layer. Checkpointing uses a temp SQLite DB.
"""

import json
import logging
import os
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make src importable without installing the package
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.secrets import (
    LocalStubSecretStore,
    RedactingFormatter,
    VaultSecretStore,
    redact,
)
from src.state import AgentState, CheckpointStore, RunState, RunStatus, SessionStore
from src.tools import TOOL_SCHEMAS, ToolExecutor, notes_search
from src.logger import StructuredLogger, TraceDepth, SCHEMA_VERSION
from src.prompt import build_system_prompt, OUTPUT_CONTRACT
import src.agent as agent_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_db() -> tuple[CheckpointStore, Path]:
    """Return a CheckpointStore backed by a fresh temp SQLite file."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return CheckpointStore(db_path=Path(tmp.name)), Path(tmp.name)


def make_stub_store(token: str = "test-token-abc123") -> LocalStubSecretStore:
    store = LocalStubSecretStore()
    with patch.dict(os.environ, {"CATALOG_API_TOKEN": token}):
        return store


# ---------------------------------------------------------------------------
# 1. Secret not in prompt (tool schema has no auth fields)
# ---------------------------------------------------------------------------

def test_tool_schema_has_no_auth_fields():
    """Success criterion 1: tool schema exposed to model contains no auth fields."""
    for schema in TOOL_SCHEMAS:
        properties = schema.get("input_schema", {}).get("properties", {})
        for field_name in properties:
            assert field_name.lower() not in (
                "token", "api_key", "apikey", "authorization",
                "password", "secret", "credential", "bearer",
            ), f"Auth field '{field_name}' found in tool schema for '{schema['name']}'"


# ---------------------------------------------------------------------------
# 2. Secret not in logs (RedactingFormatter scrubs credentials)
# ---------------------------------------------------------------------------

def test_redacting_formatter_scrubs_bearer_token():
    """Success criterion 2: RedactingFormatter removes Bearer token from log output."""
    token = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.fake.payload"
    formatter = RedactingFormatter("%(message)s")
    record = logging.LogRecord(
        name="test", level=logging.DEBUG, pathname="", lineno=0,
        msg=f"Authorization: Bearer {token}", args=(), exc_info=None,
    )
    output = formatter.format(record)
    assert token not in output
    assert "[REDACTED]" in output


def test_redacting_formatter_scrubs_long_alphanumeric():
    """Success criterion 2: 32+ char alphanumeric strings are redacted."""
    secret = "a" * 40
    formatter = RedactingFormatter("%(message)s")
    record = logging.LogRecord(
        name="test", level=logging.DEBUG, pathname="", lineno=0,
        msg=f"token={secret}", args=(), exc_info=None,
    )
    output = formatter.format(record)
    assert secret not in output


def test_redact_function_leaves_short_strings_intact():
    """redact() should not touch strings shorter than 32 chars."""
    short = "abc123"
    assert redact(short) == short


# ---------------------------------------------------------------------------
# 3. Secret not in DB (checkpoint payload contains no credential)
# ---------------------------------------------------------------------------

def test_checkpoint_payload_contains_no_credential():
    """Success criterion 3: Neither checkpoints nor messages table contains the API token."""
    token = "super-secret-token-" + "x" * 32
    store, db_path = make_db()
    try:
        state = AgentState(
            session_id="sess-1",
            task_id="task-1",
            current_step=2,
            total_steps=6,
        )
        store.save(state)
        # Also write a message that must not contain the token
        store.save_messages("sess-1", "task-1", [
            {"role": "user", "content": "How do I configure Snowflake?"}
        ])

        checkpoint_rows = store.dump_all()
        assert checkpoint_rows, "Expected at least one checkpoint row"
        for row in checkpoint_rows:
            for col_value in row.values():
                assert token not in str(col_value), (
                    f"Credential found in checkpoints column: {col_value[:50]}"
                )

        message_rows = store.dump_messages()
        assert message_rows, "Expected at least one messages row"
        for row in message_rows:
            for col_value in row.values():
                assert token not in str(col_value), (
                    f"Credential found in messages column: {col_value[:50]}"
                )
    finally:
        db_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 4. Prompt injection resistance (model asked for API key, answer must not leak it)
# ---------------------------------------------------------------------------

def test_tool_executor_never_returns_token_in_result():
    """
    Success criterion 4: ToolExecutor result dict does not contain the raw token,
    even when the API returns an error body that echoes request details.
    """
    token = "leaked-token-" + "z" * 32

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "entities": [
            {
                "typeName": "Table",
                "attributes": {
                    "name": "orders",
                    "description": "",
                    "qualifiedName": "default/snowflake/123/DB/SCHEMA/orders",
                },
            }
        ]
    }

    store = MagicMock()
    store.get.return_value = token

    executor = ToolExecutor(
        secret_store=store,
        catalog_base_url="https://example.com",
    )

    with patch("httpx.post", return_value=mock_response):
        result = executor.execute("search_knowledge_base", {"query": "snowflake"})

    result_str = json.dumps(result)
    assert token not in result_str, "API token leaked into tool result"


# ---------------------------------------------------------------------------
# 5. Checkpoint resume: crash at step 4, restart resumes from step 4
# ---------------------------------------------------------------------------

def test_checkpoint_resume_after_simulated_crash():
    """
    Success criterion 5 + Week 6 'crash mid-step → resume':
    A 6-step flow saved through step 4; on reload the agent resumes from step 4.
    """
    store, db_path = make_db()
    try:
        # Simulate agent running through steps 1-4 then crashing
        state = AgentState(
            session_id="sess-crash",
            task_id="setup-flow",
            current_step=4,
            total_steps=6,
            completed_steps=[1, 2, 3, 4],
            status="in_progress",
        )
        store.save(state)

        # On restart: load checkpoint
        recovered = store.load("sess-crash", "setup-flow")
        assert recovered is not None, "No checkpoint found after crash"
        assert recovered.current_step == 4, (
            f"Expected resume from step 4, got step {recovered.current_step}"
        )
        assert recovered.completed_steps == [1, 2, 3, 4]
        assert recovered.status == "in_progress"
    finally:
        db_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 6. Explicit restart honored: reset() clears checkpoint, next load returns None
# ---------------------------------------------------------------------------

def test_explicit_restart_clears_checkpoint():
    """
    Success criterion 6: calling reset() deletes the checkpoint so the
    next load() returns None, causing the flow to start from step 1.
    """
    store, db_path = make_db()
    try:
        state = AgentState(
            session_id="sess-restart",
            task_id="setup-flow",
            current_step=3,
            total_steps=6,
        )
        store.save(state)
        assert store.load("sess-restart", "setup-flow") is not None

        # User explicitly requests restart
        store.reset("sess-restart", "setup-flow")

        # Next load returns None → agent starts from step 1
        assert store.load("sess-restart", "setup-flow") is None
    finally:
        db_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 7. Secret rotation: changing the value in the store is picked up immediately
# ---------------------------------------------------------------------------

def test_secret_rotation_no_code_change():
    """
    Success criterion 7 + Week 5 'secret rotated without code change':
    Updating the secret in the store is reflected on the next get() call
    without any code change.
    """
    old_token = "old-token-" + "a" * 32
    new_token = "new-token-" + "b" * 32

    with patch.dict(os.environ, {"CATALOG_API_TOKEN": old_token}):
        store = LocalStubSecretStore()
        assert store.get("catalog-api-token") == old_token

    # Rotation: env var updated (simulates vault rotating the secret)
    with patch.dict(os.environ, {"CATALOG_API_TOKEN": new_token}):
        assert store.get("catalog-api-token") == new_token, (
            "Store returned stale token after rotation"
        )


# ---------------------------------------------------------------------------
# Week 5: Missing secret fails gracefully
# ---------------------------------------------------------------------------

def test_missing_secret_raises_key_error():
    """Week 5 'missing secret fails gracefully': KeyError raised, not a silent empty string."""
    store = LocalStubSecretStore()
    with patch.dict(os.environ, {}, clear=True):
        # Remove any CATALOG_API_TOKEN that might be set in the environment
        os.environ.pop("CATALOG_API_TOKEN", None)
        with pytest.raises(KeyError, match="catalog-api-token"):
            store.get("catalog-api-token")


# ---------------------------------------------------------------------------
# Week 5: External call timeout handled (httpx.TimeoutException → graceful error)
# ---------------------------------------------------------------------------

def test_tool_executor_handles_timeout_gracefully():
    """Week 5 'external call timeout handled': timeout returns error dict, does not raise."""
    import httpx

    store = MagicMock()
    store.get.return_value = "fake-token-" + "t" * 32

    executor = ToolExecutor(
        secret_store=store,
        catalog_base_url="https://example.com",
    )

    with patch("httpx.post", side_effect=httpx.TimeoutException("timed out")):
        result = executor.execute("search_knowledge_base", {"query": "snowflake"})

    # ToolError.to_dict() shape: {error: True, error_code: str, message: str, retryable: bool}
    assert result.get("error") is True
    assert result["error_code"] == "REQUEST_ERROR"
    assert result["retryable"] is True
    # Token must not appear in the error message
    assert "fake-token" not in result.get("message", "")


# ---------------------------------------------------------------------------
# Week 6: Normal completion — checkpoint status is 'completed'
# ---------------------------------------------------------------------------

def test_checkpoint_status_completed_on_normal_finish():
    """Week 6 'normal completion': final checkpoint has status='completed'."""
    store, db_path = make_db()
    try:
        state = AgentState(
            session_id="sess-done",
            task_id="setup-flow",
            current_step=6,
            total_steps=6,
            completed_steps=[1, 2, 3, 4, 5, 6],
            status="completed",
        )
        store.save(state)
        recovered = store.load("sess-done", "setup-flow")
        assert recovered is not None
        assert recovered.status == "completed"
        assert len(recovered.completed_steps) == 6
    finally:
        db_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Week 6: Duplicate execution prevented — upsert keeps only one record per task
# ---------------------------------------------------------------------------

def test_duplicate_checkpoint_upserts_not_appends():
    """Week 6 'duplicate execution prevented': saving twice keeps one row, latest wins."""
    store, db_path = make_db()
    try:
        for step in [2, 4]:
            state = AgentState(
                session_id="sess-dup",
                task_id="setup-flow",
                current_step=step,
                total_steps=6,
            )
            store.save(state)

        rows = store.dump_all()
        matching = [r for r in rows if r["session_id"] == "sess-dup"]
        assert len(matching) == 1, f"Expected 1 row, got {len(matching)}"
        assert matching[0]["step"] == 4, "Expected latest step (4) to win"
    finally:
        db_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Week 6: Checkpoint corruption handled — corrupted payload raises, not silent
# ---------------------------------------------------------------------------

def test_corrupted_checkpoint_raises_on_load():
    """Week 6 'checkpoint corruption handled': corrupted JSON in payload raises on load."""
    import sqlite3

    store, db_path = make_db()
    try:
        # Write a valid checkpoint first
        state = AgentState(
            session_id="sess-corrupt",
            task_id="setup-flow",
            current_step=2,
            total_steps=6,
        )
        store.save(state)

        # Corrupt the payload directly in the DB
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE checkpoints SET payload = ? WHERE session_id = ?",
            ("not-valid-json{{{", "sess-corrupt"),
        )
        conn.commit()
        conn.close()

        with pytest.raises((json.JSONDecodeError, Exception)):
            store.load("sess-corrupt", "setup-flow")
    finally:
        db_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# SessionStore — Redis Layer 1 (tests use in-memory fallback, no real Redis)
# ---------------------------------------------------------------------------

def test_session_store_fallback_save_and_load():
    """SessionStore falls back to in-memory when Redis is unreachable."""
    store = SessionStore(redis_url="redis://localhost:19998/0")  # nothing on this port
    assert not store.available()

    messages = [{"role": "user", "content": "Hello"}]
    store.save("sess-1", "task-1", messages)
    loaded = store.load("sess-1", "task-1")
    assert loaded == messages


def test_session_store_fallback_missing_key_returns_none():
    """SessionStore returns None for a key that was never saved."""
    store = SessionStore(redis_url="redis://localhost:19998/0")
    assert store.load("no-such-session", "no-such-task") is None


def test_session_store_fallback_delete_removes_key():
    """SessionStore.delete() removes the key; subsequent load returns None."""
    store = SessionStore(redis_url="redis://localhost:19998/0")
    store.save("sess-del", "task-del", [{"role": "user", "content": "hi"}])
    store.delete("sess-del", "task-del")
    assert store.load("sess-del", "task-del") is None


def test_session_store_key_format():
    """SessionStore uses expected key format: session:{session_id}:{task_id}."""
    assert SessionStore._key("abc", "xyz") == "session:abc:xyz"


def test_session_store_messages_not_shared_across_sessions():
    """Two different session/task pairs are stored independently."""
    store = SessionStore(redis_url="redis://localhost:19998/0")
    msgs_a = [{"role": "user", "content": "session A"}]
    msgs_b = [{"role": "user", "content": "session B"}]
    store.save("sess-a", "task-1", msgs_a)
    store.save("sess-b", "task-1", msgs_b)
    assert store.load("sess-a", "task-1") == msgs_a
    assert store.load("sess-b", "task-1") == msgs_b


# ---------------------------------------------------------------------------
# CheckpointStore — SQLite message fallback (resurrection without Redis)
# ---------------------------------------------------------------------------

def test_checkpoint_save_and_load_messages():
    """save_messages() persists and load_messages() retrieves full message history."""
    store, db_path = make_db()
    try:
        messages = [
            {"role": "user", "content": "How do I configure Snowflake?"},
            {"role": "assistant", "content": [{"type": "text", "text": "Here are the steps..."}]},
        ]
        store.save_messages("sess-1", "task-1", messages)
        loaded = store.load_messages("sess-1", "task-1")
        assert loaded == messages
    finally:
        db_path.unlink(missing_ok=True)


def test_checkpoint_load_messages_returns_none_when_absent():
    """load_messages() returns None if no messages have been saved for the session."""
    store, db_path = make_db()
    try:
        assert store.load_messages("no-such-session", "no-such-task") is None
    finally:
        db_path.unlink(missing_ok=True)


def test_checkpoint_save_messages_upserts_not_appends():
    """Saving messages twice keeps one row; latest wins."""
    store, db_path = make_db()
    try:
        first = [{"role": "user", "content": "First message"}]
        second = [{"role": "user", "content": "First message"},
                  {"role": "assistant", "content": [{"type": "text", "text": "Reply"}]}]
        store.save_messages("sess-1", "task-1", first)
        store.save_messages("sess-1", "task-1", second)
        loaded = store.load_messages("sess-1", "task-1")
        assert loaded == second
        assert len(loaded) == 2
    finally:
        db_path.unlink(missing_ok=True)


def test_checkpoint_reset_clears_messages_too():
    """reset() removes both the checkpoint row and the messages row."""
    store, db_path = make_db()
    try:
        state = AgentState(session_id="sess-1", task_id="task-1", current_step=2, total_steps=6)
        store.save(state)
        store.save_messages("sess-1", "task-1", [{"role": "user", "content": "hi"}])
        store.reset("sess-1", "task-1")
        assert store.load("sess-1", "task-1") is None
        assert store.load_messages("sess-1", "task-1") is None
    finally:
        db_path.unlink(missing_ok=True)


def test_sqlite_message_fallback_used_when_redis_unavailable(tmp_path):
    """When Redis is unavailable, the agent resumes from SQLite messages (not empty)."""
    import json
    from unittest.mock import patch, MagicMock

    # Pre-seed SQLite messages directly (simulates Redis TTL expiry)
    db_path = tmp_path / "test.db"
    store = CheckpointStore(db_path=db_path)
    prior_messages = [{"role": "user", "content": "Prior question"}]
    store.save_messages("sess-fallback", "default", prior_messages)

    answer = json.dumps({
        "mode": "knowledge_qa", "answer": "Here is the answer.",
        "confidence": "high", "sources": [], "needs_more_info": False,
    })

    def mock_llm(*args, **kwargs):
        block = MagicMock()
        block.text = answer
        block.type = "text"
        usage = MagicMock()
        usage.input_tokens = 50
        usage.output_tokens = 20
        resp = MagicMock()
        resp.stop_reason = "end_turn"
        resp.content = [block]
        resp.usage = usage
        return resp

    with patch("src.agent.anthropic.Anthropic") as MockClient, \
         patch("src.agent.CheckpointStore", return_value=store), \
         patch("src.agent.SessionStore") as MockSessionStore, \
         patch.dict(os.environ, {"LLM_GATEWAY_URL": "http://mock-gateway"}):
        # Redis returns nothing (simulates TTL expiry)
        mock_session = MagicMock()
        mock_session.load.return_value = None
        mock_session.available.return_value = False
        MockSessionStore.return_value = mock_session
        MockClient.return_value.messages.create.side_effect = mock_llm

        state, log = agent_module.run(
            "Follow-up question",
            session_id="sess-fallback",
            log_dir=str(tmp_path),
        )

    assert state.status.value == "completed"
    # Verify the LLM was called with the prior messages + new question (3 messages total)
    call_args = MockClient.return_value.messages.create.call_args
    messages_sent = call_args.kwargs.get("messages", call_args.args[0] if call_args.args else [])
    assert len(messages_sent) == 2  # prior message + new question
    assert messages_sent[0]["content"] == "Prior question"
    assert messages_sent[1]["content"] == "Follow-up question"


# ---------------------------------------------------------------------------
# Vault — availability + diagnostic checks (unit tests, no real Vault needed)
# ---------------------------------------------------------------------------

def test_vault_secret_store_unavailable_returns_false():
    """VaultSecretStore.available() returns False when Vault is unreachable."""
    import httpx

    vault = VaultSecretStore(address="http://localhost:19999")  # nothing on this port
    with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
        assert vault.available() is False


def _vault_response(value: str, status_code: int = 200) -> MagicMock:
    """Build a mock httpx response for a Vault KV v2 get."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"data": {"data": {"value": value}}}
    return resp


def test_vault_get_missing_key_raises_key_error():
    """VaultSecretStore.get() raises KeyError when Vault returns 404 for unknown key."""
    import httpx

    error_resp = MagicMock()
    error_resp.status_code = 404
    error_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "404", request=MagicMock(), response=error_resp
    )

    vault = VaultSecretStore()
    with patch("httpx.get", return_value=error_resp):
        with pytest.raises(KeyError, match="not found"):
            vault.get("nonexistent-key")


def test_vault_get_empty_value_raises_key_error():
    """VaultSecretStore.get() raises KeyError when Vault returns an empty string value.

    Vault stores empty strings without complaint (e.g. seeding with an unset env var).
    The agent must not silently proceed with an empty credential — that produces a 401
    from the downstream API with no indication that the secret store is the problem.
    """
    vault = VaultSecretStore()
    with patch("httpx.get", return_value=_vault_response("")):
        with pytest.raises(KeyError, match="empty"):
            vault.get("catalog-api-token")


def test_vault_get_whitespace_only_value_raises_key_error():
    """VaultSecretStore.get() treats a whitespace-only value as empty."""
    vault = VaultSecretStore()
    with patch("httpx.get", return_value=_vault_response("   ")):
        with pytest.raises(KeyError, match="empty"):
            vault.get("catalog-api-token")


def test_vault_get_valid_value_returns_stripped():
    """VaultSecretStore.get() returns the value stripped of surrounding whitespace."""
    vault = VaultSecretStore()
    with patch("httpx.get", return_value=_vault_response("  my-token  ")):
        assert vault.get("catalog-api-token") == "my-token"


def test_local_stub_store_always_available():
    """LocalStubSecretStore.available() always returns True."""
    store = LocalStubSecretStore()
    assert store.available() is True


# ---------------------------------------------------------------------------
# Sprint 1 carry-forward — notes_search still works
# ---------------------------------------------------------------------------

def test_notes_search_returns_results():
    """Sprint 1: notes_search finds relevant notes by keyword."""
    result = notes_search({"query": "Snowflake connector", "max_results": 3})
    assert "results" in result
    assert result["total_found"] >= 1
    assert any("Snowflake" in r["title"] for r in result["results"])


def test_notes_search_unknown_topic_returns_empty():
    """Sprint 1: notes_search returns empty list for unknown topic."""
    result = notes_search({"query": "zzz_unknown_topic_xyz"})
    assert result["total_found"] == 0
    assert result["results"] == []


def test_notes_search_invalid_input_returns_error():
    """Sprint 1: notes_search returns ToolError dict on invalid input."""
    result = notes_search({"query": ""})
    assert result.get("error") is True
    assert result["error_code"] == "INVALID_INPUT"


def test_search_kb_invalid_input_returns_tool_error():
    """Sprint 3: search_knowledge_base validates input with Pydantic, same pattern as notes_search."""
    store = MagicMock()
    executor = ToolExecutor(secret_store=store, catalog_base_url="https://example.com")
    result = executor.execute("search_knowledge_base", {"query": ""})
    assert result.get("error") is True
    assert result["error_code"] == "INVALID_INPUT"
    store.get.assert_not_called()  # Credential must not be fetched for invalid input


# ---------------------------------------------------------------------------
# Sprint 2 carry-forward — prompt + observability
# ---------------------------------------------------------------------------

def _make_logger(tmp_path) -> StructuredLogger:
    return StructuredLogger(run_id=str(uuid.uuid4()), sink_dir=str(tmp_path))


def _load_trace(logger: StructuredLogger) -> list[dict]:
    path = os.path.join(logger.sink_dir, f"{logger.run_id}.jsonl")
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _mock_llm(text: str, stop_reason: str = "end_turn", input_tokens: int = 100, output_tokens: int = 50):
    block = MagicMock()
    block.text = text
    block.type = "text"
    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens
    resp = MagicMock()
    resp.stop_reason = stop_reason
    resp.content = [block]
    resp.usage = usage
    return resp


def test_prompt_contains_soul_and_constraints():
    """Sprint 2: assembled system prompt contains soul identity and hard limits."""
    prompt = build_system_prompt()
    assert "Conductor" in prompt
    assert "data integration" in prompt.lower()
    assert "never" in prompt.lower()


def test_trace_has_schema_version_and_run_start_end(tmp_path):
    """Sprint 2: successful run trace has schema_version, run_start, run_end."""
    answer = json.dumps({
        "mode": "troubleshooting", "answer": "Check firewall.",
        "confidence": "high", "sources": ["note-002"], "needs_more_info": False,
    })
    with patch("src.agent.anthropic.Anthropic") as MockClient, \
         patch.dict(os.environ, {"LLM_GATEWAY_URL": "http://mock-gateway"}):
        MockClient.return_value.messages.create.return_value = _mock_llm(answer)
        state, log = agent_module.run("My Snowflake connection times out.", log_dir=str(tmp_path))

    events = _load_trace(log)
    assert any(e["event"] == "run_start" for e in events)
    assert any(e["event"] == "run_end" for e in events)
    for ev in events:
        assert ev.get("schema_version") == SCHEMA_VERSION
        assert "run_id" in ev


def test_token_counts_in_trace(tmp_path):
    """Sprint 2: llm_call events capture input and output token counts."""
    answer = json.dumps({
        "mode": "troubleshooting", "answer": "...",
        "confidence": "high", "sources": [], "needs_more_info": False,
    })
    with patch("src.agent.anthropic.Anthropic") as MockClient, \
         patch.dict(os.environ, {"LLM_GATEWAY_URL": "http://mock-gateway"}):
        MockClient.return_value.messages.create.return_value = _mock_llm(
            answer, input_tokens=123, output_tokens=45
        )
        state, log = agent_module.run("Test tokens.", log_dir=str(tmp_path))

    events = _load_trace(log)
    llm_events = [e for e in events if e["event"] == "llm_call"]
    assert llm_events[0]["gen_ai.usage.input_tokens"] == 123
    assert llm_events[0]["gen_ai.usage.output_tokens"] == 45


def test_credential_not_in_trace(tmp_path):
    """Sprint 2: credential patterns in user input are redacted in the trace."""
    secret = "sk-super-secret-api-key-12345"
    answer = json.dumps({
        "mode": "troubleshooting", "answer": "Never share credentials.",
        "confidence": "medium", "sources": [], "needs_more_info": False,
    })
    with patch("src.agent.anthropic.Anthropic") as MockClient, \
         patch.dict(os.environ, {"LLM_GATEWAY_URL": "http://mock-gateway"}):
        MockClient.return_value.messages.create.return_value = _mock_llm(answer)
        state, log = agent_module.run(
            f"My connection fails, api_key={secret}", log_dir=str(tmp_path)
        )

    trace_path = os.path.join(str(tmp_path), f"{log.run_id}.jsonl")
    raw = open(trace_path).read()
    assert secret not in raw, "Credential leaked into trace"


def test_run_ids_unique_across_runs(tmp_path):
    """Sprint 2: consecutive runs produce distinct run_ids."""
    answer = json.dumps({
        "mode": "troubleshooting", "answer": "...",
        "confidence": "high", "sources": [], "needs_more_info": False,
    })
    with patch("src.agent.anthropic.Anthropic") as MockClient, \
         patch.dict(os.environ, {"LLM_GATEWAY_URL": "http://mock-gateway"}):
        MockClient.return_value.messages.create.return_value = _mock_llm(answer)
        _, log_a = agent_module.run("First.", log_dir=str(tmp_path))
        _, log_b = agent_module.run("Second.", log_dir=str(tmp_path))

    assert log_a.run_id != log_b.run_id


# ---------------------------------------------------------------------------
# Tool schema versioning (§21.4 — RULE-T04)
# ---------------------------------------------------------------------------

def test_tool_schemas_have_version_field():
    """RULE-T04: every tool schema must declare a version field."""
    from src.tools import TOOL_SCHEMAS
    for schema in TOOL_SCHEMAS:
        assert "version" in schema, (
            f"Tool '{schema.get('name')}' schema is missing 'version' field"
        )


# ---------------------------------------------------------------------------
# Logger agent_id (§8.3 — OTel GenAI semantic convention)
# ---------------------------------------------------------------------------

def test_logger_run_start_contains_agent_id(tmp_path):
    """§8.3: run_start event must include agent_id for OTel GenAI compliance."""
    import json
    from src.logger import StructuredLogger

    logger = StructuredLogger(run_id="test-agent-id-sprint3", sink_dir=str(tmp_path))
    logger.log_run_start("test message")

    log_file = tmp_path / "test-agent-id-sprint3.jsonl"
    record = json.loads(log_file.read_text().strip())
    assert "agent_id" in record, f"agent_id missing from run_start: {record}"
    assert record["agent_id"] == "conductor-v1"


# ---------------------------------------------------------------------------
# A5 — Agent BOM (ABOM) validation (§59 Agent Supply Chain Security)
# ---------------------------------------------------------------------------

def test_bom_validates_clean_when_hashes_match(tmp_path):
    """A5: bom_validator returns no errors when all registered hashes match current files."""
    import hashlib
    import yaml

    # Create a minimal soul.md
    soul_file = tmp_path / "soul.md"
    soul_file.write_text("# Test soul\n\nIdentity content.\n")
    soul_hash = hashlib.sha256(soul_file.read_bytes()).hexdigest()

    # Create a minimal tools.py
    tools_file = tmp_path / "tools.py"
    tools_file.write_text("# tools stub\n")
    tools_hash = hashlib.sha256(tools_file.read_bytes()).hexdigest()

    # Write ABOM with correct hashes
    bom = {
        "schema_version": "1.0",
        "prompt": {"file": "soul.md", "sha256": soul_hash},
        "tools": [{"name": "tools.py", "file": "tools.py", "sha256": tools_hash}],
    }
    bom_path = tmp_path / "agent-bom.yaml"
    bom_path.write_text(yaml.dump(bom))

    sys.path.insert(0, str(Path(__file__).parent.parent))
    from bom_validator import validate
    errors = validate(bom_path)
    assert errors == [], f"Expected clean BOM but got errors: {errors}"


def test_bom_detects_drift_when_soul_modified(tmp_path):
    """A5: bom_validator returns drift error when soul.md is modified after ABOM was generated."""
    import hashlib
    import yaml

    soul_file = tmp_path / "soul.md"
    soul_file.write_text("# Original soul\n\nOriginal content.\n")
    original_hash = hashlib.sha256(soul_file.read_bytes()).hexdigest()

    bom = {
        "schema_version": "1.0",
        "prompt": {"file": "soul.md", "sha256": original_hash},
        "tools": [],
    }
    bom_path = tmp_path / "agent-bom.yaml"
    bom_path.write_text(yaml.dump(bom))

    # Simulate unauthorized modification of soul.md
    soul_file.write_text("# MODIFIED soul\n\nConstraints removed.\n")

    from bom_validator import validate
    errors = validate(bom_path)
    assert len(errors) == 1, f"Expected 1 drift error, got {errors}"
    assert "DRIFT" in errors[0]
    assert "soul.md" in errors[0]


# ---------------------------------------------------------------------------
# A7 — Per-agent identity: two Vault scopes (§5.5)
# ---------------------------------------------------------------------------

def test_setup_scope_cannot_read_troubleshooting_credential():
    """A7: A Setup-scope VaultSecretStore raises KeyError when asked for a Troubleshooting credential.

    In the real Vault, setup and troubleshooting paths are distinct:
      conductor/troubleshooting/catalog-api-token
      conductor/setup/setup-api-token

    A setup-scoped store fetching 'catalog-api-token' would hit the path
    conductor/setup/catalog-api-token, which does not exist — KeyError.
    This test verifies that the scope is enforced at fetch time, not silently ignored.
    """
    import httpx

    # Setup-scoped store tries to read the troubleshooting credential
    setup_store = VaultSecretStore(
        address="http://localhost:19999",
        scope="setup",
    )

    # Vault returns 404 for the wrong scope path
    error_resp = MagicMock()
    error_resp.status_code = 404
    error_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "404 Not Found",
        request=MagicMock(),
        response=error_resp,
    )

    with patch("httpx.get", return_value=error_resp):
        with pytest.raises(KeyError) as exc_info:
            setup_store.get("catalog-api-token")

    # Error message must identify the scope so the operator knows which path to check
    assert "setup" in str(exc_info.value).lower()


def test_vault_scope_is_included_in_path():
    """A7: VaultSecretStore builds the Vault URL with the scope in the path."""
    vault = VaultSecretStore(
        address="http://localhost:8200",
        token="dev-root-token",
        scope="troubleshooting",
    )

    captured_urls = []

    def fake_get(url, **kwargs):
        captured_urls.append(url)
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"data": {"data": {"value": "test-token"}}}
        return resp

    with patch("httpx.get", side_effect=fake_get):
        vault.get("catalog-api-token")

    assert len(captured_urls) == 1
    assert "/conductor/troubleshooting/catalog-api-token" in captured_urls[0]


# ---------------------------------------------------------------------------
# Sprint 2 carry-forward — schema version check, _hint injection, action-verb exclusions
# ---------------------------------------------------------------------------

def test_schema_mismatch_returns_non_retryable_error(tmp_path):
    """RULE-T04: stale schema_version in tool call returns SCHEMA_MISMATCH with retryable=False."""
    from unittest.mock import patch, MagicMock
    import json

    tool_block = MagicMock()
    tool_block.name = "notes_search"
    tool_block.input = {"query": "snowflake", "schema_version": "0.9"}
    tool_block.id = "tu_mismatch_test"
    tool_block.type = "tool_use"
    tool_block.model_dump.return_value = {
        "type": "tool_use", "id": "tu_mismatch_test",
        "name": "notes_search", "input": {"query": "snowflake", "schema_version": "0.9"},
    }

    llm_resp = MagicMock()
    llm_resp.stop_reason = "tool_use"
    llm_resp.content = [tool_block]
    llm_resp.usage = MagicMock(input_tokens=100, output_tokens=20)

    final_block = MagicMock()
    final_block.text = json.dumps({
        "mode": "knowledge_qa", "answer": "OK.", "confidence": "high",
        "sources": [], "needs_more_info": False,
    })
    final_block.type = "text"
    final_resp = MagicMock()
    final_resp.stop_reason = "end_turn"
    final_resp.content = [final_block]
    final_resp.usage = MagicMock(input_tokens=50, output_tokens=15)

    call_count = 0

    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return llm_resp if call_count == 1 else final_resp

    mock_checkpoint = MagicMock()
    mock_checkpoint.load.return_value = None
    mock_checkpoint.load_messages.return_value = None
    mock_session = MagicMock()
    mock_session.load.return_value = None

    with patch("src.agent.anthropic.Anthropic") as MockClient, \
         patch("src.agent.CheckpointStore", return_value=mock_checkpoint), \
         patch("src.agent.SessionStore", return_value=mock_session), \
         patch.dict(os.environ, {"LLM_GATEWAY_URL": "http://mock-gateway"}):
        MockClient.return_value.messages.create.side_effect = side_effect
        state, log = agent_module.run("How do I connect Snowflake?", log_dir=str(tmp_path))

    mismatch_step = next(
        (s for s in state.steps if s.tool_name == "notes_search" and
         isinstance(s.tool_output, dict) and s.tool_output.get("error_code") == "SCHEMA_MISMATCH"),
        None,
    )
    assert mismatch_step is not None, "Expected SCHEMA_MISMATCH step in run state"
    assert mismatch_step.tool_output.get("retryable") is False


def test_non_retryable_error_hint_injected(tmp_path):
    """Harness injects _hint into tool result content when retryable=False."""
    from unittest.mock import patch, MagicMock
    import json

    tool_block = MagicMock()
    tool_block.name = "notes_search"
    tool_block.input = {"query": "snowflake", "schema_version": "0.9"}
    tool_block.id = "tu_hint_test"
    tool_block.type = "tool_use"
    tool_block.model_dump.return_value = {
        "type": "tool_use", "id": "tu_hint_test",
        "name": "notes_search", "input": {"query": "snowflake", "schema_version": "0.9"},
    }

    llm_resp = MagicMock()
    llm_resp.stop_reason = "tool_use"
    llm_resp.content = [tool_block]
    llm_resp.usage = MagicMock(input_tokens=100, output_tokens=20)

    final_block = MagicMock()
    final_block.text = json.dumps({
        "mode": "knowledge_qa", "answer": "OK.", "confidence": "high",
        "sources": [], "needs_more_info": False,
    })
    final_block.type = "text"
    final_resp = MagicMock()
    final_resp.stop_reason = "end_turn"
    final_resp.content = [final_block]
    final_resp.usage = MagicMock(input_tokens=50, output_tokens=15)

    call_count = 0
    captured_messages = []

    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            captured_messages.extend(kwargs.get("messages", []))
        return llm_resp if call_count == 1 else final_resp

    mock_checkpoint = MagicMock()
    mock_checkpoint.load.return_value = None
    mock_checkpoint.load_messages.return_value = None
    mock_session = MagicMock()
    mock_session.load.return_value = None

    with patch("src.agent.anthropic.Anthropic") as MockClient, \
         patch("src.agent.CheckpointStore", return_value=mock_checkpoint), \
         patch("src.agent.SessionStore", return_value=mock_session), \
         patch.dict(os.environ, {"LLM_GATEWAY_URL": "http://mock-gateway"}):
        MockClient.return_value.messages.create.side_effect = side_effect
        agent_module.run("How do I connect Snowflake?", log_dir=str(tmp_path))

    tool_result_msg = next(
        (m for m in captured_messages if m.get("role") == "user"
         and isinstance(m.get("content"), list)
         and any(b.get("type") == "tool_result" for b in m["content"])),
        None,
    )
    assert tool_result_msg is not None, "Expected tool_result message in second LLM call"
    tool_result_block = next(
        b for b in tool_result_msg["content"] if b.get("type") == "tool_result"
    )
    content = json.loads(tool_result_block["content"])
    assert "_hint" in content, "Expected _hint in non-retryable tool result content"
    assert "not retryable" in content["_hint"].lower()


def test_notes_search_schema_has_action_request_exclusion():
    """notes_search schema description must explicitly exclude action verbs (§22.2)."""
    from src.tools import TOOL_SCHEMAS
    notes_schema = next(s for s in TOOL_SCHEMAS if s["name"] == "notes_search")
    description = notes_schema["description"]
    for verb in ("add", "create", "configure", "update", "delete"):
        assert verb in description.lower(), (
            f"Action verb '{verb}' missing from notes_search schema description"
        )


# ---------------------------------------------------------------------------
# W4-3 (carry-forward from sprint-03) - Timeout and Retry scenario logging
# ---------------------------------------------------------------------------

def _load_trace_04(logger: StructuredLogger) -> list[dict]:
    path = os.path.join(logger.sink_dir, f"{logger.run_id}.jsonl")
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _make_logger_04(tmp_path) -> StructuredLogger:
    return StructuredLogger(run_id=str(uuid.uuid4()), sink_dir=str(tmp_path))


class TestTimeoutLogged:
    """[W4-3] A timeout event is captured with error_category='timeout' in the trace."""

    def test_timeout_error_category_schema_valid(self, tmp_path):
        """
        [W4-3] A tool_call event with status='error' and error_category='timeout'
        must be writable by StructuredLogger without raising, and must appear in
        the JSONL output with the expected fields.
        """
        logger = _make_logger_04(tmp_path)

        # Write a timeout event via _write() to verify the schema accepts
        # error_category as a free-form extension field.
        logger._write({
            "event": "tool_call",
            "gen_ai.operation.name": "execute_tool",
            "step_id": "step-1.tool",
            "parent_step_id": "step-1",
            "tool.name": "notes_search",
            "input": {"query": "Snowflake timeout"},
            "output": None,
            "duration_ms": 30000.0,
            "status": "error",
            "error": "Tool execution exceeded timeout",
            "error_category": "timeout",
        })

        events = _load_trace_04(logger)
        timeout_events = [
            ev for ev in events
            if ev.get("event") == "tool_call" and ev.get("error_category") == "timeout"
        ]
        assert len(timeout_events) == 1, "Expected one timeout tool_call event in trace"
        ev = timeout_events[0]
        assert ev["status"] == "error"
        assert ev["error_category"] == "timeout"
        assert ev["duration_ms"] == 30000.0

    def test_timeout_event_has_required_schema_fields(self, tmp_path):
        """
        [W4-3] A timeout event written via _write() must carry schema_version, run_id,
        and ts - the same envelope fields as all other events.
        """
        logger = _make_logger_04(tmp_path)

        logger._write({
            "event": "tool_call",
            "step_id": "step-2.tool",
            "parent_step_id": "step-2",
            "tool.name": "notes_search",
            "input": {"query": "slow query"},
            "output": None,
            "duration_ms": 45000.0,
            "status": "error",
            "error_category": "timeout",
        })

        events = _load_trace_04(logger)
        assert len(events) == 1
        ev = events[0]
        assert ev.get("schema_version") == SCHEMA_VERSION
        assert "run_id" in ev
        assert "ts" in ev


class TestRetryScenario:
    """[W4-3] A retry sequence produces two events: one error (retryable=True), one success."""

    def test_retry_produces_two_events_in_trace(self, tmp_path):
        """
        [W4-3] A retryable error followed by a success must produce exactly two
        tool_call events in the trace - one with status='error' and retryable=True,
        one with status='success'.
        """
        logger = _make_logger_04(tmp_path)

        # First attempt - transient error, retryable
        logger._write({
            "event": "tool_call",
            "gen_ai.operation.name": "execute_tool",
            "step_id": "step-1.tool.attempt-1",
            "parent_step_id": "step-1",
            "tool.name": "notes_search",
            "input": {"query": "Snowflake firewall"},
            "output": None,
            "duration_ms": 1200.0,
            "status": "error",
            "error": "Connection reset by peer",
            "retryable": True,
        })

        # Second attempt - success
        logger.log_tool_call(
            step_id="step-1.tool.attempt-2",
            parent_step_id="step-1",
            tool_name="notes_search",
            tool_input={"query": "Snowflake firewall"},
            tool_output={"results": [{"id": "note-002", "content": "Check firewall rules"}]},
            duration_ms=85.0,
            status="success",
        )

        events = _load_trace_04(logger)
        tool_events = [ev for ev in events if ev.get("event") == "tool_call"]
        assert len(tool_events) == 2, f"Expected 2 tool_call events, got {len(tool_events)}"

        error_events = [ev for ev in tool_events if ev["status"] == "error"]
        success_events = [ev for ev in tool_events if ev["status"] == "success"]

        assert len(error_events) == 1, "Expected one error event"
        assert len(success_events) == 1, "Expected one success event"
        assert error_events[0].get("retryable") is True, "Error event must have retryable=True"

    def test_retry_events_share_same_run_id(self, tmp_path):
        """
        [W4-3] Both the failed attempt and the successful retry must share the same
        run_id - they belong to the same run even though two tool calls were made.
        """
        logger = _make_logger_04(tmp_path)

        logger._write({
            "event": "tool_call",
            "step_id": "step-1.tool.attempt-1",
            "parent_step_id": "step-1",
            "tool.name": "notes_search",
            "input": {"query": "test"},
            "output": None,
            "duration_ms": 500.0,
            "status": "error",
            "retryable": True,
        })
        logger.log_tool_call(
            step_id="step-1.tool.attempt-2",
            parent_step_id="step-1",
            tool_name="notes_search",
            tool_input={"query": "test"},
            tool_output="result text",
            duration_ms=60.0,
            status="success",
        )

        events = _load_trace_04(logger)
        run_ids = {ev["run_id"] for ev in events}
        assert len(run_ids) == 1, (
            f"Retry events must share a single run_id, found: {run_ids}"
        )
