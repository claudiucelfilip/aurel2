"""Trading strategies."""

from aurel2.strategies.dual_momentum import DualMomentumStrategy
from aurel2.strategies.mean_reversion import MeanReversionStrategy
from aurel2.strategies.robust_quarterly_early_switch import (
    RobustQuarterlyEarlySwitchStrategy,
    build_robust_quarterly_early_switch_strategy,
)
from aurel2.strategies.multi_timeframe import MultiTimeframeTrendStrategy
from aurel2.strategies.robust_quarterly import (
    RobustQuarterlyStrategy,
    build_robust_quarterly_no_tlt_strategy,
    build_robust_quarterly_strategy,
)
from aurel2.strategies.robust_quarterly_crisis import (
    RobustQuarterlyCrisisStrategy,
    build_robust_quarterly_crisis_strategy,
)

__all__ = [
    "DualMomentumStrategy",
    "MeanReversionStrategy",
    "MultiTimeframeTrendStrategy",
    "RobustQuarterlyEarlySwitchStrategy",
    "RobustQuarterlyStrategy",
    "RobustQuarterlyCrisisStrategy",
    "build_robust_quarterly_early_switch_strategy",
    "build_robust_quarterly_no_tlt_strategy",
    "build_robust_quarterly_strategy",
    "build_robust_quarterly_crisis_strategy",
]
