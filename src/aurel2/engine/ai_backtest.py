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

from aurel2.agent.advisor import AIAdvisor
from aurel2.agent.failure_analyzer import FailureAnalysis, run_failure_analysis
from aurel2.core.assets import ASSET_REGISTRY
from aurel2.core.models import AssetClass, SignalAction
from aurel2.data.providers.yahoo import YahooFinanceProvider
from aurel2.strategies.dual_momentum import DualMomentumStrategy
from aurel2.strategies.mean_reversion import MeanReversionStrategy
from aurel2.strategies.multi_timeframe import MultiTimeframeTrendStrategy

logger = structlog.get_logger()


def _normalize_signal(signal) -> dict:
    """Normalize strategy signals to a common dict format."""
    action = signal.action.value if hasattr(signal.action, 'value') else str(signal.action)
    confidence = getattr(signal, 'confidence', 0.8)

    asset_symbol = None
    if hasattr(signal, 'asset') and signal.asset:
        asset_symbol = signal.asset.symbol
    elif hasattr(signal, 'asset_class') and signal.asset_class:
        if signal.asset_class in ASSET_REGISTRY:
            asset_symbol = ASSET_REGISTRY[signal.asset_class].symbol

    reasoning = getattr(signal, 'reasoning', None) or getattr(signal, 'reason', '')

    return {
        "action": action,
        "confidence": confidence,
        "asset_symbol": asset_symbol,
        "reasoning": reasoning,
    }


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
    strategies = [
        ("dual_momentum", DualMomentumStrategy(assets=ASSET_REGISTRY)),
        ("mean_reversion", MeanReversionStrategy()),
        ("multi_timeframe", MultiTimeframeTrendStrategy()),
    ]

    # Ensure failure file exists (compute if needed)
    import os
    if not failure_file or not os.path.exists(failure_file):
        failure_start = (start - timedelta(days=365 * failure_lookback_years)).isoformat()
        full_failure_analysis = run_failure_analysis(
            start_date=failure_start,
            end_date=end_date,
        )
        if failure_file:
            full_failure_analysis.save(failure_file)
            logger.info("computed_and_saved_failure_analysis", filepath=failure_file)

    # Create advisor (handles failure learnings, context fetcher, evaluator)
    advisor = AIAdvisor(
        failure_file=failure_file or "data/failure_learnings.json",
        model="sonnet",
        lookback_years=failure_lookback_years,
    )

    # Fetch all price data upfront
    symbols = ["SPY", "EFA", "EEM", "XLK", "XLF", "XLE", "XLV", "AGG", "TLT", "GLD", "DBC", "^VIX"]
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

    # Generate monthly decision dates
    decision_dates = pd.date_range(start=start, end=end, freq="ME")
    decision_dates = [d.date() for d in decision_dates]

    decisions: list[BacktestDecision] = []
    current_holding: AssetClass | None = None
    ai_holding: AssetClass | None = None

    for i, decision_date in enumerate(decision_dates[:-1]):  # Skip last (no future to measure)
        next_date = decision_dates[i + 1]

        logger.info("evaluating_date", date=str(decision_date), progress=f"{i+1}/{len(decision_dates)-1}")

        # 1. Get signals from all 3 strategies
        signals = {}
        det_signal = None
        for name, strat in strategies:
            try:
                sig = strat.generate_signal(
                    prices=prices_df,
                    calc_date=decision_date,
                    current_holding=current_holding,
                )
                signals[name] = _normalize_signal(sig)
                if name == "dual_momentum":
                    det_signal = sig
            except Exception as e:
                logger.warning("strategy_signal_failed", strategy=name,
                               date=str(decision_date), error=str(e))
                signals[name] = {"action": "hold", "confidence": 0.0, "error": str(e)}

        if det_signal is None:
            logger.error("dual_momentum_failed", date=str(decision_date))
            continue

        det_action = det_signal.action.value
        det_asset = det_signal.asset.symbol if det_signal.asset else None

        # 2. Get AI decision via advisor.review()
        ai_holding_symbol = ai_holding.value if ai_holding else None
        try:
            ai_advice = advisor.review(
                deterministic_action=det_action,
                deterministic_asset=det_asset,
                strategy_signals=signals,
                prices=prices_df,
                current_holding=ai_holding_symbol,
                target_date=decision_date,
            )
            ai_action = ai_advice.recommended_action
            ai_asset = ai_advice.recommended_asset
            ai_reasoning = ai_advice.reasoning[:200] if ai_advice.reasoning else ""
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

    # Calculate summary stats using compound returns (not additive)
    import math
    det_total = (math.prod(1 + (d.deterministic_return or 0) / 100 for d in decisions) - 1) * 100
    ai_total = (math.prod(1 + (d.ai_return or 0) / 100 for d in decisions) - 1) * 100
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
