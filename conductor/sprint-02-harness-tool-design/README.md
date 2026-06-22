# Lab 2 - Agent Harness + Tool Design

## Hypothesis

A working ReAct agent loop with typed Pydantic tool schemas, an idempotency key on write tools,
dispatch-time schema version validation, and explicit action-verb exclusions in tool descriptions
will correctly handle valid queries, invalid inputs, and iteration limits - and every failure mode
will be caught at the harness layer before the model is involved.

## What I'm Building

- `agent.py` - ReAct loop with hard iteration cap, dispatch-time schema version check, retryable-flag-aware error handling - **Troubleshooting mode** (dynamic, observation-dependent steps)
- `tools.py` - `notes_search` (read, naturally idempotent, with action-verb exclusions in schema) + `create_connector_config` (write tool with `idempotency_key`, RULE-T03) + typed `ToolError` with retryable flag - **Q&A + Setup mode**
- `state.py` - in-memory run state scoped to one session (step count, tool results, status) - **all modes**
- JSON structured logs with `run_id` and `step_id` - all modes (debuggability)

## Success Criteria

1. Agent answers a valid notes query in <= 3 iterations
2. Invalid tool arguments (wrong type, missing required field, extra fields) are rejected by Pydantic before tool execution
3. Iteration limit fires and agent exits gracefully - no infinite loop
4. Every step visible in JSON logs: `tool_name`, `input`, `output`, `duration_ms`, `status`, `retryable`
5. Idempotency: second call with same `idempotency_key` returns cached result without re-executing (`created: False`)
6. Schema mismatch: call with stale `schema_version` is rejected at dispatch time with `SCHEMA_MISMATCH, retryable=False`
7. All tool schemas reference module-level `SCHEMA_VERSION` constant
8. Non-retryable tool errors include `_hint` in tool result content so the model does not retry
9. `notes_search` schema description explicitly excludes action verbs (create, add, configure, update, delete)

## Failure Indicators

- Agent loops past the iteration limit
- Pydantic validation is bypassed - bad input reaches tool execution
- Logs are missing `run_id`, `step_id`, or `retryable` fields
- Write tool executes twice for the same idempotency key
- Stale schema version reaches tool execution without being caught
- Non-retryable error reaches the model without a hint that retry is pointless
- Action request (add, create, configure) causes the agent to search the knowledge base instead of declining

## Out of Scope

- Tool registry management (per-agent allowlists, deprecation status) - the current `TOOL_REGISTRY` is a lookup dict, not a registry; §22.7 defines the full registry shape with `agents_allowed`, `status`, and `rate_limit`; deferred to Lab 8a
- Pydantic strict mode (extra fields currently stripped silently, not rejected) - Lab 7
- Rate limiting + rollback design for write tools - Lab 8
- Persistent state across sessions - Lab 4
- Multi-session / long-term memory - Lab 5
- Eval harness with pass rate scoring - Lab 5
- Secrets injection and credential handling - Lab 4
- Multiple harness patterns or framework comparison - Lab 6

## Evidence to Collect

| Artifact | What it shows |
|----------|---------------|
| Terminal run (full trace) | Loop executing step by step, tool call, final answer |
| `pytest` output | 23/23 green |
| Idempotency test | "idempotent hit" log line, `created=False` on second call |
| Schema mismatch test | `SCHEMA_MISMATCH, retryable=False` on stale version + `_hint` in tool result |
| JSON log sample | `run_id`, `step_id`, `tool_name`, `duration_ms`, `retryable`, all fields present |
| Action-verb exclusion test | `notes_search` schema contains all action verbs in "Do NOT use" clause |

---

## What Actually Happened

Built all four files (`state.py`, `tools.py`, `agent.py`, test suite) in one session. The loop ran
correctly on first attempt - 2 steps for a valid query, well under the 8-iteration cap. The gateway
proxy is Anthropic-compatible at `/v1/messages`, so a `base_url` override in the Anthropic SDK was
the only change needed to route through it.

Added `create_connector_config` as a write tool with `idempotency_key` - the cache-check pattern
works: same key returns `created=False` from cache, different key executes fresh. Schema version
check at dispatch time confirmed: stale `schema_version="0.9"` returns `SCHEMA_MISMATCH,
retryable=False` before the tool body runs.

The one unexpected finding: Pydantic v2 silently strips extra fields by default rather than
rejecting them. This is safe (bad fields never reach execution) but not obvious - worth revisiting
with strict mode in Lab 8 (security).

## What Failed

Nothing broke at runtime. The behavioral gap: "Add new configuration for Teradata" caused the agent
to search twice, find nothing, and answer from general knowledge anyway.

The initial diagnosis - "the constraint was on the tool, not the agent; fix belongs in the system
prompt" - was wrong. §22.2 (Tool Schema Design) says negative examples in tool descriptions improve
routing accuracy in benchmarks. The fix belongs in the tool schema, not the system prompt.

Fixed in this lab: `notes_search` schema description now explicitly names action verbs (add, create,
configure, update, delete) as "Do NOT use" cases, with the response the agent should give instead.
The system prompt carries behavior rules; the tool schema carries routing rules - they are different
layers for different failure modes.

## What I Learned

- **Write tools need idempotency keys as a correctness requirement**, not a best practice. The agent
  loop retries. The model retries. Networks retry. A write tool without idempotency is only correct
  if every retry path is also correct.
- **Schema versioning belongs at dispatch time.** A version field that's only documentation tells
  you nothing when a stale schema reaches execution. Reject at dispatch, not at execution.
- **The harness catches structural failures. Tool schema descriptions catch routing failures.**
  "The prompt had a gap" was the wrong diagnosis. The tool schema had a gap. System prompts enforce
  behavior; tool descriptions enforce routing. Fixing routing failures in the system prompt puts
  the fix in the wrong layer.
- **`retryable: bool` on `ToolError` is the tool author communicating retry semantics to the
  harness.** The model shouldn't be guessing. Non-retryable errors also carry a `_hint` in the
  tool result content so the model has explicit signal to not retry.
- **Nine components, four built.** A full harness has nine components (§01.3). Lab 2 implements
  four: the ReAct loop (C2), tool registry stub (C3), state persistence layer in-memory (C6), and
  observability/logging (C8). The remaining five are explicit deferred work, not omissions.

## How to Run

**Set up credentials** (first time only):

```bash
cp conductor/sprint-02-harness-tool-design/.env.example conductor/sprint-02-harness-tool-design/.env
# Edit .env and fill in ANTHROPIC_API_KEY (and optionally LLM_GATEWAY_URL)
```

**Install dependencies** (first time only):

```bash
cd conductor/sprint-02-harness-tool-design
UV_PROJECT_ENVIRONMENT=../.venv uv sync --extra dev
```

**Run the agent** (single query, live API call):

```bash
UV_PROJECT_ENVIRONMENT=../.venv uv run python src/agent.py "How do I connect Snowflake?"
```

**Run the test suite**:

```bash
UV_PROJECT_ENVIRONMENT=../.venv uv run pytest src/test_sprint_02.py -v
```

Tests that require a live API key are automatically skipped if `ANTHROPIC_API_KEY` is not set.
All other tests run without a key.

---

## Metrics

| Metric | Target | Actual |
|--------|--------|--------|
| Tests passing | 5/5 | 23/23 (22 unit + 1 live) |
| Valid query iterations | <= 3 | 2 |
| Invalid input rejected before execution | Yes | Yes |
| Iteration limit fires correctly | Yes | Yes |
| All log fields present (incl. step_id, retryable) | Yes | Yes |
| Idempotent write: same key returns cache | Yes | Yes |
| Schema mismatch caught at dispatch | Yes | Yes |
| Non-retryable errors carry _hint | Yes | Yes |
| notes_search excludes action verbs in schema | Yes | Yes |
