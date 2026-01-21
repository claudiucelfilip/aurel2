# Aurel2: Tax-Optimized Momentum Trading System

## Overview

A complete rebuild of the Aurel trading system focused on **proven momentum strategies** (not ML price prediction) with Romanian tax optimization.

- **Strategy**: Dual Momentum with quarterly rebalancing
- **Broker**: Start with IBKR, migrate to TradeVille for 1-3% tax rate
- **Tech Stack**: Python 3.11+
- **Dashboard**: FastAPI + HTMX (real-time P&L, positions)
- **Deployment**: Local machine
- **Starting Capital**: Under €10,000

---

## Architecture

```
aurel2/
├── src/aurel2/
│   ├── core/           # Domain models (Position, Signal, Order)
│   ├── config/         # Pydantic settings + YAML
│   ├── broker/         # IBKR adapter (ib_insync), TradeVille adapter (future)
│   ├── data/           # Price data fetching + caching
│   ├── strategies/     # Momentum strategy implementation
│   ├── engine/         # Backtest & live trading
│   ├── persistence/    # SQLite for trades/performance
│   ├── dashboard/      # FastAPI + HTMX
│   └── utils/          # Logging, notifications
├── config/             # YAML configuration
├── tests/              # pytest
└── scripts/            # Setup helpers
```

## Technology Stack

| Component | Choice | Reason |
|-----------|--------|--------|
| Broker API | `ib_insync` | Clean async IB wrapper |
| Price Data | Yahoo Finance + IB | Free backup + broker data |
| Config | Pydantic + YAML | Type-safe, readable |
| Database | SQLite | Zero setup, sufficient for MVP |
| Web | FastAPI + HTMX | Minimal JS, real-time capable |
| Logging | `structlog` | Structured JSON logs |

---

## Implementation Plan

### Phase 1: Foundation

#### 1.1 Project Setup
- [ ] Create `aurel2/` directory with Poetry configuration
- [ ] `pyproject.toml` with dependencies
- [ ] `.env.example` and `.gitignore`
- [ ] `structlog` configuration

#### 1.2 Core Models
- [ ] `src/aurel2/core/models.py` - Position, Signal, MomentumScore dataclasses
- [ ] `src/aurel2/core/enums.py` - AssetClass enum
- [ ] `src/aurel2/core/exceptions.py` - Exception hierarchy

#### 1.3 Configuration
- [ ] `src/aurel2/config/settings.py` - Pydantic settings
- [ ] `config/default.yaml` - Strategy parameters, assets, thresholds

#### 1.4 Data Pipeline
- [ ] `src/aurel2/data/providers/yahoo.py` - Yahoo Finance fetcher
- [ ] `src/aurel2/data/cache.py` - SQLite price cache
- [ ] `src/aurel2/data/momentum.py` - Momentum calculations

### Phase 2: Strategy & Backtest

#### 2.1 Momentum Strategy
- [ ] `src/aurel2/strategies/dual_momentum.py` - Core strategy logic
- [ ] Calculate 12-month momentum
- [ ] Implement switching threshold (10% rule)
- [ ] Absolute momentum check (beat cash)

#### 2.2 Backtesting Engine
- [ ] `src/aurel2/engine/backtest.py` - Historical simulation
- [ ] Performance metrics: returns, Sharpe, max drawdown
- [ ] Transaction cost modeling
- [ ] Generate comparison vs buy-and-hold

#### 2.3 Persistence
- [ ] `src/aurel2/persistence/models.py` - SQLAlchemy models
- [ ] Store: trades, signals, portfolio snapshots, performance

### Phase 3: Live Trading

#### 3.1 Broker Integration
- [ ] `src/aurel2/broker/base.py` - Abstract interface
- [ ] `src/aurel2/broker/ibkr.py` - Interactive Brokers via ib_insync
- [ ] `src/aurel2/broker/paper.py` - Paper trading mode
- [ ] Connection management, error handling

#### 3.2 Live Engine
- [ ] `src/aurel2/engine/live.py` - Live trading orchestrator
- [ ] Quarterly scheduler (cron-like)
- [ ] Order execution with confirmation
- [ ] Position reconciliation

#### 3.3 Notifications
- [ ] `src/aurel2/utils/notifications.py` - Email/Telegram alerts
- [ ] Signal generated, order executed, errors

### Phase 4: Dashboard & CLI

#### 4.1 CLI
- [ ] `src/aurel2/cli.py` - Typer commands
- [ ] `aurel2 backtest --start 2010-01-01 --end 2024-01-01`
- [ ] `aurel2 live --paper` / `aurel2 live`
- [ ] `aurel2 dashboard`
- [ ] `aurel2 status` - current positions, last signal

#### 4.2 Dashboard
- [ ] `src/aurel2/dashboard/app.py` - FastAPI app
- [ ] Current positions and P&L
- [ ] Momentum scores for each asset
- [ ] Trade history
- [ ] Performance chart vs benchmark

---

## Configuration Example

```yaml
# config/default.yaml
strategy:
  name: dual_momentum
  lookback_months: 12
  rebalance_frequency: quarterly  # or monthly
  switch_threshold: 0.10  # Only switch if >10% momentum difference

assets:
  us_stocks:
    symbol: CSPX
    isin: IE00B5BMR087
  global_stocks:
    symbol: VWRA
    isin: IE00BK5BQT80
  bonds:
    symbol: AGGH
    isin: IE00BDBRDM35
  cash_rate: 0.04  # Current risk-free rate for comparison

broker:
  type: ibkr  # or tradeville
  host: "127.0.0.1"
  port: 7497  # paper trading

risk:
  max_position_pct: 1.0  # 100% in winner (standard for dual momentum)

notifications:
  email: your@email.com
  telegram_chat_id: null  # optional

logging:
  level: INFO
  file: logs/aurel2.log
```

---

## Verification Plan

1. **Backtest validation**:
   - Run 2010-2024 backtest
   - Verify returns match published dual momentum results (~12-15% CAGR)
   - Confirm max drawdown <30%
   - Compare to SPY buy-and-hold

2. **Paper trading**:
   - Run for 1 quarter in paper mode
   - Verify signals match manual calculation
   - Confirm order execution works

3. **Dashboard**:
   - View current momentum scores
   - See historical trades
   - Verify P&L calculations

---

## ETF Selection

### Primary Assets (UCITS, Irish-domiciled, Accumulating)

| Asset Class | ETF | ISIN | TER | Exchange |
|-------------|-----|------|-----|----------|
| US Stocks | iShares Core S&P 500 (Acc) | IE00B5BMR087 | 0.07% | Xetra |
| Global Stocks | Vanguard FTSE All-World (Acc) | IE00BK5BQT80 | 0.22% | Xetra |
| Bonds | iShares Core Global Agg Bond (Acc) | IE00BDBRDM35 | 0.10% | Xetra |

---

## Future Enhancements (Post-MVP)

1. **TradeVille integration** - For 1% tax rate
2. **Sector momentum overlay** - Additional alpha
3. **VIX-based position sizing** - Reduce in high volatility
4. **Multiple strategies** - Compare momentum vs factor tilts
5. **Mobile notifications** - Telegram bot
