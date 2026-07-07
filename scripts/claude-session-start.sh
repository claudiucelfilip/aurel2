#!/usr/bin/env bash
set -euo pipefail

# Mirror Claude SessionStart hook: sync the current branch, surviving divergence.
if git rev-parse --show-toplevel >/dev/null 2>&1; then
  bash "$(git rev-parse --show-toplevel)/scripts/install-git-hooks.sh" >/dev/null 2>&1 || true
  BRANCH="$(git branch --show-current 2>/dev/null || true)"
  if [ -n "$BRANCH" ]; then
    git fetch --quiet origin "$BRANCH" 2>/dev/null || true
    git pull --ff-only --quiet 2>/dev/null \
      || git pull --rebase --autostash --quiet 2>/dev/null \
      || { git rebase --abort 2>/dev/null || true; \
           echo "WARNING: $BRANCH diverged; reconcile before working." >&2; }
  fi
fi
