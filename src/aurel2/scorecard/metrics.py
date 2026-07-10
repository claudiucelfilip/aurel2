"""Pure metric computations for the weekly scorecard.

All functions operate on a daily equity series: list[tuple[date, float]],
sorted ascending by date, one point per trading day. No I/O here -- callers
(scripts/weekly_scorecard.py) are responsible for building these series from
each arm's actual data source (see docs/plans/2026-07-10-graduation-rule.md
section 1).

Exact formulas match docs/plans/2026-07-10-graduation-rule.md sections 2-3.
"""

import math
from datetime import date
from typing import Optional

EquitySeries = list[tuple[date, float]]

MIN_RETURNS_FOR_SHARPE = 10
TRADING_DAYS_PER_YEAR = 252


def _values(series: EquitySeries) -> list[float]:
    return [v for _, v in series]


def _daily_returns(series: EquitySeries) -> list[float]:
    values = _values(series)
    return [values[i] / values[i - 1] - 1 for i in range(1, len(values))]


def cumulative_return(series: EquitySeries) -> Optional[float]:
    """Total return from the first to the last point in the series."""
    if not series:
        return None
    values = _values(series)
    if len(values) == 1:
        return 0.0
    return values[-1] / values[0] - 1


def max_drawdown(series: EquitySeries) -> Optional[float]:
    """Peak-to-trough drawdown over the series, as a positive fraction."""
    if not series:
        return None
    values = _values(series)
    peak = values[0]
    worst = 0.0
    for v in values:
        peak = max(peak, v)
        dd = (peak - v) / peak
        worst = max(worst, dd)
    return worst


def sharpe_ratio(series: EquitySeries) -> Optional[float]:
    """Annualized Sharpe (0% risk-free), sqrt(252) annualization, ddof=1.

    Returns None if fewer than MIN_RETURNS_FOR_SHARPE daily returns are
    available (too noisy to report). Returns 0.0 for a zero-volatility
    series (no divide-by-zero).
    """
    returns = _daily_returns(series)
    if len(returns) < MIN_RETURNS_FOR_SHARPE:
        return None

    mean_r = sum(returns) / len(returns)
    variance = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
    std_r = math.sqrt(variance)
    if std_r == 0:
        return 0.0
    return (mean_r / std_r) * math.sqrt(TRADING_DAYS_PER_YEAR)


def alpha_vs_benchmark(arm: EquitySeries, benchmark: EquitySeries) -> Optional[float]:
    """Cumulative-return spread: arm's return minus benchmark's return.

    None if either series is empty (missing data, never a false zero).
    """
    arm_return = cumulative_return(arm)
    bench_return = cumulative_return(benchmark)
    if arm_return is None or bench_return is None:
        return None
    return arm_return - bench_return
