# Aurel2 Discussion Summary

## Why We're Rebuilding

### Critique of Aurel v1 (ML Price Prediction)

The original Aurel tried to predict stock price direction using ML on technical indicators. This approach fails because:

1. **Markets are efficient** for liquid stocks - any pattern is arbitraged away
2. **80% accuracy is a red flag** - indicates overfitting, not real edge
3. **Technical indicators** are just transformations of price - no new information
4. **What you built**: 20+ technical indicators → 4 ML models (CatBoost, XGBoost, RandomForest, SVM) → predict direction

### Why Dual Momentum Works

Momentum is one of the few market anomalies that has persisted for 200+ years across every market studied:

- **Behavioral**: People underreact to good news, then slowly pile in
- **Institutional**: Fund managers chase performance (career risk)
- **Risk-based**: Momentum assets are riskier in crashes (so they earn a premium)

**Historical performance**:
- Backtested returns: ~12-15% annually since 1970s
- Max drawdown: ~20-25% (vs ~55% for buy-and-hold stocks)
- Works across stocks, bonds, commodities, currencies

---

## Tax Strategy (Romania 2026)

### The Key Tax Rates

| Situation | Tax Rate |
|-----------|----------|
| Capital gains via Romanian broker, held >365 days | **1%** |
| Capital gains via Romanian broker, held <365 days | **3%** |
| Capital gains via international broker (IBKR) | **16%** |
| Dividends received | 8% |
| Accumulating ETF dividends | **0%** (not taxed until sold) |

### The Trade-off

**Interactive Brokers (16% on net):**
- Can offset losses against gains
- Better ETF selection (all UCITS ETFs)
- Lower commissions

**Romanian Broker - TradeVille (1-3% per transaction):**
- Much lower tax rate if consistently profitable
- Cannot offset losses
- Limited ETF selection (but has major UCITS ETFs via Xetra access)

### Decision: Start IBKR, migrate to TradeVille

1. Start with IBKR for flexibility and loss offset capability
2. Once strategy is proven profitable, migrate to TradeVille for 1% tax
3. Use Irish-domiciled accumulating UCITS ETFs (15% US dividend withholding vs 30%)

---

## Strategy Chosen: Tax-Optimized Dual Momentum

### The Rules

**Quarterly check** (reduces trading and tax events):

1. Calculate 12-month momentum for each asset:
   - US Stocks (CSPX - iShares S&P 500)
   - Global Stocks (VWRA - Vanguard All-World)
   - Bonds (AGGH - Global Aggregate Bond)
   - Cash (baseline at risk-free rate)

2. **Rank by momentum**: (Current Price / Price 12 months ago) - 1

3. **Only switch positions if**:
   - New winner beats current holding by >10% momentum, OR
   - Current holding has negative absolute momentum (below cash)

4. **Position sizing**: 100% in the winner (simple, effective)

### Expected Performance

| Metric | Dual Momentum | S&P 500 Buy-Hold |
|--------|---------------|------------------|
| Annual Return | 10-14% | ~10% |
| Max Drawdown | 20-25% | ~55% |
| Sharpe Ratio | 0.7-0.8 | ~0.4 |

**Real edge**: Smaller drawdowns mean you won't panic-sell at the bottom.

---

## ETF Selection

### Primary Assets (UCITS, Irish-domiciled, Accumulating)

| Asset Class | ETF | ISIN | TER | Exchange |
|-------------|-----|------|-----|----------|
| US Stocks | iShares Core S&P 500 (Acc) | IE00B5BMR087 | 0.07% | Xetra |
| Global Stocks | Vanguard FTSE All-World (Acc) | IE00BK5BQT80 | 0.22% | Xetra |
| Bonds | iShares Core Global Agg Bond (Acc) | IE00BDBRDM35 | 0.10% | Xetra |

**Why Irish-domiciled?** 15% US dividend withholding (vs 30% elsewhere)
**Why Accumulating?** Romania doesn't tax reinvested dividends
**Why Xetra?** Lower trading fees than Irish exchange

---

## Key Differences from Aurel v1

| Aurel v1 | Aurel v2 |
|----------|----------|
| ML price prediction | Proven momentum strategy |
| Daily trading | Quarterly rebalancing |
| 20+ technical indicators | Simple 12-month return |
| CatBoost/XGBoost | No ML needed |
| Alpaca (US broker) | IBKR → TradeVille (Romania tax) |
| Hardcoded everything | YAML configuration |
| Print debugging | Structured logging |
| No tests | pytest coverage |
