#!/bin/bash
# PostToolUse hook: auto-deploy dashboard after backtest runs
# Detects Bash commands containing "backtest" and deploys to production

set -euo pipefail

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

# Only trigger on backtest-related commands
if ! echo "$COMMAND" | grep -qE 'backtest'; then
    exit 0
fi

# Only trigger if the command succeeded
EXIT_CODE=$(echo "$INPUT" | jq -r '.tool_response.exitCode // 1')
if [ "$EXIT_CODE" != "0" ]; then
    exit 0
fi

# Only deploy if backtest_comparison.json was actually generated/updated
if [ ! -f "data/backtest_comparison.json" ]; then
    exit 0
fi

# Use the deploy script (runs tests + syncs + rebuilds)
/root/aurel2/scripts/deploy.sh 2>/dev/null

echo '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"Dashboard deployed with updated backtest data"}}'
