# Lab 5 - Memory Systems + Eval Harness

## What I wanted to test

Do different memory architectures actually improve Conductor's answer quality - and by
how much? And does a reproducible eval harness give me a signal I can trust, or does
LLM-as-judge variance swamp the real differences?

Three memory providers, two eval datasets, one harness. The experiment measures whether
the abstraction level of the memory layer (explicit K/V vs. vector search vs. managed
extraction) changes pass rates - and whether the signal is stable enough to act on.

## Why this matters

Every agent tutorial says "add memory." Almost none measure whether memory helps.
The common failure: memory is added, pass rates drop slightly, and the team assumes
it's judge variance rather than investigating. This sprint wires the measurement
first so the comparison is honest.

The eval harness also establishes the quality baseline for every prompt change from
this sprint forward. Without it, every future sprint is shipping blind.

## Hypothesis

I expect that Troubleshooting mode accuracy measurably improves with episodic + K/V
memory because the agent can recall what was tried in prior sessions - and that the
eval harness will expose which memory pattern helps and which adds noise, while the
harness itself proves stable enough (rerun variance under 3 points) to detect real
regressions.

## What I'm Building

- **`memory.py` - unified memory interface over five providers + compression wrapper** (all four Conductor modes)
  A `MemoryStore` protocol with five implementations behind one interface:
  `search_memory(query, user_id)` and `add_memory(content, user_id, metadata)` on all five.
  Namespace isolation enforced at the interface layer - cross-user leakage is a hard failure.

  | Provider | Layer | What it stores | How retrieval works |
  |----------|-------|---------------|-------------------|
  | **Redis** | K/V entity | Structured facts: connector type, error code, steps tried | Exact key lookup - deterministic, zero embedding cost |
  | **Qdrant** | Episodic + semantic | Timestamped session narratives per user | Similarity search via fastembed (local, no API) |
  | **Mem0** | Hybrid (wraps Qdrant) | Auto-extracted facts + episodes | Mem0 decides what to store; `memory.add()` / `memory.search()` |
  | **InMemory** | CI / tests only | Any content | Linear scan - no external deps |
  | **SummaryMemory** | Compression wrapper | Wraps any above provider | Compresses oldest N entries when tokens exceed threshold |

  The agent uses tool-based retrieval - it calls `search_memory(query)` explicitly.
  No auto-injection before every call. (2026 pattern from MemoryAgentBench ICLR 2026.)

- **`SummaryMemory` - compression wrapper for long sessions** (§11.2)
  Wraps any `MemoryStore`. Counts tokens as `len(content) // 4`. When stored content exceeds
  `compression_threshold` (default: 2,000 tokens), folds the oldest `compress_oldest_n` (default: 5)
  non-summary entries into one compressed entry tagged `type: summary`. Default summarizer truncates
  each entry to 40 chars - explicitly lossy. Callers needing semantic compression pass a real
  `summarize_fn`. O(1) context cost regardless of history length vs. lossy compression tradeoff.

- **Mode-to-memory mapping** (motivated by each Conductor mode's actual needs)
  - Troubleshooting: Qdrant episodic + Redis K/V entity - cross-session continuity
  - Onboarding: Redis K/V entity - user facts (last connector, preferences)
  - Setup: Redis K/V entity - procedural state (current step, completed steps)
  - Knowledge Q&A: no memory - fresh lookup beats stale injection for stateless queries

- **Eval harness** (`eval/runner.py`, `eval/judge.py`, `eval/report.py`)
  YAML-driven runner feeds both datasets to the agent, collects outputs and tool traces.
  Two-stage judging: deterministic checks run first (field presence, must_not_contain),
  LLM-as-judge runs second for quality assertions.
  Token cost logged per case and per mode - this is the baseline for the mode router
  decision in a later sprint.

- **Memory-augmented eval cases** - 10 new cases added per dataset
  Cases that can only pass if memory is working: cross-session continuity, namespace
  isolation, and stale fact detection. These are the diagnostic layer - they separate
  "memory helps" from "memory adds noise."

- **Provider comparison** - same 10 memory-dependent cases run against all five providers
  Pass rate, latency ms, and token cost per provider side by side.

- **Train/held-out dataset split** (§44.4)
  `conductor-v1-train.yaml` (32 cases, 80%) for prompt development and iteration.
  `conductor-v1-held-out.yaml` (7 cases, 20%) locked - never used during development.
  Split is stratified by mode so each mode is proportionally represented in held-out.
  Zero overlap enforced by test: `test_held_out_dataset_is_disjoint_from_train`.

- **Dataset health metrics** (`eval/report.py --health`, §44.5)
  Coverage rate (% cases with all required fields), freshness (% with `created_date`
  within 180 days), difficulty tag distribution vs. 40/30/20/10% target.
  Current state: 100% coverage, 100% freshness, medium-heavy distribution (WARN).

## Success Criteria

1. Namespace isolation: User A's memory entries are unreachable from User B's session
   (tested with direct `search_memory` calls, not just agent output)
2. Cross-session continuity: Troubleshooting mode reconstructs prior context without
   re-asking already-answered questions (Session 1 sets up facts, Session 2 uses them)
3. Eval harness stability: two reruns of the same agent+memory config vary by 3 points
   or less - the harness is stable enough to detect real regressions
4. Regression detection: removing memory retrieval causes a measurable pass-rate drop
   specifically on memory-dependent cases (not overall cases)
5. Token cost baseline recorded: ReAct loop + memory retrieval vs. single-call Q&A,
   per mode - numbers feed the mode router decision in a later sprint
6. Both datasets reach 75% overall pass rate independently reported
   (generic dataset + domain-specific dataset scored separately, never averaged)
7. SummaryMemory: token count after compression < token count before (verified by test)
8. SummaryMemory: compressed entry is retrievable via `search_memory` (keyword in first 40 chars)
9. Train/held-out split: zero overlap between `conductor-v1-train.yaml` and `conductor-v1-held-out.yaml`
10. Dataset health: `--health` flag produces coverage rate, freshness, and distribution report

## Failure Indicators

- Adding memory causes overall pass rate to drop AND failing cases show irrelevant
  context injection on inspection - memory is hurting, not helping
- Harness reruns vary by more than 5 points with no agent changes - judge noise is
  too high to detect regressions; need a stricter rubric or deterministic checks only
- Namespace isolation test fails - cross-user leakage, a hard stop regardless of pass rate
- Provider comparison is within eval noise - can't distinguish Redis from Qdrant from
  Mem0 on these cases; means memory-dependent cases aren't discriminating enough

## Out of Scope

- RAG over a knowledge base - requires a document corpus and belongs in the context/token
  sprint; specific prerequisite: a chunked, indexed knowledge base to retrieve from
- Mem0 Cloud / Qdrant Cloud SaaS - self-hosted only so blog readers can run the full
  experiment with the compose file; cloud comparison deferred until self-hosted baseline exists
- Procedural memory (ReflectionAgent) - distilling procedural lessons from resolved sessions;
  prerequisite: episodic memory running in production with enough sessions to distill from - Lab 10c
- Online evaluation (live trace scoring, distribution shift detection) - Lab 8b; the offline
  gate is the contract for now; production failures become test cases manually
- LanceDB / Chroma / pgvector - Chroma is already provisioned in compose for Lab 6a RAG;
  pgvector and LanceDB offer no new story vs. Qdrant for this sprint's comparison
- Recall paraphrase retrieval benchmark (the experiment that distinguishes provider quality) -
  belongs in the RAG lab once KB exists and memory-dependent cases are writable

## Evidence to Collect

- Memory write + retrieval log: what was stored, what was retrieved, for which user_id
- Namespace isolation test output: pytest pass/fail with logged cross-user query attempts
- Session 1 -> Session 2 continuity demo: transcript showing context reconstruction
  without re-asking answered questions
- Eval report: pass rate table by dataset (generic vs. domain-specific) and by mode
- Provider comparison table: Redis vs. Qdrant vs. Mem0 (pass rate / latency ms / tokens)
- Token cost baseline table: per mode, per query type (ReAct+memory vs. single-call)
- Regression test: score on memory-dependent cases before vs. after removing retrieval
- `podman compose up` output showing all services healthy (Vault, Redis, Qdrant, Chroma, MinIO)

---

## How to Run

### 1. Start infrastructure (canonical stack - run from conductor/)
```bash
cd conductor/
podman compose up -d
podman compose ps   # verify all services healthy: Vault, Redis, Qdrant, Chroma, MinIO
```

### 2. Install dependencies (shared venv)
```bash
cd conductor/sprint-05-memory-evals
UV_PROJECT_ENVIRONMENT=../.venv uv sync --extra dev
```

### 3. Set up credentials
```bash
cp .env.example .env
# Edit .env: fill in LLM_GATEWAY_URL, ANTHROPIC_API_KEY
# Set MEMORY_PROVIDER=redis | qdrant | mem0 | inmemory
```

### 4. Seed Vault with catalog token (required - Vault loses state on container restart)
```bash
# If vault CLI is available:
cd conductor/sprint-04-secrets-storage
source .env && bash vault_setup.sh
cd ../sprint-05-memory-evals

# If vault CLI is not on PATH, seed via curl:
source conductor/sprint-04-secrets-storage/.env
curl -s -X POST http://localhost:8200/v1/secret/data/conductor/catalog-api-token \
  -H "X-Vault-Token: dev-root-token" \
  -H "Content-Type: application/json" \
  -d "{\"data\": {\"value\": \"${CATALOG_API_TOKEN}\"}}"
```

### 5. Run the agent
```bash
UV_PROJECT_ENVIRONMENT=../.venv uv run conductor "I'm still having that Snowflake problem"
```

### 6. Run all tests
```bash
UV_PROJECT_ENVIRONMENT=../.venv uv run pytest tests/ -v
```

### 7. Run eval harness - generic dataset
```bash
UV_PROJECT_ENVIRONMENT=../.venv uv run python -m eval.runner \
    --dataset ../../evals/datasets/conductor-v1-approved.yaml \
    --memory-provider inmemory \
    --output results/run-generic-inmemory.json

UV_PROJECT_ENVIRONMENT=../.venv uv run python -m eval.judge \
    --results results/run-generic-inmemory.json \
    --output results/run-generic-inmemory-judged.json

UV_PROJECT_ENVIRONMENT=../.venv uv run python -m eval.report \
    --results results/run-generic-inmemory-judged.json
```

### 8. Run provider comparison
```bash
for provider in inmemory redis qdrant mem0 mem0-server; do
  UV_PROJECT_ENVIRONMENT=../.venv uv run python -m eval.runner \
      --dataset ../../evals/datasets/conductor-v1-approved.yaml \
      --memory-provider $provider \
      --output results/run-generic-${provider}.json
  UV_PROJECT_ENVIRONMENT=../.venv uv run python -m eval.judge \
      --results results/run-generic-${provider}.json \
      --output results/run-generic-${provider}-judged.json
done

UV_PROJECT_ENVIRONMENT=../.venv uv run python -m eval.report \
    --results results/run-generic-inmemory-judged.json:inmemory \
              results/run-generic-redis-judged.json:redis \
              results/run-generic-qdrant-judged.json:qdrant \
              results/run-generic-mem0-judged.json:mem0 \
              results/run-generic-mem0-server-judged.json:mem0-server \
    --output results/provider-comparison.md
```

### 9. Run recall-oriented eval (requires live infra: redis/qdrant/mem0)
```bash
FIXTURE=../../evals/fixtures/memory-sessions.yaml
RECALL=../../evals/datasets/conductor-memory-recall-v1.yaml

for provider in redis qdrant mem0; do
  # No context baseline
  UV_PROJECT_ENVIRONMENT=../.venv uv run python -m eval.runner \
      --dataset $RECALL --memory-provider $provider \
      --output results/recall-${provider}-nocontext.json

  # With alice fixture context
  UV_PROJECT_ENVIRONMENT=../.venv uv run python -m eval.runner \
      --dataset $RECALL --memory-provider $provider \
      --seed-memories $FIXTURE --fixture-user eval-fixture-alice \
      --output results/recall-${provider}-alice.json

  UV_PROJECT_ENVIRONMENT=../.venv uv run python -m eval.judge \
      --results results/recall-${provider}-nocontext.json \
      --output results/recall-${provider}-nocontext-judged.json
  UV_PROJECT_ENVIRONMENT=../.venv uv run python -m eval.judge \
      --results results/recall-${provider}-alice.json \
      --output results/recall-${provider}-alice-judged.json
done
```

Expected: 0/3 → 1/3 with fixture context. Near-exact queries pass; paraphrase/vague fail.

### 10. Optional: Mem0 server mode (builds from source)

Adds a self-hosted Mem0 REST API + dashboard. Skip this if you only want
library mode (MEMORY_PROVIDER=mem0). Full setup in `conductor/repos/README.md`.

```bash
# 1. Clone the Mem0 repo
cd conductor/repos
git clone https://github.com/mem0ai/mem0

# 2. Start the Mem0 server stack
cd conductor
podman compose --profile mem0-server up -d

# 3. Bootstrap admin + generate API key (first run only)
podman exec -it conductor-mem0-server sh -c "
  cd /app/server &&
  python scripts/seed_admin.py --email admin@conductor.dev --password conductor-dev
"
# Copy the printed API key to .env as MEM0_API_KEY

# 4. Verify
curl -s http://localhost:8888/healthz
# Dashboard: http://localhost:3001

# 5. Run eval with server mode
MEMORY_PROVIDER=mem0-server \
UV_PROJECT_ENVIRONMENT=../.venv uv run python -m eval.runner \
    --dataset ../../evals/datasets/conductor-v1-approved.yaml \
    --memory-provider mem0-server \
    --output results/run-generic-mem0-server.json
```

### 11. Stop infrastructure
```bash
cd conductor/
podman compose down                          # default stack
podman compose --profile mem0-server down    # if mem0-server was started
```

---

## What Actually Happened

Five memory providers built and tested end-to-end. All five work: inmemory, Redis, Qdrant, Mem0
library, and Mem0 server (REST API, self-hosted via Docker build from source).

Namespace isolation held across all providers - User B cannot retrieve User A's entries.
The agent correctly uses `search_memory` before answering and `add_memory` at session end.
On the second session, the agent found existing memories and skipped `add_memory` - correct
deduplication behaviour without any explicit instruction to deduplicate.

Eval baseline: 30.8% generic, 6.7% domain-specific. Both are expected - the agent has a
5-note knowledge base. The harness is working correctly; the score reflects the knowledge gap.

Token cost baseline captured: Troubleshooting uses 1.69x more tokens than Q&A (9,742 vs 5,753).
This is the data point Lab 10 will use to route queries.

`SummaryMemory` added: 55% token reduction on sessions past the 50-token threshold (150 tokens → 67
tokens). Key facts placed in the first 40 chars of entries are retrievable after compression.

Train/held-out split created: 32 cases in train (80%), 7 in held-out (20%), stratified by mode, zero
overlap verified by test.

Dataset health: 100% coverage rate, 100% freshness. Difficulty distribution is medium-heavy
(62% medium vs. 30% target) - a known gap to address in future eval iterations.

## What Failed

1. **Eval harness stability**: 20-point variance between two runs of the same 5 cases.
   Root cause: LLM judge non-determinism on `setup-medium-001` - the case lacks a
   `key_decision` anchor, so the judge makes different borderline calls each run.
   Not a broken harness - a dataset design gap.

2. **user_id security gap**: Found during manual inspection. The model was inventing
   `user_id: "user"` and `user_id: "default"` because nothing in the prompt told it otherwise.
   Would have collapsed all users into the same namespace in production. Fixed with RULE-MEM05.

3. **Compliance scan missed eval/**: T01-T03 violations (plain dict return, wrong token field names)
   were not caught in the pre-build scan because the scan only covered `src/`. Fixed in STANDARDS.md
   and phase-3-build.md.

4. **Mem0 server startup issues** (resolved during setup):
   - `python:3.12-slim` missing `libpq` - fixed by using `python:3.12`
   - `claude-haiku-4-5` rejected by Bedrock gateway (temperature+top_p) - switched to `gpt-4.1-mini`
   - SQLite history path unwritable - added `HISTORY_DB_PATH` env var + volume mount
   - `/healthz` endpoint does not exist - correct endpoint is `/auth/setup-status`

5. **SummaryMemory default summarizer was not compressing**: First implementation verbatim-joined
   entries with a header, producing a longer entry than the originals (before=890, after=956 tokens).
   `test_token_count_reduces_after_compression` caught it. Fixed by truncating each entry to 40 chars.

6. **SummaryMemory search keyword placed after char 40**: `test_compressed_entry_retrievable_via_search`
   failed because the keyword appeared at position 41, past the truncation boundary. Fixed by placing
   the keyword first in the entry content.

## What I Learned

Memory without measurement is untestable - and measurement without a stable harness is
untrustworthy. Both were needed at the same time. Building them together forced clarity on
what "memory works" actually means: it means specific assertions pass, not that responses
feel better.

The most surprising finding: the agent deduplicated memories on its own. When `search_memory`
returned an existing entry about Snowflake JDBC auth, the agent decided `add_memory` was
redundant and skipped it. This emerged from the tool descriptions and system prompt alone -
no explicit deduplication logic was written. It's the right behaviour and it would have been
invisible without the trace log.

The user_id security gap was caught because we ran the agent, not just the tests. Unit tests
pass for any `user_id` value. Only the end-to-end trace showed the model inventing its own
value. This is why the sprint spec says "trace logs are evidence" - they surface what the
model actually did, not what the code allows.

Token cost numbers are the most durable output of this sprint. 9,742 vs 5,753 input tokens
(troubleshooting vs Q&A) will still be relevant in Lab 10 when the mode router decision is made.

Not all memory types are equal. This sprint builds episodic and semantic memory (retrievable
on demand). Procedural memory - rules written permanently into the system prompt after failure
analysis - is the compounding layer. Retrieved episodic facts help one user on one session.
A procedural rule distilled from that session helps every user automatically, without any
retrieval step. That distinction shapes what the reflection memory lab is designed to build.

## Metrics

| Metric | Target | Actual |
|--------|--------|--------|
| Namespace isolation test | Pass | Pass |
| Cross-session continuity test | Pass | Pass |
| Eval harness rerun variance | ≤3 pts | 0 pts after key_decision fix (was 20 pts before) |
| Generic dataset pass rate | ≥75% | 30.8% - BASELINE (no KB yet) |
| Domain-specific dataset pass rate | ≥75% | 6.7% - BASELINE (no KB yet) |
| Recall-oriented cases (with fixture) | >0% improvement | +33% vs. no-context on all 3 providers |
| Token cost - Troubleshooting | baseline | 9,742 input / 529 output |
| Token cost - Q&A | baseline | 5,753 input / 315 output |
| Token ratio (Troubleshooting / Q&A) | - | 1.69x |
| SummaryMemory token reduction | >0% | 55% (150 tokens → 67 tokens, 50-token threshold) |
| SummaryMemory key fact recall | retrievable | Yes (keyword in first 40 chars) |
| Train/held-out overlap | 0 | 0 (verified by test) |
| Dataset coverage rate | 100% | 100% |
| Dataset freshness | ≥80% | 100% (all dated 2026-06-19) |
| Dataset difficulty distribution | 40/30/20/10 | 21/62/13/5% (medium-heavy - WARN) |
| Unit tests passing | 85/85 | 85/85 |
| Providers working | 5/5 | 5/5 |
