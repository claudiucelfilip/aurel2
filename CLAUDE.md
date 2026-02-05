# Aurel2 - Claude Code Instructions

## Quick Reference

**Read ARCHITECTURE.md first** for complete system documentation.

## Critical Context

### This is a CLOUD-DEPLOYED trading system

- **Production runs on a VPS** (Hetzner CX22), NOT locally
- Services run as **Docker containers** via `docker-compose`
- Local execution is for **development/testing only**

### Deployment Location

```
Server IP: 46.225.75.110 (Hetzner CX22)
SSH: ssh root@46.225.75.110
Path: /opt/aurel2/
Config: /opt/aurel2/docker/.env
Logs: docker compose logs -f aurel2
Dashboard: http://46.225.75.110:8080
```

**IMPORTANT: Always check cloud first, not localhost!**

### When User Says "Services Are Down"

1. **Check if they mean cloud or local** - production is cloud
2. **For cloud issues**: SSH to server, check `docker compose ps`
3. **For local testing**: Check processes with `ps aux | grep aurel2`

## Key Files by Task

### Debugging Connection Issues
- `src/aurel2/live/connection.py` - IBKR connection management
- `src/aurel2/broker/ibkr.py` - Broker implementation, error handling (`ClientIdConflictError`)
- `src/aurel2/live/circuit_breaker.py` - Failure protection
- **Client ID conflicts** (error 326) are handled automatically — the connection
  manager picks a new random ID and retries. No manual restart needed.

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

### Restart Cloud Services
```bash
ssh root@SERVER_IP
cd /opt/aurel2/docker
docker compose restart aurel2
```

### View Cloud Logs
```bash
ssh root@SERVER_IP
cd /opt/aurel2/docker
docker compose logs -f aurel2
```

### Check Cloud Health
```bash
ssh root@SERVER_IP
docker compose exec aurel2 cat /root/.aurel2/heartbeat.json
```

### Deploy Code Changes
```bash
# From local machine
rsync -avz --exclude='.git' --exclude='data/' . root@SERVER:/opt/aurel2/
ssh root@SERVER "cd /opt/aurel2/docker && docker compose build aurel2 && docker compose up -d aurel2"
```

### Local Testing
```bash
# Requires IB Gateway/TWS on localhost:4002
python -m aurel2.cli live --paper
python -m aurel2.cli monitor --paper
python -m aurel2.cli dashboard
```

## Architecture Summary

```
┌─────────────────────────────────────────┐
│           CLOUD VPS (Docker)            │
│                                         │
│  IB Gateway ◄─── Aurel2 Daemon          │
│  (headless)      (live --paper)         │
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

| File | Purpose |
|------|---------|
| `data/trade_journal.json` | Audit trail of all trades |
| `data/pending_decisions.json` | Decisions awaiting approval |
| `data/failure_learnings.json` | Historical failures for AI |
| `/tmp/aurel2-heartbeat.json` | Daemon health status |

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
   ssh root@SERVER_IP -t "docker exec -it aurel2-trading-aurel2-1 bash"
   ```

2. **Run `claude` and complete browser login** (follow the URL it gives you)

3. **Credentials are stored in** `/root/.claude/.credentials.json` (mounted from host at `/opt/aurel2/docker/claude-config/`)

**Important notes:**
- The `~/.claude` volume must be **writable** (not `:ro`) for CLI to work
- `setup-token` command doesn't work - it lacks `user:profile` scope. Use full `claude login` instead
- If CLI hangs, check for stale files: `rm /root/.claude.json*`
- The `CLAUDE_CODE_OAUTH_TOKEN` env var will override file credentials (avoid using it)

### AI Evaluator Guardrails

The advisor (`src/aurel2/agent/advisor.py`) has built-in guardrails:
- After 3 consecutive AI failures, AI evaluation is skipped for 1 hour
- System continues operating on deterministic strategies without AI

## Don't Forget

- **IBKR_HOST in Docker is `ib-gateway`**, not `127.0.0.1`
- **Paper trading port is 4004** (not 4002 like local)
- **Check `docker/DEPLOY.md`** for full deployment guide
- **Never commit `.env`** - contains IBKR credentials
- **Claude CLI needs writable `~/.claude` mount** in Docker
