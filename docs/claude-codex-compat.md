# Claude To Codex Compatibility Map

This document defines how Claude-specific settings are applied when working in Codex.

## Rules And Docs

- Claude instructions: `CLAUDE.md`
- Codex equivalent entry point: `AGENTS.md`
- Architecture reference: `ARCHITECTURE.md`

## Hooks

### Session Start

- Claude: `.claude/hooks/git-pull-on-start.sh` via `.claude/settings.json`
- Codex equivalent: run `bash scripts/claude-session-start.sh`

### Post Backtest Deploy

- Claude: `.claude/hooks/deploy-after-backtest.sh` via `.claude/settings.local.json`
- Codex equivalent: manually run `bash scripts/deploy.sh` after successful backtest runs that update `data/backtest_comparison.json`

## Permissions And Extensions

- Claude permissions live in `.claude/settings.local.json`.
- Codex uses a different permission model and cannot import Claude permission rules directly.
- Operational equivalent: follow the same command safety intent and production constraints from `CLAUDE.md` and `AGENTS.md`.

## Practical Seamless Switching

1. Start session: `bash scripts/claude-session-start.sh`
2. Use cloud-first deployment and debugging workflow from `CLAUDE.md`
3. After backtest updates, run `bash scripts/deploy.sh`
