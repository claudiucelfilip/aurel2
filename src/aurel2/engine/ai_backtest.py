"""AI Backtest Engine - Compares deterministic vs AI decisions over historical data.

This module runs a historical backtest where:
1. The deterministic system makes decisions based on momentum signals
2. The AI makes decisions with access to past failure learnings (point-in-time safe)
3. Both are compared on actual returns

CRITICAL: The AI only sees failures from BEFORE each decision date (no lookahead bias).
"""

from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd
import structlog

from aurel2.agent.ai_evaluator import ExpertAIEvaluator
from aurel2.agent.failure_analyzer import FailureAnalysis, run_failure_analysis
from aurel2.core.assets import ASSET_REGISTRY
from aurel2.core.models import AssetClass, SignalAction
from aurel2.data.providers.yahoo import YahooFinanceProvider
from aurel2.strategies.dual_momentum import DualMomentumStrategy

logger = structlog.get_logger()


@dataclass
class BacktestDecision:
    """A single decision point in the backtest."""

    date: date
    deterministic_action: str
    deterministic_asset: str | None
    ai_action: str
    ai_asset: str | None
    ai_reasoning: str
    agreed: bool
    # Returns over the next period
    deterministic_return: float | None = None
    ai_return: float | None = None


@dataclass
class AIBacktestResult:
    """Results of the AI vs Deterministic backtest."""

    start_date: date
    end_date: date
    decisions: list[BacktestDecision]
    deterministic_total_return: float
    ai_total_return: float
    agreement_rate: float
    ai_win_rate: float  # When they disagree, how often AI wins


def run_ai_backtest(
    start_date: str = "2021-01-01",
    end_date: str = "2026-01-01",
    failure_lookback_years: int = 3,
    failure_file: str | None = "data/failure_learnings.json",
) -> AIBacktestResult:
    """Run backtest comparing deterministic vs AI with failure learning.

    Args:
        start_date: Start date for backtest
        end_date: End date for backtest
        failure_lookback_years: How many years of failure history to show AI
        failure_file: Path to cached failure analysis (speeds up backtest)

    Returns:
        AIBacktestResult with comparison data
    """
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)

    logger.info("starting_ai_backtest", start=start_date, end=end_date)

    # Initialize components
    provider = YahooFinanceProvider()
    strategy = DualMomentumStrategy(assets=ASSET_REGISTRY)
    ai_evaluator = ExpertAIEvaluator(use_extended_thinking=False)

    # Fetch all price data upfront
    symbols = ["SPY", "EFA", "EEM", "XLK", "XLF", "XLE", "XLV", "AGG", "TLT", "GLD", "DBC"]
    extended_start = start - timedelta(days=400)

    all_prices = []
    for symbol in symbols:
        try:
            prices = provider.get_prices(symbol, extended_start, end + timedelta(days=60))
            all_prices.append(prices)
        except Exception as e:
            logger.warning("price_fetch_failed", symbol=symbol, error=str(e))

    prices_df = pd.concat(all_prices, ignore_index=True)
    logger.info("fetched_prices", rows=len(prices_df))

    # Load failure analysis (from cache or compute fresh)
    import os
    if failure_file and os.path.exists(failure_file):
        full_failure_analysis = FailureAnalysis.load(failure_file)
        logger.info("loaded_cached_failure_analysis", filepath=failure_file)
    else:
        failure_start = (start - timedelta(days=365 * failure_lookback_years)).isoformat()
        full_failure_analysis = run_failure_analysis(
            start_date=failure_start,
            end_date=end_date,
        )
        if failure_file:
            full_failure_analysis.save(failure_file)
    logger.info(
        "failure_analysis_ready",
        num_failures=len(full_failure_analysis.failure_events),
    )

    # Generate monthly decision dates
    decision_dates = pd.date_range(start=start, end=end, freq="ME")
    decision_dates = [d.date() for d in decision_dates]

    decisions: list[BacktestDecision] = []
    current_holding: AssetClass | None = None
    ai_holding: AssetClass | None = None

    for i, decision_date in enumerate(decision_dates[:-1]):  # Skip last (no future to measure)
        next_date = decision_dates[i + 1]

        logger.info("evaluating_date", date=str(decision_date), progress=f"{i+1}/{len(decision_dates)-1}")

        # 1. Get deterministic signal
        det_signal = strategy.generate_signal(
            prices=prices_df,
            calc_date=decision_date,
            current_holding=current_holding,
        )

        det_action = det_signal.action.value
        det_asset = det_signal.asset.symbol if det_signal.asset else None

        # 2. Build context for AI (point-in-time safe failures)
        failure_context = full_failure_analysis.to_prompt_text(as_of_date=decision_date)

        # Build market context
        context_text = _build_market_context(prices_df, decision_date)
        full_context = f"{failure_context}\n\n---\n\nMARKET CONTEXT:\n{context_text}"

        # Build signals dict for AI
        signals = {
            "dual_momentum": {
                "action": det_signal.action.value,
                "asset_symbol": det_signal.asset.symbol if det_signal.asset else None,
                "asset_class": det_signal.asset.asset_class.value if det_signal.asset else None,
                "confidence": 0.7,
                "reasoning": det_signal.reason,
            }
        }

        deterministic_decision = {"action": det_action, "asset": det_asset}

        # 3. Get AI decision
        try:
            ai_decision = ai_evaluator.evaluate(
                signals=signals,
                context_text=full_context,
                current_holding=ai_holding.value if ai_holding else None,
                deterministic_decision=deterministic_decision,
            )
            ai_action = ai_decision.action
            ai_asset = ai_decision.asset
            ai_reasoning = ai_decision.reasoning[:200] if ai_decision.reasoning else ""
        except Exception as e:
            logger.error("ai_evaluation_failed", date=str(decision_date), error=str(e))
            # Fallback to deterministic
            ai_action = det_action
            ai_asset = det_asset
            ai_reasoning = f"AI failed, using deterministic: {e}"

        agreed = (det_action == ai_action) and (det_action != "buy" or det_asset == ai_asset)

        # 4. Calculate returns for both
        det_return = _calculate_return(prices_df, det_asset, decision_date, next_date)
        ai_return = _calculate_return(prices_df, ai_asset, decision_date, next_date)

        decisions.append(BacktestDecision(
            date=decision_date,
            deterministic_action=det_action,
            deterministic_asset=det_asset,
            ai_action=ai_action,
            ai_asset=ai_asset,
            ai_reasoning=ai_reasoning,
            agreed=agreed,
            deterministic_return=det_return,
            ai_return=ai_return,
        ))

        # Update holdings for next iteration
        if det_action == "buy" and det_asset:
            for ac, asset in ASSET_REGISTRY.items():
                if asset.symbol == det_asset:
                    current_holding = ac
                    break
        elif det_action == "sell":
            current_holding = None

        if ai_action == "buy" and ai_asset:
            for ac, asset in ASSET_REGISTRY.items():
                if asset.symbol == ai_asset:
                    ai_holding = ac
                    break
        elif ai_action == "sell":
            ai_holding = None

    # Calculate summary stats
    det_total = sum(d.deterministic_return or 0 for d in decisions)
    ai_total = sum(d.ai_return or 0 for d in decisions)
    agreements = sum(1 for d in decisions if d.agreed)
    disagreements = [d for d in decisions if not d.agreed]

    ai_wins = sum(
        1 for d in disagreements
        if (d.ai_return or 0) > (d.deterministic_return or 0) + 0.1
    )

    return AIBacktestResult(
        start_date=start,
        end_date=end,
        decisions=decisions,
        deterministic_total_return=det_total,
        ai_total_return=ai_total,
        agreement_rate=agreements / len(decisions) if decisions else 0,
        ai_win_rate=ai_wins / len(disagreements) if disagreements else 0,
    )


def _build_market_context(prices: pd.DataFrame, target_date: date) -> str:
    """Build market context string for the AI."""
    lines = []

    # SPY info
    spy_prices = prices[prices["symbol"] == "SPY"].copy()
    spy_prices["date"] = pd.to_datetime(spy_prices["date"])
    spy_prices = spy_prices[spy_prices["date"] <= pd.Timestamp(target_date)]

    if not spy_prices.empty:
        current_price = spy_prices.iloc[-1]["close"]
        year_ago = target_date - timedelta(days=365)
        year_prices = spy_prices[spy_prices["date"] >= pd.Timestamp(year_ago)]
        if not year_prices.empty:
            year_high = year_prices["close"].max()
            drawdown = (current_price / year_high - 1) * 100
            lines.append(f"SPY: ${current_price:.2f} (drawdown from 52w high: {drawdown:.1f}%)")

    # VIX info
    vix_prices = prices[prices["symbol"] == "^VIX"].copy() if "^VIX" in prices["symbol"].values else pd.DataFrame()
    if not vix_prices.empty:
        vix_prices["date"] = pd.to_datetime(vix_prices["date"])
        vix_prices = vix_prices[vix_prices["date"] <= pd.Timestamp(target_date)]
        if not vix_prices.empty:
            vix = vix_prices.iloc[-1]["close"]
            lines.append(f"VIX: {vix:.1f}")

    lines.append(f"Decision Date: {target_date}")

    return "\n".join(lines)


def _calculate_return(
    prices: pd.DataFrame,
    symbol: str | None,
    start_date: date,
    end_date: date,
) -> float | None:
    """Calculate return for a symbol over a period."""
    if not symbol:
        return 0.0  # Cash

    sym_prices = prices[prices["symbol"] == symbol].copy()
    if sym_prices.empty:
        return None

    sym_prices["date"] = pd.to_datetime(sym_prices["date"])

    start_prices = sym_prices[sym_prices["date"] <= pd.Timestamp(start_date)]
    end_prices = sym_prices[sym_prices["date"] <= pd.Timestamp(end_date)]

    if start_prices.empty or end_prices.empty:
        return None

    start_price = start_prices.iloc[-1]["close"]
    end_price = end_prices.iloc[-1]["close"]

    return (end_price / start_price - 1) * 100


def print_backtest_results(result: AIBacktestResult) -> None:
    """Print formatted backtest results."""
    print("\n" + "=" * 70)
    print("AI vs DETERMINISTIC BACKTEST RESULTS")
    print("=" * 70)
    print(f"Period: {result.start_date} to {result.end_date}")
    print(f"Total Decisions: {len(result.decisions)}")
    print()
    print("PERFORMANCE COMPARISON:")
    print(f"  Deterministic Total Return: {result.deterministic_total_return:+.2f}%")
    print(f"  AI Total Return:            {result.ai_total_return:+.2f}%")
    print(f"  Difference (AI - Det):      {result.ai_total_return - result.deterministic_total_return:+.2f}%")
    print()
    print("DECISION ANALYSIS:")
    print(f"  Agreement Rate: {result.agreement_rate:.1%}")
    print(f"  Disagreements:  {len([d for d in result.decisions if not d.agreed])}")
    print(f"  AI Win Rate (on disagreements): {result.ai_win_rate:.1%}")
    print()

    # Show disagreements
    disagreements = [d for d in result.decisions if not d.agreed]
    if disagreements:
        print("DISAGREEMENT DETAILS:")
        print("-" * 70)
        for d in disagreements[:20]:  # Show first 20
            det_ret = f"{d.deterministic_return:+.2f}%" if d.deterministic_return is not None else "N/A"
            ai_ret = f"{d.ai_return:+.2f}%" if d.ai_return is not None else "N/A"
            winner = "AI" if (d.ai_return or 0) > (d.deterministic_return or 0) + 0.1 else "DET" if (d.deterministic_return or 0) > (d.ai_return or 0) + 0.1 else "TIE"

            print(f"\n{d.date} [{winner}]")
            print(f"  Det: {d.deterministic_action.upper()} {d.deterministic_asset or ''} → {det_ret}")
            print(f"  AI:  {d.ai_action.upper()} {d.ai_asset or ''} → {ai_ret}")
            if d.ai_reasoning:
                print(f"  Reasoning: {d.ai_reasoning[:100]}...")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    result = run_ai_backtest(
        start_date="2021-01-01",
        end_date="2026-01-01",
        failure_lookback_years=3,
    )
    print_backtest_results(result)
