# Lab 2 - Results

## Test Run

```
============================= test session starts ==============================
platform darwin -- Python 3.13.7, pytest-9.1.0, pluggy-1.6.0
collected 23 items

src/test_sprint_02.py::test_valid_tool_input_returns_results PASSED      [  4%]
src/test_sprint_02.py::test_invalid_tool_input_wrong_type PASSED         [  8%]
src/test_sprint_02.py::test_invalid_tool_input_missing_required_field PASSED [ 13%]
src/test_sprint_02.py::test_invalid_tool_input_blank_query PASSED        [ 17%]
src/test_sprint_02.py::test_invalid_tool_input_max_results_out_of_range PASSED [ 21%]
src/test_sprint_02.py::test_state_records_no_tool_step PASSED            [ 26%]
src/test_sprint_02.py::test_extra_fields_in_tool_input PASSED            [ 30%]
src/test_sprint_02.py::test_very_long_query_rejected PASSED              [ 34%]
src/test_sprint_02.py::test_iteration_limit_fires PASSED                 [ 39%]
src/test_sprint_02.py::test_live_valid_query_completes PASSED            [ 43%]
src/test_sprint_02.py::test_idempotent_same_key_returns_cached_result PASSED [ 47%]
src/test_sprint_02.py::test_idempotent_different_key_executes_fresh PASSED [ 52%]
src/test_sprint_02.py::test_schema_mismatch_returns_error PASSED         [ 56%]
src/test_sprint_02.py::test_non_retryable_error_hint_injected PASSED     [ 60%]
src/test_sprint_02.py::test_retryable_flag_logged_in_tool_result PASSED  [ 65%]
src/test_sprint_02.py::test_notes_search_schema_has_action_request_exclusion PASSED [ 69%]
src/test_sprint_02.py::test_tool_schema_has_version_field PASSED         [ 73%]
src/test_sprint_02.py::test_all_tool_schemas_use_module_schema_version PASSED [ 78%]
src/test_sprint_02.py::test_no_tool_answer_exits_completed PASSED        [ 82%]
src/test_sprint_02.py::test_malformed_tool_input_none_returns_error PASSED [ 86%]
src/test_sprint_02.py::test_malformed_tool_input_integer_returns_error PASSED [ 91%]
src/test_sprint_02.py::test_retry_after_field_present_on_tool_error PASSED [ 95%]
src/test_sprint_02.py::test_retry_after_absent_when_none PASSED          [100%]

============================== 23 passed in 6.72s ==============================
```

Passed: 23 / 23 (22 unit + 1 live)

## Evidence Artifacts

### Terminal run — full loop trace (troubleshooting query)

```json
{"run_id": "2feb9e2a", "step_id": 0, "event": "run_start", "message": "How do I troubleshoot a connection timeout?"}
{"run_id": "2feb9e2a", "step_id": 0, "event": "tool_call", "tool": "notes_search", "input": {"query": "connection timeout troubleshoot", "max_results": 5}}
{"run_id": "2feb9e2a", "step_id": 1, "event": "tool_result", "tool": "notes_search", "status": "success", "result": {"results": [{"id": "note-002", "title": "Troubleshooting connection timeouts", "score": 1.0}, ...], "total_found": 2}, "duration_ms": 0.1, "retryable": true}
{"run_id": "2feb9e2a", "step_id": 1, "event": "final_answer", "answer": "...", "duration_ms": 3627.3}
```

All required fields present: `run_id`, `step_id`, `event`, `tool`, `status`, `duration_ms`, `retryable`.
Completed in 2 steps (target: ≤ 3).

### step → step_id fix

The initial implementation emitted `"step"` in log lines. The correct field name is `step_id`, matching
OpenTelemetry GenAI semantic conventions. Fixed in this lab before Lab 3 adds the OTel exporter.

**Before:**
```python
print(json.dumps({"run_id": run.run_id, "step": run.step_count, "event": event, **data}))
```

**After:**
```python
print(json.dumps({"run_id": run.run_id, "step_id": run.step_count, "event": event, **data}))
```

Note: Lab 2 was developed in the working tree and gitignored per the GitHub publishing protocol
(see `.git/info/exclude`). It will be committed and pushed when Lab 2 publishes on Hashnode.

### Pydantic validation failure — blank query rejected before execution

```
$ python -c "import sys; sys.path.insert(0, 'src'); from tools import notes_search; print(notes_search({'query': '   '}))"

{'error': True, 'error_code': 'INVALID_INPUT',
 'message': "1 validation error for NotesSearchInput\nquery\n  Value error, query must not be blank [type=value_error, ...]",
 'retryable': False}
```

The `field_validator` on `NotesSearchInput.query` strips and checks for blank. The tool body never runs.

### Idempotency cache path — pytest -s output

```
$ UV_PROJECT_ENVIRONMENT=../.venv uv run pytest src/test_sprint_02.py::test_idempotent_same_key_returns_cached_result -v -s

src/test_sprint_02.py::test_idempotent_same_key_returns_cached_result
[idempotent hit] key=test-key-abc returning cached config_id=cfg-a233e8e2
PASSED

1 passed in 0.01s
```

The `print` inside `create_connector_config` confirms the cache branch was exercised — not the
fresh-execution branch. `created=False` in the returned dict confirms the caller can distinguish
a cache hit from a new write.

### Repo tree

```
sprint-02-harness-tool-design/
  .env.example
  README.md
  results.md
  linkedin-posts.txt
  src/
    agent.py       ← ReAct loop, iteration cap, JSON logging (step_id), retryable-aware error handling
    tools.py       ← notes_search (action-verb exclusions in schema), Pydantic schemas, ToolError, TOOL_REGISTRY
    state.py       ← RunState, StepRecord, RunStatus (in-memory)
    test_sprint_02.py   ← 23 tests (22 unit + 1 live)
```

## Metrics

| Metric | Target | Actual |
|--------|--------|--------|
| Tests passing | 5/5 | 23/23 (22 unit + 1 live) |
| Valid query iterations | ≤ 3 | 2 |
| Invalid input rejected before execution | Yes | Yes - Pydantic fires before `notes_search` body runs |
| Iteration limit fires correctly | Yes | Yes - mocked loop hits `MAX_ITERATIONS`, exits `LIMIT_REACHED` |
| All log fields present | Yes | Yes - `run_id`, `step_id`, `event`, `tool`, `status`, `duration_ms`, `retryable` |
| Non-retryable errors carry `_hint` | Yes | Yes |
| Action-verb exclusions in `notes_search` schema | Yes | Yes |

## Evidence Artifacts Summary

| Artifact | What it shows |
|----------|---------------|
| 23/23 pytest passing | Validation, idempotency, schema versioning, retryable semantics, action-verb exclusion, iteration limit, no-tool exit, malformed input, retry_after - all green |
| `test_idempotent_same_key_returns_cached_result` | Same key returns cached result, `created=False`, log shows "idempotent hit" (see pytest -s output above) |
| `test_idempotent_different_key_executes_fresh` | Different keys produce independent executions |
| `test_schema_mismatch_returns_error` | Stale `schema_version="0.9"` blocked at dispatch, `SCHEMA_MISMATCH retryable=False` |
| `test_non_retryable_error_hint_injected` | Non-retryable errors carry `_hint` in tool result content |
| `test_retryable_flag_logged_in_tool_result` | `retryable` field present in every tool_result log line |
| `test_notes_search_schema_has_action_request_exclusion` | Schema contains all action verbs in "Do NOT use" clause |
| `test_no_tool_answer_exits_completed` | stop_reason=end_turn as first response → status=COMPLETED, 1 LLM call, no tool dispatch |
| `test_malformed_tool_input_none_returns_error` | tool_input=None handled by harness without raising; loop completes |
| `test_malformed_tool_input_integer_returns_error` | tool_input=42 → Pydantic INVALID_INPUT before tool body runs |
| `test_retry_after_field_present_on_tool_error` | ToolError with retry_after=30 includes it in to_dict; absent when None |
| Loop trace (above) | 2 steps, `step_id` + `retryable` fields present, completed under target |
| step → step_id diff (above) | One-line fix in `_log()`, field name now matches OTel GenAI semantic conventions |
| Pydantic validation failure (above) | INVALID_INPUT returned before tool body runs; retryable=False |
| `.env` + dotenv wiring | No manual export needed; `.env` gitignored |

## Failures and Fixes

**Routing failure - action requests not rejected correctly:**

Running `agent.py "Add new configuration for Teradata"` completed successfully but answered the
wrong question. The user asked to *create* something; the agent answered as if it were a Q&A query.

Initial diagnosis: "the constraint was on the tool, not the agent; fix belongs in the system
prompt." This was wrong.

Correct diagnosis: §22.2 (Tool Schema Design) says "Do NOT use" examples in tool descriptions
improve routing accuracy in benchmarks. The failure was a routing failure, not a behavior
failure. Routing belongs in the tool schema. Behavior belongs in the system prompt. Fixing a routing
failure in the system prompt puts the fix in the wrong layer.

Fix applied in this lab: `notes_search` schema description now explicitly lists action verbs
(add, create, configure, update, delete) in the "Do NOT use" clause, with the response the agent
should give instead. Test: `test_notes_search_schema_has_action_request_exclusion`.

**No runtime failures.** One design decision worth noting:

The initial test spec called for 5 tests. The final suite has 22 unit tests - the extra tests came
from splitting "invalid input" into distinct cases, tests for retryable semantics, action-verb
exclusion, no-tool exit path, malformed inputs, and retry_after field contract. Each failure mode is a different code path; collapsing them would
hide regressions.

**Test that caught a real gap:** `test_extra_fields_in_tool_input` — confirmed that extra fields
passed by the model are silently stripped by Pydantic v2 (not rejected). This is correct behavior
(the tool runs safely) but worth documenting: a future strict-mode config could flip this to an
error. Deferred to the security and guardrails lab.

## What I Would Do Differently

- The in-memory notes corpus is a placeholder. The scoring function is naive keyword overlap.
  Lab 7a/7b (RAG) replaces both with vector search — but even this stub is enough to validate the
  loop behavior, which was the point of Lab 2.
- `step_id` was inconsistent with the log spec (emitted as `step`). Fixed in this lab — logs now
  emit `step_id` to match OpenTelemetry GenAI semantic conventions before Lab 3 adds OTel.
- The Teradata routing failure was initially diagnosed as a prompt engineering problem. It was a
  tool schema problem. The lesson: before reaching for the system prompt, check whether the failure
  is in the tool description first.

---

## Technical Debt Notes

**From §20.4-20.8 — Agent Design Pattern Selection**

The Lab 2 harness correctly implements ReAct. The rationale for choosing ReAct over alternatives:

- **ReAct chosen over ReWOO**: ReWOO requires a planning pass before tool execution. For Conductor
  Q&A mode, the planning pass adds latency with no benefit. For Troubleshooting mode, the model
  commits to a plan before seeing evidence - that breaks on the first unexpected 401 or empty result.
- **ReAct chosen over Reflexion**: Same-model critique catches surface mistakes but misses underlying
  gaps. Reflexion works when the critique model differs from the generation model. Lab 8 is the right
  place to introduce cross-model critique with data.
- **Tree-of-Thoughts not applicable**: ToT is superseded for agent use cases by LATS. Neither applies
  at Lab 2 scope.

**From §01.3 — Nine Harness Components (what's built vs. deferred)**

Lab 2 implements 4 of 9 harness components:
- C2: ReAct Execution Loop - built
- C3: Tool Registry (stub - lookup dict, not a registry) - partially built; full registry per §22.7 in Lab 8a
- C6: State Persistence Layer (in-memory only) - partially built; durable in Lab 4
- C8: Observability and Tracing (JSON logs with step_id, run_id) - built

Deferred: C1 (Model Interface abstraction), C4 (Prompt Composition Engine), C5 (Memory and Context
Manager), C7 (Safety and Guardrails), C9 (Lifecycle Hooks).
