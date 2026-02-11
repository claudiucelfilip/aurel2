#!/usr/bin/env bash
set -euo pipefail

# Mirror Claude SessionStart hook: fast-forward pull only.
if git rev-parse --show-toplevel >/dev/null 2>&1; then
  git pull --ff-only --quiet || true
fi
