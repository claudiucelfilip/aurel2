# Multi-Strategy AI Agent Design

## Overview

Aurel2 evolves from a single momentum strategy to a multi-strategy system with an AI agent that selects the best approach for current market conditions. The goal is maximum ROI by using the right tool for each situation.

---

## Strategies

### Strategy 1: Dual Momentum (existing)
- **Frequency:** Quarterly rebalancing
- **Lookback:** 12 months
- **Logic:** Rank assets by momentum, hold the winner, 5% switch threshold
- **Best for:** Trending markets, crash protection
- **Weakness:** Misses V-shaped recoveries, lags in trend changes

### Strategy 2: Mean Reversion / Oversold Bounce
- **Trigger:** Market drops >10% below recent highs AND reversal signal (RSI < 30)
- **Logic:** Buy when oversold, sell when normalized
- **Best for:** Catching sharp recoveries that momentum misses
- **Weakness:** Can catch falling knives in prolonged bear markets

### Strategy 3: Multi-Timeframe Trend
- **Frequency:** Monthly evaluation
- **Lookback:** Blends 3-month, 6-month, 12-month momentum signals
- **Logic:** Weighted average of multiple timeframes for faster reaction
- **Best for:** Reducing lag in trend changes
- **Weakness:** More trades, potentially more whipsaws

---

## Asset Universe (~15 assets)

| Category | Symbol | Name | Purpose |
|----------|--------|------|---------|
| **Core Equity** | SPY | S&P 500 | US large cap |
| | EFA | MSCI EAFE | International developed |
| | EEM | MSCI Emerging Markets | Emerging markets |
| **Sectors** | XLK | Technology Select | Tech sector rotation |
| | XLF | Financial Select | Financial sector |
| | XLE | Energy Select | Energy sector |
| | XLV | Healthcare Select | Healthcare sector |
| **Bonds** | AGG | US Aggregate Bond | Broad bonds |
| | TLT | 20+ Year Treasury | Long-term treasury |
| **Alternatives** | GLD | Gold | Inflation hedge, uncorrelated |
| | DBC | Commodities Index | Commodities exposure |

**UCITS equivalents** (for European execution):
- SPY → CSPX (IE00B5BMR087)
- EFA → VWRA (IE00BK5BQT80)
- AGG → AGGH (IE00BDBRDM35)
- GLD → SGLD (IE00B4ND3602)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         Your PC                                  │
│  ┌───────────────┐    ┌───────────────┐    ┌───────────────┐   │
│  │   MCP Server  │◄───│   AI Agent    │───►│     IBKR      │   │
│  │               │    │               │    │   (execution)  │   │
│  │ - Strategies  │    │ - Daily check │    └───────────────┘   │
│  │ - Market data │    │ - Select best │                        │
│  │ - Portfolio   │    │ - Decide      │                        │
│  │ - Indicators  │    │ - Execute     │                        │
│  └───────────────┘    └───────┬───────┘                        │
│                               │                                 │
└───────────────────────────────┼─────────────────────────────────┘
                                │
                    ┌───────────┴───────────┐
                    ▼                       ▼
          ┌─────────────────┐    ┌─────────────────┐
          │   Ntfy.sh       │    │  Vercel         │
          │ (push notifs)   │    │ (approval page) │
          └─────────────────┘    └─────────────────┘
                    │                       │
                    └───────────┬───────────┘
                                ▼
                    ┌─────────────────┐
                    │   Your Phone    │
                    └─────────────────┘
```

### MCP Server Tools

| Tool | Description |
|------|-------------|
| `get_momentum_scores()` | Current momentum for each asset (3/6/12 month) |
| `get_regime()` | Bull/Bear/Neutral based on 200-day MA |
| `get_rsi(symbol)` | RSI values for mean reversion signals |
| `get_portfolio()` | Current holdings, cash, cost basis, tax status |
| `get_strategy_signals()` | Each strategy's recommendation + reasoning |
| `get_market_context()` | VIX, Fear & Greed Index, recent drawdown % |
| `get_economic_calendar()` | Upcoming Fed/ECB decisions |
| `search_news(query)` | Headlines for context (not decision input) |
| `execute_trade(action, symbol, amount)` | Place order via IBKR |

### Serverless Approval Endpoint (Vercel)

- **POST /decision** - Agent creates pending decision with unique ID
- **GET /decision/:id** - Approval page with details, Yes/No buttons
- **POST /decision/:id/respond** - User response (approve/reject)
- **GET /decision/:id/status** - Agent polls for response

---

## Decision Flow

```
Agent runs daily check
        │
        ▼
Query all 3 strategies
        │
        ▼
Classify: Routine or Non-routine?
        │
   ┌────┴────┐
   ▼         ▼
ROUTINE   NON-ROUTINE
   │         │
   ▼         ▼
Execute   Is it sleep hours? (11pm-8am Romania)
auto         │
   │    ┌────┴────┐
   │    ▼         ▼
   │   YES        NO
   │    │         │
   │    ▼         ▼
   │  Urgent?   Send Ntfy + create approval
   │    │         │
   │ ┌──┴──┐      ▼
   │ ▼    ▼    Wait (urgency-based timeout)
   │YES   NO      │
   │ │    │    ┌──┴──┐
   │ ▼    ▼    ▼     ▼
   │Act  Wait  User   Timeout
   │now  til   responds  │
   │     8am      │      ▼
   │      │       ▼   Agent decides
   │      │   Execute  autonomously
   │      │   or skip     │
   │      │       │       ▼
   └──────┴───────┴───────┘
                  │
                  ▼
          Notify outcome
```

### Routine vs Non-Routine

**ROUTINE (auto-execute):**
- All 3 strategies agree on the same action
- Regular quarterly rebalance with no unusual signals
- Holding current position (no change)

**NON-ROUTINE (ask for approval):**
- Strategies disagree
- Position change >50% of portfolio
- Extreme market conditions (VIX spike, >5% daily move)

### Timeout by Urgency

| Situation | Wait Time |
|-----------|-----------|
| Routine quarterly rebalance | 24-48 hours |
| Strategies disagree | 4-8 hours |
| Crash protection / urgent | 1-2 hours |

### Sleep Hours

- **11pm - 8am Romania time (Europe/Bucharest)**
- No waiting for approval during sleep
- Urgent → agent decides immediately
- Non-urgent → waits until 8am

---

## Notifications

### Ntfy Push Notifications

**Approval request:**
```
🔔 Aurel2: Decision Needed

Mean Reversion wants to BUY SPY (RSI: 28)
Momentum says HOLD (score: -2%)
Trend says HOLD

Market down 8% this week. Agent recommends: BUY

[Tap to review and approve]
```

**Autonomous action taken:**
```
✅ Aurel2: Trade Executed

Bought SPY @ $542.30
Reason: All strategies agreed, routine rebalance

Portfolio: $12,450 (+2.3% MTD)
```

**Autonomous action (after timeout):**
```
⚡ Aurel2: Acted Without Approval

Sold EFA, Bought SPY @ $531.20
Reason: Crash protection triggered. No response after 2 hours.

Strategies: Momentum (SELL), Mean Reversion (HOLD), Trend (SELL)
Agent reasoning: 2/3 strategies agree, VIX at 35, protecting capital.
```

### Weekly Email Report

**Contents:**
1. Portfolio value + performance vs benchmark (SPY)
2. Current regime (Bull/Bear/Neutral)
3. Current holdings breakdown
4. Trades executed this week
5. Agent decision log (what it decided and why)
6. Strategy states (what each strategy currently recommends)
7. Upcoming: next rebalance date, any scheduled events
8. Market summary (VIX, Fear & Greed, YTD performance)

---

## News & External Context

**Agent CAN access:**
- Economic calendar (Fed/ECB meeting dates)
- VIX and Fear & Greed Index
- Major headlines via web search (for context)

**Rules:**
- News is context for explanation, not input for decision
- Strategies make the quantitative decision
- Agent uses news to explain WHY and flag unusual situations
- Never override strong quantitative signals based on narrative

---

## Broker Integration

### Current: IBKR
- Full execution via ib_insync
- Paper trading first (port 7497)
- Live trading when ready (port 7496)

### Future: TradeVille
- Add when account is ready
- Read-only API initially (portfolio sync)
- Manual execution until full API access

### Execution Guardrails
- Agent can only trade assets in approved universe (~15 ETFs)
- Cannot withdraw funds
- Cannot change account settings
- All trades logged with reasoning

---

## Technical Implementation

### Components to Build

1. **MCP Server** (`src/aurel2/mcp/server.py`)
   - Expose all tools listed above
   - Connect to existing strategy code
   - Connect to IBKR broker

2. **Mean Reversion Strategy** (`src/aurel2/strategies/mean_reversion.py`)
   - RSI calculation
   - Drawdown detection
   - Entry/exit signals

3. **Multi-Timeframe Trend Strategy** (`src/aurel2/strategies/multi_timeframe.py`)
   - 3/6/12 month momentum blend
   - Weighted signal generation

4. **Agent Orchestrator** (`src/aurel2/agent/orchestrator.py`)
   - Daily scheduler
   - Strategy aggregation
   - Decision classification (routine/non-routine)
   - Timeout handling

5. **Notification Service** (`src/aurel2/notifications/`)
   - Ntfy integration
   - Email reports (weekly)

6. **Approval Endpoint** (Vercel, separate repo)
   - Simple Next.js or plain HTML/JS
   - Decision storage (KV or simple JSON)
   - Webhook callback

### Data Flow

```
Daily at market close (10pm Romania):
1. Fetch latest prices (Yahoo Finance)
2. Calculate all indicators (momentum, RSI, regime)
3. Run each strategy → get 3 signals
4. Agent evaluates signals + context
5. Classify decision
6. Execute or request approval
7. Log everything
```

---

## Testing Plan

1. **Backtest all 3 strategies** individually (2005-2026)
2. **Backtest agent selection** - simulate agent picking best strategy
3. **Paper trade** for 1 month with full flow
4. **Approval flow test** - verify Ntfy → Vercel → response works
5. **Sleep hours test** - verify timezone handling
6. **Timeout test** - verify autonomous action after timeout

---

## Success Metrics

- **Primary:** Total return vs SPY buy-and-hold
- **Secondary:**
  - Max drawdown (target: <25%)
  - Sharpe ratio (target: >0.8)
  - Win rate of agent's strategy selection
  - Response time on approvals

---

## Future Enhancements

1. TradeVille integration for 1% tax rate
2. More strategies (value, carry, volatility)
3. Position sizing based on conviction/volatility
4. Mobile app instead of Ntfy
5. Voice notifications for urgent situations
