# Codex + Claude Compatibility Rules

This repository is configured to keep Codex behavior aligned with Claude Code behavior.

## Source Of Truth

1. `CLAUDE.md`
2. `.claude/settings.json`
3. `.claude/settings.local.json`
4. `.claude/hooks/*`

If these conflict with generic defaults, follow these files for this repo.

## Required Runtime Model

- Treat this project as cloud-first production software.
- Production environment is on the VPS and runs through Docker Compose.
- Do not run Aurel2 production processes directly on the host.
- For production compose actions, run from `/opt/aurel2/docker/` on the server.

## Mandatory Operating Constraints

- Read `ARCHITECTURE.md` before major architecture or deployment changes.
- Default troubleshooting target is cloud deployment, not localhost.
- Broker is Alpaca Markets (REST API, no gateway process).
- Never commit secrets such as `.env` credentials.

## Claude Hook Equivalents In Codex

- Claude `SessionStart` hook (`git pull --ff-only`) is mirrored by:
  - `bash scripts/claude-session-start.sh`
- Claude `PostToolUse` backtest deploy hook is mirrored by:
  - After successful backtest commands that update `data/backtest_comparison.json`, run `bash scripts/deploy.sh`

## Workflow Notes

- Keep this file and `CLAUDE.md` aligned when deployment or operations rules change.
- If behavior differs across tools, prefer the stricter production-safe option.
