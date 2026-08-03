# Lab 6c - Deep Agents (LangChain): Sealed Topology

## What I Wanted to Test

Whether hiding the LangGraph graph behind `create_deep_agent()` costs anything
measurable for Conductor's specific needs - and what the abstraction ceiling looks
like in concrete terms when you try to wire Conductor's per-mode routing and HITL
escalation into a framework that owns the topology.

## Why This Matters

Labs 6 and 6a built Conductor two ways: custom harness (raw Anthropic SDK) and
explicit LangGraph graph (you draw every node and edge). Deep Agents is the third
option: same LangGraph underneath, but the graph is hidden behind a single
`create_deep_agent()` call. The tradeoff is convenience vs. control. This lab
makes that tradeoff measurable.

## What I Built

- Deep Agents port of Conductor with all four modes (Setup, Troubleshooting, Q&A,
  Onboarding) using the same `SOUL.md` identity and `conductor-troubleshoot-connector`
  skill from Lab 6
- `AGENTS.md` alongside `SOUL.md` - parallel artifacts serving different purposes
  (identity vs. structural manifest)
- `SetupStateMiddleware` enforcing the read - validate - write connector sequence
  via `wrap_tool_call`
- `MemoryMiddleware`, `SkillsMiddleware`, and `ModelCallLimitMiddleware` replacing
  three hand-rolled patterns from Labs 6 and 6a
- HITL wiring via `interrupt_on=` + `checkpointer=` + `Command(resume=...)`
- Token cost logging per query type feeding the Lab 11 mode router baseline

## Hypothesis

Porting Conductor to `create_deep_agent()` will produce fewer lines of harness
boilerplate than Lab 6a but introduce at least one concrete ceiling: a Conductor
behavior where the Deep Agents approach gives a weaker structural guarantee than
the LangGraph equivalent.

## What Actually Happened

The port succeeded with fewer lines than expected. Replacing `build_graph()` +
`graph.invoke()` with `create_deep_agent()` + `agent.invoke()` eliminated `graph.py`
entirely (328 lines gone). The final harness is 426 lines - 35% less than Lab 6a's
650 combined lines.

The hypothesis held: the abstraction ceiling was real and concrete. Three structural
guarantees weakened when the graph was hidden. None of these are framework bugs -
they are the correct Deep Agents approach to each problem, and each approach trades
structural enforcement for configuration-time simplicity.

**Ceiling 1: Per-mode conditional routing**

In Lab 6a, the graph had four conditional edges that branched execution based on
the mode parameter - a troubleshooting query went through one node sequence, a
setup query through another. This structural routing meant mode behavior was
enforced by the graph topology itself, not by model reasoning.

In Deep Agents, `create_deep_agent()` assembles and compiles the graph before
returning it. What you get back is a `CompiledStateGraph` - there is no
`add_node()`, `add_edge()`, or `add_conditional_edges()` API. The topology is
fixed at construction time. Customization happens through `middleware=`, `tools=`,
`system_prompt=`, and `interrupt_on=` - all of which configure behavior within the
sealed topology, not the topology itself.

System prompt injection is the correct Deep Agents approach to mode routing: mode
is passed as a `{mode}` block inside the system prompt at run time. The agent reads
the mode block and adjusts its behavior accordingly. This works, but the structural
guarantee is weaker. With graph edges, mode was a hard constraint enforced before
the model ran. With prompt injection, mode is a soft constraint: the agent reasons
its way into the right behavior on every call. The model can drift if the prompt
block is ambiguous.

The more principled design is dedicated agents per mode: a router dispatches to
a SetupAgent, TroubleshootAgent, QAAgent, and OnboardingAgent, each with only
the tools it needs. With that architecture the model cannot call
`write_connector_config` during troubleshooting because the tool does not exist
in that agent - enforcement moves from prompt reasoning to structural
impossibility. This lab does not do that because the experiment across Labs 6
through 6d is the same agent ported across five frameworks. Switching to
multi-agent mid-series would break the benchmark - you could no longer tell
whether eval differences come from the framework or the architecture. Prompt
injection is the acceptable cost of keeping the comparison clean.

The multi-agent design is the planned target for the multi-agent orchestration
lab later in the series. The harder case it needs to cover is cross-mode
handoff: a troubleshooting session that finds a misconfigured field should hand
off to Setup to fix it. With a single agent the handoff is implicit - the model
reasons that the mode should shift. With dedicated agents it is explicit: the
TroubleshootAgent returns a structured finding, the router decides to invoke
SetupAgent, and SetupAgent receives the diagnosis as input rather than
reconstructing it from a conversation thread. The A2A handoff preserves state;
the single-agent approach relies on the model to carry it.

**Ceiling 2: HITL at node level vs tool level**

In Lab 6a, HITL was a first-class graph concept: a `human_review_node` was an
explicit node in the graph, wired with incoming and outgoing edges. When the
agent reached a review point, execution paused at that node. The resume path
entered at a named node (`human_review_node`) and continued from there. The
caller had full control over which node triggered the pause and which node
handled the resume.

In Deep Agents, `interrupt_on=` is the intended HITL API. You name a tool, the
framework pauses before calling it, and resume re-enters the agent at the same
tool invocation point. The framework owns the pause and resume nodes. The gap is
branching: when the review decision needs to route execution differently based on
outcome - approve continues, reject routes elsewhere - tool-level HITL cannot
express that structurally. The agent interprets the decision via reasoning in the
next step, not via a conditional edge. For Conductor's escalation case this is
sufficient; the gap becomes a real constraint when you need structurally different
paths depending on the human decision.

One behavior worth noting from the logs: without `checkpointer=`, the resume
path silently returns an empty response - the graph state is lost between the
initial invoke and the Command(resume=...) invoke because there is nothing
persisting it. No error is raised. The `token_cost` event is still written at
run end, but `final_answer` is null and no `hitl_interrupt` event appears in
the session log. The `checkpointer=` requirement is the kind of thing that only
surfaces at runtime with a real interrupt flow.

**Ceiling 3: State machine state has no home in the graph**

In Lab 6a, the setup state machine lived in `ConductorState` - the TypedDict
that flowed through every node as ordinary graph state. Any node could read the
current machine state, any node could write the next state, and the graph
maintained the full transition history. The state machine was part of the run
record.

In Deep Agents, `create_deep_agent()` does expose `state_schema=` to add custom
fields to graph state. But middleware hooks (`wrap_tool_call`) only receive the
current tool call and a `next_fn` - graph state does not flow through them. The
state machine could not live in graph state because the enforcement mechanism
(middleware) cannot read or write it. It had to be threaded as an external
mutable container passed to the middleware constructor at agent construction
time - not because custom state is unavailable, but because the middleware
scope does not include it.

The enforcement behavior is identical: out-of-sequence tool calls are blocked.
What is lost is observability. In Lab 6a the state machine's current state was
part of the graph's checkpoint record. In Lab 6c it lives in a Python object
that is not persisted between sessions. The `sm_state` field in the `tool_call`
log events captures the current state per-call, but reconstructing the full
transition history requires reading through the log rather than querying the
checkpoint.

The `AGENTS.md` vs `SOUL.md` design question resolved cleanly: no duplication.
`system_prompt=` covers identity and behavioral constraints; `AGENTS.md` covers
tool structure and usage sequences. Each does one job.

## What Failed

**HITL resume path silently returns null without a checkpointer**

Without `checkpointer=` wired to the agent, calling `invoke(Command(resume=...),
config=hitl_config)` completes without error but produces no output. The
`final_answer` in the run state is null and no `hitl_interrupt` event appears in
the JSONL log. The `token_cost` event is still written, but `message_count` is 0.
This was visible only by running an actual interrupt flow and reading the log - the
framework gives no warning at construction time that `checkpointer=` is required
for interrupt/resume to work.

The fix (adding `checkpointer=_CHECKPOINTER`) made the full interrupt → resume flow
produce a `hitl_interrupt` event on the first invoke and a `hitl_resume` event on
the second, with `final_answer` populated.

**Skills progressive disclosure: metadata-only injection was not what we assumed**

The initial assumption was that `SkillsMiddleware` would inject the full `SKILL.md`
body into the context at session start. Reading the `token_cost` logs showed the
input token count for sessions with skills active was nearly identical to sessions
without. The framework injects skill name and description only - the agent reads
the full body via `read_file` when it decides a skill applies. This is actually
better behavior (zero bulk startup token cost) but it changed the assumption the
lab was testing. The token cost comparison between Lab 6a's manual `load_skill`
tool and Deep Agents native skills is a comparison of deferred vs. eager loading,
not just framework API differences.

## What I Learned

**The abstraction ceiling is a topology problem, not an API problem.** Deep Agents
is not missing features - it made a deliberate design choice to own the graph. Every
capability loss in this lab traces back to one cause: you cannot add a node, rewire
an edge, or thread arbitrary state through the run. If your agent needs those things,
you need LangGraph directly.

**Middleware is the escape hatch, but it has a scope limit.** `wrap_tool_call` can
block, rewrite, or log. `before_agent` can inject context. `wrap_model_call` can
intercept the model. These are powerful for cross-cutting concerns. They cannot do
what nodes can: branch the execution path, pause mid-graph, or carry structured state
through the run.

**soul.md and AGENTS.md are not competing.** `system_prompt=` covers identity and
behavioral constraints; `AGENTS.md` covers tool structure and usage sequences. Each
does one job. No duplication needed between the two.

**-35% boilerplate comes with a dependency.** The line count wins because Deep Agents
owns the loop. That is also the constraint. Frameworks that hide complexity are faster
to wire and harder to rewire.

**Check prebuilt middleware before writing custom code.** Three hand-rolled patterns
in the same harness (custom memory loader, custom step cap, custom skill wiring) were
all replaced by prebuilt classes: `MemoryMiddleware`, `ModelCallLimitMiddleware`,
`SkillsMiddleware`. Grepping the middleware catalogue first would have saved a full
iteration.

## Metrics

| Metric | Target | Actual |
|--------|--------|--------|
| Single-agent port: all four modes run | Yes | Yes - mode parameter + system prompt injection |
| HITL via interrupt_on: verdict | native / ceiling / not possible | Native tool-level (interrupt_on= + checkpointer= + Command(resume=...)); node-level branching requires LangGraph |
| Per-mode routing: verdict | native / ceiling / not possible | Topology ceiling - system prompt injection is the correct approach, weaker structural guarantee than graph edges |
| Boilerplate delta vs Lab 6a | < 650 lines | 426 lines (-35%) |
| Ceiling findings documented | >= 2 | 3 (routing, HITL, state threading) |
| Prebuilt middlewares replacing hand-rolled code | n/a | 3 (MemoryMiddleware, SkillsMiddleware, ModelCallLimitMiddleware) |
| Token cost - Setup / Troubleshooting | < Lab 6a baseline | Pending live run (RULE-DA03 logging wired) |
| SummarizationMiddleware: fires on long run | Yes / No | Not triggered (no live run in test phase) |
| Tests passing | 49/49 | 49/49 |

---

## Technical Reference

### Central design decision

`create_deep_agent()` owns the graph topology. The entire Lab 6a `graph.py` (328
lines, conditional edges, checkpoint wiring, `pre_tool_check` node, HITL resume node)
is replaced by a single function call plus middleware hooks. The win is fewer lines;
the cost is that every behavior requiring node-level control now has a weaker
structural guarantee: conditional routing moves to model reasoning, HITL branching
moves to model interpretation, and middleware-owned state moves to an external
container. These are the correct Deep Agents approaches - not workarounds - but each
trades topology enforcement for configuration-time simplicity.

### Key files and what to read

| File | What it contains | Core to understand |
|------|-----------------|-------------------|
| `src/agent.py` | Full harness: `create_deep_agent()` wiring, `SetupStateMiddleware`, `run()` | Lines 180-240 (middleware), 310-360 (agent construction) |
| `src/skills.py` | `make_skills_middleware()` - `FilesystemBackend` + progressive disclosure | The whole file (39 lines); shows what was wrong in the original approach |
| `src/state.py` | `SetupStateMachine` threaded as external mutable container | `SEQUENCE` dict + `is_allowed()` / `advance()` - no changes from 6a, mechanism shift only |
| `src/AGENTS.md` | Tool manifest loaded at session start by `before_agent` hook | Read alongside `SOUL.md` to see what each covers |
| `src/memory.py` | `QdrantMemoryStore` (Qdrant-only from Lab 6 forward, RULE-MEM05) | `MemoryStore` protocol + `QdrantMemoryStore` class |
| `src/logger.py` | `StructuredLogger` with `_redact_obj()` and `log_tool_call()` | `log_tool_call()` signature at line 113 |

### What to tweak and where

- **Model**: `os.environ.get("MODEL_NAME", "claude-sonnet-4-5")` in `run()` - change via `.env`
- **Middleware stack**: `middleware=[before_agent_mw, sm_middleware, memory_mw, skills_mw, limit_mw]`
  in `run()` - order matters; `before_agent` must come first
- **SM enforcement scope**: `SetupStateMachine.SEQUENCE` in `state.py` - add or remove
  tools from the enforced read-validate-write sequence
- **Step cap**: `ModelCallLimitMiddleware(run_limit=8)` in `run()` - change `run_limit`
- **HITL tools**: `_HITL_TOOLS` dict in `agent.py` - keys are tool names to gate, values
  are the interrupt config passed to `interrupt_on=`
- **Token cost approximation**: `_count_input_tokens()` uses `chars / 4` proxy - replace
  with exact token API if deepagents exposes it in a future release

### How to run end-to-end

```bash
# From the repo root
cd conductor/sprint-06c-deep-agents

# Install deps into shared venv (run once per lab or after pyproject.toml changes)
UV_PROJECT_ENVIRONMENT=../.venv uv sync --extra dev

# Run all tests (no credentials needed)
UV_PROJECT_ENVIRONMENT=../.venv uv run pytest tests/test_sprint_06c.py -v

# Run a single test class
UV_PROJECT_ENVIRONMENT=../.venv uv run pytest tests/test_sprint_06c.py::TestSetupStateMiddleware -v

# Live run (requires .env with LLM_GATEWAY_URL and ANTHROPIC_API_KEY)
cp .env.example .env  # fill in values
UV_PROJECT_ENVIRONMENT=../.venv uv run python -m src.main \
  "My Snowflake connector keeps failing on credentials validation" \
  --mode troubleshooting --log-dir logs/

# Inspect token cost log after a live run
grep '"event": "token_cost"' logs/*.jsonl

# Inspect tool call log (RULE-O02 compliance)
grep '"event": "tool_call"' logs/*.jsonl
```

