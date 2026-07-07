#!/bin/bash
# Sync the CURRENT branch on session start. Survives divergence:
# ff-only first, then rebase --autostash so a diverged checkout self-heals
# instead of silently proceeding on stale state.
cd "$(git -C "$(dirname "$0")/../.." rev-parse --show-toplevel 2>/dev/null)" || exit 0

BRANCH="$(git branch --show-current 2>/dev/null)" || exit 0
[ -z "$BRANCH" ] && exit 0

git fetch --quiet origin "$BRANCH" 2>/dev/null || exit 0

git pull --ff-only --quiet 2>/dev/null && exit 0

# Diverged: reconcile by rebasing local work onto the remote branch.
if ! git pull --rebase --autostash --quiet 2>/dev/null; then
    git rebase --abort 2>/dev/null || true
    echo "WARNING: $BRANCH is diverged and auto-rebase failed. Reconcile manually before working." >&2
fi
exit 0
