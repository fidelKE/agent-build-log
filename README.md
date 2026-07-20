# Agent Build Log

Code for the [Agent Build Log](https://agentbuildlog.hashnode.dev) series — building Conductor, an AI agent for data integration, one experiment at a time.

Every lab produces working code, passing tests, and an honest post about what broke.

## The Agent: Conductor

Conductor is a technical co-pilot for data integration with four capability modes:

| Mode | What it does |
|------|-------------|
| **Setup** | Guides through connector/integration setup step by step |
| **Onboarding** | Walks new users through first-run experience |
| **Troubleshooting** | Diagnoses and resolves integration failures |
| **Knowledge Q&A** | Answers "how do I..." questions from a knowledge base |

## Labs

| Lab | Title | Blog post |
|-----|-------|-----------|
| - | I've Built AI Agents. Now I'm Learning to Build Them Properly - From the Ground Up. | [Read](https://agentbuildlog.hashnode.dev/building-ai-agents-properly-from-the-ground-up) |
| 1 | I Wrote 40 Test Cases Before Writing Any Agent Code. Here's What Happened. | [Read](https://agentbuildlog.hashnode.dev/eval-first-40-test-cases-before-agent-code) |
| 2 | The Model Is Not the Agent: My First Harness PoC | [Read](https://agentbuildlog.hashnode.dev/model-is-not-the-agent-first-harness-poc) |
| 3 | The Prompt Is a Specification. The Trace Is the Audit Log. | [Read](https://agentbuildlog.hashnode.dev/prompt-is-a-specification-trace-is-audit-log) |
| 4 | The Agent Can't Leak What It Never Had: Secrets and Storage | [Read](https://agentbuildlog.hashnode.dev/agent-cant-leak-what-it-never-had-secrets-storage) |
| 5 | Memory Without Measurement Is Guesswork | [Read](https://agentbuildlog.hashnode.dev/memory-without-measurement-is-guesswork) |
| 5a | The Eval Gate: Gating Prompt Changes Like You Gate Code Changes | [Read](https://agentbuildlog.hashnode.dev/eval-gate-gating-prompt-changes-like-code-changes) |

## Structure

```
conductor/
  evals/datasets/      ← eval cases (generic/public)
  sprint-NN-topic/     ← one folder per lab: src, tests, results, README
  STANDARDS.md         ← engineering rules, introduced per lab
  .env.example         ← environment variable reference
.github/workflows/
  agent-eval.yml       ← CI eval quality gate (workflow_dispatch)
```

## Running the code

Labs 3 onward have a `pyproject.toml`. All labs share a single virtualenv managed by [uv](https://github.com/astral-sh/uv).

```bash
# Install deps (run once per lab)
cd conductor/sprint-NN-topic
UV_PROJECT_ENVIRONMENT=../.venv uv sync --extra dev

# Run tests
UV_PROJECT_ENVIRONMENT=../.venv uv run pytest tests/ -v
```

Labs 1 and 2 have no `pyproject.toml` — run tests directly:

```bash
cd conductor/sprint-01-eval-bootstrap
python -m pytest src/ -v

cd conductor/sprint-02-harness-tool-design
python -m pytest src/ -v
```

## CI

The repo includes a `workflow_dispatch` eval quality gate (`.github/workflows/agent-eval.yml`). It runs unit tests on every trigger and gates on eval pass rate when `ANTHROPIC_API_KEY` and `LLM_GATEWAY_URL` secrets are configured. Introduced in Lab 5a.

## Environment variables

Copy `conductor/.env.example` to `conductor/.env` and fill in the values. Never commit `.env`.

## Blog

[agentbuildlog.hashnode.dev](https://agentbuildlog.hashnode.dev)
