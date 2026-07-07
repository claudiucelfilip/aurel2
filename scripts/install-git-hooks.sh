#!/usr/bin/env bash
# Point this clone's git at the tracked .githooks/ directory so the post-commit
# auto-push hook is active. Idempotent; safe to run on every session start.
set -euo pipefail

REPO_ROOT="$(git -C "$(dirname "$0")/.." rev-parse --show-toplevel 2>/dev/null)" || exit 0
cd "$REPO_ROOT"

want=".githooks"
have="$(git config --local --get core.hooksPath || true)"

if [ "$have" != "$want" ]; then
    git config --local core.hooksPath "$want"
    echo "Set core.hooksPath -> $want"
fi

chmod +x "$REPO_ROOT/.githooks/"* 2>/dev/null || true
