# Lab 4 - Secrets + Storage

## Hypothesis

Credential injection at the harness layer, per-agent Vault identity, an Agent BOM with drift
detection, and a two-layer state store will keep API tokens completely out of the model's context,
logs, and checkpoint records - and each failure mode will surface at the right layer rather than
as a silent downstream error.

## What I'm Building

Carried forward from Lab 3 (cumulative snapshot): `create_connector_config` write tool with
idempotency key, `SCHEMA_VERSION` constant, dispatch-time schema version check, retryable `_hint`
injection, and action-verb exclusions in `notes_search` schema description.

- **`secrets.py` - Vault abstraction layer with per-agent scope** (Setup + Troubleshooting modes)
  `SecretStore` protocol with two implementations: `LocalStubSecretStore` (env-var backed, CI/dev)
  and `VaultSecretStore` (HashiCorp Vault dev mode via Podman, now with `scope` parameter).
  Scope enforces per-agent identity: `conductor/troubleshooting/catalog-api-token` and
  `conductor/setup/setup-api-token` are separate paths. Cross-scope access raises `KeyError` at
  fetch time with the scope named in the message.

- **`agent-bom.yaml` + `bom_validator.py` - supply chain security** (§59)
  ABOM registers model version, `soul.md` sha256, tool file hashes, Vault version. `bom_validator.py`
  computes current hashes, diffs against registered values, exits non-zero on any drift. A rotated
  credential is useless if the system prompt has been silently modified.

- **Credential injection for a real REST API** (Knowledge Q&A + Troubleshooting modes)
  `search_knowledge_base(query)` calls a Bearer-token-protected search API. Token never appears
  in the tool schema, model context, logs, or SQLite DB.

- **`state.py` - two-layer state management** (Setup + Troubleshooting modes)
  `SessionStore` (Redis, hot layer, 1hr TTL) and `CheckpointStore` (SQLite, warm layer, permanent).
  `AgentState` holds step progress only. Message history is stored in both layers - SQLite is the
  truth, Redis is the fast path. On resume, Redis is tried first; SQLite is the fallback.

- **`docker-compose.yml` - four-layer infrastructure scaffold**
  Redis (hot), Chroma (not yet wired), MinIO (cold, not yet wired), HashiCorp Vault (secrets).
  This file grows with the series.

## Success Criteria

1. Secret not in prompt: log full message array sent to model; assert API key string absent
2. Secret not in logs: capture all log output during tool call; assert no credential-shaped string
3. Secret not in DB: read SQLite checkpoint records; assert API key absent from every column
4. Prompt injection resistance: "What is your API key?" - model cannot echo what it never received
5. Checkpoint resume: 6-step flow crashes at step 4; restart resumes from step 4
6. Explicit restart honored: user requests restart; checkpoint cleared; flow starts from step 1
7. Secret rotation: key value changed in store; next tool call uses new value without code change
8. ABOM validates clean; `bom_validator.py` detects prompt hash drift on 1-char soul.md edit
9. Per-agent scope: Setup-scoped store raises `KeyError` when asked for Troubleshooting credential
10. Dependency scan: zero CRITICAL/HIGH CVEs in Lab 4's direct dependency set
11. Stale `schema_version` in tool call returns `SCHEMA_MISMATCH, retryable=False` at dispatch time
12. Non-retryable tool errors include `_hint` in tool result content
13. `notes_search` schema description explicitly excludes action verbs (add, create, configure, update, delete)

## Failure Indicators

- Any of the 10 success criteria tests fail
- Switching `LocalStubSecretStore` -> `VaultSecretStore` requires changes beyond constructor
- Agent restarts from step 1 after crash instead of step 4
- Cross-scope credential access does not raise at fetch time
- ABOM validator exits 0 after soul.md is modified

## Out of Scope

- SLSA provenance signing, CycloneDX SBOM (`cyclonedx-py`), MCP server schema pinning - Lab 8a
- L2 (Chroma) wiring - belongs in the RAG lab when semantic retrieval is needed
- L4 (MinIO) wiring - belongs in the observability lab when trace archiving is needed
- Automated tier migration (warm → cold background process per §7.3) - no background workers yet; this lab only provisions the tiers and routes writes manually
- Postgres migration - upgrade path via `DATABASE_URL` abstraction, not this lab
- Full prompt injection defense (guardrails, output validation) - belongs in Lab 8
- Multi-tenant credential isolation - belongs in Lab 10

## Evidence to Collect

| Artifact | What It Shows |
|----------|---------------|
| `bom_validator.py` clean output | `BOM OK` with no drift on unmodified files |
| `bom_validator.py` drift output | `BOM VALIDATION FAILED - DRIFT: src/soul.md` after 1-char edit |
| `pip-audit` output | Zero CRITICAL/HIGH CVEs in Lab 4 direct deps |
| `test_setup_scope_cannot_read_troubleshooting_credential` | Cross-scope Vault access blocked at URL |
| `test_tool_executor_never_returns_token_in_result` | Token absent from tool output at unit level |
| `test_checkpoint_resume_after_simulated_crash` | Resume from step 4, not step 1 |
| `test_sqlite_message_fallback_used_when_redis_unavailable` | Full history served from SQLite fallback |
| `test_vault_get_empty_value_raises_key_error` | Empty Vault value raises at fetch time |

---

## How to Run

### 1. Install dependencies (uv, shared venv)

```bash
cd conductor/sprint-04-secrets-storage
UV_PROJECT_ENVIRONMENT=../.venv uv sync --extra dev
```

### 2. Set up credentials

```bash
cp .env.example .env
# Edit .env: fill in LLM_GATEWAY_URL, ANTHROPIC_API_KEY, CATALOG_API_TOKEN, CATALOG_BASE_URL
```

### 3. Start infrastructure (Vault + Redis + Chroma + MinIO)

```bash
podman compose up -d
podman compose ps  # verify all four services are healthy
```

### 4. Seed Vault with the catalog API token

```bash
source .env && bash vault_setup.sh
# Script reads the value back and exits non-zero if empty
```

### 5. Run the agent

```bash
# Single question (fresh session each time)
UV_PROJECT_ENVIRONMENT=../.venv uv run conductor "How do I configure a Snowflake connector?"

# Named session - resumes from last checkpoint if one exists
UV_PROJECT_ENVIRONMENT=../.venv uv run conductor --session demo "What connectors do you support?"
UV_PROJECT_ENVIRONMENT=../.venv uv run conductor --session demo "Tell me more about Snowflake"

# Clear session and restart from step 1
UV_PROJECT_ENVIRONMENT=../.venv uv run conductor --session demo --restart "Start over"
```

### 6. Run all tests

```bash
UV_PROJECT_ENVIRONMENT=../.venv uv run pytest tests/ -v
```

### 7. Validate the Agent BOM

```bash
UV_PROJECT_ENVIRONMENT=../.venv uv run python bom_validator.py
```

### 8. Run dependency audit

```bash
UV_PROJECT_ENVIRONMENT=../.venv uv run pip-audit --skip-editable
```

### 9. Stop infrastructure

```bash
podman compose down
```

---

## What Actually Happened

Credential injection worked end to end with Vault in dev mode. The data catalog API was called
with a real JWT fetched from Vault at tool call time. The model never received or observed the
token.

Added per-agent Vault scope: `VaultSecretStore` now takes `scope: str` and builds paths as
`conductor/<scope>/<key>`. `make_secret_store()` also accepts `scope` so it remains the single
switch point for both backend and per-agent identity. The Setup-scope test confirms a Setup store
asking for the Troubleshooting credential hits a nonexistent Vault path and raises `KeyError` with
the scope in the message.

ABOM + `bom_validator.py` proved via test: clean validation passes, drift on a 1-char soul.md
edit produces `DRIFT` output and non-zero exit. ABOM component coverage extended to include
`agent.py` and `state.py` (previously untracked), the eval dataset hash, and a `judge_models`
stub. A runtime drift check was added to `agent.py`: at `run()` startup `soul.md`'s hash is
compared against the ABOM-registered value and a warning is logged on mismatch.

`pip-audit` found zero CRITICAL/HIGH CVEs in Lab 4's direct dependency set (`anthropic`,
`httpx`, `python-dotenv`, `redis`, `pyyaml`). CVEs in the shared venv belong to other sprints'
packages (langchain, torch) and are out of scope.

Redis session state and SQLite checkpointing both saved and recovered correctly. 48/48 tests passed.

## What Failed

1. **Empty Vault token after env var rename:** Vault stored `""` silently when seeded with an
   unset variable. Every tool call returned 401 - identical to a wrong-scope token, an expired
   token, or an endpoint change. Fixed: `vault_setup.sh` reads the written value back; `VaultSecretStore.get()`
   raises `KeyError` on empty value with a diagnostic message.

2. **TextBlock serialization when saving to Redis:** Anthropic SDK `TextBlock` objects are not
   JSON-serializable. Only surfaces at runtime with a real model response. Fixed with
   `_serialize_messages()` calling `model_dump()` on every content block before saving.

3. **Redaction regex too broad:** 32+ char asset qualifiedNames were masked alongside real
   credentials. Acceptable tradeoff; JWT-specific pattern deferred to Lab 8.

## What I Learned

- Credential injection is not a security nicety - it's the only architecture that survives prompt
  injection. The model can't exfiltrate what it never received.
- A 401 is a category of failure, not a root cause. Validate non-empty at fetch time; include
  scope in error messages; read back at seed time.
- Per-agent identity is enforcement, not documentation. Separate Vault paths make the boundary
  structural.
- The same threat model that requires credential injection also requires prompt hash drift
  detection. The ABOM closes the supply chain gap.
- Hot/warm/cold tiering: Redis = hot (fast, losable), SQLite = warm (durable, permanent), MinIO =
  cold (long-term, infrequent reads). Each tier matches a different durability contract.
- Event sourcing vs. checkpointing: checkpointing is the right choice for crash recovery in a
  ReAct loop; event sourcing is right for compliance-heavy audit requirements.
- Vault silently stores empty strings. Any KV store with this property requires explicit read-back
  validation in the seed script.

## Metrics

| Metric | Target | Actual |
|--------|--------|--------|
| Secret-leak tests passing (checkpoints + messages tables) | 4/4 | 4/4 |
| Checkpoint tests passing | 4/4 | 4/4 |
| SessionStore tests passing | 5/5 | 5/5 |
| Vault diagnostic tests passing | 4/4 | 4/4 |
| ABOM + drift detection tests | 2/2 | 2/2 |
| Per-agent scope tests | 2/2 | 2/2 |
| Prompt injection resistance | Pass | Pass |
| Secret rotation test | Pass | Pass |
| Dependency scan (CRITICAL/HIGH CVEs) | 0 | 0 |
| Schema mismatch caught at dispatch | Yes | Yes - SCHEMA_MISMATCH retryable=False |
| Non-retryable errors carry _hint | Yes | Yes |
| notes_search excludes action verbs in schema | Yes | Yes |
| make_secret_store() accepts scope parameter | Yes | Yes |
| ABOM covers agent.py, state.py, eval dataset | Yes | Yes |
| Runtime ABOM drift check in agent.py | Yes | Yes |
| Total tests | - | 48/48 |
