# Aurel2 Architecture Documentation

Tax-optimized dual momentum trading system with AI-assisted decision making.

## Table of Contents

1. [Project Overview](#project-overview)
2. [Deployment Architecture](#deployment-architecture)
3. [Directory Structure](#directory-structure)
4. [Core Concepts](#core-concepts)
5. [Trading Strategies](#trading-strategies)
6. [AI/Agent System](#aiagent-system)
7. [Live Trading Components](#live-trading-components)
8. [Monitoring System](#monitoring-system)
9. [Data Flow](#data-flow)
10. [Configuration](#configuration)
11. [External Services](#external-services)
12. [Data Files](#data-files)
13. [Testing](#testing)
14. [CLI Commands](#cli-commands)
15. [Technology Stack](#technology-stack)

---

## Project Overview

**Aurel2** is an automated trading system that implements momentum-based strategies with AI oversight. The system:

- Uses **proven momentum anomaly** (200+ years of data) rather than ML price prediction
- Employs **multi-strategy voting** for robust decisions
- Includes **conservative AI advisor** to guard against known failure patterns
- Requires **human approval** for non-routine decisions
- Optimizes for **Romanian tax efficiency** (UCITS ETFs, quarterly rebalancing)

### Key Principles

1. **Momentum-first**: Core strategy based on 12-month relative momentum
2. **Multi-strategy consensus**: 3 strategies vote on decisions
3. **Conservative AI**: AI guards against failures, doesn't replace system
4. **Human-in-the-loop**: Key decisions require approval
5. **Tax optimized**: Quarterly rebalancing, Irish-domiciled UCITS ETFs
6. **Audit trail**: Every decision logged for analysis

---

## Deployment Architecture

**IMPORTANT: This system runs in the CLOUD, not locally.**

### Production Setup

```
┌─────────────────────────────────────────────────────────────────┐
│                CLOUD VPS (Hetzner CX22 ~€5/month)               │
│                     Ubuntu 24.04 + Docker                        │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │              Docker Compose Network                       │  │
│  │                                                           │  │
│  │  ┌─────────────────┐      ┌──────────────────────────┐  │  │
│  │  │  IB Gateway     │      │  Aurel2 Daemon           │  │  │
│  │  │  (Headless)     │◄─────┤  (live --paper)          │  │  │
│  │  │                 │      │                          │  │  │
│  │  │ Port 4004       │      │ Daily check at 16:00     │  │  │
│  │  │ (paper trading) │      │ Romania time             │  │  │
│  │  └─────────────────┘      └──────────────────────────┘  │  │
│  │                                                           │  │
│  │  ┌─────────────────┐      ┌──────────────────────────┐  │  │
│  │  │  Dashboard      │      │  Monitor (watchdog)      │  │  │
│  │  │  Port 8080      │      │  Auto-restarts daemon    │  │  │
│  │  │  (optional)     │      │  (optional)              │  │  │
│  │  └─────────────────┘      └──────────────────────────┘  │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
         │                              │
         │ ntfy.sh                      │ Vercel
         ▼                              ▼
    Push notifications           Approval endpoint
    (mobile alerts)              (mobile-friendly UI)
```

### Docker Services (`docker/docker-compose.yml`)

| Service | Image | Purpose | Ports |
|---------|-------|---------|-------|
| `ib-gateway` | `ghcr.io/gnzsnz/ib-gateway:stable` | Headless IBKR connection | 4003/4004 (internal) |
| `aurel2` | Built from Dockerfile | Trading daemon | - |
| `dashboard` | Built from Dockerfile | Web UI (optional) | 8080 |

### Key Environment Variables (`.env`)

```bash
# IBKR Credentials (REQUIRED)
TWS_USERID=your_username
TWS_PASSWORD=your_password

# Trading Mode
TRADING_MODE=paper          # paper or live
IBKR_PORT=4004              # 4004=paper, 4003=live

# Daemon Settings
CHECK_TIME=16:00            # Daily check time (Romania timezone)
POLL_INTERVAL=5             # Minutes between approval polls
NTFY_TOPIC=aurel2           # Notification channel

# AI Settings
AI_MODEL=sonnet             # sonnet, opus, or haiku
AI_LOOKBACK_YEARS=3
```

### Deployment Commands

```bash
# Deploy to server
rsync -avz --exclude='.git' --exclude='data/' \
  . root@YOUR_SERVER:/opt/aurel2/

# Start services
ssh root@YOUR_SERVER
cd /opt/aurel2/docker
docker compose up -d

# View logs
docker compose logs -f aurel2

# Check health
docker compose exec aurel2 cat /root/.aurel2/heartbeat.json

# Restart after code changes
docker compose build aurel2 && docker compose up -d aurel2
```

### Local Development (Testing Only)

For local testing, you can run directly:
```bash
# Requires IB Gateway/TWS running locally on port 4002
python -m aurel2.cli live --paper
python -m aurel2.cli monitor --paper
python -m aurel2.cli dashboard
```

See `docker/DEPLOY.md` for complete deployment guide.

---

## Directory Structure

```
aurel2/
├── src/aurel2/              # Main application source
│   ├── agent/               # AI agent system
│   │   ├── orchestrator.py  # Central decision engine
│   │   ├── advisor.py       # AI risk advisor
│   │   ├── failure_analyzer.py  # Backtest failure analysis
│   │   └── evaluators.py    # Strategy evaluators
│   ├── broker/              # Broker integrations
│   │   ├── ibkr.py          # Interactive Brokers
│   │   └── tradeville.py    # TradeVille (future)
│   ├── core/                # Domain models
│   │   ├── models.py        # Signal, Position, Portfolio
│   │   └── assets.py        # Asset registry
│   ├── config/              # Configuration
│   │   └── settings.py      # Pydantic settings
│   ├── dashboard/           # Web dashboard (FastAPI + HTMX)
│   ├── data/                # Data providers
│   │   ├── providers/
│   │   │   └── yahoo.py     # Yahoo Finance integration
│   │   └── indicators.py    # Technical indicators
│   ├── engine/              # Analysis engines
│   │   ├── backtest.py      # Backtesting engine
│   │   └── backtest_agent.py # Agent backtesting
│   ├── live/                # Live trading
│   │   ├── daemon.py        # Main trading loop
│   │   ├── checker.py       # Single check cycle
│   │   ├── executor.py      # Order execution
│   │   ├── connection.py    # IBKR connection
│   │   ├── pending.py       # Pending approvals
│   │   └── circuit_breaker.py
│   ├── monitor/             # Health monitoring
│   │   ├── daemon_monitor.py # Daemon watcher
│   │   ├── health_checker.py # Health assessment
│   │   ├── error_analyzer.py # Error classification
│   │   ├── auto_fixer.py    # Auto-remediation
│   │   ├── incident_tracker.py
│   │   ├── session_tracker.py
│   │   └── ai_analyzer.py   # AI-powered analysis
│   ├── mcp/                 # Model Context Protocol server
│   ├── notifications/       # Notification service
│   │   └── ntfy.py          # ntfy.sh integration
│   ├── persistence/         # Portfolio persistence
│   ├── strategies/          # Trading strategies
│   │   ├── base.py          # Strategy interface
│   │   ├── dual_momentum.py # Primary strategy
│   │   ├── mean_reversion.py
│   │   ├── multi_timeframe.py
│   │   └── adaptive_momentum.py
│   ├── utils/               # Utilities
│   │   └── logging.py       # Structured logging
│   └── cli.py               # CLI entry point
├── config/                  # YAML configuration
│   └── default.yaml
├── data/                    # Persistent data files
│   ├── trade_journal.json   # Audit trail
│   ├── pending_decisions.json
│   ├── failure_learnings.json
│   ├── session_progress.json
│   ├── backtest_results.json
│   └── ai_eval_cache/       # AI evaluation cache
├── tests/                   # pytest test suite
├── docker/                  # Docker deployment
│   ├── Dockerfile
│   ├── docker-compose.yml
│   └── DEPLOY.md
├── approval-endpoint/       # Vercel serverless (Node.js)
│   └── api/decision/[id].ts
├── scripts/                 # Setup scripts
└── docs/plans/              # Planning documents
```

---

## Core Concepts

### Domain Models (`src/aurel2/core/models.py`)

```python
# Signal from a strategy
Signal:
    action: SignalAction  # BUY, SELL, HOLD
    asset: Asset
    confidence: float     # 0.0 to 1.0
    reasoning: str
    metadata: dict

# Current position
Position:
    asset: Asset
    shares: float
    entry_price: float
    entry_date: date

# Portfolio state
Portfolio:
    positions: list[Position]
    cash: float
    total_value: float
```

### Asset Registry (`src/aurel2/core/assets.py`)

11 tradeable assets organized by category:

| Category | US ETF | UCITS Equivalent | Description |
|----------|--------|------------------|-------------|
| Core Equity | SPY | CSPX (IE00B5BMR087) | S&P 500 |
| Core Equity | EFA | VWRA (IE00BK5BQT80) | International Developed |
| Core Equity | EEM | EIMI (IE00BKM4GZ66) | Emerging Markets |
| Sectors | XLK | - | Technology |
| Sectors | XLF | - | Financial |
| Sectors | XLE | - | Energy |
| Sectors | XLV | - | Healthcare |
| Fixed Income | AGG | AGGH (IE00BDBRDM35) | Aggregate Bonds |
| Fixed Income | TLT | - | Long-term Treasury |
| Alternatives | GLD | - | Gold |
| Alternatives | DBC | - | Commodities |

UCITS ETFs are Irish-domiciled and accumulating for Romanian tax optimization (15% vs 30% withholding).

### Signal Actions

```python
class SignalAction(Enum):
    BUY = "buy"      # Enter position
    SELL = "sell"    # Exit position
    HOLD = "hold"    # Maintain current
```

### Decision Types

```python
class DecisionType(Enum):
    ROUTINE = "routine"         # All strategies agree → auto-execute
    NON_ROUTINE = "non_routine" # Strategies disagree → needs approval
    URGENT = "urgent"           # Extreme conditions → short timeout
```

### Market Regimes

```python
class MarketRegime(Enum):
    BULL = "bull"           # Uptrend
    BEAR = "bear"           # Downtrend
    SIDEWAYS = "sideways"   # Range-bound
    VOLATILE = "volatile"   # High volatility
```

---

## Trading Strategies

### 1. Dual Momentum (Primary)

**File**: `src/aurel2/strategies/dual_momentum.py`

The core strategy using 12-month relative momentum with absolute momentum filter.

**Parameters**:
| Parameter | Default | Description |
|-----------|---------|-------------|
| `lookback_months` | 12 | Momentum calculation period |
| `switch_threshold` | 0.10 | Only switch if winner beats current by >10% |
| `cash_rate` | 0.04 | Baseline for absolute momentum |

**Logic**:
1. Calculate 12-month return for each asset
2. **Relative momentum**: Select asset with highest return
3. **Absolute momentum**: Only buy if return > cash_rate
4. **Switch threshold**: Avoid excessive trading

**Pilot Entry System**:
- 30% position when 3-month momentum shows inflection
- Full position when 12-month momentum confirms

**Signals**:
- `BUY`: When highest momentum asset beats current by >10%
- `SELL`: When current holding has negative momentum
- `HOLD`: Otherwise

### 2. Mean Reversion

**File**: `src/aurel2/strategies/mean_reversion.py`

RSI-based strategy for catching oversold bounces.

**Parameters**:
| Parameter | Default | Description |
|-----------|---------|-------------|
| `rsi_period` | 14 | RSI calculation period |
| `rsi_oversold` | 25 | Buy signal threshold |
| `rsi_overbought` | 75 | Sell signal threshold |
| `rsi_extreme_oversold` | 20 | High-confidence buy |
| `drawdown_threshold` | 0.10 | Additional buy trigger |

**Logic**:
- `BUY`: RSI < 25 (high confidence if < 20)
- `SELL`: RSI > 75
- `HOLD`: Otherwise

**Purpose**: Catches reversal opportunities that momentum misses during panic selling.

### 3. Multi-Timeframe Trend

**File**: `src/aurel2/strategies/multi_timeframe.py`

Blends momentum from multiple timeframes for faster reaction.

**Parameters**:
| Parameter | Default | Description |
|-----------|---------|-------------|
| `lookback_months` | [1, 3, 6, 12] | Multiple timeframes |
| `weights` | [0.30, 0.30, 0.25, 0.15] | Timeframe weights |
| `switch_threshold` | 0.03 | Lower than dual momentum |

**Logic**:
1. Calculate weighted momentum score across timeframes
2. Higher weight on short-term (30% each for 1 & 3 month)
3. Lower switch threshold for faster adaptation

**Confidence**: Based on agreement across timeframes.

### 4. Adaptive Momentum

**File**: `src/aurel2/strategies/adaptive_momentum.py`

Dynamically adjusts lookback period based on market conditions.

**Parameters**:
| Parameter | Default | Description |
|-----------|---------|-------------|
| `min_lookback` | 3 | Minimum lookback months |
| `max_lookback` | 12 | Maximum lookback months |
| `volatility_window` | 20 | Days for volatility calc |

**Logic**: Shorter lookback in high volatility, longer in low volatility.

---

## AI/Agent System

### Agent Orchestrator

**File**: `src/aurel2/agent/orchestrator.py`

Central decision-making engine that:
1. Collects signals from all 3 strategies
2. Classifies decisions (ROUTINE, NON_ROUTINE, URGENT)
3. Calculates confidence using weighted voting
4. Detects market regime
5. Produces final decision

**Decision Classification**:
```
ROUTINE:     All 3 strategies agree → auto-execute
NON_ROUTINE: Strategies disagree → requires approval
URGENT:      High drawdown (>15%) or extreme conditions
```

**Strategy Weighting**:
| Strategy | Base Weight |
|----------|-------------|
| Dual Momentum | 45% |
| Mean Reversion | 25% |
| Multi-Timeframe | 30% |

Weights adjust dynamically based on rolling accuracy (last 10 decisions).

**Output** (`AgentDecision`):
```python
{
    "decision_type": DecisionType,
    "action": SignalAction,
    "asset_symbol": str,
    "reasoning": str,
    "confidence": float,          # 0.0-1.0
    "strategy_signals": dict,     # Per-strategy signals
    "requires_approval": bool,
    "timeout_hours": float,
    "urgency": Urgency,           # LOW, MEDIUM, HIGH
    "position_size_pct": float,   # 0.0-1.0
    "regime": MarketRegime,
}
```

**Urgency Levels**:
- `LOW`: 24-48 hours to decide
- `MEDIUM`: 4-8 hours
- `HIGH`: 1-2 hours

### AI Advisor

**File**: `src/aurel2/agent/advisor.py`

Conservative "risk manager" that reviews decisions against historical failures.

**Responsibilities**:
1. Load failure patterns from `data/failure_learnings.json`
2. Review deterministic decisions against market context
3. Override only in rare, high-conviction cases
4. Prevent PBIX (past black swan) mistakes

**Failure Pattern Categories**:
| Type | Description |
|------|-------------|
| `missed_opportunity` | Held wrong asset when another was winning |
| `bad_hold` | Stayed in position that should have been sold |
| `bad_switch` | Switched too early/late |
| `late_entry` | Entered trend too late |
| `late_exit` | Exited too late |

**AI Integration**:
- Uses Claude Code CLI (not API) for cost efficiency
- Configurable model: "sonnet" (default), "opus", "haiku"
- Lookback: Last 3-5 years of failures (configurable)
- Agrees with momentum system 95%+ of the time

### Failure Analyzer

**File**: `src/aurel2/agent/failure_analyzer.py`

Analyzes backtest results to identify failure patterns:
1. Run backtest over historical period
2. Identify periods where strategy underperformed
3. Classify failure types
4. Store in `failure_learnings.json` for AI advisor

---

## Live Trading Components

### Connection Manager

**File**: `src/aurel2/live/connection.py`

Manages IBKR Gateway connection via `ib_insync`.

**Ports**:
| Mode | IB Gateway | TWS |
|------|------------|-----|
| Paper | 4002 | 7497 |
| Live | 4001 | 7496 |
| Docker | 4003/4004 | - |

**Features**:
- Auto-launch IB Gateway if not running
- Heartbeat to keep connection alive
- Circuit breaker for failure protection
- Automatic reconnection with exponential backoff
- **Client ID conflict auto-recovery**: If the IBKR client ID is already in use
  (error 326, e.g. stale connection after container restart), automatically picks
  a new random client ID and retries immediately without counting as a failure

### Circuit Breaker

**File**: `src/aurel2/live/circuit_breaker.py`

Prevents cascading failures.

**States**:
- `CLOSED`: Normal operation
- `OPEN`: Failures detected, blocking calls
- `HALF_OPEN`: Testing recovery

**Parameters**:
- Failure threshold: 3
- Reset timeout: 300 seconds (5 minutes)

### Executor

**File**: `src/aurel2/live/executor.py`

Translates decisions into IBKR orders.

**Actions**:
| Action | Implementation |
|--------|---------------|
| `BUY` | Purchase with available cash |
| `SELL` | Liquidate current position |
| `HOLD` | No action |
| `SWITCH` | Atomic sell + buy |

**Features**:
- Order validation before execution
- Connection and circuit breaker checks
- Position sizing (0.0-1.0 of capital)
- Execution details recorded for audit

### Pending Decisions Manager

**File**: `src/aurel2/live/pending.py`

Manages decisions awaiting human approval.

**Storage**: `data/pending_decisions.json`

**Status Flow**:
```
PENDING → APPROVED/REJECTED/TIMEOUT → EXECUTED
```

**Timeout**: 1 hour (both NON_ROUTINE and URGENT)

**Approval URL**: Vercel serverless endpoint for mobile approvals.

### Live Daemon

**File**: `src/aurel2/live/daemon.py`

Main continuous trading loop.

**Schedule**:
- Daily check at configured time (default 4 PM Romania)
- Polling every 5 minutes for pending approvals
- Heartbeat every 10 minutes to `/tmp/aurel2-heartbeat.json`

**Main Loop**:
1. Check if scheduled check time
2. Run Checker (strategies → orchestrator → advisor)
3. If ROUTINE → execute immediately
4. If NON_ROUTINE → create pending, notify user
5. Poll for pending decision responses
6. Update heartbeat file

**Signal Handling**: Graceful shutdown on SIGINT/SIGTERM

### Checker

**File**: `src/aurel2/live/checker.py`

Single market check cycle:
1. Sync positions from IBKR
2. Fetch market prices (Yahoo Finance)
3. Run all 3 strategies
4. Orchestrator produces decision
5. AI Advisor reviews decision
6. Execute or create pending approval
7. Log to trade journal
8. Send notifications

---

## Monitoring System

### Daemon Monitor

**File**: `src/aurel2/monitor/daemon_monitor.py`

Watches daemon health continuously.

**Check Interval**: 60 seconds

**Monitors**:
- Process status (running, CPU, memory)
- Heartbeat staleness (threshold: 10 minutes)
- Circuit breaker state
- Error logs
- Pending decisions count

**Actions**:
- Auto-fix fixable issues
- Escalate via ntfy for manual intervention
- Track restarts and error counts

### Health Checker

**File**: `src/aurel2/monitor/health_checker.py`

**Status Levels**:
| Status | Meaning |
|--------|---------|
| HEALTHY | All systems normal |
| DEGRADED | Some issues, still functional |
| UNHEALTHY | Critical issues |
| UNKNOWN | Cannot determine |

**Thresholds**:
- Stale heartbeat: >10 minutes
- Consecutive unhealthy: >3

### Error Analyzer

**File**: `src/aurel2/monitor/error_analyzer.py`

**Categories**:
- CONNECTION
- CIRCUIT_BREAKER
- PRICE_FETCH
- DAEMON_CRASH
- EXECUTION
- TIMEOUT

**Severity**: INFO, WARNING, ERROR, CRITICAL

### Auto-Fixer

**File**: `src/aurel2/monitor/auto_fixer.py`

Automatically fixes common issues:
- Connection failures → Reconnect to IBKR
- Circuit breaker open → Reset after timeout
- Daemon crash → Restart process

Supports dry-run mode for preview.

---

## Web Dashboard

**Files**: `src/aurel2/dashboard/app.py`, `src/aurel2/dashboard/templates/dashboard.html`

FastAPI application serving a real-time portfolio dashboard. Connects directly to IBKR for live data.

### Account Summary Cards

Two primary cards:
- **Total Value** — `NetLiquidation` from IBKR (includes unsettled). Shows cash breakdown only when cash != total.
- **Overall P&L** — Percentage and dollar gain/loss since first recorded account value.

### Performance Chart

Chart.js line chart with three datasets:
- **Portfolio** (blue, filled) — Actual trading performance
- **SPY** (gray, dashed) — Buy-and-hold benchmark
- **Position** (orange, dashed) — Buy-and-hold of current position

Period toggle: 1W, 1M, 6M, 1Y, 5Y.

### Activity Log

Paginated table (10 per page) showing all non-hold decisions. Columns: Date, Action, Asset, Status, AI, Account Value.

**Hold decisions are filtered out** to keep the list focused on actionable events.

#### Status Taxonomy

| Status | Badge Color | Meaning |
|--------|------------|---------|
| **Executed** | Green | Trade filled — has shares and fill price |
| **Finalized** | Blue | Decision processed (approved/timed-out) but no actual trade |
| **Failed** | Red | Execution attempted but errored (e.g., partial switch) |
| **Pending** | Yellow | Awaiting user approval |
| **Rejected** | Red | User rejected the decision |

Status is determined by cross-referencing `trade_journal.json` with `pending_decisions.json`:
- `execution_error` present → **Failed** (highest priority)
- `executed=true` AND `shares>0` → **Executed**
- `executed=true` OR pending status is "executed" → **Finalized**
- Linked pending decision exists → shows that pending status
- Otherwise → no status shown

#### AI Override Display

When `ai_agrees=false`, the AI column shows "Override" with the original deterministic action derived from strategy signal majority vote: e.g., "HOLD → BUY EFA".

### Pending Decisions

Shown as a separate block above the activity table (only when pending decisions exist). Each shows action badge, symbol, time remaining, confidence, and Approve/Reject buttons linking to the Vercel approval endpoint.

### Post-Trade State Recording

All three execution paths (`daemon._execute_approved`, `daemon._execute_timeout`, `checker._execute_decision`) capture post-trade state:
- `account_value_after` — from `connection.get_account_summary()`
- `current_holding_after` — from `executor.get_current_holding()`

These are passed to `journal.record_execution()` and persisted for audit/dashboard display.

---

## Data Flow

### Backtest Flow

```
┌─────────────────┐
│  Yahoo Finance  │
│  (Historical)   │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│    Strategies   │
│  (All 3 run)    │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│   Orchestrator  │
│ (Voting/Weight) │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Backtest Engine │
│ (Performance)   │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Failure Analyzer│
│ (Learnings)     │
└─────────────────┘
```

### Live Trading Flow

```
┌─────────────────┐      ┌─────────────────┐
│  Yahoo Finance  │      │      IBKR       │
│   (Prices)      │      │  (Positions)    │
└────────┬────────┘      └────────┬────────┘
         │                        │
         └───────────┬────────────┘
                     │
                     ▼
           ┌─────────────────┐
           │    Strategies   │
           └────────┬────────┘
                    │
                    ▼
           ┌─────────────────┐
           │   Orchestrator  │
           └────────┬────────┘
                    │
                    ▼
           ┌─────────────────┐
           │   AI Advisor    │
           └────────┬────────┘
                    │
         ┌──────────┴──────────┐
         │                     │
         ▼                     ▼
┌─────────────────┐   ┌─────────────────┐
│    ROUTINE      │   │  NON_ROUTINE    │
│  Auto-Execute   │   │ Pending Approval│
└────────┬────────┘   └────────┬────────┘
         │                     │
         │            ┌────────┴────────┐
         │            ▼                 ▼
         │   ┌──────────────┐   ┌──────────────┐
         │   │   Approved   │   │   Rejected   │
         │   └──────┬───────┘   └──────────────┘
         │          │
         └────┬─────┘
              │
              ▼
     ┌─────────────────┐
     │    Executor     │
     │  (IBKR Orders)  │
     └────────┬────────┘
              │
              ▼
     ┌─────────────────┐
     │  Trade Journal  │
     └─────────────────┘
```

---

## Configuration

### Main Config (`config/default.yaml`)

```yaml
strategy:
  name: dual_momentum
  lookback_months: 12
  rebalance_frequency: quarterly
  switch_threshold: 0.10

assets:
  us_stocks:
    symbol: SPY
    yahoo_symbol: SPY
  global_stocks:
    symbol: EFA
    yahoo_symbol: EFA
  bonds:
    symbol: AGG
    yahoo_symbol: AGG
  cash_rate: 0.04

broker:
  type: ibkr
  host: "127.0.0.1"
  port: 4002  # Paper: 4002, Live: 4001

risk:
  max_position_pct: 1.0
  transaction_cost_pct: 0.001

logging:
  level: INFO
```

### Key Parameters

**Strategy Parameters**:
| Parameter | Location | Default |
|-----------|----------|---------|
| lookback_months | strategy | 12 |
| switch_threshold | strategy | 0.10 |
| rebalance_frequency | strategy | quarterly |

**Risk Parameters**:
| Parameter | Location | Default |
|-----------|----------|---------|
| max_position_pct | risk | 1.0 |
| transaction_cost_pct | risk | 0.001 |

**Daemon Parameters**:
| Parameter | CLI Flag | Default |
|-----------|----------|---------|
| check_time | --check-time | 16:00 |
| poll_interval | --poll-interval | 300 |
| ai_model | --ai-model | sonnet |

---

## External Services

### Yahoo Finance

**File**: `src/aurel2/data/providers/yahoo.py`

- Fetches historical OHLC data
- Supports multi-symbol requests
- 400-day buffer for lookback calculations
- Returns DataFrame with columns: date, close, symbol

### Interactive Brokers (IBKR)

**File**: `src/aurel2/broker/ibkr.py`

- Uses `ib_insync` library
- Executes market and limit orders
- Retrieves positions, account summary, P&L
- Symbol mapping between US and UCITS ETFs

### ntfy.sh Notifications

**File**: `src/aurel2/notifications/ntfy.py`

- Push notifications via https://ntfy.sh
- Rate limiting: 1 per category per hour
- Priority levels: low, default, high, urgent
- Tags: emoji support

**Use Cases**:
- Daemon startup/shutdown
- Decisions made
- Approvals needed
- Errors and health alerts

### Vercel Approval Endpoint

**Directory**: `approval-endpoint/`

- Node.js/TypeScript serverless
- Vercel KV storage
- API: `GET/POST/PATCH /api/decision/[id]`
- Mobile-friendly HTML interface
- 7-day decision expiration

---

## Data Files

### trade_journal.json

Comprehensive audit trail of all trading activity.

```json
{
  "entries": [
    {
      "id": "unique_id",
      "timestamp": "2026-01-22T15:18:32Z",
      "entry_type": "decision|execution|approval|error",
      "action": "buy|sell|hold",
      "symbol": "SPY",
      "confidence": 0.85,
      "decision_type": "routine|non_routine|urgent",
      "strategy_signals": {
        "dual_momentum": {"action": "buy", "confidence": 0.8},
        "mean_reversion": {"action": "hold", "confidence": 0.5},
        "multi_timeframe": {"action": "buy", "confidence": 0.9}
      },
      "ai_agrees": true,
      "ai_action": "buy",
      "market_regime": "bull",
      "executed": true,
      "shares": 10,
      "fill_price": 450.25
    }
  ]
}
```

### pending_decisions.json

Active decisions awaiting approval.

```json
{
  "decisions": {
    "73055a33": {
      "id": "73055a33",
      "created_at": "2026-01-22T15:18:32",
      "urgency": "non_routine",
      "action": "buy",
      "symbol": "GLD",
      "confidence": 0.90,
      "status": "pending",
      "approval_url": "https://approval.vercel.app/api/decision/73055a33",
      "strategies": [
        {"name": "Dual Momentum", "action": "BUY", "confidence": 0.8}
      ],
      "market_regime": "bull"
    }
  }
}
```

### failure_learnings.json

Historical failure patterns from backtests.

```json
{
  "start_date": "2015-01-01",
  "end_date": "2024-12-31",
  "failure_events": [
    {
      "date": "2020-03-15",
      "failure_type": "late_exit",
      "asset_held": "SPY",
      "optimal_asset": "AGG",
      "actual_return": -12.5,
      "optimal_return": 3.2,
      "market_context": {
        "vix": 82.5,
        "drawdown": 0.35,
        "regime": "volatile_bear"
      }
    }
  ],
  "common_failure_types": {
    "late_exit": 28,
    "late_entry": 15
  }
}
```

### session_progress.json

Current trading session metrics.

```json
{
  "start_time": "2026-02-03T10:00:00Z",
  "decisions_made": 5,
  "decisions_executed": 3,
  "trades_won": 2,
  "trades_lost": 1,
  "win_rate": 0.67
}
```

### backtest_results.json

Historical backtest performance.

```json
{
  "start_date": "2015-01-01",
  "end_date": "2024-12-31",
  "initial_capital": 10000,
  "final_value": 45230,
  "cagr": 0.148,
  "max_drawdown": 0.235,
  "sharpe_ratio": 0.72
}
```

### ai_eval_cache/

Caches AI evaluations to avoid re-evaluation:
- `context/[DATE].json`: Decision context
- `decisions/[DATE_HASH].json`: AI evaluation result

---

## Testing

### Running Tests

```bash
# All tests
pytest

# With coverage
pytest --cov=src/aurel2

# Specific test file
pytest tests/test_agent.py

# Verbose output
pytest -v
```

### Test Files

| File | Coverage |
|------|----------|
| `test_models.py` | Core domain models |
| `test_assets.py` | Asset registry |
| `test_indicators.py` | Technical indicators |
| `test_strategy_base.py` | Strategy interface |
| `test_mean_reversion.py` | Mean reversion strategy |
| `test_multi_timeframe.py` | Multi-timeframe strategy |
| `test_agent.py` | Orchestrator and advisor |
| `test_backtest_agent.py` | Agent backtesting |
| `test_notifications.py` | Ntfy notifications |
| `test_mcp_server.py` | MCP server |
| `test_dashboard.py` | Web dashboard (status logic, pagination, filtering, AI override, integration) |
| `test_execution_recording.py` | Trade execution recording (journal, daemon, checker post-trade state) |

### Test Configuration

In `pyproject.toml`:
```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
```

---

## CLI Commands

```bash
# Backtesting
aurel2 backtest [--start DATE] [--end DATE] [--capital AMOUNT]

# Show momentum scores
aurel2 momentum [--date DATE]

# Start live daemon
aurel2 live [--paper] [--dry-run] [--ai-model sonnet]

# Single check cycle
aurel2 check [--dry-run]

# Run agent strategies
aurel2 agent [--lookback-years N]

# Evaluate agent performance
aurel2 eval_agent [--start DATE] [--end DATE]

# Get AI advice
aurel2 advise [--date DATE]

# Web dashboard
aurel2 dashboard

# Start monitor
aurel2 monitor [--ai-enabled]

# Show strategies
aurel2 strategies

# Compare strategies
aurel2 compare [--start DATE] [--end DATE]
```

---

## Technology Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.11+ |
| Config | Pydantic + YAML |
| Broker | ib_insync |
| Data | yfinance, pandas, numpy |
| Logging | structlog |
| CLI | Typer + Rich |
| Web | FastAPI + HTMX + Jinja2 |
| Notifications | ntfy.sh (HTTP) |
| Testing | pytest |
| Approval API | Node.js + Vercel KV |
| Deployment | Docker |

### Dependencies

**Core** (`pyproject.toml`):
```
yfinance>=0.2.36
pandas>=2.0.0
numpy>=1.24.0
pydantic>=2.0.0
pydantic-settings>=2.0.0
pyyaml>=6.0
structlog>=24.0.0
typer>=0.9.0
rich>=13.0.0
httpx>=0.27.0
psutil>=5.9.0
```

**Optional**:
- `broker`: ib_insync>=0.9.86
- `dashboard`: fastapi, uvicorn, jinja2
- `dev`: pytest, pytest-cov

---

## Key Files by Task

| Task | Key Files |
|------|-----------|
| Understand decision logic | `agent/orchestrator.py` |
| Modify strategies | `strategies/*.py` |
| Fix daemon issues | `live/daemon.py`, `live/checker.py` |
| Debug broker connection | `live/connection.py`, `broker/ibkr.py` |
| Add AI capabilities | `agent/advisor.py` |
| Modify notifications | `notifications/ntfy.py` |
| Add monitoring | `monitor/*.py` |
| Change configuration | `config/settings.py`, `config/default.yaml` |
| Add CLI commands | `cli.py` |
| Fix tests | `tests/test_*.py` |
