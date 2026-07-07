#!/bin/bash
# Push unpushed commits when a Claude Code session ends.
# If the push is rejected because the branch diverged, rebase onto the
# remote and retry once, so a session never ends leaving the branch split.
set -euo pipefail

REPO_ROOT="$(git -C "$(dirname "$0")/../.." rev-parse --show-toplevel 2>/dev/null)" || exit 0
cd "$REPO_ROOT"

BRANCH="$(git branch --show-current 2>/dev/null)" || exit 0
[ -z "$BRANCH" ] && exit 0

LOCAL=$(git rev-parse HEAD 2>/dev/null) || exit 0
REMOTE=$(git rev-parse @{upstream} 2>/dev/null) || exit 0

if [ "$LOCAL" != "$REMOTE" ]; then
    if ! git push --quiet 2>/dev/null; then
        # Rejected: likely diverged. Rebase onto remote, then retry.
        git fetch --quiet origin "$BRANCH" 2>/dev/null || true
        if git pull --rebase --autostash --quiet 2>/dev/null && git push --quiet 2>/dev/null; then
            :
        else
            git rebase --abort 2>/dev/null || true
            echo "WARNING: $BRANCH has unpushed commits and auto-reconcile failed. Push manually before switching devices." >&2
        fi
    fi
fi

exit 0
