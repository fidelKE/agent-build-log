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
| 1 | Eval-first: 40 test cases before any agent code | [Read](https://agentbuildlog.hashnode.dev/eval-first-40-test-cases-before-agent-code) |

## Structure

```
conductor/
  posts/               ← blog post drafts
  evals/datasets/      ← eval cases (generic/public)
  sprint-NN-topic/     ← one folder per lab: src, tests, results, README
  STANDARDS.md         ← engineering rules, introduced per lab
  .env.example         ← environment variable reference
```

## Running the code

Each lab from Lab 3 onward has a `pyproject.toml`. All labs share a single virtualenv managed by [uv](https://github.com/astral-sh/uv).

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
python -m pytest src/test_sprint_00.py -v
```

## Environment variables

Copy `conductor/.env.example` to `conductor/.env` and fill in the values. Never commit `.env`.

## Blog

[agentbuildlog.hashnode.dev](https://agentbuildlog.hashnode.dev)
