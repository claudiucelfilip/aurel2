#!/usr/bin/env python3
"""VIX Correlation Research.

Tests whether VIX levels/changes correlate with good/bad Aurel2 decisions,
and whether a VIX filter could improve strategy performance.

Usage: cd /root/aurel2 && python3 scripts/vix_correlation_research.py

Phases:
  1. Run baseline backtest, extract every decision with its outcome
  2. Align VIX data (level, 5d change, percentile) to each decision date
  3. Correlate VIX metrics with decision quality (1m/3m/6m forward returns)
  4. Design and backtest candidate VIX filters
  5. Compare filtered vs unfiltered across validation periods
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import json
import warnings
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from aurel2.core.assets import ASSET_REGISTRY, get_all_yahoo_symbols
from aurel2.data.providers.yahoo import YahooFinanceProvider
from aurel2.engine.backtest import BacktestEngine

warnings.filterwarnings("ignore", category=FutureWarning)

# ============================================================================
# CONSTANTS
# ============================================================================

FULL_START = date(2005, 6, 1)
FULL_END = date(2026, 3, 1)
IN_SAMPLE_END = date(2020, 1, 1)

VALIDATION_PERIODS = [
    ("In-Sample (2005-2020)", FULL_START, IN_SAMPLE_END),
    ("Out-of-Sample (2020-2026)", IN_SAMPLE_END, FULL_END),
    ("Full (2005-2026)", FULL_START, FULL_END),
]


# ============================================================================
# PHASE 1: EXTRACT DECISIONS FROM BASELINE BACKTEST
# ============================================================================

@dataclass
class Decision:
    """A single rebalance decision with its outcome."""
    date: date
    action: str          # "hold", "switch"
    from_asset: str | None
    to_asset: str | None
    portfolio_value: float
    # Forward returns (filled later)
    fwd_1m_return: float = 0.0
    fwd_3m_return: float = 0.0
    fwd_6m_return: float = 0.0
    # VIX context (filled later)
    vix_level: float = 0.0
    vix_5d_change: float = 0.0
    vix_20d_change: float = 0.0
    vix_percentile: float = 0.0  # percentile over trailing 1y


def extract_decisions(result) -> list[Decision]:
    """Extract decisions from backtest trades and snapshots.

    Uses BUY trades to detect switches between assets. Between BUY trades,
    the strategy is holding the last-bought asset.
    """
    decisions = []
    snapshots = result.snapshots
    trades = result.trades
    if len(snapshots) < 2:
        return decisions

    # Build a map of date → asset bought (from BUY trades)
    buy_by_date = {}
    for t in trades:
        if t.action.value == "buy":
            buy_by_date[t.date] = t.asset.symbol

    # Track held asset through time
    held_asset = None

    for i in range(1, len(snapshots)):
        curr = snapshots[i]
        rebal_date = curr.date

        bought = buy_by_date.get(rebal_date)

        if bought and bought != held_asset:
            action = "switch"
            from_asset = held_asset
            to_asset = bought
            held_asset = bought
        else:
            action = "hold"
            from_asset = held_asset
            to_asset = held_asset
            if bought:
                held_asset = bought  # re-buy same asset (DCA etc)

        decisions.append(Decision(
            date=rebal_date,
            action=action,
            from_asset=from_asset,
            to_asset=to_asset,
            portfolio_value=float(curr.total_value),
        ))

    return decisions


def fill_forward_returns(decisions: list[Decision], snapshots: list) -> None:
    """Fill forward 1m/3m/6m returns for each decision.

    Uses portfolio snapshots to measure actual returns after each decision.
    """
    # Build date → value lookup from snapshots
    value_by_date = {s.date: float(s.total_value) for s in snapshots}
    sorted_dates = sorted(value_by_date.keys())

    for d in decisions:
        d_idx = None
        for i, dt in enumerate(sorted_dates):
            if dt >= d.date:
                d_idx = i
                break
        if d_idx is None:
            continue

        base_val = value_by_date[sorted_dates[d_idx]]
        if base_val <= 0:
            continue

        # 1m forward (~1 snapshot ahead for monthly)
        if d_idx + 1 < len(sorted_dates):
            d.fwd_1m_return = (value_by_date[sorted_dates[d_idx + 1]] / base_val) - 1
        # 3m forward
        if d_idx + 3 < len(sorted_dates):
            d.fwd_3m_return = (value_by_date[sorted_dates[d_idx + 3]] / base_val) - 1
        # 6m forward
        if d_idx + 6 < len(sorted_dates):
            d.fwd_6m_return = (value_by_date[sorted_dates[d_idx + 6]] / base_val) - 1


# ============================================================================
# PHASE 2: ALIGN VIX DATA TO DECISIONS
# ============================================================================

def build_vix_features(vix_prices: pd.DataFrame) -> pd.DataFrame:
    """Build VIX feature DataFrame: level, changes, percentile.

    Returns DataFrame indexed by date with columns:
      vix_level, vix_5d_change, vix_20d_change, vix_percentile_1y
    """
    vix = vix_prices[vix_prices["symbol"] == "^VIX"].copy()
    vix["date"] = pd.to_datetime(vix["date"])
    vix = vix.sort_values("date").set_index("date")
    vix = vix[~vix.index.duplicated(keep="last")]

    features = pd.DataFrame(index=vix.index)
    features["vix_level"] = vix["close"]
    features["vix_5d_change"] = vix["close"].pct_change(5)
    features["vix_20d_change"] = vix["close"].pct_change(20)

    # Rolling 1-year percentile
    features["vix_percentile_1y"] = vix["close"].rolling(252).apply(
        lambda x: (x < x.iloc[-1]).mean() if len(x) == 252 else np.nan,
        raw=False,
    )

    return features.dropna()


def align_vix_to_decisions(decisions: list[Decision], vix_features: pd.DataFrame) -> None:
    """Attach VIX features to each decision by date (nearest prior business day)."""
    for d in decisions:
        ts = pd.Timestamp(d.date)
        prior = vix_features.index[vix_features.index <= ts]
        if len(prior) == 0:
            continue
        nearest = prior[-1]
        row = vix_features.loc[nearest]
        d.vix_level = float(row["vix_level"])
        d.vix_5d_change = float(row["vix_5d_change"])
        d.vix_20d_change = float(row["vix_20d_change"])
        d.vix_percentile = float(row["vix_percentile_1y"])


# ============================================================================
# PHASE 3: CORRELATION ANALYSIS
# ============================================================================

def analyze_correlations(decisions: list[Decision]) -> dict:
    """Analyze correlation between VIX features and decision outcomes."""
    df = pd.DataFrame([{
        "date": d.date,
        "action": d.action,
        "vix_level": d.vix_level,
        "vix_5d_change": d.vix_5d_change,
        "vix_20d_change": d.vix_20d_change,
        "vix_percentile": d.vix_percentile,
        "fwd_1m": d.fwd_1m_return,
        "fwd_3m": d.fwd_3m_return,
        "fwd_6m": d.fwd_6m_return,
    } for d in decisions])

    # Drop rows without VIX or forward returns
    df = df[(df["vix_level"] > 0) & (df["fwd_1m"] != 0)].copy()

    if len(df) < 10:
        print("  WARNING: Too few decisions with VIX data for analysis")
        return {}

    print(f"\n  Total decisions analyzed: {len(df)}")
    print(f"  Switches: {(df['action'] == 'switch').sum()}, Holds: {(df['action'] == 'hold').sum()}")

    results = {}

    # --- 3a: Raw correlations ---
    print("\n  --- Correlations: VIX features vs forward returns ---")
    vix_cols = ["vix_level", "vix_5d_change", "vix_20d_change", "vix_percentile"]
    fwd_cols = ["fwd_1m", "fwd_3m", "fwd_6m"]

    corr_matrix = {}
    for vc in vix_cols:
        corr_matrix[vc] = {}
        for fc in fwd_cols:
            r = df[vc].corr(df[fc])
            corr_matrix[vc][fc] = round(r, 4)

    print(f"  {'':>20s} {'fwd_1m':>8s} {'fwd_3m':>8s} {'fwd_6m':>8s}")
    for vc in vix_cols:
        vals = [f"{corr_matrix[vc][fc]:>8.4f}" for fc in fwd_cols]
        print(f"  {vc:>20s} {''.join(vals)}")
    results["correlations"] = corr_matrix

    # --- 3b: Bucket analysis (VIX level quintiles) ---
    print("\n  --- Bucket analysis: VIX level quintiles ---")
    df["vix_bucket"] = pd.qcut(df["vix_level"], 5, labels=["Q1-Low", "Q2", "Q3", "Q4", "Q5-High"])
    bucket_stats = df.groupby("vix_bucket", observed=True).agg(
        count=("fwd_1m", "count"),
        avg_1m=("fwd_1m", "mean"),
        avg_3m=("fwd_3m", "mean"),
        avg_6m=("fwd_6m", "mean"),
        switch_pct=("action", lambda x: (x == "switch").mean()),
        avg_vix=("vix_level", "mean"),
    ).round(4)

    print(f"  {'Bucket':<10s} {'N':>4s} {'AvgVIX':>7s} {'Sw%':>6s} {'1m':>8s} {'3m':>8s} {'6m':>8s}")
    for bucket, row in bucket_stats.iterrows():
        print(f"  {str(bucket):<10s} {row['count']:>4.0f} {row['avg_vix']:>7.1f} {row['switch_pct']:>5.1%} {row['avg_1m']:>+7.2%} {row['avg_3m']:>+7.2%} {row['avg_6m']:>+7.2%}")

    results["bucket_analysis"] = bucket_stats.reset_index().to_dict(orient="records")

    # --- 3c: Switch quality by VIX regime ---
    print("\n  --- Switch quality by VIX regime ---")
    switches = df[df["action"] == "switch"].copy()
    if len(switches) > 5:
        switches["vix_regime"] = pd.cut(
            switches["vix_level"],
            bins=[0, 15, 20, 25, 35, 100],
            labels=["<15 Calm", "15-20 Normal", "20-25 Elevated", "25-35 High", ">35 Extreme"],
        )
        switch_by_regime = switches.groupby("vix_regime", observed=True).agg(
            count=("fwd_1m", "count"),
            avg_1m=("fwd_1m", "mean"),
            avg_3m=("fwd_3m", "mean"),
            pct_positive_3m=("fwd_3m", lambda x: (x > 0).mean()),
        ).round(4)

        print(f"  {'VIX Regime':<16s} {'N':>4s} {'1m':>8s} {'3m':>8s} {'3m Win%':>8s}")
        for regime, row in switch_by_regime.iterrows():
            print(f"  {str(regime):<16s} {row['count']:>4.0f} {row['avg_1m']:>+7.2%} {row['avg_3m']:>+7.2%} {row['pct_positive_3m']:>7.1%}")

        results["switch_by_vix_regime"] = switch_by_regime.reset_index().to_dict(orient="records")

    # --- 3d: VIX spike analysis ---
    print("\n  --- VIX spike impact on switches ---")
    if len(switches) > 5:
        spike_threshold = 0.20  # VIX up 20% in 5 days
        switches["is_spike"] = switches["vix_5d_change"] > spike_threshold
        spike = switches[switches["is_spike"]]
        no_spike = switches[~switches["is_spike"]]

        print(f"  During VIX spike (>20% in 5d): N={len(spike)}, avg 3m return={spike['fwd_3m'].mean():+.2%}")
        print(f"  No spike:                      N={len(no_spike)}, avg 3m return={no_spike['fwd_3m'].mean():+.2%}")

        results["spike_analysis"] = {
            "spike_count": int(len(spike)),
            "spike_avg_3m": round(float(spike["fwd_3m"].mean()) if len(spike) > 0 else 0, 6),
            "no_spike_count": int(len(no_spike)),
            "no_spike_avg_3m": round(float(no_spike["fwd_3m"].mean()), 6),
        }

    # --- 3e: VIX percentile analysis ---
    print("\n  --- Decision quality by VIX percentile (trailing 1y) ---")
    df["vix_pctl_bucket"] = pd.cut(
        df["vix_percentile"],
        bins=[0, 0.25, 0.50, 0.75, 1.0],
        labels=["Low 0-25%", "Mid-Low 25-50%", "Mid-High 50-75%", "High 75-100%"],
    )
    pctl_stats = df.groupby("vix_pctl_bucket", observed=True).agg(
        count=("fwd_1m", "count"),
        avg_1m=("fwd_1m", "mean"),
        avg_3m=("fwd_3m", "mean"),
        switch_rate=("action", lambda x: (x == "switch").mean()),
    ).round(4)

    print(f"  {'VIX Pctl':<18s} {'N':>4s} {'Sw%':>6s} {'1m':>8s} {'3m':>8s}")
    for bucket, row in pctl_stats.iterrows():
        print(f"  {str(bucket):<18s} {row['count']:>4.0f} {row['switch_rate']:>5.1%} {row['avg_1m']:>+7.2%} {row['avg_3m']:>+7.2%}")

    results["percentile_analysis"] = pctl_stats.reset_index().to_dict(orient="records")

    return results


# ============================================================================
# PHASE 4: VIX FILTER BACKTEST
# ============================================================================

def run_vix_filter_backtest(
    prices: pd.DataFrame,
    vix_features: pd.DataFrame,
    filter_name: str,
    filter_fn,
    start: date,
    end: date,
) -> dict | None:
    """Run backtest with a VIX filter applied.

    The filter_fn receives (decision_date, vix_features_df) and returns:
      - "block_switch": prevent switching, force hold
      - "force_defensive": override to defensive asset (AGG)
      - None: no override, let strategy decide
    """
    engine = BacktestEngine(initial_capital=10000.0)

    # Monkey-patch the engine to inject VIX filter before trade execution
    original_run = engine.run

    def filtered_run(**kwargs):
        result = original_run(**kwargs)
        return result

    # We can't easily inject into the engine loop, so instead we'll compare
    # unfiltered vs filtered by post-processing the decisions.
    # Run baseline first.
    try:
        result = engine.run(prices=prices, start_date=start, end_date=end, benchmark_symbol="SPY")
        result.calculate_metrics()
    except Exception as e:
        print(f"    {filter_name} baseline failed: {e}")
        return None

    # Now simulate the filter: replay snapshots, blocking switches when filter says so
    snapshots = result.snapshots
    if len(snapshots) < 2:
        return None

    # Reconstruct portfolio with filter
    capital = float(snapshots[0].total_value)
    filtered_value = capital
    blocked_switches = 0
    forced_defensive = 0
    total_switches = 0

    # Build BUY trade date → symbol map
    buy_by_date = {}
    for t in result.trades:
        if t.action.value == "buy":
            buy_by_date[t.date] = t.asset.symbol

    # Build asset price lookup: symbol → {date → close}
    price_lookup = {}
    for _, row in prices.iterrows():
        sym = row["symbol"]
        dt = row["date"] if isinstance(row["date"], date) else row["date"].date()
        if sym not in price_lookup:
            price_lookup[sym] = {}
        price_lookup[sym][dt] = row["close"]

    filtered_values = [capital]
    filter_decisions = []
    held_asset = None  # track what we're holding

    for i in range(1, len(snapshots)):
        prev = snapshots[i - 1]
        curr = snapshots[i]

        bought = buy_by_date.get(curr.date)
        prev_asset = held_asset
        curr_asset = bought if bought else held_asset
        is_switch = (bought is not None and bought != held_asset)
        if is_switch:
            total_switches += 1

        # Apply VIX filter
        filter_action = filter_fn(curr.date, vix_features)

        if is_switch and filter_action == "block_switch":
            # Block the switch — simulate holding the previous asset instead
            blocked_switches += 1
            ret = _get_asset_return(prev_asset, prev.date, curr.date, price_lookup)
            filtered_value *= (1 + ret)
            filter_decisions.append({
                "date": str(curr.date),
                "filter": "blocked",
                "would_switch_to": curr_asset,
                "held": prev_asset,
                "return": round(ret, 6),
            })
            # held_asset stays as prev_asset (switch was blocked)
        elif filter_action == "force_defensive":
            forced_defensive += 1
            ret = _get_asset_return("AGG", prev.date, curr.date, price_lookup)
            filtered_value *= (1 + ret)
            filter_decisions.append({
                "date": str(curr.date),
                "filter": "forced_defensive",
                "would_hold": curr_asset,
                "held": "AGG",
                "return": round(ret, 6),
            })
            held_asset = "AGG"  # forced to defensive
        else:
            # No filter — use actual return from backtest
            actual_return = (float(curr.total_value) / float(prev.total_value)) - 1
            filtered_value *= (1 + actual_return)
            if bought:
                held_asset = bought  # track the actual switch

        filtered_values.append(filtered_value)

    # Calculate metrics for filtered version
    years = (end - start).days / 365.25
    filtered_cagr = (filtered_value / capital) ** (1 / years) - 1 if years > 0 else 0

    # Max drawdown
    peak = 0.0
    max_dd = 0.0
    for v in filtered_values:
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    # Sharpe
    returns_series = pd.Series(filtered_values).pct_change().dropna()
    sharpe = 0.0
    if len(returns_series) > 1 and returns_series.std() > 0:
        sharpe = (returns_series.mean() * 12) / (returns_series.std() * np.sqrt(12))

    return {
        "filter": filter_name,
        "final_value": round(filtered_value, 2),
        "cagr": round(filtered_cagr, 6),
        "max_drawdown": round(max_dd, 6),
        "sharpe": round(sharpe, 4),
        "total_switches": total_switches,
        "blocked_switches": blocked_switches,
        "forced_defensive": forced_defensive,
        "filter_decisions": filter_decisions,
    }


def _get_asset_return(symbol: str, from_date: date, to_date: date, price_lookup: dict) -> float:
    """Get return of an asset between two dates from price lookup."""
    if symbol not in price_lookup:
        return 0.0

    prices = price_lookup[symbol]

    # Find nearest available price on or before each date
    from_price = _nearest_price(prices, from_date, lookback=10)
    to_price = _nearest_price(prices, to_date, lookback=10)

    if from_price is None or to_price is None or from_price <= 0:
        return 0.0

    return (to_price / from_price) - 1


def _nearest_price(prices: dict, target: date, lookback: int = 10) -> float | None:
    """Find nearest price on or before target date."""
    for i in range(lookback + 1):
        dt = target - timedelta(days=i)
        if dt in prices:
            return prices[dt]
    return None


# ============================================================================
# CANDIDATE VIX FILTERS
# ============================================================================

def make_vix_level_filter(threshold: float):
    """Block switches when VIX > threshold."""
    def filter_fn(decision_date: date, vix_features: pd.DataFrame) -> str | None:
        ts = pd.Timestamp(decision_date)
        prior = vix_features.index[vix_features.index <= ts]
        if len(prior) == 0:
            return None
        vix = float(vix_features.loc[prior[-1], "vix_level"])
        if vix > threshold:
            return "block_switch"
        return None
    return filter_fn


def make_vix_spike_filter(spike_pct: float = 0.20, lookback_days: int = 5):
    """Block switches when VIX has spiked > spike_pct in lookback_days."""
    col = f"vix_{lookback_days}d_change" if lookback_days == 5 else "vix_20d_change"
    def filter_fn(decision_date: date, vix_features: pd.DataFrame) -> str | None:
        ts = pd.Timestamp(decision_date)
        prior = vix_features.index[vix_features.index <= ts]
        if len(prior) == 0:
            return None
        change = float(vix_features.loc[prior[-1], col])
        if change > spike_pct:
            return "block_switch"
        return None
    return filter_fn


def make_vix_percentile_filter(pctl_threshold: float = 0.80):
    """Block switches when VIX is in top percentile of trailing year."""
    def filter_fn(decision_date: date, vix_features: pd.DataFrame) -> str | None:
        ts = pd.Timestamp(decision_date)
        prior = vix_features.index[vix_features.index <= ts]
        if len(prior) == 0:
            return None
        pctl = float(vix_features.loc[prior[-1], "vix_percentile_1y"])
        if pctl > pctl_threshold:
            return "block_switch"
        return None
    return filter_fn


def make_force_defensive_filter(vix_threshold: float = 30):
    """Force defensive (AGG) when VIX > threshold, regardless of strategy."""
    def filter_fn(decision_date: date, vix_features: pd.DataFrame) -> str | None:
        ts = pd.Timestamp(decision_date)
        prior = vix_features.index[vix_features.index <= ts]
        if len(prior) == 0:
            return None
        vix = float(vix_features.loc[prior[-1], "vix_level"])
        if vix > vix_threshold:
            return "force_defensive"
        return None
    return filter_fn


def make_combo_filter(vix_block: float = 25, spike_block: float = 0.30):
    """Block switches when VIX > level OR VIX spiked > spike_pct in 5d."""
    def filter_fn(decision_date: date, vix_features: pd.DataFrame) -> str | None:
        ts = pd.Timestamp(decision_date)
        prior = vix_features.index[vix_features.index <= ts]
        if len(prior) == 0:
            return None
        row = vix_features.loc[prior[-1]]
        vix = float(row["vix_level"])
        spike = float(row["vix_5d_change"])
        if vix > vix_block or spike > spike_block:
            return "block_switch"
        return None
    return filter_fn


CANDIDATE_FILTERS = {
    "VIX > 25 block":     make_vix_level_filter(25),
    "VIX > 30 block":     make_vix_level_filter(30),
    "VIX > 35 block":     make_vix_level_filter(35),
    "Spike 20% block":    make_vix_spike_filter(0.20, 5),
    "Spike 30% block":    make_vix_spike_filter(0.30, 5),
    "Pctl > 80% block":   make_vix_percentile_filter(0.80),
    "Pctl > 90% block":   make_vix_percentile_filter(0.90),
    "Force def VIX>30":   make_force_defensive_filter(30),
    "Force def VIX>35":   make_force_defensive_filter(35),
    "Combo VIX25+Sp30":   make_combo_filter(25, 0.30),
    "Combo VIX30+Sp20":   make_combo_filter(30, 0.20),
}


# ============================================================================
# PHASE 5: VALIDATION
# ============================================================================

def run_filter_comparison(
    prices: pd.DataFrame,
    vix_features: pd.DataFrame,
) -> list[dict]:
    """Run all candidate filters across validation periods and compare."""
    all_results = []

    for period_name, start, end in VALIDATION_PERIODS:
        print(f"\n  --- {period_name} ---")

        # Baseline (no filter)
        engine = BacktestEngine(initial_capital=10000.0)
        try:
            baseline = engine.run(prices=prices, start_date=start, end_date=end, benchmark_symbol="SPY")
            baseline.calculate_metrics()
            years = (end - start).days / 365.25
            bench_cagr = (baseline.benchmark_final / 10000.0) ** (1 / years) - 1 if baseline.benchmark_final else 0

            print(f"  {'Strategy':<22s} {'CAGR':>7s} {'Sharpe':>7s} {'MaxDD':>7s} {'Blocked':>8s} {'Forced':>7s}")
            print(f"  {'-'*60}")
            print(f"  {'Baseline (no filter)':<22s} {baseline.cagr:>+6.2%} {baseline.sharpe_ratio:>7.2f} {baseline.max_drawdown:>6.2%}")
            print(f"  {'SPY Buy & Hold':<22s} {bench_cagr:>+6.2%}")

            all_results.append({
                "period": period_name,
                "filter": "Baseline",
                "cagr": round(baseline.cagr, 6),
                "sharpe": round(baseline.sharpe_ratio, 4),
                "max_drawdown": round(baseline.max_drawdown, 6),
                "final_value": round(baseline.final_value, 2),
            })
        except Exception as e:
            print(f"  Baseline failed: {e}")
            continue

        # Each filter
        for filter_name, filter_fn in CANDIDATE_FILTERS.items():
            r = run_vix_filter_backtest(prices, vix_features, filter_name, filter_fn, start, end)
            if r:
                cagr_diff = r["cagr"] - baseline.cagr
                marker = " ***" if cagr_diff > 0.005 and r["sharpe"] > baseline.sharpe_ratio else ""
                print(f"  {filter_name:<22s} {r['cagr']:>+6.2%} {r['sharpe']:>7.2f} {r['max_drawdown']:>6.2%} {r['blocked_switches']:>8d} {r['forced_defensive']:>7d}{marker}")
                r["period"] = period_name
                all_results.append(r)

    return all_results


# ============================================================================
# MAIN
# ============================================================================

def main():
    print("=" * 80)
    print("VIX CORRELATION RESEARCH")
    print("=" * 80)

    # --- Fetch data ---
    print("\n[1/5] Fetching price data (including ^VIX)...")
    provider = YahooFinanceProvider()
    symbols = get_all_yahoo_symbols()
    if "SPY" not in symbols:
        symbols.append("SPY")
    if "^VIX" not in symbols:
        symbols.append("^VIX")

    fetch_start = FULL_START - timedelta(days=500)
    fetch_end = FULL_END + timedelta(days=30)
    prices = provider.get_multi_prices(symbols, fetch_start, fetch_end)

    vix_count = len(prices[prices["symbol"] == "^VIX"])
    spy_count = len(prices[prices["symbol"] == "SPY"])
    print(f"  Loaded {len(prices)} rows across {len(symbols)} symbols")
    print(f"  VIX: {vix_count} days, SPY: {spy_count} days")

    # --- Phase 1: Extract decisions ---
    print("\n[2/5] Running baseline backtest and extracting decisions...")
    engine = BacktestEngine(initial_capital=10000.0)
    result = engine.run(prices=prices, start_date=FULL_START, end_date=FULL_END, benchmark_symbol="SPY")
    result.calculate_metrics()
    print(f"  Baseline: CAGR={result.cagr:+.2%}, Sharpe={result.sharpe_ratio:.2f}, MaxDD={result.max_drawdown:.2%}")

    decisions = extract_decisions(result)
    fill_forward_returns(decisions, result.snapshots)
    print(f"  Extracted {len(decisions)} decisions ({sum(1 for d in decisions if d.action == 'switch')} switches)")

    # --- Phase 2: Align VIX ---
    print("\n[3/5] Building VIX features and aligning to decisions...")
    vix_features = build_vix_features(prices)
    align_vix_to_decisions(decisions, vix_features)
    print(f"  VIX features: {len(vix_features)} days, range {vix_features.index[0].date()} to {vix_features.index[-1].date()}")

    vix_aligned = sum(1 for d in decisions if d.vix_level > 0)
    print(f"  Decisions with VIX data: {vix_aligned}/{len(decisions)}")

    # --- Phase 3: Correlation analysis ---
    print("\n" + "=" * 80)
    print("PHASE 3: CORRELATION ANALYSIS")
    print("=" * 80)
    correlation_results = analyze_correlations(decisions)

    # --- Phase 4 & 5: Filter backtest + comparison ---
    print("\n" + "=" * 80)
    print("PHASE 4-5: VIX FILTER BACKTEST COMPARISON")
    print("=" * 80)
    filter_results = run_filter_comparison(prices, vix_features)

    # --- Save results ---
    print("\n[5/5] Saving results...")
    output = {
        "decisions": [
            {
                "date": str(d.date),
                "action": d.action,
                "from_asset": d.from_asset,
                "to_asset": d.to_asset,
                "vix_level": round(d.vix_level, 2),
                "vix_5d_change": round(d.vix_5d_change, 4),
                "vix_percentile": round(d.vix_percentile, 4),
                "fwd_1m": round(d.fwd_1m_return, 6),
                "fwd_3m": round(d.fwd_3m_return, 6),
                "fwd_6m": round(d.fwd_6m_return, 6),
            }
            for d in decisions if d.vix_level > 0
        ],
        "correlations": correlation_results,
        "filter_comparison": [
            {k: v for k, v in r.items() if k != "filter_decisions"}
            for r in filter_results
        ],
    }

    output_path = os.path.join(os.path.dirname(__file__), "..", "data", "vix_research_results.json")
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"  Results saved to {output_path}")

    # --- Summary ---
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)

    # Find best filter across full period
    full_results = [r for r in filter_results if r.get("period") == "Full (2005-2026)" and r.get("filter") != "Baseline"]
    baseline_full = next((r for r in filter_results if r.get("period") == "Full (2005-2026)" and r.get("filter") == "Baseline"), None)

    if baseline_full and full_results:
        baseline_cagr = baseline_full["cagr"]
        baseline_sharpe = baseline_full["sharpe"]
        print(f"\n  Baseline (full period): CAGR={baseline_cagr:+.2%}, Sharpe={baseline_sharpe:.2f}")

        # Sort by Sharpe improvement
        for r in full_results:
            r["sharpe_delta"] = r.get("sharpe", 0) - baseline_sharpe
            r["cagr_delta"] = r.get("cagr", 0) - baseline_cagr

        full_results.sort(key=lambda r: r["sharpe_delta"], reverse=True)
        print(f"\n  {'Filter':<22s} {'CAGR Δ':>8s} {'Sharpe Δ':>9s} {'MaxDD':>7s} {'Blocked':>8s}")
        for r in full_results:
            print(f"  {r['filter']:<22s} {r['cagr_delta']:>+7.2%} {r['sharpe_delta']:>+8.4f} {r.get('max_drawdown', 0):>6.2%} {r.get('blocked_switches', 0):>8d}")

        best = full_results[0]
        if best["sharpe_delta"] > 0 and best["cagr_delta"] > -0.005:
            print(f"\n  RECOMMENDATION: {best['filter']} improves Sharpe by {best['sharpe_delta']:+.4f}")
            print(f"  with CAGR impact of {best['cagr_delta']:+.2%} — WORTH INVESTIGATING further")
        else:
            print(f"\n  CONCLUSION: No VIX filter improves risk-adjusted returns without material CAGR loss")
            print(f"  VIX is informative but not actionable as a simple trade filter for this strategy")

    print("\n" + "=" * 80)
    print("RESEARCH COMPLETE")
    print("=" * 80)


if __name__ == "__main__":
    main()
