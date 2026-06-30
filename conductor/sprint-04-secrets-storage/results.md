# Lab 4 - Results

## Test Run

```
============================= test session starts ==============================
platform darwin -- Python 3.13.7, pytest-9.1.0, pluggy-1.6.0
collected 52 items

tests/test_sprint_04.py::test_tool_schema_has_no_auth_fields PASSED
tests/test_sprint_04.py::test_redacting_formatter_scrubs_bearer_token PASSED
tests/test_sprint_04.py::test_redacting_formatter_scrubs_long_alphanumeric PASSED
tests/test_sprint_04.py::test_redact_function_leaves_short_strings_intact PASSED
tests/test_sprint_04.py::test_checkpoint_payload_contains_no_credential PASSED
tests/test_sprint_04.py::test_tool_executor_never_returns_token_in_result PASSED
tests/test_sprint_04.py::test_checkpoint_resume_after_simulated_crash PASSED
tests/test_sprint_04.py::test_explicit_restart_clears_checkpoint PASSED
tests/test_sprint_04.py::test_secret_rotation_no_code_change PASSED
tests/test_sprint_04.py::test_missing_secret_raises_key_error PASSED
tests/test_sprint_04.py::test_tool_executor_handles_timeout_gracefully PASSED
tests/test_sprint_04.py::test_checkpoint_status_completed_on_normal_finish PASSED
tests/test_sprint_04.py::test_duplicate_checkpoint_upserts_not_appends PASSED
tests/test_sprint_04.py::test_corrupted_checkpoint_raises_on_load PASSED
tests/test_sprint_04.py::test_session_store_fallback_save_and_load PASSED
tests/test_sprint_04.py::test_session_store_fallback_missing_key_returns_none PASSED
tests/test_sprint_04.py::test_session_store_fallback_delete_removes_key PASSED
tests/test_sprint_04.py::test_session_store_key_format PASSED
tests/test_sprint_04.py::test_session_store_messages_not_shared_across_sessions PASSED
tests/test_sprint_04.py::test_checkpoint_save_and_load_messages PASSED
tests/test_sprint_04.py::test_checkpoint_load_messages_returns_none_when_absent PASSED
tests/test_sprint_04.py::test_checkpoint_save_messages_upserts_not_appends PASSED
tests/test_sprint_04.py::test_checkpoint_reset_clears_messages_too PASSED
tests/test_sprint_04.py::test_sqlite_message_fallback_used_when_redis_unavailable PASSED
tests/test_sprint_04.py::test_vault_secret_store_unavailable_returns_false PASSED
tests/test_sprint_04.py::test_vault_get_missing_key_raises_key_error PASSED
tests/test_sprint_04.py::test_vault_get_empty_value_raises_key_error PASSED
tests/test_sprint_04.py::test_vault_get_whitespace_only_value_raises_key_error PASSED
tests/test_sprint_04.py::test_vault_get_valid_value_returns_stripped PASSED
tests/test_sprint_04.py::test_local_stub_store_always_available PASSED
tests/test_sprint_04.py::test_notes_search_returns_results PASSED
tests/test_sprint_04.py::test_notes_search_unknown_topic_returns_empty PASSED
tests/test_sprint_04.py::test_notes_search_invalid_input_returns_error PASSED
tests/test_sprint_04.py::test_search_kb_invalid_input_returns_tool_error PASSED
tests/test_sprint_04.py::test_prompt_contains_soul_and_constraints PASSED
tests/test_sprint_04.py::test_trace_has_schema_version_and_run_start_end PASSED
tests/test_sprint_04.py::test_token_counts_in_trace PASSED
tests/test_sprint_04.py::test_credential_not_in_trace PASSED
tests/test_sprint_04.py::test_run_ids_unique_across_runs PASSED
tests/test_sprint_04.py::test_tool_schemas_have_version_field PASSED
tests/test_sprint_04.py::test_logger_run_start_contains_agent_id PASSED
tests/test_sprint_04.py::test_bom_validates_clean_when_hashes_match PASSED
tests/test_sprint_04.py::test_bom_detects_drift_when_soul_modified PASSED
tests/test_sprint_04.py::test_setup_scope_cannot_read_troubleshooting_credential PASSED
tests/test_sprint_04.py::test_vault_scope_is_included_in_path PASSED
tests/test_sprint_04.py::test_schema_mismatch_returns_non_retryable_error PASSED
tests/test_sprint_04.py::test_non_retryable_error_hint_injected PASSED
tests/test_sprint_04.py::test_notes_search_schema_has_action_request_exclusion PASSED
tests/test_sprint_04.py::TestTimeoutLogged::test_timeout_error_category_schema_valid PASSED
tests/test_sprint_04.py::TestTimeoutLogged::test_timeout_event_has_required_schema_fields PASSED
tests/test_sprint_04.py::TestRetryScenario::test_retry_produces_two_events_in_trace PASSED
tests/test_sprint_04.py::TestRetryScenario::test_retry_events_share_same_run_id PASSED

============================== 52 passed in 1.40s ==============================
```
Passed: 52 / 52

## Eval Run

Lab 4 is prior to the eval harness lab (Lab 5). No eval dataset run this lab.
Baseline established in Lab 5.

## Evidence Artifacts

### Credential injection confirmed (live run with Vault)
- Vault returned 2013-char JWT; injected into Authorization header
- catalog search API returned real assets (Snowflake tables, columns, procedures)
- Token absent from all log lines, tool results, and SQLite records

### Redacted log sample
```
2026-06-15 14:30:12 INFO src.agent: Tool call: search_knowledge_base input={"query": "Snowflake connector configuration setup", "max_results": 5}
2026-06-15 14:30:13 DEBUG src.agent: Tool result: {"results": [{"name": "[REDACTED]", "type": "Procedure", ...}], "total": 5}
```
Token: absent. Asset qualifiedNames: [REDACTED] (over-broad regex - see Failures section).

### SQLite checkpoint dump (no credential in any column)
```
session_id  : evidence-sess       OK - token absent
task_id     : setup-flow          OK - token absent
step        : 3                   OK - token absent
payload     : {...}               OK - token absent
updated_at  : 2026-06-15 12:34:02 OK - token absent
```

### Token injection verified
```
Header present: True
Token in header: True   (injected correctly at call time)
Token in result: False  (never returned to model)
```

### docker-compose services running
```
conductor-chroma   Up  0.0.0.0:8001->8000/tcp
conductor-minio    Up  0.0.0.0:9000-9001->9000-9001/tcp
conductor-redis    Up  0.0.0.0:6379->6379/tcp
conductor-vault    Up  0.0.0.0:8200->8200/tcp
```

## Metrics

| Metric | Target | Actual |
|--------|--------|--------|
| Secret-leak tests passing | 4/4 | 4/4 |
| Checkpoint tests passing | 4/4 | 4/4 |
| SessionStore tests passing | 5/5 | 5/5 |
| SQLite message fallback tests passing | 5/5 | 5/5 |
| Vault diagnostic tests passing | 4/4 | 4/4 |
| Prompt injection resistance | Pass | Pass |
| Secret rotation test | Pass | Pass |
| Vault container swap (no code change) | Pass | Pass |
| Schema mismatch caught at dispatch | Yes | Yes - SCHEMA_MISMATCH retryable=False |
| Non-retryable errors carry _hint | Yes | Yes |
| notes_search excludes action verbs in schema | Yes | Yes |
| Total tests | 48 | 52 passed |
| Test runtime | - | 1.40s |

## Failures and Fixes

### 1. TextBlock not JSON-serializable when saving to Redis
**What:** `json.dumps` raised `TypeError: Object of type TextBlock is not JSON serializable`
when serializing the messages list for Redis. The Anthropic SDK returns typed objects;
plain `json.dumps` on a list that contains them fails.
**Why:** The messages list is typed `list[dict]` but dict values are `Any` - no type
checker catches SDK objects inside. Only surfaced at runtime with a real model response.
**Fix:** `_serialize_messages()` calls `model_dump()` on every content block before saving.
`_normalize_block()` also strips gateway-injected fields the API rejects on replay.

### 2. Empty token from Vault after env var rename
**What:** After renaming `SEARCH_API_TOKEN` to `CATALOG_API_TOKEN`, Vault stored ""
silently when seeded with the unset variable. Every tool call returned 401.
**Why:** Vault has no opinion about values - it stores what it receives, including nothing.
A 401 from the API is identical whether the credential is empty, expired, wrong scope,
or the endpoint changed - no way to distinguish without checking the credential layer directly.
**Fix (two parts):**
- `vault_setup.sh` now reads the written value back before exiting (`vault kv get -field=value ...`)
- `VaultSecretStore.get()` raises `KeyError` with a diagnostic message if value is empty,
  so the failure surfaces at fetch time rather than as an opaque 401 from the API.
- 4 new tests cover: 404 from Vault, empty value, whitespace-only value, valid value stripped.

### 3. Redaction regex too broad
**What:** Asset `qualifiedName` values like
`default/snowflake/1763736409/LANDING/FRONTEND_PROD/TABLE_NAME` contain 32+ char
segments matching the credential regex, masking legitimate asset names in logs.
**Why:** The regex `[A-Za-z0-9_\-]{32,}` matches any long identifier, not just credentials.
**Decision:** Left as-is. Over-redaction is safer than under-redaction. A JWT-specific
pattern (`eyJ[A-Za-z0-9_-]{10,}`) is already in `logger.py`. Full redaction policy
belongs in the security sprint.

## Before/After: Secret Leak Fix

### Before - credential appearing in trace

The logger's initial implementation wrote `tool_input` directly to the JSONL event without redaction. A tool call for `search_knowledge_base` with an injected token in the `Authorization` field would produce:

```json
{"event": "tool_call", "tool.name": "search_knowledge_base",
 "input": {"query": "Snowflake tables", "Authorization": "Bearer eyJhbGci...secret-token"},
 "status": "success"}
```

The test that caught this was `test_no_credentials_in_trace` (an early iteration later renamed to `test_credential_not_in_trace`):

```
FAILED tests/test_sprint_04.py::test_no_credentials_in_trace
AssertionError: 'api_key=secret-123' found in trace output
```

The failure surface: `ToolExecutor.execute()` was building its HTTP request by reading the token from the secret store and passing it into the tool input dict before handing that dict to the logger. The logger received the full dict - credentials included.

### Fix applied

Two changes:

1. `ToolExecutor.execute()` was refactored so the token is fetched from the secret store and injected into the HTTP `Authorization` header directly, never placed into the `tool_input` dict that the logger receives. The model sees only the query; the logger sees only the query.

2. `StructuredLogger._redact_obj()` was added as a defense-in-depth layer: it recursively applies `_REDACT_PATTERNS` to all string values in any dict or list before writing to JSONL. Even if a credential were to reach the logger by mistake, the pattern match catches it.

### After - credential absent from trace

```
PASSED tests/test_sprint_04.py::test_credential_not_in_trace
```

The JSONL event now reads:

```json
{"event": "tool_call", "tool.name": "search_knowledge_base",
 "input": {"query": "Snowflake tables"},
 "status": "success"}
```

Token: absent. The model never received it. The logger never saw it.

---

## Crash/Resume Comparison

### WITHOUT checkpoint - full restart on crash

A 4-step setup flow crashes at step 2 (tool call). On restart, the agent has no memory of prior progress and begins from step 1.

```jsonl
{"event": "run_start", "step_id": "run", "user_message": "Connect Snowflake"}
{"event": "llm_call",  "step_id": "step-1", "status": "success", "output": "I'll start connector setup..."}
{"event": "tool_call", "step_id": "step-2.tool", "tool.name": "notes_search", "status": "error", "error": "Process killed - OOM"}
--- CRASH ---
--- RESTART ---
{"event": "run_start", "step_id": "run", "user_message": "Connect Snowflake"}
{"event": "llm_call",  "step_id": "step-1", "status": "success", "output": "I'll start connector setup..."}
{"event": "tool_call", "step_id": "step-2.tool", "tool.name": "notes_search", "status": "success"}
{"event": "llm_call",  "step_id": "step-3", ...}
{"event": "tool_call", "step_id": "step-4.tool", ...}
{"event": "run_end",   "status": "completed"}
```

Steps 1 and 2 are re-executed. Any side effects from step 1 (API calls, writes) happen twice.

### WITH checkpoint - resume from crash point

The same flow with `CheckpointStore`. At step 2, the agent writes `step=2, status=in_progress` to SQLite before the tool call. On restart, the checkpoint is loaded and the agent resumes with full message history.

```jsonl
{"event": "run_start", "step_id": "run", "user_message": "Connect Snowflake"}
{"event": "llm_call",  "step_id": "step-1", "status": "success", "output": "I'll start connector setup..."}
{"event": "tool_call", "step_id": "step-2.tool", "tool.name": "notes_search", "status": "error", "error": "Process killed - OOM"}
--- CRASH (SQLite has: step=2, messages=[...step-1 exchange...]) ---
--- RESTART ---
{"event": "run_start", "step_id": "run", "user_message": "Connect Snowflake", "resumed_from_step": 2}
{"event": "tool_call", "step_id": "step-2.tool", "tool.name": "notes_search", "status": "success"}
{"event": "llm_call",  "step_id": "step-3", ...}
{"event": "tool_call", "step_id": "step-4.tool", ...}
{"event": "run_end",   "status": "completed"}
```

Step 1 is skipped entirely. The agent re-runs only step 2 (the one that crashed) and continues forward. Message history is restored from SQLite - the model has full context of what happened before the crash.

The test covering this is `test_checkpoint_resume_after_simulated_crash`: it simulates a crash mid-flow, creates a fresh agent with the same session/task IDs, and verifies the agent resumes at the saved step rather than step 1.

---

## Standards Compliance

Lab 4 introduced: SEC01, SEC02, SEC03, STO01, STO02, STO03, STO04, T04.

Violations fixed from prior sprints:
- `agent.py:UNKNOWN_TOOL` - plain dict replaced with `ToolError.to_dict()` (T02)
- `agent.py` - second logger removed, all agent events routed through StructuredLogger (O04)
- `AgentState.context["messages"]` removed - message history moved to SessionStore + CheckpointStore (STO03/STO04)
- SQLite message fallback added (`save_messages`/`load_messages`) — resurrection now TTL-independent; Redis is cache, SQLite is durable source of truth

---

## Technical Debt Notes (from §5.3 — Dynamic vs. Static Credential Comparison)

The sprint implements both static (env var) and dynamic (Vault) credential patterns via `make_secret_store()`. The trade-off between them was not written down explicitly. Documenting here:

| | Static (env var) | Dynamic (Vault) |
|---|---|---|
| Setup complexity | None - set env var | Vault server + AppRole or K8s auth |
| Rotation | Requires redeploy | Zero-downtime, rotation is a Vault operation |
| Latency | 0ms (in-process) | 1-10ms per fetch (network) |
| Audit trail | None | Full access log in Vault audit device |
| Secret exposure | In process env | Short-lived lease only |
| Best for | Local dev, CI | Production, shared infra |

The production recommendation is Vault with `make_secret_store(prefer_vault=True)`. The `prefer_vault=False` path exists for local development and tests only, not as a production option.
