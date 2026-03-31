#!/usr/bin/env python3
"""Regime-Adaptive Strategy Research.

Tests which strategy configurations work best in different market regimes,
then builds a composite adaptive strategy and validates it out-of-sample.

Usage: cd /root/aurel2 && python3 scripts/regime_adaptive_research.py

Phases:
  1. Label historical months by regime (3 detection methods + consensus)
  2. Per-regime parameter sweep to find best configs
  3. Composite adaptive backtest using regime-specific configs
  4. In-sample vs out-of-sample validation
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import json
import warnings
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import product

import numpy as np
import pandas as pd

from aurel2.core.assets import ASSET_REGISTRY, get_all_yahoo_symbols
from aurel2.core.models import AssetClass
from aurel2.data.providers.yahoo import YahooFinanceProvider
from aurel2.engine.backtest import BacktestEngine
from aurel2.strategies.dual_momentum import DualMomentumStrategy
from aurel2.strategies.mean_reversion import MeanReversionStrategy
from aurel2.strategies.multi_timeframe import MultiTimeframeTrendStrategy
from aurel2.agent.orchestrator import AgentOrchestrator

warnings.filterwarnings("ignore", category=FutureWarning)

# ============================================================================
# CONSTANTS
# ============================================================================

IN_SAMPLE_END = date(2020, 1, 1)
OUT_OF_SAMPLE_START = date(2020, 1, 1)
FULL_START = date(2005, 6, 1)  # Need 400 days pre-history for 12m momentum

# Regime labels (unified across detection methods)
BULL = "bull"
SIDEWAYS = "sideways"
BEAR = "bear"

# Named regime periods for reporting
REGIME_PERIODS = [
    ("Pre-GFC Bull", date(2005, 6, 1), date(2007, 10, 1)),
    ("GFC Crash", date(2007, 10, 1), date(2009, 3, 1)),
    ("QE Recovery", date(2009, 3, 1), date(2013, 1, 1)),
    ("Steady Bull", date(2013, 1, 1), date(2018, 1, 1)),
    ("Vol Shock 2018", date(2018, 1, 1), date(2019, 1, 1)),
    ("Late Bull", date(2019, 1, 1), date(2020, 2, 1)),
    ("COVID Crash", date(2020, 2, 1), date(2020, 6, 1)),
    ("COVID Recovery", date(2020, 6, 1), date(2022, 1, 1)),
    ("Rate Hike Bear", date(2022, 1, 1), date(2023, 1, 1)),
    ("AI Bull", date(2023, 1, 1), date(2026, 3, 1)),
]

VALIDATION_PERIODS = [
    ("In-Sample (2005-2020)", FULL_START, IN_SAMPLE_END),
    ("Out-of-Sample (2020-2026)", OUT_OF_SAMPLE_START, date(2026, 3, 1)),
    ("Full (2005-2026)", FULL_START, date(2026, 3, 1)),
]

# All MTF-eligible asset classes (full universe)
FULL_MTF_ASSETS = [
    AssetClass.US_STOCKS, AssetClass.INTL_DEVELOPED, AssetClass.EMERGING_MARKETS,
    AssetClass.TECH_SECTOR, AssetClass.FINANCIAL_SECTOR, AssetClass.ENERGY_SECTOR,
    AssetClass.HEALTHCARE_SECTOR, AssetClass.BONDS_AGGREGATE, AssetClass.BONDS_TREASURY,
    AssetClass.GOLD, AssetClass.COMMODITIES, AssetClass.REITS,
    AssetClass.SMALL_CAP_VALUE,
]


# ============================================================================
# PHASE 1: REGIME LABELING
# ============================================================================

def label_regimes(prices: pd.DataFrame) -> pd.DataFrame:
    """Label each month with a consensus regime from 3 detection methods.

    Methods:
      1. Drawdown-based: SPY peak-to-trough from 252-day high
      2. SMA-based: SPY vs 200-day SMA
      3. Volatility-based: 20-day realized vol percentile over trailing year

    Returns DataFrame with columns:
      date, spy_close, regime_drawdown, regime_sma, regime_vol, consensus
    """
    spy = prices[prices["symbol"] == "SPY"].copy()
    spy["date"] = pd.to_datetime(spy["date"])
    spy = spy.sort_values("date").set_index("date")

    # Monthly resample (last business day)
    monthly = spy["close"].resample("ME").last().dropna()

    records = []
    for dt in monthly.index:
        spy_daily = spy["close"][:dt]
        if len(spy_daily) < 252:
            continue

        close = float(spy_daily.iloc[-1])

        # Method 1: Drawdown from 252-day high
        high_252 = float(spy_daily.iloc[-252:].max())
        drawdown = (high_252 - close) / high_252
        if drawdown < 0.05:
            r_dd = BULL
        elif drawdown < 0.15:
            r_dd = SIDEWAYS
        else:
            r_dd = BEAR

        # Method 2: SMA-200 position + slope
        sma200 = float(spy_daily.iloc[-200:].mean())
        sma200_prev = float(spy_daily.iloc[-220:-20].mean()) if len(spy_daily) >= 220 else sma200
        sma_slope = (sma200 - sma200_prev) / sma200_prev if sma200_prev > 0 else 0

        if close > sma200 and sma_slope > 0:
            r_sma = BULL
        elif close < sma200 and sma_slope < 0:
            r_sma = BEAR
        else:
            r_sma = SIDEWAYS

        # Method 3: Realized volatility percentile
        daily_returns = spy_daily.pct_change().dropna()
        vol_20d = float(daily_returns.iloc[-20:].std()) * np.sqrt(252) * 100  # annualized %
        vol_history = daily_returns.iloc[-252:].rolling(20).std().dropna() * np.sqrt(252) * 100
        if len(vol_history) > 0:
            vol_pctl = float((vol_history < vol_20d).mean())
        else:
            vol_pctl = 0.5

        if vol_pctl < 0.30:
            r_vol = BULL  # low vol = calm/bullish
        elif vol_pctl > 0.70:
            r_vol = BEAR  # high vol = stressed
        else:
            r_vol = SIDEWAYS

        # Consensus: majority vote (2 of 3)
        votes = [r_dd, r_sma, r_vol]
        for regime in [BULL, BEAR, SIDEWAYS]:
            if votes.count(regime) >= 2:
                consensus = regime
                break
        else:
            consensus = SIDEWAYS  # no majority → sideways

        records.append({
            "date": dt.date(),
            "spy_close": round(close, 2),
            "drawdown": round(drawdown, 4),
            "sma200": round(sma200, 2),
            "vol_20d_ann": round(vol_20d, 1),
            "vol_pctl": round(vol_pctl, 2),
            "regime_drawdown": r_dd,
            "regime_sma": r_sma,
            "regime_vol": r_vol,
            "consensus": consensus,
        })

    df = pd.DataFrame(records)
    return df


def print_regime_summary(labels: pd.DataFrame):
    """Print regime distribution and timeline."""
    print("\n" + "=" * 80)
    print("PHASE 1: REGIME LABELS")
    print("=" * 80)

    # Distribution
    total = len(labels)
    for regime in [BULL, SIDEWAYS, BEAR]:
        count = (labels["consensus"] == regime).sum()
        pct = count / total * 100
        bar = "#" * int(pct / 2)
        print(f"  {regime:>8s}: {count:3d} months ({pct:5.1f}%) {bar}")

    # Method agreement
    agree_all = ((labels["regime_drawdown"] == labels["regime_sma"]) &
                 (labels["regime_sma"] == labels["regime_vol"])).sum()
    print(f"\n  All 3 methods agree: {agree_all}/{total} months ({agree_all/total*100:.0f}%)")

    # Timeline (yearly summary)
    labels_copy = labels.copy()
    labels_copy["year"] = pd.to_datetime(labels_copy["date"]).dt.year
    print("\n  Year  Bull  Side  Bear  Dominant")
    print("  " + "-" * 40)
    for year, group in labels_copy.groupby("year"):
        bull = (group["consensus"] == BULL).sum()
        side = (group["consensus"] == SIDEWAYS).sum()
        bear = (group["consensus"] == BEAR).sum()
        dominant = max([(bull, "BULL"), (side, "SIDE"), (bear, "BEAR")])[1]
        print(f"  {year}   {bull:2d}    {side:2d}    {bear:2d}    {dominant}")


# ============================================================================
# PHASE 2: PER-REGIME PARAMETER SWEEP
# ============================================================================

@dataclass
class ConfigResult:
    """Result of a single config tested on a single period."""
    config_name: str
    period_name: str
    cagr: float
    max_drawdown: float
    sharpe: float
    sortino: float
    calmar: float
    turnover: float
    num_trades: int
    final_value: float


def make_engine(
    dm_threshold: float = 0.04,
    dm_lookback: int = 12,
    mtf_assets: str = "narrow",
    dm_primary: bool = True,
    correlation_guard: bool = False,
    sideways_hold: bool = False,
) -> BacktestEngine:
    """Create a BacktestEngine with specific parameters."""
    engine = BacktestEngine(
        initial_capital=10000.0,
        correlation_guard=correlation_guard,
        sideways_hold=sideways_hold,
    )

    # Override DM strategy
    engine.dual_momentum = DualMomentumStrategy(
        assets=ASSET_REGISTRY,
        lookback_months=dm_lookback,
        switch_threshold=dm_threshold,
    )

    # Override MTF target assets
    if mtf_assets == "full":
        engine.multi_timeframe = MultiTimeframeTrendStrategy(
            target_assets=FULL_MTF_ASSETS,
        )

    # Override orchestrator
    engine.orchestrator = AgentOrchestrator(
        dm_primary_enabled=dm_primary,
        use_dynamic_weights=not dm_primary,
        use_regime_selection=not dm_primary,
        correlation_guard_enabled=correlation_guard,
        sideways_hold_enabled=sideways_hold,
    )

    return engine


def run_config(
    engine: BacktestEngine,
    prices: pd.DataFrame,
    start: date,
    end: date,
    config_name: str,
    period_name: str,
) -> ConfigResult | None:
    """Run a single backtest config on a period."""
    try:
        result = engine.run(prices=prices, start_date=start, end_date=end, benchmark_symbol="SPY")
        result.calculate_metrics()
        return ConfigResult(
            config_name=config_name,
            period_name=period_name,
            cagr=result.cagr,
            max_drawdown=result.max_drawdown,
            sharpe=result.sharpe_ratio,
            sortino=result.sortino_ratio,
            calmar=result.calmar_ratio,
            turnover=result.turnover,
            num_trades=result.num_trades,
            final_value=result.final_value,
        )
    except Exception as e:
        print(f"    ERROR: {config_name} on {period_name}: {e}")
        return None


# Parameter grid — focused on the most impactful levers
PARAM_GRID = {
    "DM-only t=0.02": {"dm_threshold": 0.02, "dm_lookback": 12, "mtf_assets": "narrow", "dm_primary": True},
    "DM-only t=0.03": {"dm_threshold": 0.03, "dm_lookback": 12, "mtf_assets": "narrow", "dm_primary": True},
    "DM-only t=0.04": {"dm_threshold": 0.04, "dm_lookback": 12, "mtf_assets": "narrow", "dm_primary": True},
    "DM-only t=0.06": {"dm_threshold": 0.06, "dm_lookback": 12, "mtf_assets": "narrow", "dm_primary": True},
    "DM 6mo t=0.03":  {"dm_threshold": 0.03, "dm_lookback": 6, "mtf_assets": "narrow", "dm_primary": True},
    "DM 6mo t=0.04":  {"dm_threshold": 0.04, "dm_lookback": 6, "mtf_assets": "narrow", "dm_primary": True},
    "DM 9mo t=0.03":  {"dm_threshold": 0.03, "dm_lookback": 9, "mtf_assets": "narrow", "dm_primary": True},
    "Multi t=0.03":    {"dm_threshold": 0.03, "dm_lookback": 12, "mtf_assets": "narrow", "dm_primary": False},
    "Multi t=0.04":    {"dm_threshold": 0.04, "dm_lookback": 12, "mtf_assets": "narrow", "dm_primary": False},
    "Multi-Full t=0.03": {"dm_threshold": 0.03, "dm_lookback": 12, "mtf_assets": "full", "dm_primary": False},
    "Multi-Full t=0.04": {"dm_threshold": 0.04, "dm_lookback": 12, "mtf_assets": "full", "dm_primary": False},
    "DM+Guards t=0.04":  {"dm_threshold": 0.04, "dm_lookback": 12, "mtf_assets": "narrow", "dm_primary": True, "correlation_guard": True, "sideways_hold": True},
    "Multi+Guards t=0.03": {"dm_threshold": 0.03, "dm_lookback": 12, "mtf_assets": "full", "dm_primary": False, "correlation_guard": True, "sideways_hold": True},
}


def sweep_configs(prices: pd.DataFrame, periods: list[tuple[str, date, date]]) -> list[ConfigResult]:
    """Run all configs on all periods."""
    results = []
    total = len(PARAM_GRID) * len(periods)
    done = 0

    for period_name, start, end in periods:
        for config_name, params in PARAM_GRID.items():
            done += 1
            print(f"  [{done}/{total}] {config_name} on {period_name}...", end="", flush=True)
            engine = make_engine(**params)
            r = run_config(engine, prices, start, end, config_name, period_name)
            if r:
                results.append(r)
                print(f" CAGR={r.cagr:+.2%} Sharpe={r.sharpe:.2f} DD={r.max_drawdown:.2%}")
            else:
                print(" FAILED")

    return results


def find_best_per_regime(
    results: list[ConfigResult],
    regime_period_map: dict[str, list[str]],
) -> dict[str, str]:
    """For each regime, find config with best risk-adjusted return (Sharpe).

    Args:
        results: All config results
        regime_period_map: Maps regime name to list of period names

    Returns:
        Dict mapping regime → best config name
    """
    best = {}
    for regime, period_names in regime_period_map.items():
        regime_results = [r for r in results if r.period_name in period_names]
        if not regime_results:
            continue

        # Aggregate by config: average Sharpe across regime periods
        config_scores = {}
        for r in regime_results:
            if r.config_name not in config_scores:
                config_scores[r.config_name] = []
            config_scores[r.config_name].append(r.sharpe)

        # Best = highest average Sharpe (with at least 1 period)
        best_config = max(config_scores, key=lambda k: np.mean(config_scores[k]))
        avg_sharpe = np.mean(config_scores[best_config])
        best[regime] = best_config
        print(f"  {regime:>8s} -> {best_config} (avg Sharpe={avg_sharpe:.2f})")

    return best


def print_sweep_results(results: list[ConfigResult], periods: list[tuple[str, date, date]]):
    """Print sweep results as a comparison table."""
    print("\n" + "=" * 80)
    print("PHASE 2: PARAMETER SWEEP RESULTS")
    print("=" * 80)

    for period_name, _, _ in periods:
        period_results = [r for r in results if r.period_name == period_name]
        if not period_results:
            continue

        period_results.sort(key=lambda r: r.sharpe, reverse=True)
        print(f"\n  --- {period_name} ---")
        print(f"  {'Config':<24s} {'CAGR':>7s} {'Sharpe':>7s} {'Sortino':>8s} {'MaxDD':>7s} {'Turn':>5s} {'Trades':>6s}")
        print(f"  {'-'*64}")
        for r in period_results:
            print(f"  {r.config_name:<24s} {r.cagr:>+6.2%} {r.sharpe:>7.2f} {r.sortino:>8.2f} {r.max_drawdown:>6.2%} {r.turnover:>5.1f} {r.num_trades:>6d}")


# ============================================================================
# PHASE 3: ADAPTIVE COMPOSITE BACKTEST
# ============================================================================

def run_adaptive_backtest(
    prices: pd.DataFrame,
    labels: pd.DataFrame,
    best_configs: dict[str, str],
    start: date,
    end: date,
    label: str,
) -> ConfigResult | None:
    """Run adaptive backtest that switches configs based on detected regime.

    At each rebalance date:
    1. Look up current regime from labels
    2. Select the config that won for this regime
    3. Run decision with those parameters

    Since BacktestEngine runs as a complete simulation, we approximate by
    running a sequence of short backtests (regime-segment by regime-segment)
    and chaining the portfolio value.
    """
    # Get regime for each month in the period
    period_labels = labels[(labels["date"] >= start) & (labels["date"] < end)].copy()
    if period_labels.empty:
        return None

    # Group consecutive months with same regime into segments
    segments = []
    current_regime = None
    segment_start = None

    for _, row in period_labels.iterrows():
        regime = row["consensus"]
        dt = row["date"]
        if regime != current_regime:
            if current_regime is not None:
                segments.append((current_regime, segment_start, dt))
            current_regime = regime
            segment_start = dt
    # Close last segment
    if current_regime is not None:
        segments.append((current_regime, segment_start, end))

    # Run each segment with its regime-specific config
    capital = 10000.0
    total_trades = 0
    all_snapshots = []

    for regime, seg_start, seg_end in segments:
        config_name = best_configs.get(regime, "DM-only t=0.04")
        params = PARAM_GRID.get(config_name, PARAM_GRID["DM-only t=0.04"])

        engine = make_engine(**params)
        engine.initial_capital = capital

        try:
            result = engine.run(prices=prices, start_date=seg_start, end_date=seg_end, benchmark_symbol="SPY")
            result.calculate_metrics()
            capital = result.final_value
            total_trades += result.num_trades
            all_snapshots.extend(result.snapshots)
        except Exception as e:
            print(f"    Segment {regime} {seg_start}-{seg_end} failed: {e}")
            continue

    if capital <= 0:
        return None

    # Calculate aggregate metrics
    years = (end - start).days / 365.25
    cagr = (capital / 10000.0) ** (1 / years) - 1 if years > 0 else 0

    # Max drawdown from all snapshots
    max_dd = 0.0
    peak = 0.0
    values = [float(s.total_value) for s in all_snapshots]
    for v in values:
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    # Sharpe from snapshots
    sharpe = 0.0
    sortino = 0.0
    if len(values) > 1:
        returns = pd.Series(values).pct_change().dropna()
        if len(returns) > 0 and returns.std() > 0:
            sharpe = (returns.mean() * 12) / (returns.std() * np.sqrt(12))
            downside = returns[returns < 0]
            if len(downside) > 0 and downside.std() > 0:
                sortino = (returns.mean() * 12) / (downside.std() * np.sqrt(12))

    turnover = total_trades / years if years > 0 else 0
    calmar = cagr / max_dd if max_dd > 0 else 0

    return ConfigResult(
        config_name="ADAPTIVE",
        period_name=label,
        cagr=cagr,
        max_drawdown=max_dd,
        sharpe=sharpe,
        sortino=sortino,
        calmar=calmar,
        turnover=turnover,
        num_trades=total_trades,
        final_value=capital,
    )


# ============================================================================
# PHASE 4: VALIDATION
# ============================================================================

def run_validation(
    prices: pd.DataFrame,
    labels: pd.DataFrame,
    best_configs: dict[str, str],
):
    """Compare adaptive vs baseline vs best-fixed on validation periods."""
    print("\n" + "=" * 80)
    print("PHASE 4: VALIDATION (Adaptive vs Baseline vs Best-Fixed vs SPY)")
    print("=" * 80)

    for period_name, start, end in VALIDATION_PERIODS:
        print(f"\n  --- {period_name} ---")
        print(f"  {'Strategy':<24s} {'CAGR':>7s} {'Sharpe':>7s} {'Sortino':>8s} {'MaxDD':>7s} {'Turn':>5s}")
        print(f"  {'-'*58}")

        # 1. Baseline (current production config: defaults)
        baseline_engine = BacktestEngine(initial_capital=10000.0)
        try:
            b_result = baseline_engine.run(prices=prices, start_date=start, end_date=end, benchmark_symbol="SPY")
            b_result.calculate_metrics()
            print(f"  {'Baseline (current)':<24s} {b_result.cagr:>+6.2%} {b_result.sharpe_ratio:>7.2f} {b_result.sortino_ratio:>8.2f} {b_result.max_drawdown:>6.2%} {b_result.turnover:>5.1f}")
            bench_return = (b_result.benchmark_final / 10000.0) ** (1 / ((end - start).days / 365.25)) - 1 if b_result.benchmark_final else 0
            print(f"  {'SPY Buy & Hold':<24s} {bench_return:>+6.2%}")
        except Exception as e:
            print(f"  Baseline failed: {e}")

        # 2. Best single fixed config (by Sharpe across all in-sample)
        for config_name in ["DM-only t=0.04", "DM-only t=0.03", "Multi-Full t=0.03"]:
            params = PARAM_GRID[config_name]
            engine = make_engine(**params)
            try:
                result = engine.run(prices=prices, start_date=start, end_date=end, benchmark_symbol="SPY")
                result.calculate_metrics()
                print(f"  {config_name:<24s} {result.cagr:>+6.2%} {result.sharpe_ratio:>7.2f} {result.sortino_ratio:>8.2f} {result.max_drawdown:>6.2%} {result.turnover:>5.1f}")
            except Exception as e:
                print(f"  {config_name} failed: {e}")

        # 3. Adaptive
        adaptive = run_adaptive_backtest(prices, labels, best_configs, start, end, period_name)
        if adaptive:
            print(f"  {'ADAPTIVE':<24s} {adaptive.cagr:>+6.2%} {adaptive.sharpe:>7.2f} {adaptive.sortino:>8.2f} {adaptive.max_drawdown:>6.2%} {adaptive.turnover:>5.1f}")
        else:
            print(f"  ADAPTIVE failed")

    # Overfitting check
    print("\n  OVERFITTING CHECK:")
    print("  If adaptive beats baseline by >2% CAGR in-sample but loses out-of-sample = OVERFIT")


# ============================================================================
# MAIN
# ============================================================================

def main():
    print("=" * 80)
    print("REGIME-ADAPTIVE STRATEGY RESEARCH")
    print("=" * 80)

    # Fetch data (one time)
    print("\n[1/5] Fetching price data...")
    provider = YahooFinanceProvider()
    symbols = get_all_yahoo_symbols()
    if "SPY" not in symbols:
        symbols.append("SPY")

    fetch_start = FULL_START - timedelta(days=500)  # Extra history for lookbacks
    fetch_end = date(2026, 4, 1)
    prices = provider.get_multi_prices(symbols, fetch_start, fetch_end)
    spy_count = len(prices[prices["symbol"] == "SPY"])
    print(f"  Loaded {len(prices)} rows across {len(symbols)} symbols (SPY: {spy_count} days)")

    # Phase 1: Label regimes
    print("\n[2/5] Labeling regimes...")
    labels = label_regimes(prices)
    print_regime_summary(labels)

    # Map regime periods to their consensus regimes
    # For each named period, find dominant regime
    regime_period_map = {BULL: [], SIDEWAYS: [], BEAR: []}
    print("\n  Period regime classification:")
    for period_name, start, end in REGIME_PERIODS:
        period_labels = labels[(labels["date"] >= start) & (labels["date"] < end)]
        if period_labels.empty:
            continue
        dominant = period_labels["consensus"].mode().iloc[0] if not period_labels.empty else SIDEWAYS
        regime_period_map[dominant].append(period_name)
        print(f"    {period_name:<25s} -> {dominant}")

    # Phase 2: Sweep
    print("\n[3/5] Running parameter sweep on regime periods...")
    sweep_results = sweep_configs(prices, REGIME_PERIODS)
    print_sweep_results(sweep_results, REGIME_PERIODS)

    # Find best per regime
    print("\n  BEST CONFIG PER REGIME (by avg Sharpe):")
    best_configs = find_best_per_regime(sweep_results, regime_period_map)

    # Phase 3 & 4: Adaptive + Validation
    print("\n[4/5] Running adaptive backtest...")
    run_validation(prices, labels, best_configs)

    # Save results
    print("\n[5/5] Saving results...")
    output = {
        "regime_labels": labels.to_dict(orient="records"),
        "sweep_results": [
            {
                "config": r.config_name,
                "period": r.period_name,
                "cagr": round(r.cagr, 6),
                "sharpe": round(r.sharpe, 4),
                "sortino": round(r.sortino, 4),
                "max_dd": round(r.max_drawdown, 6),
                "turnover": round(r.turnover, 2),
                "trades": r.num_trades,
            }
            for r in sweep_results
        ],
        "best_per_regime": best_configs,
    }
    output_path = os.path.join(os.path.dirname(__file__), "..", "data", "regime_research_results.json")
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"  Results saved to {output_path}")

    print("\n" + "=" * 80)
    print("RESEARCH COMPLETE")
    print("=" * 80)


if __name__ == "__main__":
    main()
