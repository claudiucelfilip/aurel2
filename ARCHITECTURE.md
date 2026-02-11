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
- Employs **DM-primary + calm-hold** orchestration with multi-strategy classification
- Includes **conservative AI advisor** to guard against known failure patterns
- Requires **human approval** for non-routine decisions
- Optimizes for **Romanian tax efficiency** (UCITS ETFs, quarterly rebalancing)

### Key Principles

1. **Momentum-first**: Core strategy based on 12-month relative momentum
2. **DM-primary + calm-hold**: Dual momentum drives trades; calm-hold prevents churn in bull markets
3. **Multi-strategy classification**: 3 strategies determine decision type (routine vs non-routine), but DM signal drives the actual trade
4. **Conservative AI**: AI guards against failures, doesn't replace system (disabled by default)
5. **Human-in-the-loop**: Non-routine decisions require approval
6. **Tax optimized**: Monthly rebalancing, Irish-domiciled UCITS ETFs
7. **Audit trail**: Every decision logged for analysis

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

# AI Settings (disabled by default, enable with --ai flag)
AI_MODEL=haiku              # haiku, sonnet, or opus
AI_LOOKBACK_YEARS=3
```

### Deployment Commands

```bash
# Deploy (runs tests, syncs code, rebuilds containers):
./scripts/deploy.sh

# Or manually:
cd /opt/aurel2/docker
docker compose up -d

# View logs
docker compose logs -f aurel2

# Check health
docker compose exec aurel2 cat /root/.aurel2/heartbeat.json
```

### Local Development (Testing Only)

For local testing, you can run directly:
```bash
# Requires IB Gateway running locally on port 4002
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
│   │   └── ibkr.py          # Interactive Brokers
│   ├── core/                # Domain models
│   │   ├── models.py        # Signal, Position, Portfolio
│   │   └── assets.py        # Asset registry
│   ├── config/              # Configuration
│   │   └── settings.py      # Pydantic settings
│   ├── dashboard/           # Web dashboard (FastAPI + HTMX)
│   ├── data/                # Data providers
│   │   ├── providers/
│   │   │   ├── yahoo.py     # Yahoo Finance integration
│   │   │   ├── cache.py     # Disk-cached price provider (Parquet)
│   │   │   └── ibkr.py      # IBKR historical data provider
│   │   ├── validation.py    # Price data validation/cleaning
│   │   └── indicators.py    # Technical indicators
│   ├── engine/              # Analysis engines
│   │   ├── backtest.py      # Full-path backtesting engine
│   │   └── backtest_agent.py # Agent backtesting
│   ├── live/                # Live trading
│   │   ├── daemon.py        # Main trading loop
│   │   ├── checker.py       # Single check cycle
│   │   ├── executor.py      # Order execution
│   │   ├── connection.py    # IBKR connection
│   │   ├── pending.py       # Pending approvals
│   │   ├── trade_recorder.py # Shared execute→record→notify pipeline
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
│   │   └── multi_timeframe.py
│   ├── utils/               # Utilities
│   │   └── logging.py       # Structured logging
│   └── cli.py               # CLI entry point
├── config/                  # YAML configuration
│   └── default.yaml
├── data/                    # Persistent data files
│   ├── paper/               # Paper trading data (mode-partitioned)
│   │   ├── trade_journal.json
│   │   ├── pending_decisions.json
│   │   └── session_progress.json
│   ├── live/                # Live trading data (mode-partitioned)
│   ├── archive/             # Archived data from resets
│   ├── failure_learnings.json  # Shared across modes
│   ├── backtest_comparison.json  # Pre-computed backtest for dashboard
│   └── price_cache/         # Cached price data (Parquet)
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
| `switch_threshold` | 0.10 | Same-category switch threshold |
| `equity_to_defensive_threshold` | 0.15 | Threshold to leave equity for bonds/gold (harder) |
| `defensive_to_equity_threshold` | 0.05 | Threshold to return to equity (easier) |
| `cash_rate` | 0.0 | Baseline for absolute momentum (disabled — always invests) |
| `pilot_entry_enabled` | True | Pilot entry system |

**Logic**:
1. Calculate 12-month return for each asset
2. **Relative momentum**: Select asset with highest return
3. **Absolute momentum**: Only buy if return > cash_rate (currently 0% = always buy)
4. **Asymmetric switch thresholds**: 15% to leave equity, 5% to return, 10% same-category

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

---

## AI/Agent System

### Agent Orchestrator

**File**: `src/aurel2/agent/orchestrator.py`

Central decision-making engine. All 3 strategies run on each rebalance, but the
orchestrator uses **DM-primary + calm-hold** logic — not blended voting:

1. **Calm-hold check**: If already holding an asset, drawdown < 5%, and market
   isn't urgent → HOLD current position. Prevents unnecessary churn in bull markets.
   - **Negative momentum escape hatch**: If the held asset has negative 12-month
     momentum, the switch goes through even in calm markets. Prevents getting
     trapped in a losing position during a bull market (e.g. VNQ 2016-2018).
2. **Dual momentum primary**: Uses the dual momentum signal directly for trade
   decisions. The 3-strategy weighted vote dilutes conviction and hurts alpha.
3. **Classification**: The other 2 strategies still determine whether the decision
   is ROUTINE (all agree → auto-execute) or NON_ROUTINE (disagree → needs approval).

```
Decision Flow:
  1. Run all 3 strategies
  2. Check calm-hold: drawdown < 5% + already holding + held asset has positive momentum → HOLD
     (if held asset has negative 12m momentum → escape hatch, allow the switch)
  3. Otherwise: use dual momentum signal directly
  4. Classify: all 3 agree → ROUTINE (auto-execute), else → NON_ROUTINE (approval)
  5. Urgency override: drawdown > 15% → URGENT (short timeout, auto-execute)
```

**Decision Classification**:
```
ROUTINE:     All 3 strategies agree → auto-execute
NON_ROUTINE: Strategies disagree → requires approval
URGENT:      High drawdown (>15%) or extreme conditions
```

**Calm-Hold Threshold**: `calm_market_hold_threshold = 0.05` (5% drawdown from
52-week high). This is adaptive — when markets drop >5%, calm-hold turns off and
the system starts actively switching assets. In a crash (>15%), it goes URGENT.

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
- Configurable model: "haiku" (default), "sonnet", "opus"
- Lookback: Last 3-5 years of failures (configurable)
- **Disabled by default** — see AI Advisor Evaluation below for findings
- Enable with `--ai` flag for backtesting or `--ai` for live trading

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
| Mode | IB Gateway |
|------|------------|
| Paper | 4002 |
| Live | 4001 |
| Docker | 4003/4004 |

**Features**:
- Auto-launch IB Gateway if not running (skipped in Docker mode)
- Heartbeat to keep connection alive
- Circuit breaker for failure protection
- Automatic reconnection with exponential backoff
- **Client ID conflict auto-recovery**: If the IBKR client ID is already in use
  (error 326, e.g. stale connection after container restart), automatically picks
  a new random client ID and retries immediately without counting as a failure
- **Fatal exit after sustained failure**: After 30 consecutive heartbeat failures
  (~30 min), exits with code 78 so Docker restarts the container. Prevents the
  daemon from running indefinitely in a broken state.
- **Upstream disconnect handling**: Detects when TCP connection to IB Gateway is
  alive but IBKR upstream is down (error 1100). Waits for automatic restoration
  (error 1102) instead of attempting reconnection.

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

**Storage**: `data/{mode}/pending_decisions.json` (paper or live)

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
- Independent heartbeat writer every 60s to `~/.aurel2/heartbeat.json`

**Main Loop**:
1. Check if scheduled check time
2. Run Checker (strategies → orchestrator → advisor)
3. If ROUTINE → execute immediately
4. If NON_ROUTINE → create pending, notify user
5. Poll for pending decision responses
6. Update strategy accuracy from previous decision's P&L

**Heartbeat Writer**: Runs as an independent async task, decoupled from the
poll loop. Writes epoch timestamps (not ISO strings) for reliable staleness
detection. File location moved from `/tmp/` to `~/.aurel2/` so it survives
container restarts.

**Signal Handling**: Graceful shutdown on SIGINT/SIGTERM (cancels heartbeat task)

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

### Currency

All monetary values are displayed in EUR (the IBKR account base currency):
- Dashboard: account summary, positions, charts, trade history
- Approval endpoint: SPY price converted to EUR
- Notifications: regime change SPY/MA values in EUR
- Position values use `ib.portfolio()` which returns EUR-converted amounts
- SPY price converted via `broker.get_eur_usd_rate()` (live IBKR Forex quote)

### Account Summary Cards

Two primary cards:
- **Total Value** — `NetLiquidation` from IBKR (includes unsettled). Shows cash breakdown only when cash != total.
- **Overall P&L** — Percentage and euro gain/loss since first recorded account value.

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

### Backtest Comparison Chart

Async-loaded Chart.js chart showing strategy equity curve vs SPY buy-and-hold with 5Y/10Y toggle. Loaded via `fetch('/api/backtest-comparison')` on page load — not re-fetched on the 60s auto-refresh.

**Endpoint**: `GET /api/backtest-comparison` — reads pre-computed results from `data/backtest_comparison.json`.

**Generating results**:
```bash
python -m aurel2.engine.backtest
```

This runs the production `BacktestEngine` (DM-primary + calm-hold via orchestrator, all 3 strategies, no AI) for both 5Y and 10Y periods and saves the JSON.

**Metrics displayed**: CAGR, Sharpe, Max Drawdown, Alpha vs SPY.

### Post-Trade State Recording

All execution paths use a shared `TradeRecorder` (`live/trade_recorder.py`) that handles:
1. Execute via broker
2. Capture post-trade state (`account_value_after`, `current_holding_after`)
3. Record in `TradeJournal`
4. Send notification

This eliminates duplication between daemon and checker execution paths.

---

## Data Flow

### Backtest Flow

The backtest engine (`src/aurel2/engine/backtest.py`) mirrors the full live
trading path. It creates all components internally and runs the same decision
pipeline as production on each rebalance date.

```
┌─────────────────┐
│  Yahoo Finance  │
│  (Cached)       │
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
│  Market Context │
│ (Regime detect) │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│   Orchestrator  │
│  (DM-primary +  │
│   calm-hold)    │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│   AI Advisor    │
│ (off by default,│
│  --ai enables)  │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Trade Execution │
│ (Position Size) │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│   Performance   │
│   Metrics       │
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
  cash_rate: 0.0

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
| equity_to_defensive_threshold | strategy | 0.15 |
| defensive_to_equity_threshold | strategy | 0.05 |
| cash_rate | strategy | 0.0 |
| pilot_entry_enabled | strategy | False |
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
| ai_model | --ai-model | haiku |

---

## External Services

### Yahoo Finance

**File**: `src/aurel2/data/providers/yahoo.py`

- Fetches historical OHLC data
- Supports multi-symbol requests
- 400-day buffer for lookback calculations
- Returns DataFrame with columns: date, close, symbol
- Price validation via `data/validation.py` (catches gaps, outliers)

### Cached Price Provider

**File**: `src/aurel2/data/providers/cache.py`

- Wraps Yahoo Finance provider with disk caching (Parquet format)
- Cache directory: `data/price_cache/`
- Used by backtest CLI for fast repeated runs
- Falls back to Yahoo Finance on cache miss

### Interactive Brokers (IBKR)

**File**: `src/aurel2/broker/ibkr.py`

- Uses `ib_insync` library
- Executes market and limit orders
- Retrieves positions, account summary, P&L
- Symbol mapping for all 11 tradeable US ETFs (SPY, EFA, EEM, XLK, XLF, XLE,
  XLV, AGG, TLT, GLD, DBC) plus 3 UCITS equivalents (VWRA, CSPX, AGGH)

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

### backtest_comparison.json

Pre-computed backtest results for the dashboard chart. Generated by `python -m aurel2.engine.backtest`.

```json
{
  "10y": {
    "metrics": { "cagr": 19.7, "sharpe_ratio": 0.99, "max_drawdown": -17.4, "alpha": 164.1, "num_trades": 10 },
    "portfolio": [{"date": "2016-03-01", "value": 10000}, ...],
    "benchmark": [{"date": "2016-03-01", "value": 10000}, ...]
  },
  "5y": { ... }
}
```

Committed to git so it deploys with rsync. Re-generate periodically to update with latest prices.

**Dashboard reads backtest data via bind mount** (`/opt/aurel2/data:/app/host-data:ro`)
so updating `data/backtest_comparison.json` in the repo and deploying automatically
updates the dashboard without needing to rebuild containers or copy into volumes.

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
# Backtesting (mirrors full live path: 3 strategies + orchestrator)
aurel2 backtest [--start DATE] [--end DATE] [--capital AMOUNT] [--ai]

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

# Reset data for a trading mode (archives old data)
aurel2 reset-data paper

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
exchange_calendars>=4.5.0
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

---

## Strategy Experiment Log

Record of strategy changes tested and their backtest results. All comparisons
use `--no-ai` backtests to isolate strategy impact from AI variability.

**Current baseline (main branch, Feb 2026):**

Dual momentum primary + calm-hold with negative momentum escape hatch +
asymmetric switch thresholds (15% to leave equity, 5% to return, 10% same-category).

| Period | CAGR | Alpha vs SPY | Max DD | Sharpe | Trades |
|--------|------|-------------|--------|--------|--------|
| 10yr (2016-2026) | 22.1% | +306.3% | 17.4% | 1.05 | 7 |
| 5yr (2021-2026) | 28.7% | +163.9% | 17.1% | 1.17 | 4 |

**Original baseline (before calm-hold + escape hatch):**

| Period | Return | Alpha vs SPY | CAGR | Max DD | Sharpe | Trades |
|--------|--------|-------------|------|--------|--------|--------|
| 10yr (2015-2026) | 255.43% | -49.41% | 12.10% | 25.22% | 0.76 | 16 |
| 5yr (2020-2026) | 216.73% | +84.74% | 20.79% | 14.64% | 1.07 | 10 |

### Experiment 1: Strategy-Level Changes (Reverted)

**Branch:** `improve/strategy-correctness`

**Changes tested (all at once):**
1. Wider mean reversion RSI thresholds (25/75 → 35/65) with gradient confidence
2. Dynamic T-bill cash rate (fetched from ^IRX instead of static 4%)
3. Rewritten AI expert prompt (more skeptical, fewer overrides)

**Results:**

| Period | Return | Alpha | CAGR | Max DD | Sharpe | Trades |
|--------|--------|-------|------|--------|--------|--------|
| 10yr | 236.94% | -67.91% | 11.56% | 25.24% | 0.82 | 24 |
| 5yr | 200.26% | +68.27% | 19.74% | 12.96% | 1.22 | 18 |

**Verdict:** Reverted. Traded more (24 vs 16) but returned less (237% vs 255%).
Better risk-adjusted metrics (Sharpe 0.82 vs 0.76, lower drawdown) but not
enough to justify -18% return and -18% alpha. The wider RSI thresholds caused
over-trading.

### Experiment 2: Leading Indicator Overlay (Reverted)

**Branch:** `improve/strategy-correctness`

**Approach:** Non-tradeable indicators (UUP, HYG, SMH) as position-sizing
overlay. Indicators never change trade direction — only reduce position size
(0.5x-1.0x) when multiple indicators signal stress.

**Lead-lag relationships:**
- UUP (US Dollar) → EEM: Rising dollar = EM headwind
- HYG/AGG ratio (credit stress) → SPY: Falling ratio = credit deterioration
- SMH (semiconductors) → SPY/XLK: Semis lead broad market

**Signals:**
- Credit stress: HYG/AGG ratio MA20 vs MA63
- Dollar signal: 20-day UUP momentum, normalized to [-1, +1]
- Semis divergence: 20-day SMH return minus 20-day SPY return

**Combined overlay:** Weighted average (credit 0.4, dollar 0.2, semis 0.4).
If combined < -0.5 → 0.6x position. If < -0.3 → 0.8x. Otherwise → 1.0x.

**Results (v2, tighter thresholds):**

| Period | Return | Alpha | CAGR | Max DD | Sharpe | Trades |
|--------|--------|-------|------|--------|--------|--------|
| 10yr | 228.42% | -76.42% | 11.31% | 22.43% | 0.83 | 24 |
| 5yr | 179.98% | +47.99% | 18.38% | 12.86% | 1.19 | 18 |

**AI A/B test (5yr with AI enabled):**

| Config | Return | Alpha | Sharpe | Trades |
|--------|--------|-------|--------|--------|
| AI + Indicators | 199.22% | +67.24% | 1.26 | 21 |
| AI, No Indicators | 222.22% | +90.24% | 1.33 | 18 |

**Verdict:** Reverted. The overlay reduced returns by ~17-20% with marginal
drawdown improvement. Root cause: momentum strategy already rotates to safe
assets during stress, so the indicator overlay double-counts risk by also
reducing position size. Cash left uninvested during recoveries never compounds.
AI advisor also performed worse with indicator noise (3 extra unnecessary
override trades).

### Experiment 3: Quarterly Rebalancing (Reverted)

**Change:** Switch from monthly to quarterly rebalance frequency (config-only).

**Results:**

| Period | Return | Alpha | CAGR | Max DD | Sharpe | Trades |
|--------|--------|-------|------|--------|--------|--------|
| 10yr | 139.20% | -165.64% | 8.17% | 22.83% | 0.97 | 16 |
| 5yr | 180.49% | +48.50% | 18.41% | 9.54% | 1.94 | 7 |

**Verdict:** Reverted. Much better risk-adjusted metrics (5yr Sharpe 1.94 vs
1.07, max DD 9.5% vs 14.6%) but dramatically lower returns (-116% on 10yr).
Quarterly checks miss momentum shifts by up to 3 months. A pure return-vs-risk
tradeoff — not worthwhile for a growth-oriented system.

### Experiment 4: Skip-Month Momentum, 12-1 (Reverted)

**Change:** Exclude most recent month from 12-month momentum calculation to
filter short-term reversal noise (Novy-Marx 2012). Momentum uses months 2-12
instead of 1-12.

**Results:**

| Period | Return | Alpha | CAGR | Max DD | Sharpe | Trades |
|--------|--------|-------|------|--------|--------|--------|
| 10yr | 222.24% | -82.61% | 11.12% | 28.82% | 0.71 | 17 |
| 5yr | 225.18% | +93.20% | 21.32% | 14.81% | 1.10 | 10 |

**Verdict:** Reverted. Mixed results — slightly better 5yr (+8% return, +0.03
Sharpe) but worse 10yr (-33% return, +3.6% drawdown). The skip-month effect
that works in academic cross-sectional momentum doesn't help in time-series
momentum with a concentrated portfolio. The recent month's signal contains
useful information about continuation.

### Experiment 5: Asymmetric Switch Thresholds (Reverted)

**Change:** Lower the switch threshold from 10% to 5% when the currently held
asset has negative 12-month momentum. Keep 10% for switching between winners.
Rationale: easier to leave a sinking ship than to abandon a working one.

**Results:**

| Period | Return | Alpha | CAGR | Max DD | Sharpe | Trades |
|--------|--------|-------|------|--------|--------|--------|
| 10yr | 197.00% | -107.85% | 10.30% | 25.51% | 0.67 | 19 |
| 5yr | 216.73% | +84.74% | 20.79% | 14.64% | 1.07 | 10 |

**Verdict:** Reverted. Worse on 10yr (-58% return, +3 trades) and identical on
5yr (the lower threshold never triggered). The existing absolute momentum check
(go to cash when all assets < cash rate) already handles losers. The 5%
threshold caused premature exits during temporary dips that reversed.

### Experiment 6: Reduced Asset Universe — No Sectors (Reverted)

**Change:** Remove 4 sector ETFs (XLK, XLF, XLE, XLV) from the tradeable
universe. Keep core 7: SPY, EFA, EEM, AGG, TLT, GLD, DBC.

**Results:**

| Period | Return | Alpha | CAGR | Max DD | Sharpe | Trades |
|--------|--------|-------|------|--------|--------|--------|
| 10yr | 80.26% | -224.59% | 5.45% | 32.42% | 0.51 | 14 |
| 5yr | 169.31% | +37.32% | 17.63% | 18.62% | 1.36 | 6 |

**Verdict:** Reverted. Catastrophic on 10yr (-175% return, +7% drawdown).
Sectors — especially XLE (energy) and GLD (gold, which stayed in universe) —
are major alpha contributors. The system's ability to rotate into high-momentum
sectors during commodity and rate cycles is a core strength, not noise.

### Experiment 7: Volatility-Adjusted Momentum Scoring (Reverted)

**Change:** Divide 12-month return by annualized realized volatility to rank
assets by risk-adjusted momentum (lookback Sharpe ratio). Based on Barroso &
Santa-Clara (2015).

**Results:**

| Period | Return | Alpha | CAGR | Max DD | Sharpe | Trades |
|--------|--------|-------|------|--------|--------|--------|
| 10yr | 93.15% | -211.69% | 6.11% | 25.13% | 0.52 | 31 |
| 5yr | 79.98% | -52.00% | 10.11% | 25.13% | 0.72 | 17 |

**Verdict:** Reverted. Nearly doubled trades (31 vs 16) and halved returns.
Vol-adjusting the scores constantly re-ranks assets as their volatility changes,
causing excessive churn. Low-vol assets (bonds, gold) get artificially boosted
in rankings, pulling capital away from high-momentum equities/sectors during
strong trends. The academic result applies to cross-sectional factor portfolios,
not concentrated single-asset momentum.

### Experiment 8: 200-Day MA Trend Gate (Reverted)

**Change:** Block equity BUY signals when SPY is below its 200-day moving
average. Redirect to AGG (bonds) instead. Binary gate — not a weight adjustment.

**Results:**

| Period | Return | Alpha | CAGR | Max DD | Sharpe | Trades |
|--------|--------|-------|------|--------|--------|--------|
| 10yr | 255.43% | -49.41% | 12.10% | 25.22% | 0.76 | 16 |
| 5yr | 216.73% | +84.74% | 20.79% | 14.64% | 1.07 | 10 |

**Verdict:** Reverted (no effect). The gate never triggered across the full
10-year backtest. The momentum system already rotates out of equities when they
have negative momentum, which closely coincides with SPY < 200MA. This confirms
the core strategy already acts as its own trend filter — an explicit gate is
redundant.

### Experiment 9: Drawdown Emergency Exit (Reverted)

**Change:** Force sell to AGG (bonds) when the held asset's trailing drawdown
from its 252-day peak exceeds a threshold. Tested at 15% and 20% thresholds.

**Results (15% threshold):**

| Period | Return | Alpha | CAGR | Max DD | Sharpe | Trades |
|--------|--------|-------|------|--------|--------|--------|
| 10yr | 61.40% | -243.45% | 4.41% | 34.26% | 0.36 | 26 |
| 5yr | 93.44% | -38.54% | 11.42% | 34.26% | 0.72 | 16 |

**Results (20% threshold):**

| Period | Return | Alpha | CAGR | Max DD | Sharpe | Trades |
|--------|--------|-------|------|--------|--------|--------|
| 10yr | 129.44% | -175.40% | 7.77% | 30.80% | 0.57 | 21 |
| 5yr | 104.46% | -27.52% | 12.43% | 30.80% | 0.77 | 15 |

**Verdict:** Reverted. Catastrophic at both thresholds. Paradoxically *increased*
max drawdown (34% vs 25%) because it sells after the drop, locks in losses, then
misses the recovery. Classic stop-loss trap for momentum strategies — the very
drawdowns that trigger the exit are often V-shaped recoveries. The momentum
system handles drawdowns better by rotating at the next rebalance based on
forward-looking momentum, not backward-looking price damage.

### AI Advisor Evaluation (Disabled by Default)

**Goal:** Test whether the Claude-based AI advisor adds alpha over the
deterministic momentum system.

**Methodology:** 3 consistency runs per configuration (AI is non-deterministic).
Override threshold: AI must disagree with >0.70 confidence to override.

**Forced-decision backtesting:** The calm-market hold rule suppresses most
non-HOLD decisions in bull markets (drawdown <5%), so the AI is almost never
called under normal conditions. To properly evaluate the AI, we use
"forced-decision backtesting" — temporarily disabling calm-hold at code level
to force the orchestrator to produce active BUY/SELL decisions that the AI
can review. This generates 2-3x more AI calls per backtest, making results
statistically meaningful in a ~15 minute run instead of getting 0-1 AI calls.
The initial evaluation ran without forced decisions (fewer AI calls). The
amnesia evaluation used forced-decision backtesting for conclusive results.
Calm-hold is now permanently enabled in production with no opt-out (see
Lessons Learned #9).

**Results (10yr, 2015-2026):**

| Config | Avg Return | Spread | Avg Overrides | Alpha vs Baseline |
|--------|-----------|--------|---------------|-------------------|
| No-AI baseline | 291.48% | 0pp | 0 | — |
| Haiku @ 0.70 | 329.52% | 16pp | 1.3 | +38pp |
| Sonnet @ 0.70 | 324.25% | 57pp | 2.3 | +33pp |
| Opus @ 0.70 | 291.48% | 0pp | 0 | 0pp (no-op) |

**Results (5yr, 2020-2026):**

| Config | Avg Return | Spread | Alpha vs Baseline |
|--------|-----------|--------|-------------------|
| No-AI baseline | 240.44% | 0pp | — |
| Haiku @ 0.70 | ~217% | 57pp | **-23pp** |

**Key Findings:**

1. **Opus is useless** — never disagrees with the deterministic strategy (0
   overrides across 3 runs). Too conservative to add value.

2. **Sonnet @ 0.75 is a no-op** — disagreements always at ~0.72 confidence,
   below the 0.75 threshold. Binary gap: no sweet spot between 0.70 and 0.75.

3. **Haiku is the most opinionated** — disagrees 7/10 times, but most at low
   confidence (0.62-0.68). Only 1-2 pass the 0.70 threshold per run.

4. **10yr alpha is likely data leakage** — The AI's consistently good call
   (CASH→bonds Feb 2016) is well within training data. It's remembering
   outcomes, not predicting. The 5yr window (more recent data) shows the AI
   actively hurts performance (-23pp avg).

5. **2020 COVID recovery is the failure mode** — Haiku frequently overrides
   the correct TLT→XLK transition during the V-shaped recovery, holding bonds
   or switching to AGG instead. These calls destroy 20-60pp of returns.

6. **Amnesia test confirms data leakage** — An `--amnesia` flag was added
   that instructs the AI to ignore training knowledge and redacts all dates
   from the prompt (so it can't key on remembered events). Results:

   | Config (10yr, forced-decision) | Avg Return | Spread | Alpha vs Baseline |
   |------------------------------------|-----------|--------|-------------------|
   | No-AI baseline | 255.43% | 0pp | — |
   | Amnesia haiku x3 | 239.96% | 132pp | **-15pp** |
   | Non-amnesia haiku (prior) | ~329% | 16pp | +38pp (with calm-hold) |

   Without remembered knowledge the AI averages -15pp (hurts). With memory
   it averages +38pp (helps). The ~53pp gap is the data leakage premium.
   The amnesia AI also shows 132pp run-to-run spread — pure noise.

**Decision:** AI advisor disabled by default. Available via `--ai` flag for
experimentation, with `--amnesia` for leakage-free testing.
Default model changed to haiku (best 10yr results if used).

### Experiment 10: Calm-Hold Negative Momentum Escape Hatch (Adopted)

**Change:** Allow dual momentum to switch assets even in calm markets (drawdown
<5%) when the held asset has negative 12-month momentum. Without this, calm-hold
traps the portfolio in losing positions during bull markets.

**Root cause:** VNQ (REITs) held for 20 months (Jun 2016 - Feb 2018) returning
-11% while SPY gained +28% and XLK gained +47%. Dual momentum correctly
signaled switches but calm-hold blocked them because SPY drawdown was <5%.

**Escape hatch triggers (10Y):**
1. 2017-06-30: VNQ (mom: -1.9%) → switched to XLF
2. 2023-05-31: XLE (mom: -8.3%) → switched to XLK

**Results:**

| Period | Return | Alpha | CAGR | Max DD | Sharpe | Trades |
|--------|--------|-------|------|--------|--------|--------|
| 10yr (before) | 419.70% | +83.09% | 17.93% | 23.49% | 0.91 | 9 |
| 10yr (after) | 500.70% | +164.09% | 19.65% | 17.38% | 0.99 | 10 |
| 5yr (before) | 245.79% | +156.13% | 28.18% | 17.07% | 1.16 | 6 |
| 5yr (after) | 252.76% | +163.10% | 28.70% | 17.07% | 1.17 | 4 |

**Overfitting check:** Fires on 2 different assets (VNQ, XLE), 6 years apart
(2017, 2023). The rule is economically sound — it doesn't add a new signal, it
removes an overly broad suppression that contradicts dual momentum's core logic.

**Verdict:** Adopted. +81pp alpha, -6pp max drawdown, +1 trade over 10 years.
Every metric improved.

### Lessons Learned

1. **Position-sizing overlays hurt momentum strategies.** The core strategy
   already handles risk by rotating to bonds/cash. Reducing position size on
   top of that is double-counting. (Experiments 2, 9)

2. **More signals ≠ better AI decisions.** The AI advisor made better calls
   with fewer, cleaner inputs (regime + price data only). (Experiment 2)

3. **Test strategy changes in isolation.** The first experiment bundled 3
   changes together, making it impossible to identify which helped or hurt.

4. **Sharpe improvements don't justify return drag.** A Sharpe of 0.82 vs 0.76
   sounds better, but giving up 18% return for smoother equity curve is a bad
   trade for a long-term system. (Experiments 1, 3)

5. **The momentum system IS the risk manager.** Trend gates (exp 8) and
   stop-losses (exp 9) are redundant or harmful because momentum rotation
   already moves capital to safety. Don't layer protective mechanisms on
   top of a strategy that already protects itself.

6. **Sector ETFs are alpha, not noise.** Removing sectors (exp 6) destroyed
   returns. The ability to rotate into XLE/GLD during commodity cycles is a
   core feature.

7. **Academic factors don't always transfer.** Skip-month momentum (exp 4)
   and vol-adjusted scoring (exp 7) are proven in cross-sectional academic
   research but hurt a concentrated time-series momentum portfolio.

8. **Stop-losses cause the losses they aim to prevent.** Drawdown exits
   (exp 9) sold after drops and missed recoveries, paradoxically increasing
   max drawdown from 25% to 34%. Momentum's forward-looking rotation
   handles drawdowns better than backward-looking price triggers.

9. **Calm-market hold is the single best enhancement — with an escape hatch.**
   Keeping the current asset when drawdown <5% avoids unnecessary switching.
   But it needs a negative-momentum escape hatch: if the held asset has
   negative 12-month momentum, the switch must go through. Without this,
   calm-hold traps the portfolio in losing positions (VNQ 2016-2018 cost
   ~40% of relative alpha). The escape hatch fired only 2 times in 10 years
   but added +81pp alpha. (Experiment 10)

10. **LLM backtesting alpha is likely data leakage.** AI models trained on
   historical data "remember" outcomes rather than predict them. The AI
   advisor showed +38pp alpha on 10yr backtests but -23pp on recent 5yr
   data closer to training cutoff. Don't trust AI backtest alpha unless
   validated on truly out-of-sample data.

### Experiment 11: Asymmetric Switch Thresholds — Equity Bias (Adopted)

**Change:** Different switch thresholds based on asset category direction:
- Leaving equity for non-equity: 15% threshold (harder to leave)
- Returning to equity from non-equity: 5% threshold (easier to return)
- Same-category switches: 10% threshold (default)

**Rationale:** The system has a 6-year negative alpha period (2016-2022) during
sustained equity bull markets. The symmetric 10% threshold treats leaving
equities the same as entering them, but the cost of missing an equity bull
market is much higher than the cost of being slightly late to rotate out.

**Results:**

| Period | Return | Alpha | CAGR | Max DD | Sharpe | Trades |
|--------|--------|-------|------|--------|--------|--------|
| 10yr (baseline) | 500.70% | +164.09% | 19.65% | 17.38% | 0.99 | 10 |
| 10yr (asym) | 636.60% | +299.98% | 22.12% | 17.38% | 1.05 | 7 |
| 5yr (baseline) | 252.76% | +163.10% | 28.70% | 17.07% | 1.17 | 4 |
| 5yr (asym) | 252.76% | +163.10% | 28.70% | 17.07% | 1.17 | 4 |

**Analysis:** +136pp alpha on 10Y with same max drawdown and fewer trades (7 vs
10). The higher exit threshold kept the system in equities during 2016-2022
bull, avoiding unnecessary rotations to bonds/gold. The 5Y results are
identical — the asymmetric thresholds only matter during category transitions,
which didn't occur in the recent 5Y period.

**Verdict:** Adopted. Best single-experiment result — doubles 10Y alpha with no
downside. Integrated into `DualMomentumStrategy.generate_signal()` at the
switch threshold check.

### Experiment 12: Canary Gate — SPY > 200-SMA Blocks Equity Exit (No Effect)

**Change:** Before rotating from equity to non-equity, require SPY to be below
its 200-day SMA. If SPY is above 200-SMA, block the rotation and HOLD.

**Results:** Identical to baseline (no effect). The DM strategy + calm-hold
already prevents equity exits during uptrends. The canary gate never triggered
because the existing system already acts as its own trend filter.

**Verdict:** Redundant. Confirms lesson #5 — the momentum system IS the risk
manager.

### Experiment 13: Top-3 Diversification — Equal-Weight (Rejected)

**Change:** Hold top 3 momentum assets equally weighted instead of
winner-take-all. Monthly rebalance. Absolute momentum filter (only hold assets
beating cash).

**Results:**

| Period | Return | Alpha | CAGR | Max DD | Sharpe | Trades |
|--------|--------|-------|------|--------|--------|--------|
| 10yr | 199.41% | -137.20% | 11.60% | 15.15% | 0.91 | 99 |
| 5yr | 140.13% | +50.47% | 19.16% | 12.93% | 1.31 | 48 |

**Verdict:** Rejected. Lower drawdown (15% vs 17%) but dramatically worse
returns (-301% vs baseline). Diversification dilutes the momentum signal that
makes winner-take-all work. 99 trades in 10Y vs 10 = excessive churn.

### Experiment 14: Composite Momentum — 1/3/6/12 Month Blend (Rejected)

**Change:** Replace pure 12-month momentum with equal-weight average of 1, 3,
6, 12-month returns. Pilot entry disabled (incompatible with blended scores).

**Results:**

| Period | Return | Alpha | CAGR | Max DD | Sharpe | Trades |
|--------|--------|-------|------|--------|--------|--------|
| 10yr | 547.53% | +210.91% | 20.55% | 17.07% | 1.02 | 9 |
| 5yr | 198.63% | +108.97% | 24.48% | 17.07% | 1.10 | 4 |

**Verdict:** Rejected. Modestly improves 10Y alpha (+211% vs +164%) but hurts
5Y performance (+109% vs +163%). The shorter timeframes (1m, 3m) add noise.
Not worth the complexity and loss of pilot entry. Experiment A achieves a
bigger improvement with a simpler mechanism.

### Experiment 15: Alpha Research Parameter Optimization (Partially Reverted)

**Change:** Systematic optimization of dual momentum defaults via multi-round
AI-to-AI review (Claude Code vs Codex). Three parameter changes tested:
1. `switch_threshold` 0.10 → 0.02 (lower same-category barrier)
2. `cash_rate` 0.04 → 0.0 (always invest, never sit in cash)
3. `pilot_entry_enabled` True → False (remove pilot entry complexity)

Also disabled `correlation_guard` and `sideways_hold` in backtest defaults.

**Isolation test (10yr):**

| Change | CAGR | Return | Alpha | Trades |
|--------|------|--------|-------|--------|
| Baseline (0.10/0.04/pilot) | 22.1% | 636.6% | +306% | 7 |
| Only switch→0.02 | 20.4% | 537.0% | +207% | 9 |
| Only cash→0.0 | 22.1% | 636.6% | +306% | 7 |
| Only pilot→off | 19.7% | 502.5% | +172% | 8 |
| All three | 18.0% | 421.1% | +91% | 10 |

**Verdict:** `cash_rate` 0.04→0.0 adopted (neutral — no effect). The other
two changes reverted: lower switch_threshold caused 2 unnecessary rotations
(-100% return), and disabling pilot entry lost early inflection detection
(-134% return). The AI-to-AI review process failed to catch the regression
because it reasoned about parameters theoretically without running backtests.

### Lessons Learned (continued)

11. **Asymmetric thresholds work because equity bull markets are the norm.**
    Making it harder to leave equities (15% vs 10%) aligns the strategy with
    the long-term equity premium. The cost of whipsawing out of equities during
    a bull far exceeds the cost of being slightly late to rotate in a bear.
    (Experiment 11)

12. **Diversification destroys concentrated momentum.** Winner-take-all is not
    a bug — it's the mechanism. Spreading across top-3 dilutes the signal and
    adds 10x the trades. (Experiment 13)

13. **Blended momentum timeframes add noise.** Pure 12-month momentum is
    robust precisely because it filters short-term noise. Adding 1m and 3m
    returns reintroduces the noise the 12-month window was designed to
    avoid. (Experiment 14)
