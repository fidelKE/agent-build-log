#!/usr/bin/env bash
# Install Conductor's git hooks into .git/hooks/.
#
# The pre-commit hook runs only deterministic unit tests (pytest, no LLM calls)
# and must complete in under 60 seconds (RULE-CI04).
#
# Usage (run from repo root or any subdirectory within the repo):
#   bash conductor/sprint-05a-cicd-eval-gate/scripts/install_hooks.sh

set -euo pipefail

# Find repo root by walking up from CWD
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null)
if [[ -z "$REPO_ROOT" ]]; then
    echo "ERROR: not inside a git repository" >&2
    exit 1
fi

HOOKS_DIR="$REPO_ROOT/.git/hooks"
HOOK_FILE="$HOOKS_DIR/pre-commit"

mkdir -p "$HOOKS_DIR"

# Detect the conductor sprint-05 venv (shared across sprints)
VENV_PATH="$REPO_ROOT/conductor/.venv"

cat > "$HOOK_FILE" << HOOK
#!/usr/bin/env bash
# pre-commit — run deterministic unit tests only (RULE-CI04)
# Installed by conductor/sprint-05a-cicd-eval-gate/scripts/install_hooks.sh
set -euo pipefail

SPRINT_DIR="\$(git rev-parse --show-toplevel)/conductor/sprint-05a-cicd-eval-gate"

if [[ ! -d "\$SPRINT_DIR" ]]; then
    echo "pre-commit: sprint-05a not found, skipping unit tests"
    exit 0
fi

echo "pre-commit: running unit tests..."

UV_PROJECT_ENVIRONMENT="$VENV_PATH" uv run pytest "\$SPRINT_DIR/tests/" -q --tb=short 2>&1
rc=\$?

if [[ \$rc -ne 0 ]]; then
    echo ""
    echo "pre-commit: tests failed — commit blocked. Fix failures or use git commit --no-verify to skip."
    exit 1
fi

echo "pre-commit: all tests passed"
exit 0
HOOK

chmod +x "$HOOK_FILE"
echo "Installed pre-commit hook at $HOOK_FILE"
echo "It will run unit tests from sprint-05a/tests/ before every commit."
echo "To uninstall: rm $HOOK_FILE"
