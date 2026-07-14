# Codex + Claude Compatibility Rules

This repository is configured to keep Codex behavior aligned with Claude Code behavior.

## Source Of Truth

1. `CLAUDE.md`
2. `.claude/settings.json`
3. `.claude/settings.local.json`
4. `.claude/hooks/*`

If these conflict with generic defaults, follow these files for this repo.

## Required Runtime Model

- Treat this project as production-first trading software.
- Production environment is on Dumbo and runs through Docker/Colima.
- The former VPS layout is preserved on Dumbo for compatibility:
  `/root` is a symlink to `/Users/claudiu/vps-root`, so `/root/aurel2`
  and `/Users/claudiu/vps-root/aurel2` are the same Dumbo checkout.
- Do not use the deprecated VPS (`46.225.75.110`) for active Aurel2 work unless
  explicitly asked to inspect old state.
- Do not run Aurel2 production processes directly on the host.
- For production Docker actions, target the active Dumbo containers. The live
  runner bind-mounts `/root/aurel2/config` and `/root/aurel2/src`; `/opt/aurel2`
  remains the synced deployment copy and holds runtime files such as
  `/opt/aurel2/docker/.env`.

## Mandatory Operating Constraints

- Read `ARCHITECTURE.md` before major architecture or deployment changes.
- Default troubleshooting target is the Dumbo deployment, not localhost or the deprecated VPS.
- Broker is Alpaca Markets (REST API, no gateway process).
- Never commit secrets such as `.env` credentials.
- Do not schedule Claw/OpenClaw to run autonomous strategy research, backtests,
  optimization, experiments, or improvement loops.

## Claude Hook Equivalents In Codex

- Claude `SessionStart` hook (`git pull --ff-only`) is mirrored by:
  - `bash scripts/claude-session-start.sh`
- Claude `PostToolUse` backtest deploy hook is mirrored by:
  - After successful backtest commands that update `data/backtest_comparison.json`, run `bash scripts/deploy.sh`

## Git Sync — Never Diverge

This repo is worked on from two machines (local Mac + Dumbo), both by AI agents.
Divergence happens when one machine commits without the other pulling. Prevent it:

- Pull before starting: `git pull --rebase --autostash` on the current branch.
- Push immediately after every commit. If the push is rejected because the remote
  moved, `git pull --rebase` then push again. Never leave a commit local.
- Never end a turn with an unpushed commit or a diverged branch. If you cannot
  reconcile, state that explicitly in your report.

## Workflow Notes

- Keep this file and `CLAUDE.md` aligned when deployment or operations rules change.
- If behavior differs across tools, prefer the stricter production-safe option.
