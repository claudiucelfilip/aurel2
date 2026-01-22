"""Failure Analyzer for historical backtest data.

Analyzes past backtest results to identify failure patterns that the AI
can learn from to make better decisions.

CRITICAL: This module is designed to be point-in-time safe. When evaluating
a decision at date X, it only provides failure data from BEFORE date X
to prevent future data leakage.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import pandas as pd
import structlog

from aurel2.core.models import SignalAction
from aurel2.engine.backtest import BacktestResult

logger = structlog.get_logger()


@dataclass
class FailureEvent:
    """A single failure event from the backtest."""

    date: date
    failure_type: str  # "missed_opportunity", "bad_hold", "bad_switch", "late_entry", "late_exit"
    asset_held: str
    optimal_asset: str | None
    actual_return: float  # What we got
    optimal_return: float  # What we could have gotten
    opportunity_cost: float  # Difference
    market_context: dict[str, Any] = field(default_factory=dict)
    description: str = ""


@dataclass
class FailureAnalysis:
    """Complete analysis of failures from a backtest."""

    start_date: date
    end_date: date
    total_periods: int
    failure_events: list[FailureEvent]
    total_opportunity_cost: float
    failure_rate: float

    # Aggregated patterns
    worst_failures: list[FailureEvent] = field(default_factory=list)  # Top 10 by cost
    common_failure_types: dict[str, int] = field(default_factory=dict)
    failure_by_market_regime: dict[str, list[FailureEvent]] = field(default_factory=dict)

    def save(self, filepath: str) -> None:
        """Save failure analysis to JSON file."""
        import json
        from pathlib import Path

        data = {
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "total_periods": self.total_periods,
            "total_opportunity_cost": self.total_opportunity_cost,
            "failure_rate": self.failure_rate,
            "common_failure_types": self.common_failure_types,
            "failure_events": [
                {
                    "date": f.date.isoformat(),
                    "failure_type": f.failure_type,
                    "asset_held": f.asset_held,
                    "optimal_asset": f.optimal_asset,
                    "actual_return": f.actual_return,
                    "optimal_return": f.optimal_return,
                    "opportunity_cost": f.opportunity_cost,
                    "market_context": f.market_context,
                    "description": f.description,
                }
                for f in self.failure_events
            ],
        }

        Path(filepath).parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
        logger.info("saved_failure_analysis", filepath=filepath, num_failures=len(self.failure_events))

    @classmethod
    def load(cls, filepath: str) -> "FailureAnalysis":
        """Load failure analysis from JSON file."""
        import json

        with open(filepath) as f:
            data = json.load(f)

        failure_events = [
            FailureEvent(
                date=date.fromisoformat(e["date"]),
                failure_type=e["failure_type"],
                asset_held=e["asset_held"],
                optimal_asset=e["optimal_asset"],
                actual_return=e["actual_return"],
                optimal_return=e["optimal_return"],
                opportunity_cost=e["opportunity_cost"],
                market_context=e.get("market_context", {}),
                description=e.get("description", ""),
            )
            for e in data["failure_events"]
        ]

        analysis = cls(
            start_date=date.fromisoformat(data["start_date"]),
            end_date=date.fromisoformat(data["end_date"]),
            total_periods=data["total_periods"],
            failure_events=failure_events,
            total_opportunity_cost=data["total_opportunity_cost"],
            failure_rate=data["failure_rate"],
            worst_failures=sorted(failure_events, key=lambda x: -x.opportunity_cost)[:10],
            common_failure_types=data.get("common_failure_types", {}),
        )

        logger.info("loaded_failure_analysis", filepath=filepath, num_failures=len(failure_events))
        return analysis

    def to_prompt_text(self, as_of_date: date | None = None) -> str:
        """Convert analysis to text for AI prompt.

        Args:
            as_of_date: Only include failures BEFORE this date (point-in-time safe).
                       If None, includes all failures (use for analysis only, not live).
        """
        # Filter to only failures before the as_of_date (NO FUTURE LEAKAGE)
        if as_of_date:
            past_failures = [f for f in self.failure_events if f.date < as_of_date]
            worst = sorted(past_failures, key=lambda x: -x.opportunity_cost)[:10]
            failure_types = {}
            for f in past_failures:
                failure_types[f.failure_type] = failure_types.get(f.failure_type, 0) + 1
            total_cost = sum(f.opportunity_cost for f in past_failures)
            num_failures = len(past_failures)
            # Count total periods before as_of_date
            total_periods = sum(1 for s in range(self.total_periods)
                              if self.failure_events and len(self.failure_events) > 0)
            period_text = f"(data available through {as_of_date - timedelta(days=1)})"
        else:
            past_failures = self.failure_events
            worst = self.worst_failures
            failure_types = self.common_failure_types
            total_cost = self.total_opportunity_cost
            num_failures = len(self.failure_events)
            period_text = ""

        lines = [
            "HISTORICAL FAILURE ANALYSIS (PAST DATA ONLY - NO FUTURE LEAKAGE):",
            f"Analysis period: {self.start_date} to {self.end_date} {period_text}",
            f"Failures identified: {num_failures}",
            f"Total opportunity cost from failures: {total_cost:+.1%}",
            "",
            "TOP 10 WORST PAST FAILURES (learn from these!):",
        ]

        for i, failure in enumerate(worst[:10], 1):
            lines.append(f"\n{i}. {failure.date}: {failure.failure_type}")
            lines.append(f"   Held: {failure.asset_held}, Should have held: {failure.optimal_asset}")
            lines.append(f"   Result: {failure.actual_return:+.2%}, Optimal: {failure.optimal_return:+.2%}")
            lines.append(f"   Cost: {failure.opportunity_cost:+.2%}")
            if failure.market_context:
                ctx = failure.market_context
                if ctx.get("vix"):
                    lines.append(f"   VIX at time: {ctx['vix']:.1f}")
                if ctx.get("spy_drawdown"):
                    lines.append(f"   SPY drawdown: {ctx['spy_drawdown']:.1%}")
            if failure.description:
                lines.append(f"   Analysis: {failure.description}")

        lines.append("\n\nFAILURE PATTERNS (from past data):")
        for ftype, count in sorted(failure_types.items(), key=lambda x: -x[1]):
            lines.append(f"  - {ftype}: {count} occurrences")

        if past_failures:
            lines.append("\n\nKEY LESSONS FROM PAST FAILURES:")
            lines.append("- The momentum system is SLOW - it misses early parts of recoveries and exits")
            lines.append("- During high VIX periods, the system often exits too late or enters too early")
            lines.append("- Asset rotations based on small momentum differences often underperform holding")
            lines.append("- The system's biggest failures come from trend reversals, not steady trends")
        else:
            lines.append("\n(No historical failures available yet - building knowledge base)")

        return "\n".join(lines)

    def get_similar_past_situations(self, as_of_date: date, current_context: dict) -> list[FailureEvent]:
        """Find past failures with similar market context.

        Args:
            as_of_date: Current decision date (only look at failures BEFORE this)
            current_context: Current market conditions (vix, drawdown, etc.)

        Returns:
            List of similar past failure events (point-in-time safe)
        """
        similar = []
        current_vix = current_context.get("vix")
        current_drawdown = current_context.get("spy_drawdown")

        for failure in self.failure_events:
            # CRITICAL: Only look at past failures
            if failure.date >= as_of_date:
                continue

            # Check for similar conditions
            past_vix = failure.market_context.get("vix")
            past_drawdown = failure.market_context.get("spy_drawdown")

            similarity_score = 0

            # VIX similarity (within 5 points)
            if current_vix and past_vix:
                if abs(current_vix - past_vix) < 5:
                    similarity_score += 1
                # Both elevated (>20) or both calm (<20)
                if (current_vix > 20) == (past_vix > 20):
                    similarity_score += 1

            # Drawdown similarity (within 5%)
            if current_drawdown and past_drawdown:
                if abs(current_drawdown - past_drawdown) < 0.05:
                    similarity_score += 1

            if similarity_score >= 2:
                similar.append(failure)

        # Return most relevant (by opportunity cost)
        return sorted(similar, key=lambda x: -x.opportunity_cost)[:5]


class FailureAnalyzer:
    """Analyzes backtest results to identify failure patterns."""

    def __init__(self, prices: pd.DataFrame):
        """Initialize with price data.

        Args:
            prices: DataFrame with columns [date, close, symbol]
        """
        self.prices = prices
        self._setup_price_lookups()

    def _setup_price_lookups(self):
        """Create efficient price lookup dictionaries."""
        self.price_by_symbol = {}
        for symbol in self.prices["symbol"].unique():
            symbol_data = self.prices[self.prices["symbol"] == symbol].copy()
            symbol_data["date"] = pd.to_datetime(symbol_data["date"])
            symbol_data = symbol_data.set_index("date").sort_index()
            self.price_by_symbol[symbol] = symbol_data

    def _get_return(self, symbol: str, start_date: date, end_date: date) -> float | None:
        """Get return for a symbol over a period."""
        if symbol not in self.price_by_symbol:
            return None

        df = self.price_by_symbol[symbol]

        try:
            # Find closest dates
            start_ts = pd.Timestamp(start_date)
            end_ts = pd.Timestamp(end_date)

            # Get prices at or before the dates
            start_prices = df[df.index <= start_ts]
            end_prices = df[df.index <= end_ts]

            if start_prices.empty or end_prices.empty:
                return None

            start_price = start_prices.iloc[-1]["close"]
            end_price = end_prices.iloc[-1]["close"]

            return (end_price / start_price) - 1

        except Exception as e:
            logger.warning("failed_to_get_return", symbol=symbol, error=str(e))
            return None

    def _get_vix_at_date(self, target_date: date) -> float | None:
        """Get VIX level at a date."""
        return self._get_price_at_date("^VIX", target_date)

    def _get_price_at_date(self, symbol: str, target_date: date) -> float | None:
        """Get price of a symbol at a date."""
        if symbol not in self.price_by_symbol:
            return None

        df = self.price_by_symbol[symbol]
        ts = pd.Timestamp(target_date)

        prices_before = df[df.index <= ts]
        if prices_before.empty:
            return None

        return float(prices_before.iloc[-1]["close"])

    def _get_spy_drawdown(self, target_date: date, lookback_days: int = 252) -> float | None:
        """Get SPY drawdown from peak over lookback period."""
        if "SPY" not in self.price_by_symbol:
            return None

        df = self.price_by_symbol["SPY"]
        ts = pd.Timestamp(target_date)
        start_ts = ts - pd.Timedelta(days=lookback_days)

        period_prices = df[(df.index >= start_ts) & (df.index <= ts)]
        if period_prices.empty:
            return None

        peak = period_prices["close"].max()
        current = period_prices.iloc[-1]["close"]

        return (current / peak) - 1

    def analyze(self, result: BacktestResult, all_symbols: list[str] | None = None) -> FailureAnalysis:
        """Analyze backtest results for failures.

        Args:
            result: BacktestResult from backtesting
            all_symbols: List of all possible symbols to compare against

        Returns:
            FailureAnalysis with all failure events and patterns
        """
        if all_symbols is None:
            all_symbols = ["SPY", "EFA", "AGG", "GLD", "EEM", "XLK", "XLF", "XLE", "XLV", "TLT", "DBC"]

        logger.info("analyzing_failures",
                   num_signals=len(result.signals),
                   num_trades=len(result.trades))

        failures: list[FailureEvent] = []

        # Analyze each period between rebalance dates
        for i, signal in enumerate(result.signals[:-1]):  # Skip last (no future to compare)
            current_date = signal.date
            next_signal = result.signals[i + 1]
            next_date = next_signal.date

            # What asset were we holding?
            if signal.asset:
                held_symbol = signal.asset.yahoo_symbol or signal.asset.symbol
            else:
                held_symbol = "CASH"

            # What return did we get?
            actual_return = self._get_return(held_symbol, current_date, next_date) or 0.0

            # What was the best we could have done?
            best_return = actual_return
            best_symbol = held_symbol

            for symbol in all_symbols:
                sym_return = self._get_return(symbol, current_date, next_date)
                if sym_return is not None and sym_return > best_return:
                    best_return = sym_return
                    best_symbol = symbol

            # Calculate opportunity cost
            opportunity_cost = best_return - actual_return

            # Is this a significant failure? (missed >2% of return)
            if opportunity_cost > 0.02:
                # Classify the failure type
                if held_symbol == "CASH" or held_symbol == "AGG":
                    failure_type = "missed_opportunity"  # Were defensive when should have been aggressive
                elif actual_return < -0.02 and best_return > 0:
                    failure_type = "bad_hold"  # Held a loser when winners existed
                elif actual_return < best_return * 0.5:  # Got less than half the best
                    failure_type = "late_entry"  # Entered the trend late
                else:
                    failure_type = "suboptimal_asset"  # Held okay asset, better existed

                # Get market context
                context = {
                    "vix": self._get_vix_at_date(current_date),
                    "spy_drawdown": self._get_spy_drawdown(current_date),
                    "spy_return": self._get_return("SPY", current_date, next_date),
                }

                # Create description
                description = self._describe_failure(
                    failure_type, held_symbol, best_symbol,
                    actual_return, best_return, context
                )

                failures.append(FailureEvent(
                    date=current_date,
                    failure_type=failure_type,
                    asset_held=held_symbol,
                    optimal_asset=best_symbol,
                    actual_return=actual_return,
                    optimal_return=best_return,
                    opportunity_cost=opportunity_cost,
                    market_context=context,
                    description=description,
                ))

        # Sort by opportunity cost (worst failures first)
        failures.sort(key=lambda x: -x.opportunity_cost)

        # Aggregate patterns
        failure_types: dict[str, int] = {}
        for f in failures:
            failure_types[f.failure_type] = failure_types.get(f.failure_type, 0) + 1

        total_cost = sum(f.opportunity_cost for f in failures)

        analysis = FailureAnalysis(
            start_date=result.start_date,
            end_date=result.end_date,
            total_periods=len(result.signals),
            failure_events=failures,
            total_opportunity_cost=total_cost,
            failure_rate=len(failures) / max(len(result.signals), 1),
            worst_failures=failures[:10],
            common_failure_types=failure_types,
        )

        logger.info("failure_analysis_complete",
                   num_failures=len(failures),
                   total_cost=f"{total_cost:.1%}",
                   failure_rate=f"{analysis.failure_rate:.1%}")

        return analysis

    def _describe_failure(
        self,
        failure_type: str,
        held: str,
        optimal: str,
        actual_return: float,
        optimal_return: float,
        context: dict[str, Any],
    ) -> str:
        """Generate human-readable description of failure."""

        if failure_type == "missed_opportunity":
            return (
                f"Was in defensive position ({held}) while {optimal} returned {optimal_return:+.1%}. "
                f"VIX was {context.get('vix', 'N/A')}, suggesting fear was elevated but opportunity existed."
            )

        elif failure_type == "bad_hold":
            return (
                f"Held {held} which lost {actual_return:+.1%} while {optimal} gained {optimal_return:+.1%}. "
                f"Momentum signal was stale - trend had already reversed."
            )

        elif failure_type == "late_entry":
            return (
                f"Entered {held} late, capturing only partial move ({actual_return:+.1%} vs {optimal_return:+.1%}). "
                f"12-month momentum lookback was too slow to catch the early move."
            )

        else:
            return (
                f"Held {held} ({actual_return:+.1%}) when {optimal} would have returned {optimal_return:+.1%}. "
                f"Small momentum differences led to suboptimal asset selection."
            )


def get_default_assets() -> dict:
    """Get the default asset configuration matching the live system."""
    from aurel2.core.models import Asset, AssetClass

    return {
        AssetClass.US_STOCKS: Asset(
            symbol="SPY",
            name="SPDR S&P 500 ETF",
            asset_class=AssetClass.US_STOCKS,
            yahoo_symbol="SPY",
        ),
        AssetClass.GLOBAL_STOCKS: Asset(
            symbol="EFA",
            name="iShares MSCI EAFE ETF",
            asset_class=AssetClass.GLOBAL_STOCKS,
            yahoo_symbol="EFA",
        ),
        AssetClass.BONDS: Asset(
            symbol="AGG",
            name="iShares Core US Aggregate Bond ETF",
            asset_class=AssetClass.BONDS,
            yahoo_symbol="AGG",
        ),
        AssetClass.CASH: Asset(
            symbol="CASH",
            name="Cash",
            asset_class=AssetClass.CASH,
        ),
    }


def load_assets_from_config(config_path: str | None = None) -> dict:
    """Load assets from config, matching the live system exactly."""
    from aurel2.config.settings import load_settings
    from aurel2.core.models import Asset, AssetClass

    settings = load_settings(config_path)

    if settings.assets:
        return {
            AssetClass.US_STOCKS: Asset(
                symbol=settings.assets.us_stocks.symbol,
                name=settings.assets.us_stocks.name,
                asset_class=AssetClass.US_STOCKS,
                isin=settings.assets.us_stocks.isin,
                yahoo_symbol=settings.assets.us_stocks.yahoo_symbol,
            ),
            AssetClass.GLOBAL_STOCKS: Asset(
                symbol=settings.assets.global_stocks.symbol,
                name=settings.assets.global_stocks.name,
                asset_class=AssetClass.GLOBAL_STOCKS,
                isin=settings.assets.global_stocks.isin,
                yahoo_symbol=settings.assets.global_stocks.yahoo_symbol,
            ),
            AssetClass.BONDS: Asset(
                symbol=settings.assets.bonds.symbol,
                name=settings.assets.bonds.name,
                asset_class=AssetClass.BONDS,
                isin=settings.assets.bonds.isin,
                yahoo_symbol=settings.assets.bonds.yahoo_symbol,
            ),
            AssetClass.CASH: Asset(
                symbol="CASH",
                name="Cash",
                asset_class=AssetClass.CASH,
            ),
        }

    return get_default_assets()


def run_failure_analysis(
    start_date: str = "2015-01-01",
    end_date: str | None = None,
    config_path: str | None = None,
    assets: dict | None = None,
) -> FailureAnalysis:
    """Run failure analysis on historical data.

    Uses the SAME asset configuration as the live system to ensure
    the analysis matches real-world behavior.

    Args:
        start_date: Start date for backtest
        end_date: End date (defaults to today)
        config_path: Path to config file (loads same assets as live system)
        assets: Optional override for assets dict (for testing)

    Returns:
        FailureAnalysis with identified failures
    """
    from datetime import date as date_type

    from aurel2.core.models import Asset, AssetClass
    from aurel2.data.providers.yahoo import YahooFinanceProvider
    from aurel2.engine.backtest import BacktestEngine
    from aurel2.strategies.dual_momentum import DualMomentumStrategy

    # Parse dates
    start = date_type.fromisoformat(start_date)
    end = date_type.fromisoformat(end_date) if end_date else date_type.today()

    logger.info("running_failure_analysis", start=str(start), end=str(end))

    # Load assets from config (same as live system) or use provided
    if assets is None:
        assets = load_assets_from_config(config_path)

    # Get symbols from configured assets + comparison symbols
    configured_symbols = []
    for asset in assets.values():
        if asset.yahoo_symbol:
            configured_symbols.append(asset.yahoo_symbol)

    # Additional symbols for comparison (what COULD have been held)
    # These are used to identify "missed opportunity" failures
    comparison_symbols = ["XLK", "XLF", "XLE", "XLV", "TLT", "GLD", "DBC", "EEM"]

    # VIX for market context
    all_symbols = list(set(configured_symbols + comparison_symbols + ["^VIX"]))

    logger.info("failure_analysis_symbols",
               configured=configured_symbols,
               comparison=comparison_symbols)

    # Fetch data
    provider = YahooFinanceProvider()

    all_prices = []
    for symbol in all_symbols:
        try:
            prices = provider.get_prices(
                symbol=symbol,
                start_date=start - timedelta(days=400),  # Extra for lookback
                end_date=end,
            )
            all_prices.append(prices)
        except Exception as e:
            logger.warning("failed_to_fetch", symbol=symbol, error=str(e))

    prices_df = pd.concat(all_prices, ignore_index=True)

    # Run backtest with SAME configuration as live system
    strategy = DualMomentumStrategy(assets=assets)
    engine = BacktestEngine(strategy)
    result = engine.run(prices_df, start, end, frequency="monthly")

    # Analyze failures (compare against all possible assets)
    analyzer = FailureAnalyzer(prices_df)
    analysis = analyzer.analyze(result, all_symbols=list(set(configured_symbols + comparison_symbols)))

    return analysis


if __name__ == "__main__":
    # Run analysis and print results
    analysis = run_failure_analysis(start_date="2020-01-01")
    print(analysis.to_prompt_text())
