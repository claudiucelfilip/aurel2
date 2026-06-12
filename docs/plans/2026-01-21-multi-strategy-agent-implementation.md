# Multi-Strategy AI Agent Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a multi-strategy trading system with an AI agent that selects the best strategy for current market conditions, executes via IBKR, and notifies via push notifications.

**Architecture:** MCP server exposes strategy signals and market data. AI agent runs daily, queries MCP tools, selects which strategy to follow, and either auto-executes or requests approval. Serverless endpoint on Vercel handles approval flow.

**Tech Stack:** Python 3.11+, MCP SDK, FastAPI, IBKR (ib_insync), Ntfy.sh, Vercel (serverless), structlog

---

## Phase 1: Extended Asset Universe & Indicators

### Task 1.1: Extend Asset Models

**Files:**
- Modify: `src/aurel2/core/models.py`
- Test: `tests/test_models.py`

**Step 1: Write the failing test**

Create `tests/test_models.py`:

```python
"""Tests for core models."""
import pytest
from aurel2.core.models import AssetClass, Asset, AssetCategory


def test_asset_class_has_extended_values():
    """AssetClass should include sectors and alternatives."""
    assert AssetClass.US_STOCKS.value == "us_stocks"
    assert AssetClass.INTL_DEVELOPED.value == "intl_developed"
    assert AssetClass.EMERGING_MARKETS.value == "emerging_markets"
    assert AssetClass.TECH_SECTOR.value == "tech_sector"
    assert AssetClass.FINANCIAL_SECTOR.value == "financial_sector"
    assert AssetClass.ENERGY_SECTOR.value == "energy_sector"
    assert AssetClass.HEALTHCARE_SECTOR.value == "healthcare_sector"
    assert AssetClass.BONDS_AGGREGATE.value == "bonds_aggregate"
    assert AssetClass.BONDS_TREASURY.value == "bonds_treasury"
    assert AssetClass.GOLD.value == "gold"
    assert AssetClass.COMMODITIES.value == "commodities"
    assert AssetClass.CASH.value == "cash"


def test_asset_category_enum():
    """AssetCategory groups asset classes."""
    assert AssetCategory.EQUITY.value == "equity"
    assert AssetCategory.FIXED_INCOME.value == "fixed_income"
    assert AssetCategory.ALTERNATIVE.value == "alternative"
    assert AssetCategory.CASH.value == "cash"


def test_asset_has_category():
    """Asset should have a category field."""
    asset = Asset(
        symbol="SPY",
        name="S&P 500",
        asset_class=AssetClass.US_STOCKS,
        category=AssetCategory.EQUITY,
        yahoo_symbol="SPY",
    )
    assert asset.category == AssetCategory.EQUITY
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_models.py -v`
Expected: FAIL with "cannot import name 'AssetCategory'"

**Step 3: Write minimal implementation**

Update `src/aurel2/core/models.py`:

```python
"""Core domain models for Aurel2."""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum


class AssetCategory(str, Enum):
    """Broad asset categories."""
    EQUITY = "equity"
    FIXED_INCOME = "fixed_income"
    ALTERNATIVE = "alternative"
    CASH = "cash"


class AssetClass(str, Enum):
    """Specific asset classes for strategy selection."""
    # Core Equity
    US_STOCKS = "us_stocks"
    INTL_DEVELOPED = "intl_developed"
    EMERGING_MARKETS = "emerging_markets"
    # Sectors
    TECH_SECTOR = "tech_sector"
    FINANCIAL_SECTOR = "financial_sector"
    ENERGY_SECTOR = "energy_sector"
    HEALTHCARE_SECTOR = "healthcare_sector"
    # Fixed Income
    BONDS_AGGREGATE = "bonds_aggregate"
    BONDS_TREASURY = "bonds_treasury"
    # Alternatives
    GOLD = "gold"
    COMMODITIES = "commodities"
    # Cash
    CASH = "cash"

    # Legacy aliases for backward compatibility
    GLOBAL_STOCKS = "intl_developed"
    BONDS = "bonds_aggregate"


class SignalAction(str, Enum):
    """Trading signal actions."""
    HOLD = "hold"
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class Asset:
    """An investable asset."""
    symbol: str
    name: str
    asset_class: AssetClass
    category: AssetCategory = AssetCategory.EQUITY
    isin: str | None = None
    yahoo_symbol: str | None = None
    ucits_symbol: str | None = None  # European equivalent


# ... rest of existing dataclasses unchanged ...
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_models.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/aurel2/core/models.py tests/test_models.py
git commit -m "feat: extend asset classes for multi-strategy universe"
```

---

### Task 1.2: Create Asset Registry

**Files:**
- Create: `src/aurel2/core/assets.py`
- Test: `tests/test_assets.py`

**Step 1: Write the failing test**

Create `tests/test_assets.py`:

```python
"""Tests for asset registry."""
import pytest
from aurel2.core.assets import ASSET_REGISTRY, get_asset, get_assets_by_category
from aurel2.core.models import AssetClass, AssetCategory


def test_registry_has_core_assets():
    """Registry should have all core assets."""
    assert AssetClass.US_STOCKS in ASSET_REGISTRY
    assert AssetClass.INTL_DEVELOPED in ASSET_REGISTRY
    assert AssetClass.EMERGING_MARKETS in ASSET_REGISTRY
    assert AssetClass.GOLD in ASSET_REGISTRY


def test_get_asset():
    """get_asset returns correct asset."""
    spy = get_asset(AssetClass.US_STOCKS)
    assert spy.symbol == "SPY"
    assert spy.yahoo_symbol == "SPY"


def test_get_assets_by_category():
    """get_assets_by_category filters correctly."""
    equities = get_assets_by_category(AssetCategory.EQUITY)
    assert len(equities) >= 4  # US, INTL, EM, sectors

    for asset in equities:
        assert asset.category == AssetCategory.EQUITY
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_assets.py -v`
Expected: FAIL with "No module named 'aurel2.core.assets'"

**Step 3: Write minimal implementation**

Create `src/aurel2/core/assets.py`:

```python
"""Asset registry with all tradeable assets."""

from aurel2.core.models import Asset, AssetClass, AssetCategory


ASSET_REGISTRY: dict[AssetClass, Asset] = {
    # Core Equity
    AssetClass.US_STOCKS: Asset(
        symbol="SPY",
        name="S&P 500 ETF",
        asset_class=AssetClass.US_STOCKS,
        category=AssetCategory.EQUITY,
        yahoo_symbol="SPY",
        ucits_symbol="CSPX",
        isin="IE00B5BMR087",
    ),
    AssetClass.INTL_DEVELOPED: Asset(
        symbol="EFA",
        name="MSCI EAFE ETF",
        asset_class=AssetClass.INTL_DEVELOPED,
        category=AssetCategory.EQUITY,
        yahoo_symbol="EFA",
        ucits_symbol="VWRA",
        isin="IE00BK5BQT80",
    ),
    AssetClass.EMERGING_MARKETS: Asset(
        symbol="EEM",
        name="MSCI Emerging Markets ETF",
        asset_class=AssetClass.EMERGING_MARKETS,
        category=AssetCategory.EQUITY,
        yahoo_symbol="EEM",
        ucits_symbol="EIMI",
        isin="IE00BKM4GZ66",
    ),
    # Sectors
    AssetClass.TECH_SECTOR: Asset(
        symbol="XLK",
        name="Technology Select Sector ETF",
        asset_class=AssetClass.TECH_SECTOR,
        category=AssetCategory.EQUITY,
        yahoo_symbol="XLK",
    ),
    AssetClass.FINANCIAL_SECTOR: Asset(
        symbol="XLF",
        name="Financial Select Sector ETF",
        asset_class=AssetClass.FINANCIAL_SECTOR,
        category=AssetCategory.EQUITY,
        yahoo_symbol="XLF",
    ),
    AssetClass.ENERGY_SECTOR: Asset(
        symbol="XLE",
        name="Energy Select Sector ETF",
        asset_class=AssetClass.ENERGY_SECTOR,
        category=AssetCategory.EQUITY,
        yahoo_symbol="XLE",
    ),
    AssetClass.HEALTHCARE_SECTOR: Asset(
        symbol="XLV",
        name="Healthcare Select Sector ETF",
        asset_class=AssetClass.HEALTHCARE_SECTOR,
        category=AssetCategory.EQUITY,
        yahoo_symbol="XLV",
    ),
    # Fixed Income
    AssetClass.BONDS_AGGREGATE: Asset(
        symbol="AGG",
        name="US Aggregate Bond ETF",
        asset_class=AssetClass.BONDS_AGGREGATE,
        category=AssetCategory.FIXED_INCOME,
        yahoo_symbol="AGG",
        ucits_symbol="AGGH",
        isin="IE00BDBRDM35",
    ),
    AssetClass.BONDS_TREASURY: Asset(
        symbol="TLT",
        name="20+ Year Treasury ETF",
        asset_class=AssetClass.BONDS_TREASURY,
        category=AssetCategory.FIXED_INCOME,
        yahoo_symbol="TLT",
    ),
    # Alternatives
    AssetClass.GOLD: Asset(
        symbol="GLD",
        name="Gold ETF",
        asset_class=AssetClass.GOLD,
        category=AssetCategory.ALTERNATIVE,
        yahoo_symbol="GLD",
        ucits_symbol="SGLD",
        isin="IE00B4ND3602",
    ),
    AssetClass.COMMODITIES: Asset(
        symbol="DBC",
        name="Commodities Index ETF",
        asset_class=AssetClass.COMMODITIES,
        category=AssetCategory.ALTERNATIVE,
        yahoo_symbol="DBC",
    ),
    # Cash
    AssetClass.CASH: Asset(
        symbol="CASH",
        name="Cash / Money Market",
        asset_class=AssetClass.CASH,
        category=AssetCategory.CASH,
    ),
}


def get_asset(asset_class: AssetClass) -> Asset:
    """Get asset by asset class."""
    return ASSET_REGISTRY[asset_class]


def get_assets_by_category(category: AssetCategory) -> list[Asset]:
    """Get all assets in a category."""
    return [a for a in ASSET_REGISTRY.values() if a.category == category]


def get_all_yahoo_symbols() -> list[str]:
    """Get all Yahoo Finance symbols (excluding cash)."""
    return [a.yahoo_symbol for a in ASSET_REGISTRY.values() if a.yahoo_symbol]
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_assets.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/aurel2/core/assets.py tests/test_assets.py
git commit -m "feat: add asset registry with extended universe"
```

---

### Task 1.3: Add RSI Indicator

**Files:**
- Create: `src/aurel2/data/indicators.py`
- Test: `tests/test_indicators.py`

**Step 1: Write the failing test**

Create `tests/test_indicators.py`:

```python
"""Tests for technical indicators."""
import pytest
import pandas as pd
import numpy as np
from aurel2.data.indicators import calculate_rsi, calculate_drawdown


def test_calculate_rsi_basic():
    """RSI should be between 0 and 100."""
    # Create sample price data with clear trend
    prices = pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=50, freq="D"),
        "close": [100 + i * 0.5 for i in range(50)],  # Uptrend
        "symbol": ["SPY"] * 50,
    })

    rsi = calculate_rsi(prices, "SPY", period=14)

    assert rsi is not None
    assert 0 <= rsi <= 100


def test_calculate_rsi_oversold():
    """RSI should be low after price drops."""
    # Create downtrend data
    prices = pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=50, freq="D"),
        "close": [100 - i * 1.5 for i in range(50)],  # Strong downtrend
        "symbol": ["SPY"] * 50,
    })

    rsi = calculate_rsi(prices, "SPY", period=14)

    assert rsi is not None
    assert rsi < 30  # Oversold


def test_calculate_drawdown():
    """Drawdown should measure decline from peak."""
    prices = pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=10, freq="D"),
        "close": [100, 105, 110, 100, 95, 90, 92, 94, 96, 98],
        "symbol": ["SPY"] * 10,
    })

    drawdown = calculate_drawdown(prices, "SPY")

    # Peak was 110, current is 98, drawdown = (110-98)/110 = 10.9%
    assert drawdown is not None
    assert abs(drawdown - 0.109) < 0.01
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_indicators.py -v`
Expected: FAIL with "No module named 'aurel2.data.indicators'"

**Step 3: Write minimal implementation**

Create `src/aurel2/data/indicators.py`:

```python
"""Technical indicators for strategy signals."""

import pandas as pd
import numpy as np
import structlog

logger = structlog.get_logger()


def calculate_rsi(
    prices: pd.DataFrame,
    symbol: str,
    period: int = 14,
) -> float | None:
    """
    Calculate Relative Strength Index (RSI).

    RSI = 100 - (100 / (1 + RS))
    RS = Average Gain / Average Loss

    Args:
        prices: DataFrame with columns: date, close, symbol
        symbol: Symbol to calculate RSI for
        period: RSI period (default 14)

    Returns:
        RSI value (0-100) or None if insufficient data
    """
    symbol_prices = prices[prices["symbol"] == symbol].copy()

    if len(symbol_prices) < period + 1:
        logger.warning("insufficient_data_for_rsi", symbol=symbol, rows=len(symbol_prices))
        return None

    symbol_prices = symbol_prices.sort_values("date")
    closes = symbol_prices["close"].values

    # Calculate price changes
    deltas = np.diff(closes)

    # Separate gains and losses
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)

    # Calculate average gain and loss (simple moving average for first calc)
    avg_gain = np.mean(gains[-period:])
    avg_loss = np.mean(losses[-period:])

    if avg_loss == 0:
        return 100.0  # No losses = RSI 100

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))

    logger.debug("calculated_rsi", symbol=symbol, rsi=f"{rsi:.1f}", period=period)

    return float(rsi)


def calculate_drawdown(
    prices: pd.DataFrame,
    symbol: str,
    lookback_days: int = 252,
) -> float | None:
    """
    Calculate current drawdown from recent peak.

    Drawdown = (Peak - Current) / Peak

    Args:
        prices: DataFrame with columns: date, close, symbol
        symbol: Symbol to calculate drawdown for
        lookback_days: Days to look back for peak (default 252 = 1 year)

    Returns:
        Drawdown as decimal (0.10 = 10% drawdown) or None
    """
    symbol_prices = prices[prices["symbol"] == symbol].copy()

    if len(symbol_prices) < 2:
        return None

    symbol_prices = symbol_prices.sort_values("date")

    # Get recent prices
    recent = symbol_prices.tail(lookback_days)

    peak = recent["close"].max()
    current = recent["close"].iloc[-1]

    if peak == 0:
        return None

    drawdown = (peak - current) / peak

    logger.debug(
        "calculated_drawdown",
        symbol=symbol,
        peak=f"{peak:.2f}",
        current=f"{current:.2f}",
        drawdown=f"{drawdown:.2%}",
    )

    return float(drawdown)


def calculate_moving_average(
    prices: pd.DataFrame,
    symbol: str,
    period: int = 200,
) -> float | None:
    """
    Calculate simple moving average.

    Args:
        prices: DataFrame with columns: date, close, symbol
        symbol: Symbol to calculate MA for
        period: MA period (default 200)

    Returns:
        Moving average value or None
    """
    symbol_prices = prices[prices["symbol"] == symbol].copy()

    if len(symbol_prices) < period:
        return None

    symbol_prices = symbol_prices.sort_values("date")
    ma = symbol_prices["close"].tail(period).mean()

    return float(ma)


def get_current_price(prices: pd.DataFrame, symbol: str) -> float | None:
    """Get most recent price for symbol."""
    symbol_prices = prices[prices["symbol"] == symbol]

    if symbol_prices.empty:
        return None

    return float(symbol_prices.sort_values("date").iloc[-1]["close"])
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_indicators.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/aurel2/data/indicators.py tests/test_indicators.py
git commit -m "feat: add RSI and drawdown indicators"
```

---

## Phase 2: New Strategies

### Task 2.1: Create Strategy Base Class

**Files:**
- Create: `src/aurel2/strategies/base.py`
- Test: `tests/test_strategy_base.py`

**Step 1: Write the failing test**

Create `tests/test_strategy_base.py`:

```python
"""Tests for strategy base class."""
import pytest
from datetime import date
import pandas as pd
from aurel2.strategies.base import BaseStrategy, StrategySignal
from aurel2.core.models import AssetClass, SignalAction


def test_strategy_signal_dataclass():
    """StrategySignal should have required fields."""
    signal = StrategySignal(
        strategy_name="test",
        date=date(2024, 1, 1),
        action=SignalAction.BUY,
        asset_class=AssetClass.US_STOCKS,
        confidence=0.8,
        reasoning="Test reasoning",
        metadata={"rsi": 25},
    )

    assert signal.strategy_name == "test"
    assert signal.confidence == 0.8
    assert signal.metadata["rsi"] == 25


def test_base_strategy_is_abstract():
    """BaseStrategy should not be instantiable directly."""
    with pytest.raises(TypeError):
        BaseStrategy()
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_strategy_base.py -v`
Expected: FAIL with "No module named 'aurel2.strategies.base'"

**Step 3: Write minimal implementation**

Create `src/aurel2/strategies/base.py`:

```python
"""Base class for all strategies."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd

from aurel2.core.models import AssetClass, SignalAction


@dataclass
class StrategySignal:
    """Signal generated by a strategy."""
    strategy_name: str
    date: date
    action: SignalAction
    asset_class: AssetClass | None  # None means stay in cash
    confidence: float  # 0.0 to 1.0
    reasoning: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "strategy_name": self.strategy_name,
            "date": self.date.isoformat(),
            "action": self.action.value,
            "asset_class": self.asset_class.value if self.asset_class else None,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "metadata": self.metadata,
        }


class BaseStrategy(ABC):
    """Abstract base class for trading strategies."""

    name: str = "base"

    @abstractmethod
    def generate_signal(
        self,
        prices: pd.DataFrame,
        calc_date: date,
        current_holding: AssetClass | None = None,
    ) -> StrategySignal:
        """
        Generate trading signal.

        Args:
            prices: Historical price data (date, close, symbol columns)
            calc_date: Date to generate signal for
            current_holding: Currently held asset class

        Returns:
            StrategySignal with recommendation
        """
        pass

    @abstractmethod
    def get_rebalance_dates(
        self,
        start_date: date,
        end_date: date,
    ) -> list[date]:
        """
        Get dates when strategy should be evaluated.

        Args:
            start_date: Start of period
            end_date: End of period

        Returns:
            List of evaluation dates
        """
        pass
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_strategy_base.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/aurel2/strategies/base.py tests/test_strategy_base.py
git commit -m "feat: add strategy base class with StrategySignal"
```

---

### Task 2.2: Implement Mean Reversion Strategy

**Files:**
- Create: `src/aurel2/strategies/mean_reversion.py`
- Test: `tests/test_mean_reversion.py`

**Step 1: Write the failing test**

Create `tests/test_mean_reversion.py`:

```python
"""Tests for mean reversion strategy."""
import pytest
from datetime import date
import pandas as pd
import numpy as np
from aurel2.strategies.mean_reversion import MeanReversionStrategy
from aurel2.core.models import AssetClass, SignalAction


@pytest.fixture
def oversold_prices():
    """Create price data with oversold condition."""
    # Strong downtrend -> RSI will be low
    dates = pd.date_range("2024-01-01", periods=50, freq="D")
    prices = 100 - np.arange(50) * 1.5  # Down 75% over 50 days

    return pd.DataFrame({
        "date": dates,
        "close": prices,
        "symbol": ["SPY"] * 50,
    })


@pytest.fixture
def normal_prices():
    """Create price data with normal conditions."""
    dates = pd.date_range("2024-01-01", periods=50, freq="D")
    # Sideways with small movements
    np.random.seed(42)
    prices = 100 + np.random.randn(50) * 2

    return pd.DataFrame({
        "date": dates,
        "close": prices,
        "symbol": ["SPY"] * 50,
    })


def test_mean_reversion_signals_buy_when_oversold(oversold_prices):
    """Strategy should signal BUY when RSI is oversold."""
    strategy = MeanReversionStrategy(
        rsi_oversold=30,
        rsi_overbought=70,
        drawdown_threshold=0.10,
    )

    signal = strategy.generate_signal(
        prices=oversold_prices,
        calc_date=date(2024, 2, 19),
        current_holding=None,
    )

    assert signal.strategy_name == "mean_reversion"
    assert signal.action == SignalAction.BUY
    assert signal.confidence > 0.5
    assert "oversold" in signal.reasoning.lower() or "rsi" in signal.reasoning.lower()


def test_mean_reversion_signals_hold_when_normal(normal_prices):
    """Strategy should signal HOLD when conditions are normal."""
    strategy = MeanReversionStrategy()

    signal = strategy.generate_signal(
        prices=normal_prices,
        calc_date=date(2024, 2, 19),
        current_holding=AssetClass.US_STOCKS,
    )

    assert signal.action == SignalAction.HOLD


def test_mean_reversion_is_event_driven():
    """Mean reversion should check daily, not on schedule."""
    strategy = MeanReversionStrategy()

    dates = strategy.get_rebalance_dates(date(2024, 1, 1), date(2024, 1, 31))

    # Should return daily dates (business days)
    assert len(dates) >= 20
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_mean_reversion.py -v`
Expected: FAIL with "No module named 'aurel2.strategies.mean_reversion'"

**Step 3: Write minimal implementation**

Create `src/aurel2/strategies/mean_reversion.py`:

```python
"""Mean Reversion Strategy - catches oversold bounces."""

from datetime import date

import pandas as pd
import structlog

from aurel2.core.models import AssetClass, SignalAction
from aurel2.core.assets import ASSET_REGISTRY, get_assets_by_category
from aurel2.data.indicators import calculate_rsi, calculate_drawdown, get_current_price
from aurel2.strategies.base import BaseStrategy, StrategySignal

logger = structlog.get_logger()


class MeanReversionStrategy(BaseStrategy):
    """
    Mean Reversion / Oversold Bounce Strategy.

    Signals BUY when:
    - RSI < oversold threshold (default 30)
    - AND/OR drawdown > threshold (default 10%)

    Signals SELL when:
    - RSI > overbought threshold (default 70)
    - AND position is profitable

    Best for: Catching V-shaped recoveries that momentum misses.
    """

    name = "mean_reversion"

    def __init__(
        self,
        rsi_oversold: float = 30,
        rsi_overbought: float = 70,
        rsi_period: int = 14,
        drawdown_threshold: float = 0.10,
        target_assets: list[AssetClass] | None = None,
    ):
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.rsi_period = rsi_period
        self.drawdown_threshold = drawdown_threshold
        self.target_assets = target_assets or [
            AssetClass.US_STOCKS,
            AssetClass.INTL_DEVELOPED,
            AssetClass.EMERGING_MARKETS,
        ]

    def generate_signal(
        self,
        prices: pd.DataFrame,
        calc_date: date,
        current_holding: AssetClass | None = None,
    ) -> StrategySignal:
        """Generate mean reversion signal."""

        # Find the most oversold asset
        best_opportunity = None
        best_rsi = 100
        opportunities = []

        for asset_class in self.target_assets:
            asset = ASSET_REGISTRY.get(asset_class)
            if not asset or not asset.yahoo_symbol:
                continue

            symbol = asset.yahoo_symbol
            rsi = calculate_rsi(prices, symbol, self.rsi_period)
            drawdown = calculate_drawdown(prices, symbol)

            if rsi is None:
                continue

            opportunities.append({
                "asset_class": asset_class,
                "symbol": symbol,
                "rsi": rsi,
                "drawdown": drawdown or 0,
            })

            # Track most oversold
            if rsi < best_rsi:
                best_rsi = rsi
                best_opportunity = asset_class

        logger.info(
            "mean_reversion_scan",
            date=str(calc_date),
            opportunities=len(opportunities),
            best_rsi=f"{best_rsi:.1f}" if best_rsi < 100 else "N/A",
        )

        # Check for oversold buy signal
        if best_rsi < self.rsi_oversold:
            return StrategySignal(
                strategy_name=self.name,
                date=calc_date,
                action=SignalAction.BUY,
                asset_class=best_opportunity,
                confidence=min(0.9, (self.rsi_oversold - best_rsi) / 30 + 0.5),
                reasoning=f"RSI oversold at {best_rsi:.1f} (threshold: {self.rsi_oversold})",
                metadata={
                    "rsi": best_rsi,
                    "threshold": self.rsi_oversold,
                    "opportunities": opportunities,
                },
            )

        # Check for overbought sell signal
        if current_holding and current_holding in self.target_assets:
            asset = ASSET_REGISTRY.get(current_holding)
            if asset and asset.yahoo_symbol:
                current_rsi = calculate_rsi(prices, asset.yahoo_symbol, self.rsi_period)
                if current_rsi and current_rsi > self.rsi_overbought:
                    return StrategySignal(
                        strategy_name=self.name,
                        date=calc_date,
                        action=SignalAction.SELL,
                        asset_class=current_holding,
                        confidence=min(0.9, (current_rsi - self.rsi_overbought) / 30 + 0.5),
                        reasoning=f"RSI overbought at {current_rsi:.1f} (threshold: {self.rsi_overbought})",
                        metadata={"rsi": current_rsi},
                    )

        # No signal - hold
        return StrategySignal(
            strategy_name=self.name,
            date=calc_date,
            action=SignalAction.HOLD,
            asset_class=current_holding,
            confidence=0.5,
            reasoning=f"No oversold conditions. Best RSI: {best_rsi:.1f}",
            metadata={"best_rsi": best_rsi, "opportunities": opportunities},
        )

    def get_rebalance_dates(
        self,
        start_date: date,
        end_date: date,
    ) -> list[date]:
        """Mean reversion checks daily (business days)."""
        dates = pd.bdate_range(start=start_date, end=end_date)
        return [d.date() for d in dates]
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_mean_reversion.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/aurel2/strategies/mean_reversion.py tests/test_mean_reversion.py
git commit -m "feat: add mean reversion strategy with RSI signals"
```

---

### Task 2.3: Implement Multi-Timeframe Trend Strategy

**Files:**
- Create: `src/aurel2/strategies/multi_timeframe.py`
- Test: `tests/test_multi_timeframe.py`

**Step 1: Write the failing test**

Create `tests/test_multi_timeframe.py`:

```python
"""Tests for multi-timeframe trend strategy."""
import pytest
from datetime import date
import pandas as pd
import numpy as np
from aurel2.strategies.multi_timeframe import MultiTimeframeTrendStrategy
from aurel2.core.models import AssetClass, SignalAction


@pytest.fixture
def uptrend_prices():
    """Create price data with clear uptrend across all timeframes."""
    dates = pd.date_range("2023-01-01", periods=400, freq="D")
    # Steady uptrend
    prices = 100 * (1.0005 ** np.arange(400))  # ~20% annual growth

    return pd.DataFrame({
        "date": dates,
        "close": prices,
        "symbol": ["SPY"] * 400,
    })


def test_multi_timeframe_calculates_blended_momentum(uptrend_prices):
    """Strategy should blend 3, 6, 12 month momentum."""
    strategy = MultiTimeframeTrendStrategy(
        lookback_months=[3, 6, 12],
        weights=[0.4, 0.35, 0.25],
    )

    signal = strategy.generate_signal(
        prices=uptrend_prices,
        calc_date=date(2024, 2, 1),
        current_holding=None,
    )

    assert signal.strategy_name == "multi_timeframe_trend"
    assert "momentum_3m" in signal.metadata
    assert "momentum_6m" in signal.metadata
    assert "momentum_12m" in signal.metadata
    assert "blended_momentum" in signal.metadata


def test_multi_timeframe_signals_buy_in_uptrend(uptrend_prices):
    """Strategy should signal BUY in clear uptrend."""
    strategy = MultiTimeframeTrendStrategy()

    signal = strategy.generate_signal(
        prices=uptrend_prices,
        calc_date=date(2024, 2, 1),
        current_holding=None,
    )

    assert signal.action == SignalAction.BUY
    assert signal.confidence > 0.6


def test_multi_timeframe_rebalances_monthly():
    """Strategy should rebalance monthly."""
    strategy = MultiTimeframeTrendStrategy()

    dates = strategy.get_rebalance_dates(date(2024, 1, 1), date(2024, 6, 30))

    # Should have ~6 monthly dates
    assert 5 <= len(dates) <= 7
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_multi_timeframe.py -v`
Expected: FAIL with "No module named 'aurel2.strategies.multi_timeframe'"

**Step 3: Write minimal implementation**

Create `src/aurel2/strategies/multi_timeframe.py`:

```python
"""Multi-Timeframe Trend Strategy - faster reaction than pure 12-month momentum."""

from datetime import date
from dateutil.relativedelta import relativedelta

import pandas as pd
import structlog

from aurel2.core.models import AssetClass, SignalAction
from aurel2.core.assets import ASSET_REGISTRY
from aurel2.strategies.base import BaseStrategy, StrategySignal

logger = structlog.get_logger()


class MultiTimeframeTrendStrategy(BaseStrategy):
    """
    Multi-Timeframe Trend Strategy.

    Blends momentum signals from multiple lookback periods:
    - 3-month: Fast, catches recent trends
    - 6-month: Medium, balances noise and signal
    - 12-month: Slow, traditional momentum

    Weighted average of all timeframes provides faster reaction
    than pure 12-month momentum while reducing whipsaws.
    """

    name = "multi_timeframe_trend"

    def __init__(
        self,
        lookback_months: list[int] | None = None,
        weights: list[float] | None = None,
        switch_threshold: float = 0.05,
        target_assets: list[AssetClass] | None = None,
    ):
        self.lookback_months = lookback_months or [3, 6, 12]
        self.weights = weights or [0.4, 0.35, 0.25]  # Weight toward shorter term
        self.switch_threshold = switch_threshold
        self.target_assets = target_assets or [
            AssetClass.US_STOCKS,
            AssetClass.INTL_DEVELOPED,
            AssetClass.EMERGING_MARKETS,
            AssetClass.BONDS_AGGREGATE,
            AssetClass.GOLD,
        ]
        self.current_holding: AssetClass | None = None

        if len(self.lookback_months) != len(self.weights):
            raise ValueError("lookback_months and weights must have same length")
        if abs(sum(self.weights) - 1.0) > 0.01:
            raise ValueError("weights must sum to 1.0")

    def _calculate_momentum(
        self,
        prices: pd.DataFrame,
        symbol: str,
        calc_date: date,
        months: int,
    ) -> float | None:
        """Calculate momentum for a specific lookback period."""
        lookback_date = calc_date - relativedelta(months=months)

        symbol_prices = prices[prices["symbol"] == symbol].copy()
        if symbol_prices.empty:
            return None

        symbol_prices["date"] = pd.to_datetime(symbol_prices["date"])

        # Current price
        current = symbol_prices[symbol_prices["date"] <= pd.Timestamp(calc_date)]
        if current.empty:
            return None
        current_price = float(current.iloc[-1]["close"])

        # Past price
        past = symbol_prices[symbol_prices["date"] <= pd.Timestamp(lookback_date)]
        if past.empty:
            return None
        past_price = float(past.iloc[-1]["close"])

        if past_price == 0:
            return None

        return (current_price / past_price) - 1

    def _calculate_blended_momentum(
        self,
        prices: pd.DataFrame,
        symbol: str,
        calc_date: date,
    ) -> tuple[float | None, dict]:
        """Calculate weighted average momentum across timeframes."""
        momentums = {}

        for months in self.lookback_months:
            mom = self._calculate_momentum(prices, symbol, calc_date, months)
            momentums[f"momentum_{months}m"] = mom

        # If any momentum is None, can't calculate blend
        values = [momentums[f"momentum_{m}m"] for m in self.lookback_months]
        if None in values:
            return None, momentums

        # Weighted average
        blended = sum(v * w for v, w in zip(values, self.weights))
        momentums["blended_momentum"] = blended

        return blended, momentums

    def generate_signal(
        self,
        prices: pd.DataFrame,
        calc_date: date,
        current_holding: AssetClass | None = None,
    ) -> StrategySignal:
        """Generate multi-timeframe trend signal."""

        if current_holding is not None:
            self.current_holding = current_holding

        # Calculate blended momentum for all assets
        scores = {}
        all_metadata = {}

        for asset_class in self.target_assets:
            asset = ASSET_REGISTRY.get(asset_class)
            if not asset or not asset.yahoo_symbol:
                continue

            blended, momentums = self._calculate_blended_momentum(
                prices, asset.yahoo_symbol, calc_date
            )

            if blended is not None:
                scores[asset_class] = blended
                all_metadata[asset_class.value] = momentums

        if not scores:
            return StrategySignal(
                strategy_name=self.name,
                date=calc_date,
                action=SignalAction.HOLD,
                asset_class=self.current_holding,
                confidence=0.3,
                reasoning="Insufficient data for momentum calculation",
                metadata={},
            )

        # Find winner
        winner = max(scores.keys(), key=lambda k: scores[k])
        winner_score = scores[winner]

        # Log scores
        for ac, score in sorted(scores.items(), key=lambda x: x[1], reverse=True):
            logger.info(
                "multi_tf_score",
                date=str(calc_date),
                asset=ac.value,
                blended=f"{score:.2%}",
            )

        # Decision logic
        if self.current_holding is None:
            # No position - buy winner if positive
            if winner_score > 0:
                return StrategySignal(
                    strategy_name=self.name,
                    date=calc_date,
                    action=SignalAction.BUY,
                    asset_class=winner,
                    confidence=min(0.9, 0.5 + winner_score),
                    reasoning=f"Initial buy: {winner.value} has best blended momentum ({winner_score:.2%})",
                    metadata=all_metadata.get(winner.value, {}),
                )
            else:
                return StrategySignal(
                    strategy_name=self.name,
                    date=calc_date,
                    action=SignalAction.HOLD,
                    asset_class=None,
                    confidence=0.6,
                    reasoning=f"All assets negative. Best: {winner.value} ({winner_score:.2%})",
                    metadata=all_metadata,
                )

        # Have position - check if should switch
        current_score = scores.get(self.current_holding, 0)
        diff = winner_score - current_score

        if winner != self.current_holding and diff > self.switch_threshold:
            self.current_holding = winner
            return StrategySignal(
                strategy_name=self.name,
                date=calc_date,
                action=SignalAction.BUY,
                asset_class=winner,
                confidence=min(0.9, 0.5 + diff),
                reasoning=f"Switch from {current_holding.value} ({current_score:.2%}) to {winner.value} ({winner_score:.2%}). Diff: {diff:.2%}",
                metadata=all_metadata.get(winner.value, {}),
            )

        # Hold
        return StrategySignal(
            strategy_name=self.name,
            date=calc_date,
            action=SignalAction.HOLD,
            asset_class=self.current_holding,
            confidence=0.5,
            reasoning=f"Holding {self.current_holding.value} ({current_score:.2%}). Winner {winner.value} ({winner_score:.2%}) diff {diff:.2%} < {self.switch_threshold:.2%}",
            metadata=all_metadata.get(self.current_holding.value, {}),
        )

    def get_rebalance_dates(
        self,
        start_date: date,
        end_date: date,
    ) -> list[date]:
        """Monthly rebalancing."""
        dates = pd.date_range(start=start_date, end=end_date, freq="ME")
        return [d.date() for d in dates]
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_multi_timeframe.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/aurel2/strategies/multi_timeframe.py tests/test_multi_timeframe.py
git commit -m "feat: add multi-timeframe trend strategy"
```

---

## Phase 3: MCP Server

### Task 3.1: Create MCP Server Foundation

**Files:**
- Create: `src/aurel2/mcp/__init__.py`
- Create: `src/aurel2/mcp/server.py`
- Modify: `pyproject.toml` (add mcp dependency)
- Test: `tests/test_mcp_server.py`

**Step 1: Add MCP dependency**

Update `pyproject.toml`:

```toml
[project.optional-dependencies]
# ... existing ...
mcp = [
    "mcp>=1.0.0",
]
agent = [
    "mcp>=1.0.0",
    "httpx>=0.25.0",
    "schedule>=1.2.0",
]
```

Run: `pip install -e ".[agent]"`

**Step 2: Write the failing test**

Create `tests/test_mcp_server.py`:

```python
"""Tests for MCP server."""
import pytest
from aurel2.mcp.server import Aurel2MCPServer


def test_server_has_required_tools():
    """Server should expose all required tools."""
    server = Aurel2MCPServer()
    tools = server.list_tools()

    tool_names = [t["name"] for t in tools]

    assert "get_momentum_scores" in tool_names
    assert "get_regime" in tool_names
    assert "get_rsi" in tool_names
    assert "get_portfolio" in tool_names
    assert "get_strategy_signals" in tool_names
    assert "get_market_context" in tool_names
    assert "execute_trade" in tool_names


def test_server_get_regime_returns_valid_regime():
    """get_regime should return bull, bear, or neutral."""
    server = Aurel2MCPServer()

    result = server.call_tool("get_regime", {})

    assert result["regime"] in ["bull", "bear", "neutral"]
    assert "spy_price" in result
    assert "ma_200" in result
```

**Step 3: Run test to verify it fails**

Run: `pytest tests/test_mcp_server.py -v`
Expected: FAIL with "No module named 'aurel2.mcp'"

**Step 4: Write minimal implementation**

Create `src/aurel2/mcp/__init__.py`:

```python
"""MCP server for Aurel2."""
```

Create `src/aurel2/mcp/server.py`:

```python
"""MCP Server exposing Aurel2 strategy and market data."""

from datetime import date, timedelta
from typing import Any

import structlog

from aurel2.core.models import AssetClass
from aurel2.core.assets import ASSET_REGISTRY, get_all_yahoo_symbols
from aurel2.data.providers.yahoo import YahooFinanceProvider
from aurel2.data.indicators import calculate_rsi, calculate_drawdown, calculate_moving_average, get_current_price
from aurel2.data.momentum import calculate_momentum_scores
from aurel2.strategies.base import StrategySignal
from aurel2.strategies.dual_momentum import DualMomentumStrategy
from aurel2.strategies.mean_reversion import MeanReversionStrategy
from aurel2.strategies.multi_timeframe import MultiTimeframeTrendStrategy
from aurel2.persistence.portfolio import PortfolioStore

logger = structlog.get_logger()


class Aurel2MCPServer:
    """
    MCP Server for Aurel2 trading system.

    Exposes tools for:
    - Market data (momentum, RSI, regime)
    - Strategy signals
    - Portfolio state
    - Trade execution
    """

    def __init__(self):
        self.provider = YahooFinanceProvider()
        self.portfolio_store = PortfolioStore()
        self._price_cache: dict[str, Any] = {}
        self._cache_date: date | None = None

        # Initialize strategies
        self.strategies = {
            "dual_momentum": DualMomentumStrategy(
                assets=ASSET_REGISTRY,
                lookback_months=12,
                switch_threshold=0.05,
                cash_rate=-999,  # No cash rule
            ),
            "mean_reversion": MeanReversionStrategy(),
            "multi_timeframe": MultiTimeframeTrendStrategy(),
        }

    def _get_prices(self, lookback_days: int = 400) -> Any:
        """Get cached price data."""
        today = date.today()

        if self._cache_date != today or not self._price_cache:
            start = today - timedelta(days=lookback_days)
            symbols = get_all_yahoo_symbols()
            self._price_cache = self.provider.get_multi_prices(symbols, start, today)
            self._cache_date = today
            logger.info("refreshed_price_cache", symbols=len(symbols))

        return self._price_cache

    def list_tools(self) -> list[dict]:
        """List available tools."""
        return [
            {
                "name": "get_momentum_scores",
                "description": "Get momentum scores for all assets",
                "parameters": {
                    "lookback_months": {"type": "integer", "default": 12},
                },
            },
            {
                "name": "get_regime",
                "description": "Get current market regime (bull/bear/neutral)",
                "parameters": {},
            },
            {
                "name": "get_rsi",
                "description": "Get RSI for a symbol",
                "parameters": {
                    "symbol": {"type": "string", "required": True},
                    "period": {"type": "integer", "default": 14},
                },
            },
            {
                "name": "get_portfolio",
                "description": "Get current portfolio holdings",
                "parameters": {},
            },
            {
                "name": "get_strategy_signals",
                "description": "Get signals from all strategies",
                "parameters": {},
            },
            {
                "name": "get_market_context",
                "description": "Get market context (VIX proxy, drawdown, etc.)",
                "parameters": {},
            },
            {
                "name": "execute_trade",
                "description": "Execute a trade via IBKR",
                "parameters": {
                    "action": {"type": "string", "enum": ["BUY", "SELL"]},
                    "symbol": {"type": "string"},
                    "quantity": {"type": "number"},
                },
            },
        ]

    def call_tool(self, name: str, args: dict) -> dict:
        """Call a tool by name."""
        if name == "get_momentum_scores":
            return self._get_momentum_scores(args.get("lookback_months", 12))
        elif name == "get_regime":
            return self._get_regime()
        elif name == "get_rsi":
            return self._get_rsi(args["symbol"], args.get("period", 14))
        elif name == "get_portfolio":
            return self._get_portfolio()
        elif name == "get_strategy_signals":
            return self._get_strategy_signals()
        elif name == "get_market_context":
            return self._get_market_context()
        elif name == "execute_trade":
            return self._execute_trade(args["action"], args["symbol"], args["quantity"])
        else:
            raise ValueError(f"Unknown tool: {name}")

    def _get_momentum_scores(self, lookback_months: int = 12) -> dict:
        """Get momentum scores for all assets."""
        prices = self._get_prices()
        today = date.today()

        scores = calculate_momentum_scores(
            prices=prices,
            assets=ASSET_REGISTRY,
            calc_date=today,
            lookback_months=lookback_months,
            cash_rate=0.04,
        )

        return {
            "date": today.isoformat(),
            "lookback_months": lookback_months,
            "scores": {
                ac.value: {
                    "momentum": score.momentum_12m,
                    "price": score.price,
                    "symbol": score.asset.symbol,
                }
                for ac, score in scores.items()
            },
        }

    def _get_regime(self) -> dict:
        """Detect market regime using SPY vs 200-day MA."""
        prices = self._get_prices()

        spy_price = get_current_price(prices, "SPY")
        ma_200 = calculate_moving_average(prices, "SPY", 200)

        if spy_price is None or ma_200 is None:
            regime = "neutral"
        elif spy_price > ma_200 * 1.02:  # 2% above MA
            regime = "bull"
        elif spy_price < ma_200 * 0.98:  # 2% below MA
            regime = "bear"
        else:
            regime = "neutral"

        return {
            "regime": regime,
            "spy_price": spy_price,
            "ma_200": ma_200,
            "price_vs_ma": (spy_price / ma_200 - 1) if (spy_price and ma_200) else None,
        }

    def _get_rsi(self, symbol: str, period: int = 14) -> dict:
        """Get RSI for a symbol."""
        prices = self._get_prices()
        rsi = calculate_rsi(prices, symbol, period)

        return {
            "symbol": symbol,
            "rsi": rsi,
            "period": period,
            "oversold": rsi < 30 if rsi else False,
            "overbought": rsi > 70 if rsi else False,
        }

    def _get_portfolio(self) -> dict:
        """Get current portfolio."""
        portfolio = self.portfolio_store.load()

        return {
            "cash": portfolio.cash,
            "total_invested": portfolio.total_invested,
            "holdings": [h.to_dict() for h in portfolio.holdings],
        }

    def _get_strategy_signals(self) -> dict:
        """Get signals from all strategies."""
        prices = self._get_prices()
        today = date.today()

        # Get current holding
        portfolio = self.portfolio_store.load()
        current_holding = None
        if portfolio.holdings:
            # Map symbol to asset class
            symbol = portfolio.holdings[0].symbol.upper()
            for ac, asset in ASSET_REGISTRY.items():
                if asset.symbol == symbol or asset.yahoo_symbol == symbol:
                    current_holding = ac
                    break

        signals = {}
        for name, strategy in self.strategies.items():
            try:
                signal = strategy.generate_signal(prices, today, current_holding)
                signals[name] = signal.to_dict()
            except Exception as e:
                logger.error("strategy_error", strategy=name, error=str(e))
                signals[name] = {"error": str(e)}

        return {
            "date": today.isoformat(),
            "current_holding": current_holding.value if current_holding else None,
            "signals": signals,
        }

    def _get_market_context(self) -> dict:
        """Get market context data."""
        prices = self._get_prices()

        spy_drawdown = calculate_drawdown(prices, "SPY")
        spy_rsi = calculate_rsi(prices, "SPY", 14)
        regime = self._get_regime()

        return {
            "date": date.today().isoformat(),
            "regime": regime["regime"],
            "spy_drawdown": spy_drawdown,
            "spy_rsi": spy_rsi,
            "extreme_conditions": spy_drawdown and spy_drawdown > 0.10,
        }

    def _execute_trade(self, action: str, symbol: str, quantity: float) -> dict:
        """Execute trade via IBKR (placeholder - needs broker connection)."""
        logger.warning(
            "trade_execution_placeholder",
            action=action,
            symbol=symbol,
            quantity=quantity,
        )

        return {
            "status": "pending",
            "message": "Trade execution requires IBKR connection",
            "action": action,
            "symbol": symbol,
            "quantity": quantity,
        }
```

**Step 5: Run test to verify it passes**

Run: `pytest tests/test_mcp_server.py -v`
Expected: PASS

**Step 6: Commit**

```bash
git add src/aurel2/mcp/ tests/test_mcp_server.py pyproject.toml
git commit -m "feat: add MCP server with strategy and market data tools"
```

---

## Phase 4: Agent Orchestrator

### Task 4.1: Create Agent Core

**Files:**
- Create: `src/aurel2/agent/__init__.py`
- Create: `src/aurel2/agent/orchestrator.py`
- Test: `tests/test_agent.py`

**Step 1: Write the failing test**

Create `tests/test_agent.py`:

```python
"""Tests for agent orchestrator."""
import pytest
from datetime import time
from aurel2.agent.orchestrator import AgentOrchestrator, DecisionType, AgentDecision
from aurel2.core.models import SignalAction


def test_decision_type_classification():
    """Agent should classify decisions correctly."""
    orchestrator = AgentOrchestrator()

    # All agree = routine
    signals = {
        "dual_momentum": {"action": "hold"},
        "mean_reversion": {"action": "hold"},
        "multi_timeframe": {"action": "hold"},
    }
    assert orchestrator._classify_decision(signals) == DecisionType.ROUTINE

    # Disagree = non-routine
    signals = {
        "dual_momentum": {"action": "hold"},
        "mean_reversion": {"action": "buy"},
        "multi_timeframe": {"action": "hold"},
    }
    assert orchestrator._classify_decision(signals) == DecisionType.NON_ROUTINE


def test_is_sleep_hours():
    """Should correctly identify sleep hours in Romania."""
    orchestrator = AgentOrchestrator(
        timezone="Europe/Bucharest",
        sleep_start=time(23, 0),
        sleep_end=time(8, 0),
    )

    # 2am Romania = sleep
    assert orchestrator._is_sleep_hours(time(2, 0)) == True

    # 10am Romania = awake
    assert orchestrator._is_sleep_hours(time(10, 0)) == False

    # 11:30pm Romania = sleep
    assert orchestrator._is_sleep_hours(time(23, 30)) == True


def test_agent_decision_dataclass():
    """AgentDecision should have required fields."""
    decision = AgentDecision(
        decision_type=DecisionType.ROUTINE,
        action=SignalAction.HOLD,
        asset_symbol="SPY",
        reasoning="All strategies agree to hold",
        confidence=0.9,
        strategy_signals={},
        requires_approval=False,
        timeout_hours=0,
    )

    assert decision.decision_type == DecisionType.ROUTINE
    assert decision.requires_approval == False
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_agent.py -v`
Expected: FAIL with "No module named 'aurel2.agent'"

**Step 3: Write minimal implementation**

Create `src/aurel2/agent/__init__.py`:

```python
"""AI Agent for strategy selection."""
```

Create `src/aurel2/agent/orchestrator.py`:

```python
"""Agent orchestrator for multi-strategy selection."""

from dataclasses import dataclass, field
from datetime import datetime, time, date
from enum import Enum
from typing import Any
import pytz

import structlog

from aurel2.core.models import SignalAction, AssetClass
from aurel2.mcp.server import Aurel2MCPServer

logger = structlog.get_logger()


class DecisionType(str, Enum):
    """Type of decision for approval routing."""
    ROUTINE = "routine"          # Auto-execute
    NON_ROUTINE = "non_routine"  # Needs approval
    URGENT = "urgent"            # Needs approval, short timeout


class Urgency(str, Enum):
    """Urgency level for timeout calculation."""
    LOW = "low"        # 24-48 hours
    MEDIUM = "medium"  # 4-8 hours
    HIGH = "high"      # 1-2 hours


@dataclass
class AgentDecision:
    """Decision made by the agent."""
    decision_type: DecisionType
    action: SignalAction
    asset_symbol: str | None
    reasoning: str
    confidence: float
    strategy_signals: dict[str, Any]
    requires_approval: bool
    timeout_hours: float
    urgency: Urgency = Urgency.LOW
    market_context: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "decision_type": self.decision_type.value,
            "action": self.action.value,
            "asset_symbol": self.asset_symbol,
            "reasoning": self.reasoning,
            "confidence": self.confidence,
            "requires_approval": self.requires_approval,
            "timeout_hours": self.timeout_hours,
            "urgency": self.urgency.value,
        }


class AgentOrchestrator:
    """
    Orchestrates multi-strategy decision making.

    Responsibilities:
    1. Query all strategies via MCP
    2. Analyze and reconcile signals
    3. Classify decision (routine/non-routine)
    4. Handle approval flow
    5. Execute or defer
    """

    def __init__(
        self,
        timezone: str = "Europe/Bucharest",
        sleep_start: time = time(23, 0),
        sleep_end: time = time(8, 0),
    ):
        self.mcp = Aurel2MCPServer()
        self.timezone = pytz.timezone(timezone)
        self.sleep_start = sleep_start
        self.sleep_end = sleep_end

    def _is_sleep_hours(self, current_time: time | None = None) -> bool:
        """Check if current time is within sleep hours."""
        if current_time is None:
            now = datetime.now(self.timezone)
            current_time = now.time()

        # Handle overnight sleep (23:00 - 08:00)
        if self.sleep_start > self.sleep_end:
            return current_time >= self.sleep_start or current_time < self.sleep_end
        else:
            return self.sleep_start <= current_time < self.sleep_end

    def _classify_decision(self, signals: dict[str, dict]) -> DecisionType:
        """Classify decision based on strategy agreement."""
        actions = []
        for name, signal in signals.items():
            if "error" not in signal:
                actions.append(signal.get("action", "hold"))

        # All agree = routine
        if len(set(actions)) == 1:
            return DecisionType.ROUTINE

        # Check for urgent conditions (any strategy says sell urgently)
        for signal in signals.values():
            if signal.get("action") == "sell" and signal.get("confidence", 0) > 0.8:
                return DecisionType.URGENT

        return DecisionType.NON_ROUTINE

    def _calculate_timeout(self, decision_type: DecisionType, urgency: Urgency) -> float:
        """Calculate timeout in hours based on urgency."""
        if decision_type == DecisionType.ROUTINE:
            return 0  # No timeout, auto-execute

        timeouts = {
            Urgency.LOW: 36,    # 24-48 hours
            Urgency.MEDIUM: 6,  # 4-8 hours
            Urgency.HIGH: 1.5,  # 1-2 hours
        }
        return timeouts[urgency]

    def _determine_urgency(self, signals: dict, market_context: dict) -> Urgency:
        """Determine urgency based on market conditions."""
        # Check for extreme conditions
        if market_context.get("extreme_conditions"):
            return Urgency.HIGH

        # Check drawdown
        drawdown = market_context.get("spy_drawdown", 0)
        if drawdown and drawdown > 0.15:
            return Urgency.HIGH
        elif drawdown and drawdown > 0.08:
            return Urgency.MEDIUM

        return Urgency.LOW

    def _select_best_action(self, signals: dict, market_context: dict) -> tuple[SignalAction, str | None, str, float]:
        """
        Select best action based on strategy signals and market context.

        Returns: (action, asset_symbol, reasoning, confidence)
        """
        # Count votes
        action_votes = {"hold": 0, "buy": 0, "sell": 0}
        buy_targets = {}
        total_confidence = 0

        for name, signal in signals.items():
            if "error" in signal:
                continue

            action = signal.get("action", "hold")
            confidence = signal.get("confidence", 0.5)
            action_votes[action] += confidence
            total_confidence += confidence

            if action == "buy" and signal.get("asset_class"):
                target = signal["asset_class"]
                buy_targets[target] = buy_targets.get(target, 0) + confidence

        # Determine winning action
        best_action = max(action_votes.keys(), key=lambda k: action_votes[k])

        # Determine target asset for buy
        asset_symbol = None
        if best_action == "buy" and buy_targets:
            best_target = max(buy_targets.keys(), key=lambda k: buy_targets[k])
            # Get symbol from asset class
            from aurel2.core.assets import ASSET_REGISTRY
            for ac, asset in ASSET_REGISTRY.items():
                if ac.value == best_target:
                    asset_symbol = asset.symbol
                    break

        # Build reasoning
        reasoning_parts = []
        for name, signal in signals.items():
            if "error" not in signal:
                reasoning_parts.append(f"{name}: {signal.get('action', 'hold').upper()}")

        reasoning = f"Strategies: {', '.join(reasoning_parts)}. "
        reasoning += f"Consensus: {best_action.upper()} with {action_votes[best_action]:.1f} weighted votes."

        avg_confidence = total_confidence / len([s for s in signals.values() if "error" not in s])

        return SignalAction(best_action), asset_symbol, reasoning, avg_confidence

    def analyze(self) -> AgentDecision:
        """
        Analyze current market and generate decision.

        Returns:
            AgentDecision with recommendation
        """
        logger.info("agent_analysis_started")

        # Get data from MCP
        signals_data = self.mcp.call_tool("get_strategy_signals", {})
        market_context = self.mcp.call_tool("get_market_context", {})
        regime = self.mcp.call_tool("get_regime", {})

        signals = signals_data.get("signals", {})

        # Classify decision
        decision_type = self._classify_decision(signals)
        urgency = self._determine_urgency(signals, market_context)

        # Select best action
        action, asset_symbol, reasoning, confidence = self._select_best_action(signals, market_context)

        # Determine approval requirement
        is_sleep = self._is_sleep_hours()
        requires_approval = decision_type != DecisionType.ROUTINE

        # If sleeping and urgent, don't require approval
        if is_sleep and urgency == Urgency.HIGH:
            requires_approval = False
            reasoning += " [Auto-executed during sleep due to urgency]"

        timeout = self._calculate_timeout(decision_type, urgency)

        decision = AgentDecision(
            decision_type=decision_type,
            action=action,
            asset_symbol=asset_symbol,
            reasoning=reasoning,
            confidence=confidence,
            strategy_signals=signals,
            requires_approval=requires_approval,
            timeout_hours=timeout,
            urgency=urgency,
            market_context={
                "regime": regime.get("regime"),
                "spy_drawdown": market_context.get("spy_drawdown"),
                "spy_rsi": market_context.get("spy_rsi"),
            },
        )

        logger.info(
            "agent_decision",
            decision_type=decision_type.value,
            action=action.value,
            requires_approval=requires_approval,
            urgency=urgency.value,
        )

        return decision

    def execute(self, decision: AgentDecision) -> dict:
        """Execute a decision."""
        if decision.action == SignalAction.HOLD:
            logger.info("agent_hold", reasoning=decision.reasoning)
            return {"status": "hold", "message": "No action needed"}

        if decision.action == SignalAction.BUY and decision.asset_symbol:
            # TODO: Calculate quantity based on portfolio
            result = self.mcp.call_tool("execute_trade", {
                "action": "BUY",
                "symbol": decision.asset_symbol,
                "quantity": 0,  # Placeholder
            })
            return result

        if decision.action == SignalAction.SELL:
            # TODO: Get current position to sell
            return {"status": "pending", "message": "Sell execution not yet implemented"}

        return {"status": "error", "message": "Unknown action"}
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_agent.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/aurel2/agent/ tests/test_agent.py
git commit -m "feat: add agent orchestrator with decision classification"
```

---

## Phase 5: Notifications

### Task 5.1: Create Ntfy Notification Service

**Files:**
- Create: `src/aurel2/notifications/__init__.py`
- Create: `src/aurel2/notifications/ntfy.py`
- Test: `tests/test_notifications.py`

**Step 1: Write the failing test**

Create `tests/test_notifications.py`:

```python
"""Tests for notification services."""
import pytest
from unittest.mock import patch, MagicMock
from aurel2.notifications.ntfy import NtfyNotifier


def test_ntfy_formats_approval_message():
    """Should format approval request correctly."""
    notifier = NtfyNotifier(topic="test-topic")

    message = notifier._format_approval_message(
        action="BUY",
        symbol="SPY",
        reasoning="All strategies agree",
        approval_url="https://example.com/approve/123",
    )

    assert "BUY" in message
    assert "SPY" in message
    assert "https://example.com/approve/123" in message


def test_ntfy_formats_execution_message():
    """Should format execution notification correctly."""
    notifier = NtfyNotifier(topic="test-topic")

    message = notifier._format_execution_message(
        action="BUY",
        symbol="SPY",
        price=542.30,
        reasoning="Routine rebalance",
    )

    assert "BUY" in message
    assert "SPY" in message
    assert "542.30" in message


@patch("httpx.post")
def test_ntfy_send_notification(mock_post):
    """Should send HTTP POST to ntfy.sh."""
    mock_post.return_value = MagicMock(status_code=200)

    notifier = NtfyNotifier(topic="test-topic")
    result = notifier.send("Test message", priority="high")

    assert result == True
    mock_post.assert_called_once()
    call_url = mock_post.call_args[0][0]
    assert "ntfy.sh/test-topic" in call_url
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_notifications.py -v`
Expected: FAIL with "No module named 'aurel2.notifications'"

**Step 3: Write minimal implementation**

Create `src/aurel2/notifications/__init__.py`:

```python
"""Notification services."""
```

Create `src/aurel2/notifications/ntfy.py`:

```python
"""Ntfy.sh push notification service."""

import httpx
import structlog

logger = structlog.get_logger()


class NtfyNotifier:
    """
    Send push notifications via ntfy.sh.

    Usage:
        notifier = NtfyNotifier(topic="aurel2-yourname")
        notifier.send("Hello!", priority="high")
    """

    def __init__(
        self,
        topic: str,
        server: str = "https://ntfy.sh",
    ):
        self.topic = topic
        self.server = server
        self.url = f"{server}/{topic}"

    def send(
        self,
        message: str,
        title: str | None = None,
        priority: str = "default",
        tags: list[str] | None = None,
        click_url: str | None = None,
        actions: list[dict] | None = None,
    ) -> bool:
        """
        Send a notification.

        Args:
            message: Notification body
            title: Notification title
            priority: min, low, default, high, urgent
            tags: Emoji tags (e.g., ["chart_with_upwards_trend"])
            click_url: URL to open when notification is clicked
            actions: Action buttons

        Returns:
            True if sent successfully
        """
        headers = {}

        if title:
            headers["Title"] = title
        if priority:
            headers["Priority"] = priority
        if tags:
            headers["Tags"] = ",".join(tags)
        if click_url:
            headers["Click"] = click_url
        if actions:
            # Format: action=view, Open, https://example.com
            action_strs = []
            for a in actions:
                action_strs.append(f"{a['type']}, {a['label']}, {a['url']}")
            headers["Actions"] = "; ".join(action_strs)

        try:
            response = httpx.post(
                self.url,
                content=message,
                headers=headers,
                timeout=10,
            )

            if response.status_code == 200:
                logger.info("notification_sent", topic=self.topic)
                return True
            else:
                logger.error("notification_failed", status=response.status_code)
                return False

        except Exception as e:
            logger.error("notification_error", error=str(e))
            return False

    def _format_approval_message(
        self,
        action: str,
        symbol: str,
        reasoning: str,
        approval_url: str,
    ) -> str:
        """Format approval request message."""
        return f"""🔔 Aurel2: Decision Needed

Action: {action} {symbol}
Reasoning: {reasoning}

Tap to review and approve:
{approval_url}"""

    def _format_execution_message(
        self,
        action: str,
        symbol: str,
        price: float,
        reasoning: str,
    ) -> str:
        """Format execution notification message."""
        emoji = "✅" if action == "BUY" else "🔴"
        return f"""{emoji} Aurel2: Trade Executed

{action} {symbol} @ ${price:.2f}
Reason: {reasoning}"""

    def _format_autonomous_message(
        self,
        action: str,
        symbol: str,
        price: float,
        reasoning: str,
        wait_time: str,
    ) -> str:
        """Format autonomous action message."""
        return f"""⚡ Aurel2: Acted Without Approval

{action} {symbol} @ ${price:.2f}
Reason: {reasoning}

No response after {wait_time}."""

    def send_approval_request(
        self,
        action: str,
        symbol: str,
        reasoning: str,
        approval_url: str,
        confidence: float,
    ) -> bool:
        """Send approval request notification."""
        message = self._format_approval_message(action, symbol, reasoning, approval_url)

        return self.send(
            message=message,
            title="Aurel2: Approval Needed",
            priority="high" if confidence > 0.7 else "default",
            tags=["chart_with_upwards_trend", "moneybag"],
            click_url=approval_url,
            actions=[
                {"type": "view", "label": "Review", "url": approval_url},
            ],
        )

    def send_execution_notice(
        self,
        action: str,
        symbol: str,
        price: float,
        reasoning: str,
    ) -> bool:
        """Send trade execution notification."""
        message = self._format_execution_message(action, symbol, price, reasoning)

        return self.send(
            message=message,
            title="Aurel2: Trade Executed",
            priority="default",
            tags=["white_check_mark"],
        )

    def send_autonomous_notice(
        self,
        action: str,
        symbol: str,
        price: float,
        reasoning: str,
        wait_time: str,
    ) -> bool:
        """Send autonomous action notification."""
        message = self._format_autonomous_message(action, symbol, price, reasoning, wait_time)

        return self.send(
            message=message,
            title="Aurel2: Autonomous Action",
            priority="high",
            tags=["zap", "robot"],
        )
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_notifications.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/aurel2/notifications/ tests/test_notifications.py
git commit -m "feat: add Ntfy notification service"
```

---

## Phase 6: Serverless Approval Endpoint

### Task 6.1: Create Vercel Approval App

**Files:**
- Create: `approval-endpoint/` directory (separate from main project)
- Create: `approval-endpoint/api/decision/[id].ts`
- Create: `approval-endpoint/package.json`
- Create: `approval-endpoint/vercel.json`

**Note:** This is a separate mini-project to deploy on Vercel.

**Step 1: Create project structure**

```bash
mkdir -p approval-endpoint/api/decision
cd approval-endpoint
```

**Step 2: Create package.json**

Create `approval-endpoint/package.json`:

```json
{
  "name": "aurel2-approval",
  "version": "1.0.0",
  "private": true,
  "scripts": {
    "dev": "vercel dev"
  },
  "dependencies": {
    "@vercel/kv": "^1.0.0"
  }
}
```

**Step 3: Create vercel.json**

Create `approval-endpoint/vercel.json`:

```json
{
  "version": 2,
  "routes": [
    {
      "src": "/api/decision/(.*)",
      "dest": "/api/decision/[id].ts"
    }
  ]
}
```

**Step 4: Create API endpoint**

Create `approval-endpoint/api/decision/[id].ts`:

```typescript
import { kv } from '@vercel/kv';

interface Decision {
  id: string;
  action: string;
  symbol: string;
  reasoning: string;
  confidence: number;
  created_at: string;
  status: 'pending' | 'approved' | 'rejected';
  responded_at?: string;
}

export const config = {
  runtime: 'edge',
};

export default async function handler(request: Request) {
  const url = new URL(request.url);
  const id = url.pathname.split('/').pop();

  if (!id) {
    return new Response('Missing decision ID', { status: 400 });
  }

  // GET - Show approval page or return status
  if (request.method === 'GET') {
    const decision = await kv.get<Decision>(`decision:${id}`);

    if (!decision) {
      return new Response('Decision not found', { status: 404 });
    }

    // Check if JSON requested
    if (request.headers.get('Accept')?.includes('application/json')) {
      return Response.json(decision);
    }

    // Return HTML approval page
    return new Response(renderApprovalPage(decision, id), {
      headers: { 'Content-Type': 'text/html' },
    });
  }

  // POST - Create new decision
  if (request.method === 'POST') {
    const body = await request.json();

    const decision: Decision = {
      id,
      action: body.action,
      symbol: body.symbol,
      reasoning: body.reasoning,
      confidence: body.confidence,
      created_at: new Date().toISOString(),
      status: 'pending',
    };

    await kv.set(`decision:${id}`, decision, { ex: 86400 }); // Expire in 24h

    return Response.json({ success: true, id });
  }

  // PATCH - Respond to decision
  if (request.method === 'PATCH') {
    const body = await request.json();
    const decision = await kv.get<Decision>(`decision:${id}`);

    if (!decision) {
      return new Response('Decision not found', { status: 404 });
    }

    decision.status = body.approved ? 'approved' : 'rejected';
    decision.responded_at = new Date().toISOString();

    await kv.set(`decision:${id}`, decision, { ex: 86400 });

    return Response.json({ success: true, decision });
  }

  return new Response('Method not allowed', { status: 405 });
}

function renderApprovalPage(decision: Decision, id: string): string {
  const statusColor = decision.status === 'pending' ? '#f59e0b' :
                      decision.status === 'approved' ? '#22c55e' : '#ef4444';

  return `<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Aurel2 - Approve Decision</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, system-ui, sans-serif;
      background: #0f172a;
      color: #e2e8f0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 20px;
    }
    .card {
      background: #1e293b;
      border-radius: 16px;
      padding: 32px;
      max-width: 400px;
      width: 100%;
    }
    h1 { font-size: 1.5rem; margin-bottom: 24px; }
    .status {
      display: inline-block;
      padding: 4px 12px;
      border-radius: 20px;
      font-size: 0.8rem;
      font-weight: 600;
      background: ${statusColor}33;
      color: ${statusColor};
      margin-bottom: 24px;
    }
    .detail { margin-bottom: 16px; }
    .label { color: #94a3b8; font-size: 0.85rem; margin-bottom: 4px; }
    .value { font-size: 1.1rem; font-weight: 500; }
    .action-text { font-size: 2rem; font-weight: 700; color: #22c55e; }
    .buttons { display: flex; gap: 12px; margin-top: 24px; }
    button {
      flex: 1;
      padding: 14px;
      border: none;
      border-radius: 8px;
      font-size: 1rem;
      font-weight: 600;
      cursor: pointer;
    }
    .approve { background: #22c55e; color: #000; }
    .reject { background: #334155; color: #e2e8f0; }
    button:disabled { opacity: 0.5; cursor: not-allowed; }
    .reasoning {
      background: #0f172a;
      padding: 16px;
      border-radius: 8px;
      margin-top: 16px;
      font-size: 0.9rem;
      line-height: 1.5;
    }
  </style>
</head>
<body>
  <div class="card">
    <h1>🤖 Aurel2 Decision</h1>
    <span class="status">${decision.status.toUpperCase()}</span>

    <div class="detail">
      <div class="label">Action</div>
      <div class="action-text">${decision.action} ${decision.symbol}</div>
    </div>

    <div class="detail">
      <div class="label">Confidence</div>
      <div class="value">${(decision.confidence * 100).toFixed(0)}%</div>
    </div>

    <div class="reasoning">
      ${decision.reasoning}
    </div>

    ${decision.status === 'pending' ? `
    <div class="buttons">
      <button class="approve" onclick="respond(true)">✓ Approve</button>
      <button class="reject" onclick="respond(false)">✗ Reject</button>
    </div>
    ` : `
    <div class="detail" style="margin-top: 24px;">
      <div class="label">Responded at</div>
      <div class="value">${new Date(decision.responded_at!).toLocaleString()}</div>
    </div>
    `}
  </div>

  <script>
    async function respond(approved) {
      const buttons = document.querySelectorAll('button');
      buttons.forEach(b => b.disabled = true);

      const res = await fetch('/api/decision/${id}', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ approved }),
      });

      if (res.ok) {
        location.reload();
      } else {
        alert('Failed to submit response');
        buttons.forEach(b => b.disabled = false);
      }
    }
  </script>
</body>
</html>`;
}
```

**Step 5: Commit**

```bash
git add approval-endpoint/
git commit -m "feat: add Vercel approval endpoint"
```

---

## Phase 7: Integration & CLI

### Task 7.1: Add Agent CLI Commands

**Files:**
- Modify: `src/aurel2/cli.py`
- Test: Manual testing

**Step 1: Add agent commands to CLI**

Add to `src/aurel2/cli.py`:

```python
@app.command()
def agent(
    once: bool = typer.Option(False, "--once", help="Run once and exit"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Don't execute trades"),
):
    """Run the AI agent."""
    from aurel2.agent.orchestrator import AgentOrchestrator

    console.print("[bold blue]Aurel2 Agent[/bold blue]")

    orchestrator = AgentOrchestrator()
    decision = orchestrator.analyze()

    console.print(f"\n[bold]Decision:[/bold] {decision.action.value.upper()}")
    console.print(f"[bold]Asset:[/bold] {decision.asset_symbol or 'N/A'}")
    console.print(f"[bold]Type:[/bold] {decision.decision_type.value}")
    console.print(f"[bold]Confidence:[/bold] {decision.confidence:.0%}")
    console.print(f"[bold]Requires Approval:[/bold] {decision.requires_approval}")
    console.print(f"\n[dim]{decision.reasoning}[/dim]")

    if not dry_run and not decision.requires_approval:
        if decision.action.value != "hold":
            console.print("\n[yellow]Executing...[/yellow]")
            result = orchestrator.execute(decision)
            console.print(f"Result: {result}")
    elif decision.requires_approval:
        console.print("\n[yellow]This decision requires your approval.[/yellow]")
        console.print("In production, a notification would be sent.")


@app.command()
def strategies():
    """Show current signals from all strategies."""
    from aurel2.mcp.server import Aurel2MCPServer

    console.print("[bold blue]Strategy Signals[/bold blue]\n")

    mcp = Aurel2MCPServer()
    signals = mcp.call_tool("get_strategy_signals", {})

    for name, signal in signals.get("signals", {}).items():
        if "error" in signal:
            console.print(f"[red]{name}: ERROR - {signal['error']}[/red]")
        else:
            action = signal.get("action", "unknown").upper()
            color = "green" if action == "BUY" else "red" if action == "SELL" else "yellow"
            console.print(f"[{color}]{name}: {action}[/{color}]")
            console.print(f"  [dim]{signal.get('reasoning', '')}[/dim]")
```

**Step 2: Commit**

```bash
git add src/aurel2/cli.py
git commit -m "feat: add agent and strategies CLI commands"
```

---

## Summary

This implementation plan covers:

1. **Phase 1**: Extended asset universe (~15 ETFs) + RSI indicator
2. **Phase 2**: Two new strategies (Mean Reversion, Multi-Timeframe Trend)
3. **Phase 3**: MCP server with 7 tools
4. **Phase 4**: Agent orchestrator with decision classification
5. **Phase 5**: Ntfy push notifications
6. **Phase 6**: Vercel approval endpoint
7. **Phase 7**: CLI integration

**Estimated commits:** 12
**Estimated tests:** 15+

---

**Plan complete and saved to `docs/plans/2026-01-21-multi-strategy-agent-implementation.md`.**

**Two execution options:**

1. **Subagent-Driven (this session)** - I dispatch fresh subagent per task, review between tasks, fast iteration

2. **Parallel Session (separate)** - Open new session with executing-plans, batch execution with checkpoints

**Which approach?**
