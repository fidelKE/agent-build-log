---
title: "The Model Is Not the Agent: My First Harness PoC"
subtitle: "Building the minimal ReAct loop that separates structural failures from behavioral ones — and the three tool design rules that show up before the lab is over."
slug: model-is-not-the-agent-first-harness-poc
tags:
  - ai-agents
  - llm
  - python
  - software-engineering
coverImageURL: ""
coverImagePrompt: "A wide-format tech blog cover (1600x840). Dark background (#0f1117). Center: a tight vertical stack of three interconnected rectangular blocks - each block connected to the next by a single thin arrow pointing downward. The blocks glow in electric blue (#60a5fa) with subtly different intensities, suggesting a step-by-step execution loop. Inside the middle block: a faint branching split (two thin lines forking left and right), implying conditional dispatch. To the right, offset slightly: a small diamond node with the same glow, suggesting a decision point. Background has a faint blueprint grid. Lines in electric blue (#60a5fa) and violet (#a78bfa). Lower-left: partial scaffolding lines fading into the grid - visual continuity with the series. 4-pointed white star in the bottom-right corner. Flat, modern, developer-aesthetic. No text, no humans, no robots. The mood: precise, systematic, structural. For Midjourney: --ar 16:9 --style raw --v 6."
seriesName: "Agent Build Log"
---

# The Model Is Not the Agent: My First Harness PoC

> **TL;DR:** Built a minimal ReAct agent loop with typed tool schemas, an idempotency key on the
> first write tool, and dispatch-time schema version validation. The harness caught every structural
> failure before it reached the model. Then a user typed "Add new configuration for Teradata" and
> the agent answered a question nobody asked. The loop was fine. The prompt wasn't.

---

## What I Wanted to Test

Can I build the smallest working agent loop and understand every line of it?

Not "does it produce good answers" - that comes later when the eval harness exists. Just: does the
loop work correctly? Does it dispatch tools, respect iteration limits, reject bad input, and produce
structured logs I can actually read?

And one specific sub-question: if the agent retries a write tool twice, can I guarantee it doesn't
create the same resource twice?

---

## Why This Matters

There's a mental model mistake I see constantly: people treat the model as the agent. They swap in
a better model when things break. Sometimes it helps. Often it doesn't.

Before getting into what I built, it's worth pinning down four terms that get conflated constantly:

| Term | Scope | Example |
|------|-------|---------|
| **Harness** | Per-call control — one message in, one message out | The ReAct loop in `agent.py` |
| **Scaffold** | Per-session control — state, memory, session lifecycle | `RunState`, context window management |
| **Framework** | Pre-runtime configuration — what tools are loaded, which model, retry policy | `TOOL_REGISTRY`, `MAX_ITERATIONS` |
| **Orchestrator** | Runtime coordination — spawns multiple agents, routes tasks between them | Not built until the multi-agent lab |

This lab builds a harness and scaffolding. It does not build a framework or an orchestrator.
Those distinctions matter because production post-mortems consistently point to harness defects -
not prompt quality, not model capability - as the dominant failure class. The three most common failure types: context drift
(state corruption across turns), schema misalignment (tool call format doesn't match registered
schema), and state degradation (step records lost or corrupted mid-run). All three are harness
failures. All three are tested explicitly in this lab.

The agent is model + harness. A full production harness has nine components. This lab builds four of them:

| # | Component | What it does | Current status |
|---|-----------|-------------|--------------|
| 1 | Model Interface | Abstracts which LLM you call | Anthropic SDK, no abstraction yet |
| 2 | ReAct Execution Loop | The reason-act cycle | Built |
| 3 | Tool Registry | Catalog of tools with schemas and permissions | Lookup dict (not a registry yet) |
| 4 | Prompt Composition Engine | Assembles system prompt modularly | Hardcoded string |
| 5 | Memory and Context Manager | What enters the context window each call | Not built |
| 6 | State Persistence | Durable state across calls and sessions | In-memory only |
| 7 | Safety and Guardrails | Defense-in-depth | Not built |
| 8 | Observability and Tracing | Inline structured JSON logging (run_id + step_id) | Built |
| 9 | Lifecycle Hooks | Pre/post execution hooks | Not built |

Components 1, 4, 5, 7, and 9 are explicit deferred work with assigned labs. Not accidental omissions.

This experiment is about building the harness correctly - not finding the best model.

---

## What I Built

Four files, one test suite:

```
sprint-02-harness-tool-design/src/
  state.py              — in-memory run state (step count, tool results, status)
  tools.py              — notes_search (read) + create_connector_config (write with idempotency)
  agent.py              — ReAct loop, dispatch-time schema version check, JSON structured logs
  test_sprint_02.py     — 23 tests
```

**The loop contract in `agent.py`:**

1. Send messages + tool schemas to the model
2. Model returns `tool_use` - dispatch to registry - append result - loop
3. Model returns text with no tool call - final answer, exit
4. Step count hits `MAX_ITERATIONS` - exit with `limit_reached` status

**The tool contract in `tools.py`:**

Every tool has three things:
- Pydantic input schema - validated *before* the tool body runs
- Pydantic output schema - the tool always returns the same structure
- Typed `ToolError` with a `retryable` flag - the agent decides whether to retry, not the tool

```python
class ToolError(BaseModel):
    error_code: str
    message: str
    retryable: bool  # True = worth retrying; False = bad input, don't bother
```

This matters because a tool that raises an exception gives the agent nothing to reason about.
A tool that returns a structured error gives the agent a decision: retry, escalate, or give up cleanly.

Every tool in the registry satisfies five properties. These aren't optional:

| Property | What it means | How it's enforced |
|----------|--------------|------------------|
| **Typed input** | Input validated with Pydantic before the tool body runs | `NotesSearchInput.model_validate(raw_input)` |
| **Typed output** | Tool always returns the same structure | `NotesSearchOutput` or `ToolError.to_dict()` |
| **Structured error** | Errors return a dict, never raise | try/except in every tool function |
| **Retryable flag** | Tool author declares whether retrying is safe | `ToolError.retryable: bool` |
| **Idempotency on writes** | Write tools carry a key to make retries safe | `idempotency_key` on `create_connector_config` |

A tool that raises an exception gives the agent nothing to reason about. A tool that returns a
structured error gives the agent a decision: retry, escalate, or give up cleanly.

**ReAct cost note:** The full loop uses two LLM calls per query - one to decide which tool to call,
one to produce the final answer. For troubleshooting queries that require dynamic branching, that
cost is justified. For simple Q&A queries where the answer is a single lookup, it isn't. This lab
uses ReAct for everything because the volume is low and the goal is loop correctness. The token cost
baseline is measured in the memory lab; mode routing to avoid the overhead on simple queries comes
after that measurement.

**Read tool: `notes_search`**

Search the notes knowledge base. Naturally idempotent - calling it twice with the same query is
always safe.

**Write tool: `create_connector_config`**

Create a connector configuration entry. Not naturally idempotent - calling it twice with the same
config would create a duplicate. The tool carries an `idempotency_key` field to fix this.

---

## Idempotency on Write Tools

The agent loop retries. Networks retry. Models re-issue tool calls on partial failures. If a write
tool executes twice with the same arguments, you get two records. The question isn't whether retries
happen - they do. The question is whether the tool can handle them safely.

The `idempotency_key` pattern:

```python
class ConnectorConfigInput(BaseModel):
    idempotency_key: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Unique key for this operation; same key = return cached result, no re-execution",
    )
    connector_type: str
    display_name: str
```

The harness checks the key before executing:

```python
if args.idempotency_key in _IDEMPOTENCY_CACHE:
    cached = _IDEMPOTENCY_CACHE[args.idempotency_key]
    print(f"[idempotent hit] key={args.idempotency_key} returning cached config_id={cached['config_id']}")
    return ConnectorConfigOutput(**{**cached, "created": False}).model_dump()
```

Same key, no re-execution. The result carries `created: False` so the caller can tell whether this
was a fresh write or a cache hit. Different key executes fresh.

Test evidence:

```
test_idempotent_same_key_returns_cached_result PASSED
  [idempotent hit] key=test-key-abc returning cached config_id=cfg-4b846682
  second call: config_id = cfg-4b846682, created = False

test_idempotent_different_key_executes_fresh PASSED
  first:  config_id = cfg-a1b2c3d4, created = True
  second: config_id = cfg-e5f6g7h8, created = True
```

The key insight: **the tool author knows whether a tool is safely retryable. The model should not
guess.** The full read/write contract:

| Tool type | Retry safe by default? | How to make it safe | ToolError retryable |
|-----------|----------------------|--------------------|--------------------|
| **Read** | Yes - always | Nothing needed | `True` |
| **Write without idempotency** | No | Don't build tools this way | `False` |
| **Write with idempotency key** | Yes - same key returns cached result | Add `idempotency_key` field | `True` |
| **Rate-limited** | Yes - after delay | Set `retry_after` seconds on `ToolError` | `True` |

The `retryable` flag on `ToolError` and the `idempotency_key` on write tools are how the tool
author communicates this to the harness.

---

## Schema Versioning at Dispatch Time

Every tool schema carries a `version` field. This isn't documentation - it's a runtime guard.

If the model was given an old schema (a cached prompt, a stale context window), it might issue a
tool call with parameters from the old shape. Executing that call silently would produce wrong
results or corrupt state.

The fix: check the schema version at dispatch time, before execution:

```python
call_version = tool_input.get("schema_version") if isinstance(tool_input, dict) else None
registered_version = TOOL_REGISTRY[tool_name]["schema"].get("version")
if call_version is not None and call_version != registered_version:
    return ToolError(
        error_code="SCHEMA_MISMATCH",
        message=f"Tool '{tool_name}' schema version mismatch: call carries '{call_version}', registered is '{registered_version}'",
        retryable=False,
    ).to_dict()
```

`retryable=False` because retrying with the same stale schema won't fix it. The model needs to be
re-prompted with the current schema.

All tools reference a single module-level `SCHEMA_VERSION = "1.0"` constant so version bumps are
one-line changes that affect every tool atomically.

Test evidence:

```
test_schema_mismatch_returns_error PASSED
  tool called with schema_version="0.9" (stale)
  dispatched: SCHEMA_MISMATCH, retryable=False
  model received the error and answered without retrying

test_all_tool_schemas_use_module_schema_version PASSED
```

---

## Why ReAct, Not ReWOO or Reflexion

ReAct wasn't the only option. Two alternatives worth naming explicitly, because choosing without
reasoning is the same as not choosing.

**ReWOO** separates the planning pass from execution - the model first generates a full plan with
all tool calls, then executes them without seeing intermediate results. The benefit: fewer LLM calls
for known workflows. The cost: the model commits to a plan before seeing any evidence. For
Conductor's Troubleshooting mode - where the next tool call depends on the result of the previous
one - a committed upfront plan breaks on the first unexpected 401 or empty result. The ReWOO
benefit evaporates the moment a workflow is adaptive.

**Reflexion** adds a critique pass after each response: the model evaluates its own output and
revises if needed. The benefit: catches some errors a single pass misses. The limitation: the same
model that produced a wrong answer is asked to evaluate whether the answer is wrong. Reflexion works
best when the critique model is different from or stronger than the generation model. For a
single-model setup, same-model critique catches surface mistakes but misses the underlying gaps.
Cross-model critique is worth exploring, but that's a reliability experiment with data - not an
assumption to bake into the first loop.

ReAct is the right default for a read-execute-react agent: simple, debuggable, and adaptive at
every step. The complexity cost of ReWOO or Reflexion only pays back when you have data showing the
simpler approach is failing in a specific, measurable way.

---

## What I Expected

The loop to work on the first try, tests to pass, done in an afternoon.

---

## What Actually Happened

The loop worked on the first try. Tests passed. Done in an afternoon.

Which sounds boring until you run this:

```bash
uv run agent.py "Add new configuration for Teradata"
```

```
Status: RunStatus.COMPLETED
Steps:  3
Answer: I don't have specific Teradata setup documentation available in the
        knowledge base. However, based on standard data integration practices,
        here's what you'll typically need to configure a Teradata connection...
```

The agent answered a question nobody asked.

The user said *add new configuration*. That's an action request. The agent searched the knowledge
base twice, found nothing about Teradata, then fell back to general knowledge and gave setup
instructions anyway - confidently, helpfully, completely wrong in scope.

---

## What Broke

The model read "Add new configuration", treated it as a setup question, searched, found nothing
specific, and answered from general knowledge anyway.

My first diagnosis: "the constraint is on the tool, not the agent - the fix belongs in the system
prompt." That was wrong.

The correct layer for routing failures is the tool schema. Prior research on tool description design
reports routing accuracy improvements from adding explicit "when NOT to use" examples. Routing
failures belong in tool descriptions. Behavior failures belong in the system prompt. They are
different failure modes for different layers.

The original `notes_search` description said: "Do NOT use for creating notes, modifying connector
config, or executing actions." That's vague. The model doesn't know that "Add new configuration for
Teradata" is an action request - it reads it as a setup question.

The fix - applied in this lab, not deferred:

```python
"description": (
    "Search the integration knowledge base for setup guides, "
    "troubleshooting steps, and how-to notes. "
    "Use when the user asks 'how do I...', 'what is...', or reports an error. "
    "Do NOT use for action requests — if the user says 'add', 'create', 'configure', "
    "'update', 'delete', or 'set up' anything, do not call this tool. "
    "Respond instead: 'I can help you find information and troubleshoot issues, "
    "but I cannot make changes to configurations directly.' "
    "Do NOT use for creating notes, modifying connector config, or executing actions."
),
```

The action verbs are explicit. The fallback response is explicit. A good tool description covers not
just what the tool does, but exactly when the model should not call it.

The harness worked perfectly. The loop was correct. The tool validation fired correctly.
The tool schema routing description had a gap - and that's where the fix belongs.

---

## What I Learned

**Write tools need idempotency keys, not as a best practice but as a correctness requirement.**
The agent loop retries. The model retries. Networks retry. A write tool without idempotency is only
correct if every retry path is also correct - and they're not. The tool author knows this; the
model doesn't.

**Tool schema versioning belongs at dispatch time, not just as documentation.** A version field that
only serves as a comment tells you nothing when a stale schema reaches execution. Checking at
dispatch time - and returning `SCHEMA_MISMATCH, retryable=False` - catches stale-context bugs
before they corrupt state.

**Pydantic v2 strips extra fields silently.** If the model passes `{"query": "Snowflake", "injected_field": "malicious"}`, Pydantic strips the extra field and the tool runs safely. This is correct
behavior - but it's not obvious, and when we get to security and guardrails it's worth revisiting
with strict mode on. Deferred to the security and guardrails lab.

**`retryable: bool` on ToolError is a small design decision with real consequences.** Without it,
the agent has to guess whether an error is worth retrying. With it, the tool author - who knows
the failure mode - tells the agent what to do. The model shouldn't be making that call.

Non-retryable errors also carry a `_hint` field in the tool result content passed back to the model:
`"This error is not retryable. Do not retry with the same input."` The `retryable` flag is the
tool contract. The hint is the model signal. Both matter - the flag for harness logic, the hint for
model context.

**Routing failures belong in tool descriptions, not system prompts.** The Teradata failure was a
routing failure - the model selected `notes_search` for an action request. My initial instinct was
to fix it in the system prompt. That's the wrong layer. Prior research shows "when NOT to use"
examples in tool descriptions meaningfully improve routing accuracy. Routing is a tool schema
responsibility. Behavior is a system prompt responsibility. They are not interchangeable.

---

## Evidence

| Artifact | What it shows |
|----------|---------------|
| 23/23 pytest passing | Validation, idempotency, schema versioning, retryable semantics, action-verb exclusion, iteration limit all green |
| `test_idempotent_same_key_returns_cached_result` | Same key returns cached result, `created=False`, log shows "idempotent hit" |
| `test_idempotent_different_key_executes_fresh` | Different keys produce independent executions |
| `test_schema_mismatch_returns_error` | Stale `schema_version="0.9"` blocked at dispatch, `SCHEMA_MISMATCH retryable=False` |
| `test_non_retryable_error_hint_injected` | Non-retryable errors carry `_hint` in tool result content |
| `test_retryable_flag_logged_in_tool_result` | `retryable` field present in every tool_result log line |
| `test_notes_search_schema_has_action_request_exclusion` | Schema contains all action verbs in "Do NOT use" clause |
| Loop trace (Snowflake query) | 2 steps, `step_id` + `retryable` fields present, completed under target |
| `.env` + dotenv wiring | No manual export needed; `.env` gitignored |

---

## What I'd Do Differently

The `step_id` vs `step` naming inconsistency was caught during the gap analysis and fixed in this
lab. Logs now emit `step_id` to match OpenTelemetry GenAI semantic conventions. Fixing before the
observability lab adds OTel costs one line. Fixing during the observability lab while wiring the
exporter costs a schema migration.

The current `TOOL_REGISTRY` is a lookup dict, not a registry. It maps tool names to callables and
schemas. A proper registry has: per-agent `agents_allowed` allowlists, `status` (active /
deprecated / experimental), `rate_limit`, `audit_required`, and documentation URLs. The dict is
correct for two tools and a single agent. When Conductor grows to 10+ tools across 4 modes, the
registry structure becomes a correctness requirement - each mode should only see its allowed tools.
Deferred to the MCP hardening lab.

---

## Out of Scope

- Tool registry management (per-agent allowlists, deprecation status) - the MCP hardening lab; current TOOL_REGISTRY is a lookup dict
- Pydantic strict mode (extra fields stripped silently rather than rejected) - the security and guardrails lab
- Rate limiting + rollback design for write tools - the security and guardrails lab
- Persistent state across sessions - the secrets and storage lab
- Multi-session / long-term memory - the memory and evals lab
- Eval harness with pass rate scoring - the memory and evals lab
- Secrets injection and credential handling - the secrets and storage lab
- Multiple harness patterns or framework comparison - the cross-provider benchmark post
- Cross-model benchmark (weaker model + strong harness vs. stronger model + weak harness) - the cross-provider benchmark post

---

## Code

Code: [`conductor/sprint-02-harness-tool-design/`](https://github.com/fidelKE/agent-build-log/tree/main/conductor/sprint-02-harness-tool-design)

---

When you hit a routing failure in your own agents - the model calling the wrong tool, or answering a question nobody asked - where does your fix land? System prompt, tool description, or somewhere else? I'd genuinely like to know whether the harness-vs-prompt distinction holds in practice for others, or whether it breaks down at some edge case I haven't hit yet.
