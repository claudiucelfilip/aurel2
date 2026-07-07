# Aurel2 - Claude Code Instructions

## Quick Reference

**Read ARCHITECTURE.md first** for complete system documentation.

## Critical Context

### This is a DUMBO-DEPLOYED trading system

- **Production runs on Dumbo** (Claudiu's Intel Touch Bar MacBook), NOT on this control machine
- Services run as **Docker containers** via Docker/Colima
- Local execution is for **development/testing only**
- **NEVER run aurel2 processes directly on the host** — only via Docker containers. No `python -m aurel2.cli ...` outside of Docker. All instances must be managed through `docker compose`.
- The old VPS path layout is preserved on Dumbo for compatibility: `/root` is a symlink to `/Users/claudiu/vps-root`, so `/root/aurel2` and `/Users/claudiu/vps-root/aurel2` are the same Dumbo checkout.
- The Hetzner VPS (`46.225.75.110`) is deprecated. Do not use it for active Aurel2 work unless Claudiu explicitly asks to inspect old state.

### Deployment Location

```
Host: Dumbo
SSH: ssh 100.122.64.94
Repo path: /root/aurel2
Real repo path: /Users/claudiu/vps-root/aurel2
Synced deployment copy: /opt/aurel2
Runtime env: /opt/aurel2/docker/.env
Live runner mounts: /root/aurel2/config and /root/aurel2/src
Logs: docker logs --tail 200 aurel2-live-runner
Dashboard: http://127.0.0.1:8080 on Dumbo, public route https://aurel2.clawdiu.org/
```

**IMPORTANT: Always check Dumbo first, not the deprecated VPS and not this control machine!**

**CRITICAL: Run production Docker commands only against the active Dumbo deployment.** Do not recreate containers from a checkout that lacks `/opt/aurel2/docker/.env`.

### When User Says "Services Are Down"

1. **Check if they mean Dumbo or the control machine** - production is Dumbo
2. **For production issues**: SSH to Dumbo and inspect Docker/Colima containers
3. **For local testing only**: Check processes with `ps aux | grep aurel2`

### Weekly Strategy Research

Weekly strategy experiments must not run directly in `/root/aurel2`.

Use the disposable workspace helper:

```bash
python3 /root/aurel2/scripts/weekly_strategy_workspace.py create --repo /root/aurel2
python3 /root/aurel2/scripts/weekly_strategy_workspace.py run --worktree <worktree> -- python3 scripts/<backtest>.py
python3 /root/aurel2/scripts/weekly_strategy_workspace.py finalize --worktree <worktree> --cleanup
```

Backtest failures inside the disposable workspace are research outcomes to report. They should not dirty the protected checkout or make the weekly cron stop without a useful summary.

## Key Files by Task

### Debugging Connection Issues
- `src/aurel2/live/connection.py` - Alpaca connection management
- `src/aurel2/broker/alpaca.py` - Alpaca broker implementation
- `src/aurel2/live/circuit_breaker.py` - Failure protection

### Modifying Trading Logic
- `src/aurel2/agent/orchestrator.py` - Central decision engine
- `src/aurel2/strategies/dual_momentum.py` - Primary strategy
- `src/aurel2/strategies/mean_reversion.py` - RSI-based strategy
- `src/aurel2/strategies/multi_timeframe.py` - Multi-timeframe momentum

### Fixing Daemon Issues
- `src/aurel2/live/daemon.py` - Main trading loop
- `src/aurel2/live/checker.py` - Single check cycle
- `src/aurel2/monitor/daemon_monitor.py` - Watchdog with auto-recovery

### Approval System
- `src/aurel2/live/pending.py` - Pending decisions manager
- `approval-endpoint/api/decision/[id].ts` - Vercel serverless endpoint

### Notifications
- `src/aurel2/notifications/ntfy.py` - Push notifications

## Common Operations

### Restart Production Services On Dumbo
```bash
ssh 100.122.64.94
docker ps --format 'table {{.Names}}\t{{.Status}}'
docker restart aurel2-live-runner
```

### View Production Logs On Dumbo
```bash
ssh 100.122.64.94
docker logs --tail 200 aurel2-live-runner
```

### Check Production Health
```bash
ssh 100.122.64.94
docker exec aurel2-live-runner cat /root/.aurel2/heartbeat.json
```

### Deploy Code Changes
```bash
# Since the agent runs on Dumbo, deploy locally there (no VPS SSH needed):
./scripts/deploy.sh
# This syncs to /opt/aurel2 and rebuilds the active Aurel2 + dashboard containers
# NEVER recreate production containers from a checkout that lacks /opt/aurel2/docker/.env
```

### Publish Backtests Without Restarting The Daemon (Safe During An Active Paper Run)
```bash
# Regenerates data/backtest_comparison.json and publishes it to /opt/aurel2/data/
# without syncing code/config or restarting any containers.
bash scripts/publish_backtests_only.sh
```

### Local Testing
```bash
# Requires APCA_API_KEY_ID and APCA_API_SECRET_KEY env vars
python -m aurel2.cli live --paper
python -m aurel2.cli monitor --paper
python -m aurel2.cli dashboard
```

## Architecture Summary

```
┌─────────────────────────────────────────┐
│              DUMBO (Docker)             │
│                                         │
│  Alpaca API ◄─── Aurel2 Daemon          │
│  (REST)          (live --paper)         │
│                       │                 │
│                       ▼                 │
│              Monitor (watchdog)         │
│              Auto-recovery enabled      │
└─────────────────────────────────────────┘
         │                    │
         ▼                    ▼
    ntfy.sh              Vercel
    (alerts)          (approvals)
```

## Data Files

Data is partitioned by trading mode (`paper`/`live`):

| File | Purpose |
|------|---------|
| `data/{mode}/trade_journal.json` | Audit trail of all trades |
| `data/{mode}/pending_decisions.json` | Decisions awaiting approval |
| `data/{mode}/session_progress.json` | Session tracking |
| `data/failure_learnings.json` | Historical failures for AI (shared) |
| `data/backtest_comparison.json` | Backtest results (shared) |
| `~/.aurel2/heartbeat.json` | Daemon health status only |
| `data/archive/` | Archived data from resets |

Important: do *not* expect a per-mode runtime directory like `~/.aurel2/paper/`.
Paper/live journals and pending approvals live under `data/{mode}/...`; `~/.aurel2`
is only for runtime state such as heartbeat/log files.

## Decision Flow

1. **ROUTINE** (all strategies agree) → Auto-execute
2. **NON_ROUTINE** (disagreement) → Requires approval via Vercel
3. **URGENT** (extreme conditions) → 1-hour timeout then auto-execute

## Monitoring & Recovery

The watchdog (`aurel2 monitor`) automatically:
- Restarts daemon after 5 consecutive disconnected checks
- Restarts daemon if process dies
- Rate-limits restarts to 3 per hour
- Escalates via ntfy if can't auto-fix

## Claude Code CLI Authentication in Docker

The AI evaluator uses Claude Code CLI (`claude -p "prompt"`). To authenticate:

1. **SSH into the container**:
   ```bash
   ssh 100.122.64.94 -t "docker exec -it aurel2-trading-aurel2-1 bash"
   ```

2. **Run `claude` and complete browser login** (follow the URL it gives you)

3. **Credentials are stored in** `/root/.claude/.credentials.json` inside the container's mounted Claude config

**Important notes:**
- The `~/.claude` volume must be **writable** (not `:ro`) for CLI to work
- `setup-token` command doesn't work - it lacks `user:profile` scope. Use full `claude login` instead
- If CLI hangs, check for stale files: `rm /root/.claude.json*`
- The `CLAUDE_CODE_OAUTH_TOKEN` env var will override file credentials (avoid using it)

### AI Evaluator Guardrails

The advisor (`src/aurel2/agent/advisor.py`) has built-in guardrails:
- After 3 consecutive AI failures, AI evaluation is skipped for 1 hour
- System continues operating on deterministic strategies without AI

## Git Sync — never diverge

This repo is worked on from two machines (local Mac + Dumbo), both by AI agents. Divergence happens when one machine commits without the other pulling. To prevent it:

- **Pull before you start.** `git pull --rebase --autostash` on the current branch. The session-start hook does this; if it warned about divergence, reconcile before doing new work.
- **Push immediately after every commit** — do not leave a commit local. If the push is rejected (remote moved), `git pull --rebase` then push again.
- **Never end a turn with an unpushed commit or a diverged branch.** If you can't reconcile, say so explicitly in your report.

## Don't Forget

- **Broker is Alpaca Markets** — REST API, no gateway process needed
- **Credentials**: `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY` env vars
- **Check `docker/DEPLOY.md`** for full deployment guide
- **Never commit `.env`** - contains Alpaca credentials
- **Claude CLI needs writable `~/.claude` mount** in Docker
