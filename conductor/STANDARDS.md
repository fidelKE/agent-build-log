# Conductor — Engineering Standards

> Single source of truth for all architectural rules in the series.
> Append-only. Rules are never deleted — only superseded.
> Every new sprint's Phase 3 must comply with all **active** rules before code is accepted.
> Violations in any previous sprint's `src/` must be fixed in the sprint that introduces the rule.
>
> **Compliance scope:** Rules apply to ALL code that produces or consumes structured data
> in the sprint — not only `src/`. This includes `eval/`, `scripts/`, and any harness-adjacent
> code. The compliance scan must cover every directory written in the sprint.

---

## How Rules Work

- **Status: active** — all new and existing code must comply.
- **Status: superseded by RULE-XX** — old rule is replaced. Code still following the old pattern is a violation of the new rule.
- When a sprint fixes a violation in a previous sprint's `src/`, document it in `results.md` under "Standards Compliance" and in the blog under "What Changed and Why".

---

## Category: Tools (T)

### RULE-T01
- **Sprint introduced:** 1
- **Status:** active
- **Requirement:** Every tool validates raw input with a Pydantic model before executing. No raw dict access before validation.
- **Violation:** Tool function accesses `raw_input["key"]` or uses `**kwargs` before calling `Model.model_validate(raw_input)`.
- **Applies to:** All functions in `src/tools.py` that are callable by the agent.

### RULE-T02
- **Sprint introduced:** 1
- **Status:** active
- **Requirement:** A tool that receives invalid input returns `ToolError.to_dict()`. It never raises an exception to the agent loop.
- **Violation:** `raise` inside a tool function on validation failure, or returning a plain `{"error": "..."}` dict instead of `ToolError.to_dict()`.
- **Applies to:** All tool functions in `src/tools.py`.

### RULE-T03
- **Sprint introduced:** 1
- **Status:** active
- **Requirement:** Successful tool output is a typed Pydantic model returned via `.model_dump()`. No plain dict returns on the success path.
- **Violation:** `return {"results": [...]}` on the success path instead of `return SomeOutputModel(...).model_dump()`.
- **Applies to:** All tool functions in `src/tools.py`.

### RULE-T04
- **Sprint introduced:** 3
- **Status:** active
- **Requirement:** Tools that call external APIs must accept a `raw_input: dict` argument and validate it with Pydantic before fetching any credential. The credential is fetched inside the validated path only.
- **Violation:** Credential fetched before `Model.model_validate(raw_input)` is called; or credential fetched on the error/validation-failure path.
- **Applies to:** `ToolExecutor` methods in `src/tools.py`.

---

## Category: Secrets (SEC)

### RULE-SEC01
- **Sprint introduced:** 3
- **Status:** active
- **Requirement:** Credentials are fetched from `SecretStore` at tool execution time only — after the model has committed to calling the tool. Never loaded into the agent reasoning context, system prompt, tool schemas, or message history.
- **Violation:** Credential string in `SYSTEM_PROMPT`, `TOOL_SCHEMAS`, `messages` list, or any variable accessible to the model before tool dispatch.
- **Applies to:** `src/agent.py`, `src/tools.py`, `src/prompt.py`.

### RULE-SEC02
- **Sprint introduced:** 3
- **Status:** active
- **Requirement:** No credential ever appears in log output. All log output passes through `RedactingFormatter` or `StructuredLogger` (which has built-in redaction). No raw `print()` or `logging.info()` with credential values.
- **Violation:** Raw credential string in any `.jsonl` trace file or stdout log line; `print(token)` anywhere in `src/`.
- **Applies to:** All files in `src/`.

### RULE-SEC03
- **Sprint introduced:** 3
- **Status:** active
- **Requirement:** No credential ever appears in SQLite checkpoint records. The `context` dict saved to checkpoints must never contain tokens, passwords, or API keys.
- **Violation:** Credential string present in any row returned by `CheckpointStore.dump_all()`.
- **Applies to:** `src/state.py`, `src/agent.py`.

---

## Category: Agent Loop (A)

### RULE-A01
- **Sprint introduced:** 1
- **Status:** active
- **Requirement:** The agent loop has a `MAX_ITERATIONS` hard limit. When reached, status is set to `limit_reached` and the loop exits cleanly.
- **Violation:** `while True` loop with no iteration cap; loop that raises on limit instead of setting status.
- **Applies to:** `src/agent.py`.

### RULE-A02
- **Sprint introduced:** 1
- **Status:** active
- **Requirement:** Agent loop tracks run state via `RunState` (steps, status, final answer). State is passed to `StructuredLogger` at run end.
- **Violation:** Loop that discards step records; run end logged without `total_steps`.
- **Applies to:** `src/agent.py`.
- **Scope note:** `StructuredLogger` introduced in sprint 2. Sprint 1 uses `_log()` inline — this is a known pre-standard gap, not a violation. Sprint 2+ must comply.

### RULE-A03
- **Sprint introduced:** 3 (violation found in sprints 1 and 2, fixed retroactively)
- **Status:** disabled from sprint-6 — SDK handles tool dispatch internally
- **Requirement:** The tool dispatch path must handle ALL `tool_use` blocks returned in a single model response. Every `tool_use` block must have a matching `tool_result` in the next message. All results must be appended together in a single user message.
- **Violation:** `next(b for b in response.content if b.type == "tool_use")` — takes only the first block and silently drops the rest; appending a single `tool_result` when the model returned multiple `tool_use` blocks; separate user messages per tool result.
- **Applies to:** `src/agent.py` in sprints 1–5a only.
- **SDK note:** From sprint-6 onward, `ClaudeSDKClient` dispatches all `tool_use` blocks internally. This rule's violation patterns cannot occur in SDK-based harnesses — there is no manual dispatch loop. Sprint-6+ code correctly contains no `messages.append` or `tool_use` dispatch logic.

### RULE-A04
- **Sprint introduced:** 3 (violation found in sprints 1 and 2, fixed retroactively)
- **Status:** disabled from sprint-6 — SDK manages message serialization internally
- **Requirement:** `response.content` (Anthropic SDK typed objects) must be serialized to plain dicts via `model_dump()` and stripped of gateway-injected fields via `_normalize_block()` before being appended to the `messages` list. The live `messages` list sent to the API must contain only plain dicts — never SDK objects.
- **Violation:** `messages.append({"role": "assistant", "content": response.content})` without serialization; SDK `ToolUseBlock` or `TextBlock` objects in the messages list; `json.dumps` raising `TypeError: Object of type XBlock is not JSON serializable` from the messages list.
- **Applies to:** `src/agent.py` in sprints 1–5a only.
- **SDK note:** From sprint-6 onward, `ClaudeSDKClient` manages the messages list internally. Harness code never sees `response.content` or appends to a messages list. Sprint-6+ code correctly contains no `messages.append` or `model_dump()` serialization logic.

---

## Category: Observability (O)

### RULE-O01
- **Sprint introduced:** 2
- **Status:** active
- **Requirement:** Every LLM call is logged via `StructuredLogger.log_llm_call()` with `input_tokens`, `output_tokens`, and `duration_ms`.
- **Violation:** LLM call with no log entry; log entry missing token counts or duration.
- **Applies to:** `src/agent.py`.
- **SDK scope note (from sprint-6):** `ClaudeSDKClient` executes LLM calls internally and does not surface per-call token usage to the calling process. In SDK-based harnesses, run-level aggregates are logged via `log_run_end()`. Full per-call telemetry requires OTel instrumentation, deferred to sprint-9b. This is a known gap — not a new violation for sprint-6+.

### RULE-O02
- **Sprint introduced:** 2
- **Status:** active
- **Requirement:** Every tool call is logged via `StructuredLogger.log_tool_call()` with `tool_name`, `tool_input`, `tool_output`, `duration_ms`, and `status` (`success` | `error`).
- **Violation:** Tool dispatch with no log entry; missing status field.
- **Applies to:** `src/agent.py`.

### RULE-O04
- **Sprint introduced:** 2
- **Status:** active
- **Requirement:** `StructuredLogger` is the single observability surface in `agent.py`. No `logging.Logger` or `print()` calls for agent-level events (checkpoint resume, run start, tool dispatch). Python `logging` may be used inside `secrets.py`, `tools.py`, and `state.py` for internal-layer debug output only.
- **Violation:** `logger = logging.getLogger(...)` or `configure_redacting_logger()` imported and used in `agent.py`; agent-level events written to stdout instead of `structured_logger._write()` or a `log_*` method.
- **Applies to:** `src/agent.py`.

### RULE-O03
- **Sprint introduced:** 2
- **Status:** active
- **Requirement:** `StructuredLogger` redacts credential patterns before writing to the trace file. Redaction is applied at the logger layer — not at call sites. `_redact_obj()` must handle Anthropic SDK objects (e.g. `ToolUseBlock`) by calling `model_dump()` before recursing.
- **Violation:** Credential value present in any `.jsonl` trace file; redaction applied by manually scrubbing strings before passing to `log_*` methods; `TypeError: Object of type XBlock is not JSON serializable` from `_write()`.
- **Applies to:** `src/logger.py`.

---

## Category: Prompt (P)

### RULE-P01
- **Sprint introduced:** 2
- **Status:** active
- **Requirement:** The system prompt is assembled from `soul.md` (identity) + `prompt.py` (behavioral contract) via `build_system_prompt()`. No hardcoded prompt strings in `agent.py`.
- **Violation:** Inline `SYSTEM_PROMPT = "You are..."` string in `agent.py`; soul loaded inline rather than from `soul.md`.
- **Applies to:** `src/agent.py`, `src/prompt.py`.

### RULE-P02
- **Sprint introduced:** 2
- **Status:** disabled from sprint-6 — SDK returns structured output; raw text parse pattern is absent
- **Requirement:** Model text output is extracted with `_extract_json()` before parsing. Never `json.loads(response.content[0].text)` directly.
- **Violation:** Direct JSON parse of raw model text without `_extract_json()` guard.
- **Applies to:** `src/agent.py` in sprints 1–5a only.
- **SDK note:** From sprint-6 onward, `ClaudeSDKClient` returns output via `result.output` / `result.final_output`. The `json.loads(response.content[0].text)` anti-pattern is architecturally absent — there is no raw text to parse at the harness level. Sprint-6+ code correctly contains no `_extract_json()` or direct `json.loads` on model output.

---

## Category: Storage (STO)

### RULE-STO01
- **Sprint introduced:** 3
- **Status:** active
- **Requirement:** Multi-step agent flows persist state via `CheckpointStore` after each tool call and on completion. On restart, the agent loads the checkpoint before processing the new message.
- **Violation:** Agent loop with no checkpoint save; restart that ignores existing checkpoint.
- **Applies to:** `src/agent.py`.
- **SDK pattern note (from sprint-6):** In SDK-based harnesses, individual tool calls are processed internally by the subprocess — mid-loop checkpointing is not possible. `AgentState` is saved via `checkpoints.save()` at run end; message history is persisted via the Stop hook. The restart contract (load prior messages before the next call) is preserved via `sessions.load() or checkpoints.load_messages()`. The "after each tool call" timing does not apply to SDK-based harnesses.

### RULE-STO02
- **Sprint introduced:** 3
- **Status:** active
- **Requirement:** Checkpoint lookup uses structured keys (`session_id` + `task_id`) against a SQL store. Semantic/vector search is never used for checkpoint retrieval.
- **Violation:** Checkpoint retrieved via embedding similarity; checkpoint stored in a vector DB.
- **Applies to:** `src/state.py`.

### RULE-STO03
- **Sprint introduced:** 3
- **Status:** active
- **Requirement:** Message history is stored in two places: `SessionStore` (Redis, Layer 1) as a fast cache, and `CheckpointStore.save_messages()` (SQLite, Layer 3) as the durable fallback. Both are written on every step. On load, Redis is tried first; SQLite is the fallback if Redis returns nothing (TTL expiry, Redis unavailable). This makes resurrection TTL-independent. `AgentState` holds step progress only (`current_step`, `completed_steps`, `status`) — never message history.
- **Violation:** Messages stored only in Redis with no SQLite fallback; `agent_state.context["messages"]` written; SQLite message fallback omitted from the load path.
- **Applies to:** `src/state.py`, `src/agent.py`.
- **SDK pattern note (from sprint-6):** In SDK-based harnesses, message history is saved via the Stop hook at session end rather than on every step. Both Redis (`sessions.save()`) and SQLite (`checkpoints.save_messages()`) writes are still required — only the timing changes. The dual-store and load-order requirements remain fully in force.

### RULE-STO04
- **Sprint introduced:** 3
- **Status:** active
- **Requirement:** Every sprint that runs an agent loop must instantiate `SessionStore` (Redis cache) and `CheckpointStore` (SQLite durable store) and write messages to both on every step. The in-memory fallback in `SessionStore` is acceptable for CI only.
- **Violation:** Agent loop that stores message history in a plain Python list only; sprint that omits `CheckpointStore.save_messages()` from the save path.
- **Applies to:** `src/agent.py` in every sprint from Sprint 3 onward.

---

## Category: Memory (MEM)

### RULE-MEM01
- **Sprint introduced:** 4
- **Status:** active
- **Requirement:** Every `add_memory` and `search_memory` call must include a `user_id`. The `MemoryStore` interface enforces this as a required parameter — no call without a `user_id` is valid.
- **Violation:** `memory.add(content)` or `memory.search(query)` without `user_id`; `user_id` defaulting to `None` silently.
- **Applies to:** `src/memory.py`, any caller in `src/agent.py` or `src/tools.py`.

### RULE-MEM05
- **Sprint introduced:** 4
- **Status:** active
- **Requirement:** `user_id` used in memory operations must originate from the authenticated session context, injected into the system prompt by the harness before the first LLM call. The model must never infer, guess, or construct `user_id` from conversation content.
- **Violation:** `user_id` absent from the system prompt; `user_id` value in a memory tool call that differs from the session-injected value; harness calling `run()` without passing `user_id` from an authenticated source.
- **Applies to:** `src/prompt.py`, `src/agent.py`, `src/main.py`, `eval/runner.py`.

### RULE-MEM02
- **Sprint introduced:** 4
- **Status:** active
- **Requirement:** Memory retrieval is tool-based only. The agent calls `search_memory(query, user_id)` explicitly as a tool. Memory is never automatically injected into the system prompt or message history before an LLM call.
- **Violation:** Memory contents prepended to `SYSTEM_PROMPT` or injected into the `messages` list before the model call outside of a tool result; any code path that retrieves memory without an explicit agent tool call.
- **Applies to:** `src/agent.py`, `src/memory.py`, `src/prompt.py`.

### RULE-MEM03
- **Sprint introduced:** 4
- **Status:** active
- **Requirement:** Memory provider is selected via a constructor argument or environment variable. Switching providers (Redis / Qdrant / Mem0) requires no changes outside of instantiation. The `MemoryStore` protocol is the only interface used by callers.
- **Violation:** Provider-specific method calls (`redis_client.hset(...)`, `qdrant_client.upsert(...)`) outside of the provider's own implementation class; `isinstance` checks on the memory object in `agent.py` or `tools.py`.
- **Applies to:** `src/memory.py`, `src/agent.py`.

### RULE-MEM04
- **Sprint introduced:** 4
- **Status:** active
- **Requirement:** Memory operations (add, search) are logged via `StructuredLogger` with `user_id`, `provider`, `operation` (`add` | `search`), `query_or_content` (truncated to 200 chars), and `result_count` or `stored_key`. No memory operation is silent.
- **Violation:** `memory.add()` or `memory.search()` call with no corresponding log entry; log entry missing `user_id` or `provider`.
- **Applies to:** `src/memory.py` or the call site in `src/agent.py`.

---

## Category: Eval (EVL)

### RULE-EVL01
- **Sprint introduced:** 4
- **Status:** active
- **Requirement:** The eval runner accepts `--dataset <path>` and is dataset-agnostic. It must not hardcode any dataset path. Both datasets (generic and domain-specific) are run independently and reported separately — their pass rates are never averaged together.
- **Violation:** Hardcoded dataset path in `eval/runner.py`; single combined pass rate across both datasets in the report.
- **Applies to:** `eval/runner.py`, `eval/report.py`.

### RULE-EVL02
- **Sprint introduced:** 4
- **Status:** active
- **Requirement:** Deterministic checks (field presence, `must_not_contain`) run before the LLM judge on every eval case. A case that fails a deterministic check is marked `FAIL` immediately — the LLM judge is not called for it.
- **Violation:** LLM judge called on cases that fail `must_not_contain`; deterministic checks skipped or run after the judge.
- **Applies to:** `eval/judge.py`, `eval/runner.py`.

### RULE-EVL03
- **Sprint introduced:** 4
- **Status:** active
- **Requirement:** Every eval case logs `input_tokens`, `output_tokens`, and `duration_ms` for the agent call. The report aggregates these as per-mode averages. This data is the token cost baseline for the mode router decision in a later sprint.
- **Violation:** Eval run with no token cost logging; report that omits per-mode token averages.
- **Applies to:** `eval/runner.py`, `eval/report.py`.

---

## Compliance History

| Sprint | Rules introduced | Rules superseded | Violations fixed |
|--------|-----------------|-----------------|-----------------|
| 1 | T01, T02, T03, A01, A02 | — | — |
| 2 | O01, O02, O03, O04, P01, P02 | — | Sprint 1 `agent.py`: added StructuredLogger (O01, O02, O04); added `soul.md` + `prompt.py` (P01) |
| 3 | A03, A04, SEC01, SEC02, SEC03, STO01, STO02, STO03, STO04, T04 | — | Sprint 1+2 `agent.py`: parallel tool_use handling — loop now dispatches all blocks, not just first (A03); `response.content` serialized via `model_dump()` + `_normalize_block()` before appending to messages (A04). Sprint 2+3 `agent.py:UNKNOWN_TOOL` — plain dict replaced with `ToolError.to_dict()` (T02); sprint 3 `agent.py` — second logger removed, all agent events routed through StructuredLogger (O04); `search_knowledge_base` brought to T01/T02/T03 parity; `AgentState.context["messages"]` removed — message history moved to Redis + SQLite (STO03/STO04) |
| 4 | MEM01, MEM02, MEM03, MEM04, MEM05, EVL01, EVL02, EVL03 | — | Sprint 4 `prompt.py`: user_id injected into system prompt (MEM05); `agent.py`: build_system_prompt(user_id) called per session, SYSTEM_PROMPT pre-build removed (P01/MEM05); `main.py`: --user-id arg added; `eval/runner.py`: user_id passed per case. Violations found during testing: `eval/runner.py` returned plain dict without Pydantic model (T01/T03) — fixed by adding `CaseResult` model; `eval/runner.py` read wrong OTel token field names producing zero token counts (EVL03) — fixed by reading `gen_ai.usage.input_tokens`. Root cause: compliance scan only covered `src/`, not `eval/`. Standards and phase-3 skill updated to require full scan of all sprint directories. |
| 5a | CI01, CI02, CI03, CI04 | — | No violations. Float precision bug found and fixed in `regression_check.py` during tests: `0.89 - 0.86` evaluates to `-0.030000000000000002` in IEEE 754, causing the boundary case to fire incorrectly. Fixed with epsilon tolerance `delta < -(max_regression + 1e-9)`. |
| 6 | SDK01, SDK02, SKL01, STM01 | A03 (disabled), A04 (disabled), P02 (disabled) | Violations found and fixed: (1) `run_loop.py` in `conductor/scripts/` imported `anthropic` with no `pyproject.toml` — fixed by creating `conductor/scripts/pyproject.toml`. (2) O04: `_logging.getLogger(__name__).warning()` inside `_check_prompt_hash()` — fixed by returning warning string and logging via `structured_logger._write()` after it is initialized. (3) O02: five tools (`notes_search`, `search_knowledge_base`, `search_memory`, `add_memory`, `delete_memory`) had no PostToolUse logger — fixed by adding `post_tool_log_hook` with empty matcher (allowed per RULE-SDK02 Exception). Rules A03, A04, P02 disabled from sprint-6 (SDK handles these concerns internally). O01, STO01, STO03 carry SDK-scope notes acknowledging mechanism changes. |

---

## Category: CI/CD Eval Gate (CI)

### RULE-CI01
- **Sprint introduced:** 5a
- **Status:** active
- **Requirement:** The eval gate script (`scripts/check_eval_gate.py`) must evaluate both an overall pass rate threshold AND each required category independently. A report that passes overall but fails a per-category threshold must exit 1. Per-category thresholds are the only protection against aggregate masking.
- **Violation:** Gate that returns exit 0 when any required category is below its threshold; gate that only checks overall pass rate without per-category evaluation.
- **Applies to:** `scripts/check_eval_gate.py`.

### RULE-CI02
- **Sprint introduced:** 5a
- **Status:** active
- **Requirement:** The regression checker (`scripts/regression_check.py`) must print the specific case IDs and inputs that regressed (cases that passed in baseline but fail now). A regression report with no case-level detail is not actionable.
- **Violation:** Regression checker that exits 1 without identifying which cases regressed; checker that only reports aggregate delta.
- **Applies to:** `scripts/regression_check.py`.

### RULE-CI03
- **Sprint introduced:** 5a
- **Status:** active
- **Requirement:** All gate and regression scripts must be runnable standalone without GitHub Actions. No script may depend on CI environment variables (e.g. `GITHUB_*`) for its core logic. The workflow is a thin caller of scripts — scripts are the primary interface.
- **Violation:** Script that reads `GITHUB_OUTPUT`, `GITHUB_ENV`, or any `GITHUB_*` variable in its core pass/fail logic; script that cannot be invoked with `python scripts/check_eval_gate.py --report <path>` on a developer's machine.
- **Applies to:** `scripts/check_eval_gate.py`, `scripts/regression_check.py`, `scripts/run_eval_sample.py`.

### RULE-CI04
- **Sprint introduced:** 5a
- **Status:** active
- **Requirement:** The pre-commit hook installed by `scripts/install_hooks.sh` must only run deterministic tests (pytest, no LLM calls). It must complete in under 60 seconds. Any test that makes LLM calls must not be included in the pre-commit hook.
- **Violation:** Pre-commit hook that invokes `eval/runner.py`, `eval/judge.py`, or any script that calls the Anthropic API; hook that takes > 60s on a cold run.
- **Applies to:** `scripts/install_hooks.sh`, `.git/hooks/pre-commit`.

---

## Category: Claude Agent SDK (SDK)

### RULE-SDK01
- **Sprint introduced:** 6
- **Status:** active
- **Requirement:** The agent loop must use `ClaudeSDKClient` with `ClaudeAgentOptions`. `allowed_tools` must be an explicit positive allowlist of every tool the agent may call — anything not listed is denied at the harness level. `permission_mode` must be set explicitly (omitting it defaults to interactive, which blocks headless operation). `setting_sources` must be set explicitly when skills are expected to load.
- **Violation:** Custom `while True` loop over `client.messages.create()` in sprint 6+; `ClaudeAgentOptions` with no `permission_mode` in a headless context; `allowed_tools` omitted (all tools allowed — security violation); `setting_sources` omitted when skills are expected to fire.
- **Applies to:** `src/agent.py` from sprint 6 onward.

### RULE-SDK02
- **Sprint introduced:** 6
- **Status:** active
- **Requirement:** Safety constraints that must hold 100% of the time must be implemented as hooks (`PreToolUse` or `PermissionRequest`), not as prompt instructions. `HookMatcher` on `PreToolUse` and `PermissionRequest` hooks must always set `matcher=` — an empty matcher fires on every tool invocation, causing unintended latency and logic errors in safety hooks. Deny responses must return `permissionDecision` inside `hookSpecificOutput`.
- **Violation:** `PreToolUse` or `PermissionRequest` `HookMatcher` with no `matcher=` value; safety constraint implemented only via system prompt when a hook could enforce it deterministically; `PermissionRequest` hook absent for tools designated as HITL-required (per RULE-STM01); deny response with `permissionDecision` outside `hookSpecificOutput`.
- **Applies to:** `src/agent.py` from sprint 6 onward.
- **Exception:** A `PostToolUse` hook with an empty matcher is permitted for cross-cutting observability (logging, tracing) since it cannot deny or delay tool execution. The empty-matcher restriction applies to `PreToolUse` and `PermissionRequest` hooks only.

---

## Category: Skills (SKL)

### RULE-SKL01
- **Sprint introduced:** 6
- **Status:** active
- **Requirement:** SKILL.md `description:` field must be ≤500 characters (cross-provider authoring rule for Lab 6c parity; applies from sprint 6 onward). Skills must be loaded via `setting_sources=["project"]` in `ClaudeAgentOptions`. The `Skill` tool must appear in `allowed_tools`. The `description:` field controls the trigger decision only — no workflow instructions or tool body content in the description.
- **Violation:** SKILL.md `description:` field exceeding 500 characters; skills not loading because `setting_sources` was omitted; `Skill` missing from `allowed_tools`; workflow instructions or tool body content embedded in `description:` instead of the SKILL.md body.
- **Applies to:** `.claude/skills/*/SKILL.md`, `src/agent.py` from sprint 6 onward.

---

## Category: State Machine (STM)

### RULE-STM01
- **Sprint introduced:** 6
- **Status:** active
- **Requirement:** Modes with a defined step sequence (currently: Setup mode — read_credentials → validate → write_config) must enforce the sequence in a `SetupStateMachine` class in code, not via prompt instructions. The state machine must be checked in a `PreToolUse` hook before every tool call. Write-step tools must be blocked if the validate step has not completed. A prompt injection that asks to skip the sequence must be rejected by the hook, not the model.
- **Violation:** Setup mode sequence enforced via system prompt instruction only; write-step tool callable without prior validate completion; state machine check absent from `PreToolUse` hook; sequence state stored only in conversation history (model-only, not enforced in code).
- **Applies to:** `src/state.py`, `src/agent.py` from sprint 6 onward.
