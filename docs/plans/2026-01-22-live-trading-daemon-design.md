# Live Trading Daemon Design

**Date:** 2026-01-22

## Overview

Implement a live trading daemon that continuously monitors the market, executes trades via Interactive Brokers, and handles human approval workflow for non-routine decisions.

## Commands

```bash
aurel2 live --paper          # Run daemon connected to paper trading (port 7497)
aurel2 live --real           # Run daemon connected to live trading (port 7496)
aurel2 check --paper         # Manual one-shot check (for testing)
aurel2 check --real          # Manual one-shot check (live)
```

Both `live` and `check` commands:
- Connect to IBKR on startup
- Sync positions from IBKR
- Run strategy analysis
- Execute or request approval based on decision type

The difference:
- `live` runs continuously, checking daily at 4 PM Romania time
- `check` runs once and exits (for testing)

## Decision Types & Behavior

| Decision Type | Condition | Action |
|---------------|-----------|--------|
| ROUTINE | All 3 strategies agree | Auto-execute immediately |
| NON_ROUTINE | Strategies disagree | Notify, wait 1h, then execute |
| URGENT | Drawdown >15% or extreme volatility | Notify, wait 1h, then execute |

## Schedule

- **Daily check:** 4 PM Romania time (Europe/Bucharest)
  - 30 minutes after US market open (9:30 AM ET = 3:30 PM Romania)
  - Allows opening volatility to settle
- **Approval polling:** Every 5 minutes
- **Connection heartbeat:** Every 10 minutes

## Daily Check Flow

```
1. CONNECT
   └─ Connect to IBKR (retry 3x if fails)
   └─ Sync current positions from IBKR
   └─ Sync account balance

2. ANALYZE
   └─ Fetch latest prices (Yahoo Finance)
   └─ Run all 3 strategies
   └─ Orchestrator produces decision (ROUTINE/NON_ROUTINE/URGENT)
   └─ AI advisor reviews (optional override)

3. ACT
   ├─ ROUTINE: Execute immediately via IBKR
   ├─ NON_ROUTINE: Send notification, wait up to 1h, then execute
   └─ URGENT: Send notification, wait up to 1h, then execute

4. NOTIFY
   └─ Send result via ntfy (executed, pending approval, or error)
```

## Daemon Loop

```
while running:
    wait until 4 PM Romania time (or next day if past)
    run daily check

    while waiting for next check:
        poll for pending approvals every 5 minutes
        execute any approved decisions immediately
        execute any timed-out decisions (1h)
        send heartbeat to IBKR every 10 minutes
```

## Pending Decisions & Approval Polling

```
1. CREATE
   └─ Generate unique decision ID
   └─ POST to Vercel approval endpoint (stores in KV)
   └─ Send ntfy notification with approve/reject link
   └─ Store locally: {id, created_at, decision_data, status: "pending"}

2. POLL (every 5 minutes)
   └─ For each pending decision:
       ├─ GET from Vercel endpoint - check if approved/rejected
       ├─ If approved: execute immediately
       ├─ If rejected: cancel, notify, remove from pending
       └─ If timeout (1 hour): execute, remove from pending

3. PERSIST
   └─ Pending decisions saved to data/pending_decisions.json
   └─ Survives daemon restart
```

## IBKR Connection Management

```
STARTUP
  └─ Connect to IBKR (paper: 7497, live: 7496)
  └─ If fails: check if TWS is running
      └─ If not running: launch TWS, wait for user to log in
      └─ Retry connection every 10 seconds until success or user quits
  └─ If still fails after TWS launched: exit with error

DAILY CHECK
  └─ Verify connection before each check
  └─ If disconnected: reconnect
  └─ If reconnect fails: skip check, notify you, try again next cycle

EXECUTION
  └─ Place order via IBKR
  └─ Wait up to 30 seconds for fill
  └─ If not filled: log warning, keep order open (IBKR handles it)
  └─ Notify result either way

IDLE
  └─ Keep connection alive (IBKR disconnects after ~30 min idle)
  └─ Send heartbeat every 10 minutes
```

## Position Sync

On startup and before each check:
- Query IBKR for actual positions
- Use IBKR as source of truth (handles manual trades made outside Aurel2)
- Update local portfolio state to match

## File Structure

```
src/aurel2/
├── live/
│   ├── __init__.py
│   ├── daemon.py          # Main daemon loop, scheduling
│   ├── checker.py         # Single check logic (shared by daemon & manual)
│   ├── executor.py        # Executes trades via IBKR
│   ├── pending.py         # Manages pending decisions & polling
│   └── connection.py      # IBKR connection management, TWS launcher
│
├── cli.py                 # Add 'live' and 'check' commands

data/
├── pending_decisions.json # Persisted pending approvals
```

### Module Responsibilities

- **daemon.py** - Main loop, scheduling checks at 4 PM, polling approvals
- **checker.py** - Core "run one check" logic, used by both daemon and manual command
- **executor.py** - Translates decisions into IBKR orders, handles fills
- **pending.py** - Tracks pending approvals, handles timeouts, persists to JSON
- **connection.py** - Connect/reconnect/launch TWS, heartbeat logic

## Configuration

Configurable via CLI flags or config file:

| Setting | Default | Description |
|---------|---------|-------------|
| `check_time` | 16:00 | Daily check time (Romania timezone) |
| `poll_interval` | 5 min | How often to poll for approvals |
| `heartbeat_interval` | 10 min | IBKR keepalive interval |
| `approval_timeout` | 1 hour | Auto-execute after this time |
| `ntfy_topic` | aurel2 | Notification topic |

## Error Handling

| Error | Handling |
|-------|----------|
| IBKR not connected | Launch TWS, wait for login, retry |
| IBKR connection lost mid-check | Reconnect, retry check |
| Order rejected | Log error, notify user, don't retry |
| Yahoo Finance unavailable | Retry 3x, then skip check, notify |
| Vercel endpoint unavailable | Use local pending state, retry later |

## Notifications

All notifications via ntfy.sh:

| Event | Priority | Tags |
|-------|----------|------|
| ROUTINE executed | Low | ✅ chart |
| NON_ROUTINE pending | Default | ❓ chart |
| URGENT pending | High | ⚠️ chart |
| Approval timeout, executing | Default | ⏰ chart |
| Error (connection, order) | High | ❌ |

## Future Enhancements (Not in Scope)

- IBC integration for fully automated TWS login
- Multiple broker support (execute via TradeVille when available)
- Web dashboard for monitoring daemon status
- Telegram notifications as alternative to ntfy
