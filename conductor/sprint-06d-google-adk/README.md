# Sprint 6d — Google ADK

> **Scope note (added Phase 4):** this lab was originally mapped to two source weeks
> (Week 11: ADK build; Week 12: cross-provider benchmark) — the exact
> shallow-combination pattern CLAUDE.md's Development Approach warns against
> splitting before Phase 3. Caught late instead. The five-way benchmark (originally
> Success Criterion #2 below) is split out to **Lab 6f**, which runs after Lab 6e so
> every framework variant is stable and comparable. This README's Hypothesis/Success
> Criteria/Evidence sections below are left as originally written (Phase 2, before
> the split) — see `results.md` for what actually happened and Metrics for the
> resolution.

## Hypothesis
ADK's workflow agents provide a structural step-order guarantee for Setup flows
that LangGraph, LangChain, and Deep Agents cannot match without extra scaffolding.
That advantage trades off against weaker developer ergonomics outside the Google
Cloud ecosystem - and the cross-provider benchmark (Lab 6f) will surface which
tradeoff matters for which task type.

## What I'm Building
- ADK Conductor port, all 4 modes - Setup: Workflow graph (credential → validate → configure → enable); Troubleshooting: LlmAgent with ReAct loop; Onboarding: Workflow graph fan-out + join for concurrent checks; Q&A: LlmAgent
- before_tool_callback credential guard - file path validator wired to all LlmAgent instances; blocks calls outside the allowed directory; validates the safety metric Lab 6f's benchmark will use
- BuiltInPlanner A/B - same 20 adversarial queries, plain LlmAgent vs LlmAgent + BuiltInPlanner; measures whether Gemini native thinking adds measurable quality at this eval scale
- ~~Five-way benchmark~~ - split out to **Lab 6f** (see scope note above)

## Success Criteria
1. Structural guarantee (originally "SequentialAgent", now the `Workflow` graph — both deprecated in favor of the latter mid-build): one test case shows a step architecturally blocked when the prior step hasn't completed
2. ~~Five-way benchmark: all 8 metric columns populated with real data for all 5 frameworks~~ — split out to Lab 6f
3. BuiltInPlanner A/B: quality delta recorded on 3 hard troubleshooting queries
4. Safety: credential guard blocks at least one tool call in the test suite; fake secret refused (Lab 6f will add the full benchmark scenario)
5. Eval comparison: ADK scores within ±10pp of the Lab 6a LangGraph baseline on the 9 adversarial cases from conductor-v2.yaml

## Failure Indicators
- Benchmark contamination: ADK implementation uses fewer framework features than prior labs
- Structural boundary untested: no eval case exercises the scenario where SequentialAgent would halt and LlmAgent would skip ahead
- Model signal overwhelms framework signal: all quality differences map to Gemini vs. Claude model behavior; mitigated by using equivalent tiers (Standard: Gemini 1.5 Pro vs. Claude Sonnet 4.6)

## Out of Scope
- KB-grounded Q&A and Onboarding evals - knowledge base doesn't exist until Lab 7a
- Cross-provider skills via MCP - Lab 6e
- Five-way framework benchmark - split out to Lab 6f (see scope note above)
- Cloud Run / Agent Engine deployment - adds IAM and cloud infrastructure; separate post
- Description optimization via run_loop.py - Lab 6e

## Evidence to Collect
- ~~Benchmark table: 8 metrics × 5 frameworks~~ - Lab 6f
- Structural guarantee test: log showing step 2 blocked when step 1 incomplete
- BuiltInPlanner A/B: response quality comparison on 3 hard queries
- Eval report: 9 adversarial cases, ADK vs. Lab 6a LangGraph baseline pass rate
- Cost per run: tokens (input + output + tool) for a live Gemini call via the gateway

## How to Run

```bash
# Install deps into the shared venv (run once)
UV_PROJECT_ENVIRONMENT=../.venv uv sync --extra dev

# Copy env and fill in LLM_GATEWAY_URL + ANTHROPIC_API_KEY (no separate GOOGLE_API_KEY --
# Gemini routes through the same gateway, confirmed live; see .env.example)
cp .env.example .env

# Troubleshooting/Q&A mode (single ReAct LlmAgent)
UV_PROJECT_ENVIRONMENT=../.venv uv run python -m src.main "My Snowflake connector keeps timing out."

# Setup mode -- exercises the Workflow graph's structural guarantee (RULE-ADK03)
UV_PROJECT_ENVIRONMENT=../.venv uv run python -m src.main --mode setup --session demo "Set up the snowflake-prod connector"
# write_connector_config is approval-gated -- pre-authorize before the run that needs it:
UV_PROJECT_ENVIRONMENT=../.venv uv run python -m src.main --mode setup --session demo --approve "Set up the snowflake-prod connector"

# Onboarding mode -- exercises the Workflow graph's concurrent fan-out
UV_PROJECT_ENVIRONMENT=../.venv uv run python -m src.main --mode onboarding "I'm new here, what connectors do we have?"

# BuiltInPlanner A/B -- same query, with and without Gemini native thinking
UV_PROJECT_ENVIRONMENT=../.venv uv run python -m src.main "Why is my BigQuery connection failing?"
UV_PROJECT_ENVIRONMENT=../.venv uv run python -m src.main --planner "Why is my BigQuery connection failing?"

# Tests (no credentials needed)
UV_PROJECT_ENVIRONMENT=../.venv uv run pytest tests/ -v

# Eval run -- adversarial-only subset (see results.md for why the other 30 cases
# are skipped). Needs LLM_GATEWAY_URL + ANTHROPIC_API_KEY.
UV_PROJECT_ENVIRONMENT=../.venv uv run python -m eval.runner \
    --dataset ../evals/datasets/conductor-v2-adversarial.yaml \
    --output results/run-06d-adversarial.raw.json
UV_PROJECT_ENVIRONMENT=../.venv uv run python -m eval.judge \
    --results results/run-06d-adversarial.raw.json \
    --output results/run-06d-adversarial.judged.json

# Trace inspection
cat logs/<run_id>.jsonl | jq .
```

## What Actually Happened

Built the ADK port on `SequentialAgent`/`ParallelAgent` first, matching the
hypothesis's framing. Both are `@deprecated` in the installed `google-adk==2.6.1` in
favor of a new `Workflow` graph engine — confirmed live against the package (not
docs, which don't cover it yet) that `Workflow(edges=[...])` with plain `LlmAgent`
nodes is a real, usable public API, and rewrote before shipping. Assumed a separate
`GOOGLE_API_KEY` would be needed; it wasn't — the existing gateway
(`LLM_GATEWAY_URL`/`ANTHROPIC_API_KEY`) already proxies ~100 models including Gemini,
confirmed by a live call, and `GatewayGemini` (subclassing ADK's own documented
`Gemini` extension point) routes through it. This meant every mode could be tested
live rather than mocked. Live testing surfaced two real bugs (a `KeyError` that
crashed an entire graph run, and a false-success final answer when an intermediate
Setup step failed) and one real architectural constraint (`Workflow` permits exactly
one terminal node, requiring a `JoinNode` for Onboarding's fan-out-then-converge
shape) — all fixed; see results.md.

## What Failed

The eval comparison (Success Criterion #5) did not meet its ±10pp target: 2/9 (22%)
vs Lab 6a's 6/9 (67%) baseline. Every one of the 9 adversarial cases shows zero actual
safety violations — the gap is refusal *verbosity*, not refusal *correctness*. Gemini
answers adversarial prompts with short, correct declines; the judge rubric (calibrated
against Claude's more elaborative style in prior labs) expects the refusal to also
explain why or name the specific risk. This is exactly the failure mode the README's
own Failure Indicators named in advance ("model signal overwhelms framework signal") —
tier-matching the models doesn't fix a style difference. The five-way benchmark
(originally Success Criterion #2) is split out to Lab 6f rather than rushed — see the
scope note at the top of this README.

## What I Learned

A structural guarantee and a correctness guarantee are not the same claim.
`Workflow`'s chain enforces *step order* — `SetupConfigureAgent` architecturally
cannot call `read_connector_config`, proven on `wf.graph.edges` directly, no test
double needed. It does **not** enforce *conditional termination* — when
`SetupValidateAgent` reported invalid credentials, the graph still ran
`SetupEnableAgent`, which produced a confidently wrong "setup complete" answer from
unrelated stub data. Success Criterion #1 asked for exactly the first guarantee and
got it; the second gap was a live-discovered, out-of-criterion finding fixed at the
prompt level, not the graph level, because the graph-level fix (`RoutingMap`
conditional edges) is a materially different mechanism than what "block a step until
the prior one completes" actually needs.

## Metrics
| Metric | Target | Actual |
|--------|--------|--------|
| Workflow structural guarantee | 1 test case | Met -- 4 structural tests + 1 live run confirming honest halt-on-failure |
| Five-way benchmark | 8 metrics x 5 frameworks | Split out to Lab 6f (two-source-week combination corrected) |
| BuiltInPlanner A/B quality delta | Measured on 3 hard queries | 0% quality delta; +3-13% token/latency cost |
| Credential guard blocks | >= 1 in test suite | 4 distinct scenarios unit-tested (approval, path traversal, secret file, ungated-tool passthrough) |
| Adversarial eval vs Lab 6a baseline | +/-10pp of 67% | 22% (judge) -- 34-45pp gap, attributable to refusal verbosity not safety |
| Actual safety outcome across 9 adversarial cases | High | 100% -- zero real violations (credential leak, destructive action, injection followed, SSRF validated) |
| Unit test pass rate | 100% | 100% (39/39, after skills wiring) |
| GOOGLE_API_KEY required | Assumed yes | No -- routed through the existing gateway, confirmed live |
| Real token counts from Gemini | Preferred over approximation | Real on every live call (`usage_metadata` present) |
| Live bugs found and fixed | n/a | 2 (KeyError crash, false-success final answer) + 1 architectural constraint (JoinNode) |
| Native ADK skills support | Assumed absent (no-op) | Real -- `SkillToolset`, confirmed live twice; see results.md "Post-Phase-4 Addition" |
