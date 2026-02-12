#\!/bin/bash
# Push unpushed commits when a Claude Code session ends.
set -euo pipefail

REPO_ROOT="$(git -C "$(dirname "$0")/../.." rev-parse --show-toplevel 2>/dev/null)" || exit 0
cd "$REPO_ROOT"

LOCAL=$(git rev-parse HEAD 2>/dev/null) || exit 0
REMOTE=$(git rev-parse @{upstream} 2>/dev/null) || exit 0

if [ "$LOCAL" \!= "$REMOTE" ]; then
    git push --quiet 2>/dev/null || {
        echo "WARNING: Unpushed commits on $(git branch --show-current). Push before switching devices." >&2
    }
fi

exit 0
