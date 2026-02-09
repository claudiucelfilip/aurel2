#!/usr/bin/env python3
"""Backtest strategy experiments runner.

Runs temporary experiment variants against the baseline DM strategy
and saves results to data/backtest_comparison.json for dashboard inspection.

Usage:
    python scripts/run_experiment.py --experiment A
    python scripts/run_experiment.py --experiment B
    python scripts/run_experiment.py --experiment C
    python scripts/run_experiment.py --experiment D
    python scripts/run_experiment.py --experiment baseline  # re-run baseline only
"""

import argparse
import json
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import numpy as np

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aurel2.core.assets import ASSET_REGISTRY, get_all_yahoo_symbols, get_assets_by_category
from aurel2.core.models import (
    Asset, AssetCategory, AssetClass, MomentumScore,
    PortfolioSnapshot, Signal, SignalAction, Trade,
)
from aurel2.data.momentum import calculate_momentum_scores
from aurel2.data.providers.yahoo import YahooFinanceProvider
from aurel2.engine.backtest import BacktestEngine, BacktestResult
from aurel2.strategies.dual_momentum import DualMomentumStrategy
from aurel2.agent.orchestrator import AgentOrchestrator, AgentDecision, DecisionType, Urgency


# =============================================================================
# Experiment A: Asymmetric Switch Thresholds (Equity Bias)
# =============================================================================

EQUITY_SYMBOLS = {a.symbol for a in get_assets_by_category(AssetCategory.EQUITY)}


class AsymmetricDMStrategy(DualMomentumStrategy):
    """DM with asymmetric switch thresholds favoring equity holdings.

    - Leaving equity for non-equity: threshold = 15% (harder to leave)
    - Returning to equity from non-equity: threshold = 5% (easier to return)
    - Same-category switches: threshold = 10% (default)
    """

    def generate_signal(self, prices, calc_date, current_holding=None):
        if current_holding is not None:
            self.current_holding = current_holding

        # Calculate scores same as parent
        scores = calculate_momentum_scores(
            prices=prices, assets=self.assets, calc_date=calc_date,
            lookback_months=self.lookback_months, cash_rate=self.cash_rate,
        )
        pilot_scores = None
        if self.pilot_entry_enabled:
            pilot_scores = calculate_momentum_scores(
                prices=prices, assets=self.assets, calc_date=calc_date,
                lookback_months=self.pilot_lookback_months, cash_rate=self.cash_rate,
            )

        if not scores:
            return Signal(
                date=calc_date, action=SignalAction.HOLD,
                asset=self.assets.get(self.current_holding or AssetClass.CASH),
                reason="No momentum data available", momentum_scores=scores,
            )

        risky_scores = {k: v for k, v in scores.items() if k != AssetClass.CASH}
        winner_class = max(risky_scores.keys(), key=lambda k: risky_scores[k].momentum_12m) if risky_scores else AssetClass.CASH
        winner_score = scores[winner_class]
        cash_score = scores.get(AssetClass.CASH)

        # Pilot entry logic (same as parent)
        pilot_winner_class = None
        pilot_winner_score = None
        if pilot_scores:
            pilot_risky = {k: v for k, v in pilot_scores.items() if k != AssetClass.CASH}
            if pilot_risky:
                pilot_winner_class = max(pilot_risky.keys(), key=lambda k: pilot_risky[k].momentum_12m)
                pilot_winner_score = pilot_scores[pilot_winner_class]

        # No current position
        if self.current_holding is None:
            if self.pilot_entry_enabled and pilot_winner_score and pilot_winner_class:
                pilot_mom = pilot_winner_score.momentum_12m
                long_mom = scores[pilot_winner_class].momentum_12m if pilot_winner_class in scores else 0
                if pilot_mom > 0.05 and long_mom <= 0.10:
                    self.current_holding = pilot_winner_class
                    self.is_pilot_position = True
                    return Signal(
                        date=calc_date, action=SignalAction.BUY,
                        asset=self.assets[pilot_winner_class],
                        reason=f"PILOT ENTRY: {pilot_winner_class.value} 3m ({pilot_mom:.2%}), 12m ({long_mom:.2%})",
                        momentum_scores=scores,
                    )
            if cash_score and winner_score.momentum_12m <= cash_score.momentum_12m:
                return Signal(
                    date=calc_date, action=SignalAction.BUY,
                    asset=self.assets[AssetClass.CASH],
                    reason=f"Winner below cash", momentum_scores=scores,
                )
            self.current_holding = winner_class
            self.is_pilot_position = False
            return Signal(
                date=calc_date, action=SignalAction.BUY,
                asset=winner_score.asset,
                reason=f"Initial buy: {winner_class.value} ({winner_score.momentum_12m:.2%})",
                momentum_scores=scores,
            )

        current_score = scores.get(self.current_holding)
        if current_score is None:
            self.current_holding = winner_class
            self.is_pilot_position = False
            return Signal(
                date=calc_date, action=SignalAction.BUY,
                asset=winner_score.asset,
                reason=f"No data for current, switching to {winner_class.value}",
                momentum_scores=scores,
            )

        # Pilot scale-up / exit (same as parent)
        if self.is_pilot_position:
            if current_score.momentum_12m > 0.10:
                self.is_pilot_position = False
                return Signal(
                    date=calc_date, action=SignalAction.BUY,
                    asset=current_score.asset,
                    reason=f"SCALE UP: 12m confirmed ({current_score.momentum_12m:.2%})",
                    momentum_scores=scores,
                )
            if pilot_scores and self.current_holding in pilot_scores:
                pilot_current = pilot_scores[self.current_holding].momentum_12m
                if pilot_current < -0.02:
                    self.current_holding = None
                    self.is_pilot_position = False
                    return Signal(
                        date=calc_date, action=SignalAction.SELL,
                        asset=current_score.asset,
                        reason=f"EXIT PILOT: 3m negative ({pilot_current:.2%})",
                        momentum_scores=scores,
                    )

        # Absolute momentum check
        if cash_score and current_score.momentum_12m < cash_score.momentum_12m:
            if winner_score.momentum_12m < cash_score.momentum_12m:
                self.current_holding = AssetClass.CASH
                self.is_pilot_position = False
                return Signal(
                    date=calc_date, action=SignalAction.BUY,
                    asset=self.assets[AssetClass.CASH],
                    reason=f"All below cash ({cash_score.momentum_12m:.2%})",
                    momentum_scores=scores,
                )

        # === ASYMMETRIC THRESHOLD LOGIC ===
        momentum_diff = winner_score.momentum_12m - current_score.momentum_12m

        if winner_class != self.current_holding and momentum_diff > 0:
            current_is_equity = current_score.asset.category == AssetCategory.EQUITY
            winner_is_equity = winner_score.asset.category == AssetCategory.EQUITY

            if current_is_equity and not winner_is_equity:
                threshold = 0.15  # Hard to leave equity
            elif not current_is_equity and winner_is_equity:
                threshold = 0.05  # Easy to return to equity
            else:
                threshold = 0.10  # Same-category default

            if momentum_diff > threshold:
                old = self.current_holding
                self.current_holding = winner_class
                self.is_pilot_position = False
                return Signal(
                    date=calc_date, action=SignalAction.BUY,
                    asset=winner_score.asset,
                    reason=f"ASYM SWITCH: {old.value}→{winner_class.value} diff {momentum_diff:.2%} > threshold {threshold:.2%}",
                    momentum_scores=scores,
                )

        position_type = "PILOT " if self.is_pilot_position else ""
        return Signal(
            date=calc_date, action=SignalAction.HOLD,
            asset=current_score.asset,
            reason=f"Holding {position_type}{self.current_holding.value}",
            momentum_scores=scores,
        )


# =============================================================================
# Experiment B: Canary Gate Before Leaving Equities
# =============================================================================

class CanaryGateOrchestrator(AgentOrchestrator):
    """Orchestrator that blocks equity→non-equity switches when SPY is above 200-day SMA."""

    def analyze(self, signals, market_context=None, current_holding=None):
        decision = super().analyze(signals, market_context, current_holding)

        if market_context is None:
            return decision

        # Only intercept BUY signals that switch from equity to non-equity
        if decision.action != SignalAction.BUY or not decision.asset_symbol:
            return decision
        if not current_holding or current_holding == "CASH":
            return decision

        # Check if current is equity and target is non-equity
        current_is_equity = current_holding in EQUITY_SYMBOLS
        target_is_equity = decision.asset_symbol in EQUITY_SYMBOLS
        target_is_cash = decision.asset_symbol == "CASH"

        if current_is_equity and not target_is_equity and not target_is_cash:
            spy_price = market_context.get("spy_price")
            ma_200 = market_context.get("ma_200")

            if spy_price and ma_200 and spy_price > ma_200:
                # SPY above 200-day SMA → block the rotation
                return AgentDecision(
                    decision_type=decision.decision_type,
                    action=SignalAction.HOLD,
                    asset_symbol=None,
                    reasoning=f"CANARY GATE: Blocked {current_holding}→{decision.asset_symbol}. SPY ({spy_price:.0f}) > 200-SMA ({ma_200:.0f})",
                    confidence=decision.confidence,
                    strategy_signals=decision.strategy_signals,
                    requires_approval=False,
                    timeout_hours=decision.timeout_hours,
                    urgency=decision.urgency,
                    market_context=market_context,
                    position_size_pct=1.0,
                    regime=decision.regime,
                )

        return decision


# =============================================================================
# Experiment C: Top-3 Diversification
# =============================================================================

def run_top3_backtest(
    prices: pd.DataFrame,
    start_date: date,
    end_date: date,
    initial_capital: float = 10000.0,
    transaction_cost_pct: float = 0.001,
) -> BacktestResult:
    """Custom backtest: hold top 3 momentum assets equally weighted, rebalance monthly."""

    assets = ASSET_REGISTRY
    dm = DualMomentumStrategy(assets=assets)
    rebalance_dates = dm.get_rebalance_dates(start_date, end_date, "monthly")

    cash = Decimal(str(initial_capital))
    # positions: {AssetClass: (shares, symbol)}
    positions: dict[AssetClass, tuple[Decimal, str]] = {}
    trades: list[Trade] = []
    snapshots: list[PortfolioSnapshot] = []

    def get_price(symbol: str, as_of: date) -> float | None:
        sp = prices[prices["symbol"] == symbol].copy()
        if sp.empty:
            return None
        sp["date"] = pd.to_datetime(sp["date"])
        rows = sp[sp["date"] <= pd.Timestamp(as_of)]
        if rows.empty:
            return None
        return float(rows.iloc[-1]["close"])

    total_dates = len(rebalance_dates)
    for i, rebal_date in enumerate(rebalance_dates, 1):
        print(f"\r  [{i}/{total_dates}] {rebal_date}", end="", flush=True)

        # Calculate 12-month momentum scores
        scores = calculate_momentum_scores(
            prices=prices, assets=assets, calc_date=rebal_date,
            lookback_months=12, cash_rate=0.04,
        )
        if not scores:
            continue

        cash_score = scores.get(AssetClass.CASH)
        risky_scores = {k: v for k, v in scores.items() if k != AssetClass.CASH}
        if not risky_scores:
            continue

        # Rank by momentum, pick top 3
        ranked = sorted(risky_scores.items(), key=lambda x: x[1].momentum_12m, reverse=True)
        top3 = []
        for ac, sc in ranked[:3]:
            # Only include if beats cash (absolute momentum filter)
            if cash_score and sc.momentum_12m > cash_score.momentum_12m:
                top3.append(ac)
        if not top3:
            top3_set = set()
        else:
            top3_set = set(top3)

        # Liquidate positions not in top 3
        for ac in list(positions.keys()):
            if ac not in top3_set:
                shares, sym = positions[ac]
                price = get_price(sym, rebal_date)
                if price and shares > 0:
                    sell_value = float(shares) * price
                    commission = sell_value * transaction_cost_pct
                    trades.append(Trade(
                        date=rebal_date, asset=assets[ac],
                        action=SignalAction.SELL, shares=shares,
                        price=price, commission=commission,
                    ))
                    cash += Decimal(str(sell_value - commission))
                del positions[ac]

        # Calculate target allocation per slot
        # First get current portfolio value
        portfolio_value = float(cash)
        for ac, (shares, sym) in positions.items():
            price = get_price(sym, rebal_date)
            if price:
                portfolio_value += float(shares) * price

        if not top3:
            # All in cash
            pass
        else:
            target_per_slot = portfolio_value / len(top3)

            # Rebalance existing and add new positions
            for ac in top3:
                asset = assets[ac]
                sym = asset.yahoo_symbol or asset.symbol
                price = get_price(sym, rebal_date)
                if not price:
                    continue

                current_value = 0.0
                if ac in positions:
                    current_value = float(positions[ac][0]) * price

                diff = target_per_slot - current_value
                if abs(diff) < portfolio_value * 0.02:
                    # Skip rebalance if within 2% tolerance
                    continue

                if diff > 0:
                    # Buy more
                    buy_amount = min(diff, float(cash))
                    if buy_amount < 10:
                        continue
                    commission = buy_amount * transaction_cost_pct
                    net = buy_amount - commission
                    new_shares = Decimal(str(net / price))
                    trades.append(Trade(
                        date=rebal_date, asset=asset,
                        action=SignalAction.BUY, shares=new_shares,
                        price=price, commission=commission,
                    ))
                    existing_shares = positions.get(ac, (Decimal("0"), sym))[0]
                    positions[ac] = (existing_shares + new_shares, sym)
                    cash -= Decimal(str(buy_amount))
                elif diff < 0:
                    # Sell some
                    sell_amount = abs(diff)
                    shares_to_sell = Decimal(str(sell_amount / price))
                    existing_shares = positions.get(ac, (Decimal("0"), sym))[0]
                    shares_to_sell = min(shares_to_sell, existing_shares)
                    if shares_to_sell <= 0:
                        continue
                    commission = float(shares_to_sell) * price * transaction_cost_pct
                    trades.append(Trade(
                        date=rebal_date, asset=asset,
                        action=SignalAction.SELL, shares=shares_to_sell,
                        price=price, commission=commission,
                    ))
                    positions[ac] = (existing_shares - shares_to_sell, sym)
                    cash += Decimal(str(float(shares_to_sell) * price - commission))

        # Record snapshot
        total_value = float(cash)
        for ac, (shares, sym) in positions.items():
            price = get_price(sym, rebal_date)
            if price:
                total_value += float(shares) * price

        snapshots.append(PortfolioSnapshot(
            date=rebal_date, cash=cash,
            positions=[], total_value=Decimal(str(total_value)),
        ))

    print()  # newline after progress

    final_value = float(snapshots[-1].total_value) if snapshots else initial_capital

    # Calculate benchmark
    benchmark_final = None
    spy_df = prices[prices["symbol"] == "SPY"].copy()
    if not spy_df.empty:
        spy_df["date"] = pd.to_datetime(spy_df["date"])
        start_rows = spy_df[spy_df["date"] >= pd.Timestamp(start_date)]
        if not start_rows.empty:
            spy_start = float(start_rows.iloc[0]["close"])
            end_rows = spy_df[spy_df["date"] <= pd.Timestamp(end_date)]
            if not end_rows.empty:
                spy_end = float(end_rows.iloc[-1]["close"])
                benchmark_final = initial_capital * (spy_end / spy_start)

    result = BacktestResult(
        start_date=start_date, end_date=end_date,
        initial_capital=initial_capital, final_value=final_value,
        trades=trades, signals=[], snapshots=snapshots,
        benchmark_final=benchmark_final,
    )
    result.calculate_metrics()
    return result


# =============================================================================
# Experiment D: Composite Momentum (1/3/6/12 month blend)
# =============================================================================

class CompositeMomentumDMStrategy(DualMomentumStrategy):
    """DM using blended 1/3/6/12 month momentum instead of pure 12-month."""

    def generate_signal(self, prices, calc_date, current_holding=None):
        if current_holding is not None:
            self.current_holding = current_holding

        # Calculate momentum at 4 timeframes and blend
        scores_1m = calculate_momentum_scores(
            prices=prices, assets=self.assets, calc_date=calc_date,
            lookback_months=1, cash_rate=self.cash_rate,
        )
        scores_3m = calculate_momentum_scores(
            prices=prices, assets=self.assets, calc_date=calc_date,
            lookback_months=3, cash_rate=self.cash_rate,
        )
        scores_6m = calculate_momentum_scores(
            prices=prices, assets=self.assets, calc_date=calc_date,
            lookback_months=6, cash_rate=self.cash_rate,
        )
        scores_12m = calculate_momentum_scores(
            prices=prices, assets=self.assets, calc_date=calc_date,
            lookback_months=12, cash_rate=self.cash_rate,
        )

        if not scores_12m:
            return Signal(
                date=calc_date, action=SignalAction.HOLD,
                asset=self.assets.get(self.current_holding or AssetClass.CASH),
                reason="No momentum data available",
                momentum_scores=scores_12m,
            )

        # Blend: 25% each of 1m, 3m, 6m, 12m
        # Create synthetic MomentumScore objects with blended momentum_12m values
        blended_scores: dict[AssetClass, MomentumScore] = {}
        for ac in scores_12m:
            m1 = scores_1m.get(ac)
            m3 = scores_3m.get(ac)
            m6 = scores_6m.get(ac)
            m12 = scores_12m[ac]

            # Use whichever timeframes are available
            moms = []
            for s in [m1, m3, m6, m12]:
                if s is not None:
                    moms.append(s.momentum_12m)
            blended_mom = sum(moms) / len(moms) if moms else m12.momentum_12m

            blended_scores[ac] = MomentumScore(
                asset=m12.asset,
                date=calc_date,
                momentum_12m=blended_mom,  # Store blended value in momentum_12m field
                price=m12.price,
                price_12m_ago=m12.price_12m_ago,
            )

        # Now run standard DM logic using blended scores
        scores = blended_scores

        risky_scores = {k: v for k, v in scores.items() if k != AssetClass.CASH}
        winner_class = max(risky_scores.keys(), key=lambda k: risky_scores[k].momentum_12m) if risky_scores else AssetClass.CASH
        winner_score = scores[winner_class]
        cash_score = scores.get(AssetClass.CASH)

        # No current position
        if self.current_holding is None:
            if cash_score and winner_score.momentum_12m <= cash_score.momentum_12m:
                return Signal(
                    date=calc_date, action=SignalAction.BUY,
                    asset=self.assets[AssetClass.CASH],
                    reason=f"Winner below cash (composite)", momentum_scores=scores,
                )
            self.current_holding = winner_class
            self.is_pilot_position = False
            return Signal(
                date=calc_date, action=SignalAction.BUY,
                asset=winner_score.asset,
                reason=f"Initial buy: {winner_class.value} composite momentum ({winner_score.momentum_12m:.2%})",
                momentum_scores=scores,
            )

        current_score = scores.get(self.current_holding)
        if current_score is None:
            self.current_holding = winner_class
            self.is_pilot_position = False
            return Signal(
                date=calc_date, action=SignalAction.BUY,
                asset=winner_score.asset,
                reason=f"No data for current, switching to {winner_class.value}",
                momentum_scores=scores,
            )

        # Absolute momentum check
        if cash_score and current_score.momentum_12m < cash_score.momentum_12m:
            if winner_score.momentum_12m < cash_score.momentum_12m:
                self.current_holding = AssetClass.CASH
                self.is_pilot_position = False
                return Signal(
                    date=calc_date, action=SignalAction.BUY,
                    asset=self.assets[AssetClass.CASH],
                    reason=f"All below cash (composite)", momentum_scores=scores,
                )

        # Switch threshold check
        momentum_diff = winner_score.momentum_12m - current_score.momentum_12m
        if winner_class != self.current_holding and momentum_diff > self.switch_threshold:
            old = self.current_holding
            self.current_holding = winner_class
            self.is_pilot_position = False
            return Signal(
                date=calc_date, action=SignalAction.BUY,
                asset=winner_score.asset,
                reason=f"COMPOSITE SWITCH: {old.value}→{winner_class.value} diff {momentum_diff:.2%}",
                momentum_scores=scores,
            )

        return Signal(
            date=calc_date, action=SignalAction.HOLD,
            asset=current_score.asset,
            reason=f"Holding {self.current_holding.value} (composite momentum {current_score.momentum_12m:.2%})",
            momentum_scores=scores,
        )


# =============================================================================
# Experiment Runner
# =============================================================================

def fetch_prices(end_date: date) -> pd.DataFrame:
    """Fetch 10+ years of price data for all assets."""
    provider = YahooFinanceProvider()
    symbols = get_all_yahoo_symbols()
    if "SPY" not in symbols:
        symbols.append("SPY")

    extended_start = end_date - timedelta(days=10 * 365 + 600)
    print(f"Fetching prices for {len(symbols)} symbols...")
    prices = provider.get_multi_prices(symbols, extended_start, end_date + timedelta(days=5))
    print(f"Total: {len(prices)} price records\n")
    return prices


def run_baseline(prices: pd.DataFrame, start_date: date, end_date: date, capital: float) -> BacktestResult:
    """Run baseline backtest (current production strategy)."""
    engine = BacktestEngine(initial_capital=capital, use_ai=False)
    return engine.run(prices=prices, start_date=start_date, end_date=end_date, benchmark_symbol="SPY")


def run_experiment_a(prices: pd.DataFrame, start_date: date, end_date: date, capital: float) -> BacktestResult:
    """Experiment A: Asymmetric switch thresholds."""
    engine = BacktestEngine(initial_capital=capital, use_ai=False)
    engine.dual_momentum = AsymmetricDMStrategy(assets=ASSET_REGISTRY)
    return engine.run(prices=prices, start_date=start_date, end_date=end_date, benchmark_symbol="SPY")


def run_experiment_b(prices: pd.DataFrame, start_date: date, end_date: date, capital: float) -> BacktestResult:
    """Experiment B: Canary gate before leaving equities."""
    engine = BacktestEngine(initial_capital=capital, use_ai=False)
    engine.orchestrator = CanaryGateOrchestrator()
    return engine.run(prices=prices, start_date=start_date, end_date=end_date, benchmark_symbol="SPY")


def run_experiment_c(prices: pd.DataFrame, start_date: date, end_date: date, capital: float) -> BacktestResult:
    """Experiment C: Top-3 diversification."""
    return run_top3_backtest(prices, start_date, end_date, initial_capital=capital)


def run_experiment_d(prices: pd.DataFrame, start_date: date, end_date: date, capital: float) -> BacktestResult:
    """Experiment D: Composite momentum (1/3/6/12 month blend)."""
    engine = BacktestEngine(initial_capital=capital, use_ai=False)
    engine.dual_momentum = CompositeMomentumDMStrategy(
        assets=ASSET_REGISTRY, pilot_entry_enabled=False,
    )
    return engine.run(prices=prices, start_date=start_date, end_date=end_date, benchmark_symbol="SPY")


EXPERIMENTS = {
    "baseline": ("Baseline (current production)", run_baseline),
    "A": ("Asymmetric Switch Thresholds (equity bias)", run_experiment_a),
    "B": ("Canary Gate (SPY > 200-SMA blocks exit)", run_experiment_b),
    "C": ("Top-3 Diversification (equal-weight)", run_experiment_c),
    "D": ("Composite Momentum (1/3/6/12m blend)", run_experiment_d),
}


def result_to_json(result: BacktestResult, prices: pd.DataFrame, capital: float) -> dict:
    """Convert BacktestResult to dashboard JSON format."""
    start_date = result.start_date
    end_date = result.end_date

    portfolio_data = [
        {"date": s.date.isoformat(), "value": round(float(s.total_value), 0)}
        for s in result.snapshots
    ]

    # Build benchmark series
    benchmark_data = []
    spy_df = prices[prices["symbol"] == "SPY"].copy()
    if not spy_df.empty:
        spy_df["date"] = pd.to_datetime(spy_df["date"])
        spy_df = spy_df.sort_values("date")
        start_rows = spy_df[spy_df["date"] >= pd.Timestamp(start_date)]
        if not start_rows.empty:
            spy_start_p = float(start_rows.iloc[0]["close"])
            for snap in result.snapshots:
                row = spy_df[spy_df["date"] <= pd.Timestamp(snap.date)]
                if not row.empty:
                    spy_p = float(row.iloc[-1]["close"])
                    benchmark_data.append({
                        "date": snap.date.isoformat(),
                        "value": round(capital * (spy_p / spy_start_p), 0),
                    })

    bench_return = ((result.benchmark_final / capital) - 1) * 100 if result.benchmark_final else 0
    bench_years = (end_date - start_date).days / 365.25
    bench_cagr = ((result.benchmark_final / capital) ** (1 / bench_years) - 1) * 100 if result.benchmark_final and bench_years > 0 else 0

    return {
        "metrics": {
            "total_return": round(result.total_return * 100, 1),
            "cagr": round(result.cagr * 100, 1),
            "max_drawdown": round(result.max_drawdown * 100, 1),
            "sharpe_ratio": round(result.sharpe_ratio, 2),
            "num_trades": result.num_trades,
            "benchmark_return": round(bench_return, 1),
            "benchmark_cagr": round(bench_cagr, 1),
            "alpha": round(result.total_return * 100 - bench_return, 1),
        },
        "portfolio": portfolio_data,
        "benchmark": benchmark_data,
    }


def main():
    parser = argparse.ArgumentParser(description="Run backtest strategy experiments")
    parser.add_argument("--experiment", "-e", required=True,
                        choices=list(EXPERIMENTS.keys()),
                        help="Experiment to run: A, B, C, D, or baseline")
    parser.add_argument("--output", "-o", default="data/backtest_comparison.json",
                        help="Output JSON file (default: data/backtest_comparison.json)")
    args = parser.parse_args()

    exp_name, exp_fn = EXPERIMENTS[args.experiment]
    end_date = date.today()
    capital = 10000

    print(f"=" * 60)
    print(f"EXPERIMENT: {args.experiment} — {exp_name}")
    print(f"=" * 60)
    print()

    prices = fetch_prices(end_date)
    if prices.empty:
        print("ERROR: No price data available")
        sys.exit(1)

    results = {}
    for label, years in [("10y", 10), ("5y", 5)]:
        start = end_date - timedelta(days=years * 365)
        print(f"Running {label} backtest ({start} → {end_date})...")

        result = exp_fn(prices, start, end_date, capital)
        results[label] = result_to_json(result, prices, capital)
        result.print_summary()

    # Save
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2))
    print(f"\nSaved to {out.resolve()}")
    print(f"\nTo deploy: scp {out} root@46.225.75.110:/tmp/ && ssh root@46.225.75.110 'docker cp /tmp/{out.name} aurel2-trading-aurel2-1:/opt/aurel2/data/'")


if __name__ == "__main__":
    main()
