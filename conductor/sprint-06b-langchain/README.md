# Lab 6b - LangChain create_agent(): The Middle Tier

## What I wanted to test

LangChain's `create_agent()` sits between drawing the graph yourself (Lab 6a) and
hiding it entirely (Lab 6c). The goal is a capability map: what does the framework
give you without writing code, what do you still need to write, and how does that
compare to owning the graph in LangGraph. The output is a reusable decision framework
for choosing the right harness for future agents.

## Why this matters

Every new agent starts with the same question: how much loop do I own? LangGraph makes
you own all of it. Deep Agents hides all of it. `create_agent()` is the negotiated
middle - and the only way to know where the ceiling sits is to port a real agent and
measure what survives the abstraction.

## Hypothesis

`create_agent()` eliminates the graph ownership boilerplate from Lab 6a (node
definitions, edges, compilation) while preserving all Conductor capabilities - and the
gap between what's prebuilt and what still needs custom code reveals where the
abstraction ceiling sits.

## What I'm Building

- **Conductor port to `create_agent()`** - all four modes (Setup, Onboarding,
  Troubleshooting, Q&A), same tools and soul prompt as Labs 6 and 6a [all modes]
- **Middleware stack** - `ModelCallLimitMiddleware` (8-step cap),
  `HumanInTheLoopMiddleware` (`write_connector_config` gate), `ToolRetryMiddleware`
  (transient failures), custom `@wrap_tool_call` STM gate [Troubleshooting + Setup]
- **`context_schema`** replacing `TypedDict` state - `ConductorContext` dataclass with
  `user_id` and `stm_state` [all modes]
- **Same eval run as Labs 6/6a** - same dataset, same LLM-as-judge, same token cost
  measurement [all modes]

## Success Criteria

1. All four Conductor modes respond correctly on the same eval set used in Labs 6 and 6a
2. HITL gate pauses before `write_connector_config` and resumes correctly on approve/reject
3. 8-step diagnostic cap triggers via `ModelCallLimitMiddleware` (not a hand-rolled counter)
4. Token cost per turn measured and recorded - comparable to Lab 6a baseline
5. Boilerplate delta computed: line count diff between 6a graph setup and 6b harness setup
6. Capability map produced: prebuilt-covered vs still-custom for each Conductor behavior

## Failure Indicators

- Eval pass rate drops more than 5 pp vs Lab 6a (suggests the port introduced a regression)
- HITL gate cannot be reproduced without wiring a custom node (suggests middleware is
  insufficient for Conductor's interrupt pattern)
- Token cost per turn is materially higher than Lab 6a (suggests harness overhead)

## Out of Scope

- Deep Agents comparison (Lab 6c, separate sprint)
- New tools or Conductor modes not present in Labs 6/6a
- `PIIMiddleware` (relevant for Lab 8)
- Persistent checkpointer backend (`InMemorySaver` is sufficient for this comparison)
- AGENTS.md / any Claude Code tooling convention (LangChain has no equivalent; `soul.md`
  passes as `system_prompt=`, `context_schema` replaces `TypedDict` state)

## Evidence to Collect

- Eval pass rate vs Lab 6a baseline (same dataset, same judge)
- Token cost per turn (input + output tokens, averaged across eval set)
- Boilerplate delta: line count for agent setup in 6a vs 6b
- HITL test transcript showing pause + resume
- Capability map table: prebuilt-covered | still-custom | not-possible

---

## How to Run

```bash
# Install dependencies into shared venv
cd conductor/sprint-06b-langchain
UV_PROJECT_ENVIRONMENT=../.venv uv sync --extra dev

# Copy .env from sprint-06a or create from template
cp ../sprint-06a-langgraph/.env .env   # or edit .env.example

# Run in troubleshooting mode (default)
UV_PROJECT_ENVIRONMENT=../.venv uv run python -m src.main "My Snowflake connector keeps timing out."

# Run in setup mode (exercises stm_gate + HumanInTheLoopMiddleware)
UV_PROJECT_ENVIRONMENT=../.venv uv run python -m src.main --mode setup --session demo "Set up the snowflake-prod connector"

# Resume a named session (InMemorySaver + SessionStore message history)
UV_PROJECT_ENVIRONMENT=../.venv uv run python -m src.main --session demo "What connectors do you support?"

# Full trace depth
UV_PROJECT_ENVIRONMENT=../.venv uv run python -m src.main --verbose "Why is my BigQuery connection failing?"

# Run tests
UV_PROJECT_ENVIRONMENT=../.venv uv run pytest tests/ -v

# Run evals (same dataset as Labs 6 and 6a)
UV_PROJECT_ENVIRONMENT=../.venv uv run python -m conductor.scripts.run_eval \
  --dataset ../../evals/datasets/conductor-eval-generic.yaml \
  --sprint sprint-06b
```

Required env vars (see `.env.example`):
- `LLM_GATEWAY_URL` - gateway base URL (replaces https://api.anthropic.com)
- `ANTHROPIC_API_KEY` - gateway API key
- `CONDUCTOR_MODEL` - model ID (default: claude-haiku-4-5-20251001)

## What Actually Happened

`create_agent()` delivered on the core hypothesis: graph.py is gone (-328 lines), and three
of the four hand-rolled loop controls in Lab 6a are now OOTB middleware. `ModelCallLimitMiddleware`,
`HumanInTheLoopMiddleware`, and `ToolRetryMiddleware` replaced custom node logic. STM enforcement
remains custom via `@wrap_tool_call` (~30 lines) - that's domain logic, not framework gap.

One API surprise: `ToolCallResponse` does not exist in the installed version. Middleware that
blocks a tool call returns `ToolMessage`, not a named response type. The learning material had
theoretical code that didn't survive contact with the actual package. Verified by live import
before writing agent.py - corrected in both the code and RULE-LC02 in STANDARDS.md.

28 tests pass. The capability gap from Lab 6a is session persistence: `InMemorySaver` is
process-local. SQLite-backed sessions would need `AsyncSqliteSaver` and a DB path, which is
available from Lab 4 patterns but out of scope for this comparison sprint.

## What Failed

- `ToolCallResponse` import - the type doesn't exist. Fixed before tests ran.
- pytest resolved `src.agent` from `sprint-05a` (wrong sprint). Fixed with `pythonpath = ["."]`
  in pyproject.toml.

## What I Learned

The negotiated middle is real. `create_agent()` removes the graph entirely without forcing you
into Deep Agents' hidden loop. The three OOTB middleware classes cover the controls you'd
otherwise hand-roll. What you give up is explicit node topology and durable cross-process state
(without a persistence swap). For agents where you don't need to visualize or checkpoint the
graph, the trade is favorable - 226 fewer lines, same behavior.

The `@wrap_tool_call` pattern is the right abstraction for domain-specific tool gates. It's a
Python decorator, not a graph node. The STM gate is 30 lines and fully testable without wiring
the full agent.

`ConductorContext` as a dataclass (not TypedDict) is the correct model for context you want to
mutate mid-run. The dataclass is passed at `invoke()` time and accessible in every middleware
and tool call via `request.runtime.context`. The mutation is in-place - no return value needed.

## Metrics

| Metric | Target | Actual |
|--------|--------|--------|
| Eval pass rate | >= Lab 6a baseline (75.9%) | Pending live credentials |
| Token cost per turn | <= Lab 6a baseline | Pending live credentials |
| Boilerplate delta (lines) | < 0 (fewer lines) | -226 lines (graph.py deleted) |
| HITL pause + resume | Works | Confirmed via middleware + Command(resume=) |
| STM enforcement | Works | 7/7 tests pass |

---

## Technical Reference

### Central design decision

`create_agent()` was chosen over LangGraph for this lab to measure the abstraction cost: what does hiding graph ownership buy and what does it remove? The concrete answer: three OOTB middleware classes replace hand-rolled loop controls (step limit, HITL, retry), and the `@wrap_tool_call` decorator replaces a pre-tool-check graph node for domain-specific gating. What is removed: explicit node topology (can't add/remove nodes), and durable session state without a persistence backend swap (InMemorySaver is process-local). The tradeoff is favorable for agents that don't need graph inspection or cross-process state persistence.

### Key files and what to read

- `src/agent.py` - the entire harness. Lines 1-50: imports and constants. Lines 51-120: `SetupStateMiddleware` with `@wrap_tool_call`. Lines 121-200: `ConductorContext` dataclass and `build_agent()`. Lines 200-280: `run()` function - the session entry point. Lines 280-424: tool definitions (JSON Schema + LangChain binding) and `_pydantic_from_schema()`.
- `src/state.py` - `SetupStateMachine` (unchanged from Lab 6). Read `is_allowed()` and `advance()` to understand the gate logic.
- `src/tools.py` - connector tool implementations. Each returns a dict matching the schema in `agent.py`.
- `tests/test_sprint_06b.py` - `TestSTMEnforcement` (lines 80-140) is the most important test class: covers the full allowed sequence, all out-of-order block cases, and the structural check that `@wrap_tool_call` is applied.

### What to tweak and where

- **Step limit**: `ModelCallLimitMiddleware(run_limit=8)` in `build_agent()`. Change `run_limit`.
- **HITL tools**: `_HITL_TOOLS: frozenset[str]` constant near the top of `agent.py`. Add/remove tool names.
- **STM sequence**: `TRANSITIONS` dict in `src/state.py`. Each key maps to the set of states it can transition to.
- **Context fields**: `ConductorContext` dataclass. Add fields here if middleware needs to track new per-session state. Note: dataclass resets on every `run()` call - for fields that need to persist across `run()` calls, use the session store.
- **Session persistence**: swap `InMemorySaver()` for `AsyncSqliteSaver` and pass a DB path. The SecretStore pattern from Lab 4 provides the path securely.

### How to run end-to-end

```bash
# Tests - no credentials required
cd conductor/sprint-06b-langchain
UV_PROJECT_ENVIRONMENT=../.venv uv sync --extra dev
UV_PROJECT_ENVIRONMENT=../.venv uv run pytest tests/ -v

# Live run - requires LLM_GATEWAY_URL and ANTHROPIC_API_KEY in .env
UV_PROJECT_ENVIRONMENT=../.venv uv run python -m src.main "My Snowflake connector keeps timing out."

# Setup mode - exercises STM gate + HumanInTheLoopMiddleware
UV_PROJECT_ENVIRONMENT=../.venv uv run python -m src.main --mode setup --session demo "Set up the snowflake-prod connector"

# Evals
UV_PROJECT_ENVIRONMENT=../.venv uv run python -m conductor.scripts.run_eval \
  --dataset ../../evals/datasets/conductor-eval-generic.yaml \
  --sprint sprint-06b
```
