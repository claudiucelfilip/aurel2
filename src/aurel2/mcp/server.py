"""MCP Server for Aurel2 AI agent integration.

This server exposes tools for the AI agent to query market data, strategy signals,
and execute trades.
"""

from datetime import date, timedelta
from typing import Any

import pandas as pd
import structlog

from aurel2.core.assets import ASSET_REGISTRY
from aurel2.core.models import AssetClass, Signal, SignalAction
from aurel2.data.indicators import (
    calculate_drawdown,
    calculate_moving_average,
    calculate_rsi,
    get_current_price,
)
from aurel2.data.momentum import calculate_momentum_scores
from aurel2.data.providers.yahoo import YahooFinanceProvider
from aurel2.persistence.portfolio import PortfolioStore
from aurel2.strategies.base import StrategySignal
from aurel2.strategies.dual_momentum import DualMomentumStrategy
from aurel2.strategies.mean_reversion import MeanReversionStrategy
from aurel2.strategies.multi_timeframe import MultiTimeframeTrendStrategy

logger = structlog.get_logger()


# Symbols to fetch for market data
DEFAULT_SYMBOLS = ["SPY", "EFA", "AGG", "EEM", "XLK", "XLF", "XLE", "XLV", "TLT", "GLD", "DBC"]


class Aurel2MCPServer:
    """MCP Server that exposes Aurel2 tools for AI agents.

    This server provides 7 tools:
    1. get_momentum_scores - Get momentum scores for all assets
    2. get_regime - Get market regime (bull/bear/neutral) based on SPY vs 200-day MA
    3. get_rsi - Get RSI for a specific symbol
    4. get_portfolio - Get current portfolio holdings
    5. get_strategy_signals - Get signals from all 3 strategies
    6. get_market_context - Get market context (regime, drawdown, RSI)
    7. execute_trade - Placeholder for trade execution
    """

    def __init__(self, portfolio_path: str | None = None):
        """Initialize the MCP server.

        Args:
            portfolio_path: Optional path to portfolio JSON file.
        """
        self._data_provider = YahooFinanceProvider()
        self._portfolio_store = PortfolioStore(portfolio_path) if portfolio_path else PortfolioStore()

        # Price cache (refreshed daily)
        self._prices_cache: pd.DataFrame | None = None
        self._cache_date: date | None = None

        # Initialize strategies
        self._dual_momentum = DualMomentumStrategy(
            assets=ASSET_REGISTRY,
            lookback_months=12,
            switch_threshold=0.10,
        )
        self._mean_reversion = MeanReversionStrategy()
        self._multi_timeframe = MultiTimeframeTrendStrategy()

        logger.info("mcp_server_initialized")

    def list_tools(self) -> list[dict[str, Any]]:
        """List all available tools.

        Returns:
            List of tool definitions with name and description.
        """
        return [
            {
                "name": "get_momentum_scores",
                "description": "Get 12-month momentum scores for all tracked assets. Returns momentum (return) for each asset class.",
            },
            {
                "name": "get_regime",
                "description": "Get market regime (bull/bear/neutral) based on SPY price vs 200-day moving average.",
            },
            {
                "name": "get_rsi",
                "description": "Get the Relative Strength Index (RSI) for a specific symbol. RSI below 30 indicates oversold, above 70 indicates overbought.",
            },
            {
                "name": "get_portfolio",
                "description": "Get current portfolio holdings including cash, positions, and total invested value.",
            },
            {
                "name": "get_strategy_signals",
                "description": "Get trading signals from all three strategies: dual_momentum, mean_reversion, and multi_timeframe.",
            },
            {
                "name": "get_market_context",
                "description": "Get comprehensive market context including regime, drawdown from peak, and RSI.",
            },
            {
                "name": "execute_trade",
                "description": "Execute a trade (placeholder - not yet implemented).",
            },
        ]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a tool by name with the given arguments.

        Args:
            name: The tool name to call.
            arguments: Dictionary of arguments for the tool.

        Returns:
            Dictionary with tool results.
        """
        tool_map = {
            "get_momentum_scores": self._get_momentum_scores,
            "get_regime": self._get_regime,
            "get_rsi": self._get_rsi,
            "get_portfolio": self._get_portfolio,
            "get_strategy_signals": self._get_strategy_signals,
            "get_market_context": self._get_market_context,
            "execute_trade": self._execute_trade,
        }

        handler = tool_map.get(name)
        if handler is None:
            logger.warning("unknown_tool_called", tool_name=name)
            return {"error": f"Unknown tool: {name}"}

        try:
            return handler(**arguments)
        except Exception as e:
            logger.error("tool_execution_error", tool_name=name, error=str(e))
            return {"error": str(e)}

    def _get_prices(self) -> pd.DataFrame:
        """Get price data, using cache if available for today.

        Returns:
            DataFrame with price data for all symbols.
        """
        today = date.today()

        if self._prices_cache is not None and self._cache_date == today:
            logger.debug("using_cached_prices", cache_date=str(self._cache_date))
            return self._prices_cache

        logger.info("fetching_fresh_prices")

        # Fetch 400 days to ensure we have enough for 200-day MA and 12-month momentum
        start_date = today - timedelta(days=400)
        end_date = today

        prices = self._data_provider.get_multi_prices(
            symbols=DEFAULT_SYMBOLS,
            start_date=start_date,
            end_date=end_date,
        )

        self._prices_cache = prices
        self._cache_date = today

        return prices

    def _get_momentum_scores(self) -> dict[str, Any]:
        """Get momentum scores for all assets.

        Returns:
            Dictionary with asset class momentum scores.
        """
        prices = self._get_prices()
        today = date.today()

        scores = calculate_momentum_scores(
            prices=prices,
            assets=ASSET_REGISTRY,
            calc_date=today,
            lookback_months=12,
        )

        result = {}
        for asset_class, score in scores.items():
            result[asset_class.value] = {
                "momentum_12m": round(score.momentum_12m, 4),
                "price": round(score.price, 2),
                "price_12m_ago": round(score.price_12m_ago, 2),
                "symbol": score.asset.symbol,
            }

        return {"scores": result, "date": str(today)}

    def _get_regime(self) -> dict[str, Any]:
        """Get market regime based on SPY vs 200-day MA.

        Returns:
            Dictionary with regime, SPY price, 200-day MA, and percentage from MA.
        """
        prices = self._get_prices()
        symbol = "SPY"

        spy_price = get_current_price(prices, symbol)
        ma_200 = calculate_moving_average(prices, symbol, period=200)

        if spy_price is None or ma_200 is None:
            return {
                "regime": "neutral",
                "spy_price": None,
                "ma_200": None,
                "pct_from_ma": None,
                "error": "Insufficient data to determine regime",
            }

        pct_from_ma = (spy_price - ma_200) / ma_200

        # Determine regime with some buffer to avoid whipsaws
        if pct_from_ma > 0.02:  # 2% above MA
            regime = "bull"
        elif pct_from_ma < -0.02:  # 2% below MA
            regime = "bear"
        else:
            regime = "neutral"

        logger.info(
            "market_regime_calculated",
            regime=regime,
            spy_price=round(spy_price, 2),
            ma_200=round(ma_200, 2),
            pct_from_ma=f"{pct_from_ma:.2%}",
        )

        return {
            "regime": regime,
            "spy_price": round(spy_price, 2),
            "ma_200": round(ma_200, 2),
            "pct_from_ma": round(pct_from_ma, 4),
        }

    def _get_rsi(self, symbol: str = "SPY") -> dict[str, Any]:
        """Get RSI for a specific symbol.

        Args:
            symbol: The symbol to calculate RSI for.

        Returns:
            Dictionary with symbol and RSI value.
        """
        prices = self._get_prices()
        rsi = calculate_rsi(prices, symbol, period=14)

        if rsi is not None:
            rsi = round(rsi, 2)

        return {
            "symbol": symbol,
            "rsi": rsi,
            "interpretation": self._interpret_rsi(rsi),
        }

    def _interpret_rsi(self, rsi: float | None) -> str:
        """Interpret RSI value.

        Args:
            rsi: RSI value to interpret.

        Returns:
            Human-readable interpretation.
        """
        if rsi is None:
            return "insufficient data"
        if rsi < 30:
            return "oversold"
        if rsi > 70:
            return "overbought"
        return "neutral"

    def _get_portfolio(self) -> dict[str, Any]:
        """Get current portfolio holdings.

        Returns:
            Dictionary with portfolio data.
        """
        portfolio = self._portfolio_store.load()

        holdings_data = []
        for holding in portfolio.holdings:
            holdings_data.append({
                "symbol": holding.symbol,
                "name": holding.name,
                "shares": holding.shares,
                "entry_price": holding.entry_price,
                "entry_date": str(holding.entry_date),
                "broker": holding.broker,
                "cost_basis": holding.cost_basis,
                "days_held": holding.days_held(),
                "is_long_term": holding.is_long_term(),
                "tax_rate": holding.tax_rate(),
            })

        return {
            "cash": portfolio.cash,
            "holdings": holdings_data,
            "total_invested": portfolio.total_invested,
            "last_updated": str(portfolio.last_updated),
        }

    def _get_strategy_signals(self) -> dict[str, Any]:
        """Get signals from all three strategies.

        Returns:
            Dictionary with signals from each strategy.
        """
        prices = self._get_prices()
        today = date.today()

        # Get current holding from portfolio (if any)
        portfolio = self._portfolio_store.load()
        current_holding = None
        if portfolio.holdings:
            # Try to determine asset class from first holding
            first_symbol = portfolio.holdings[0].symbol.upper()
            for asset_class, asset in ASSET_REGISTRY.items():
                if asset.symbol == first_symbol or asset.yahoo_symbol == first_symbol:
                    current_holding = asset_class
                    break

        signals = {}

        # Dual Momentum (returns Signal, not StrategySignal)
        try:
            dm_signal: Signal = self._dual_momentum.generate_signal(
                prices=prices,
                calc_date=today,
                current_holding=current_holding,
            )
            signals["dual_momentum"] = {
                "action": dm_signal.action.value,
                "asset": dm_signal.asset.symbol if dm_signal.asset else None,
                "reason": dm_signal.reason,
            }
        except Exception as e:
            logger.error("dual_momentum_signal_error", error=str(e))
            signals["dual_momentum"] = {"error": str(e)}

        # Mean Reversion (returns StrategySignal)
        try:
            mr_signal: StrategySignal = self._mean_reversion.generate_signal(
                prices=prices,
                calc_date=today,
                current_holding=current_holding,
            )
            signals["mean_reversion"] = {
                "action": mr_signal.action.value,
                "asset_class": mr_signal.asset_class.value if mr_signal.asset_class else None,
                "confidence": round(mr_signal.confidence, 2),
                "reasoning": mr_signal.reasoning,
                "metadata": mr_signal.metadata,
            }
        except Exception as e:
            logger.error("mean_reversion_signal_error", error=str(e))
            signals["mean_reversion"] = {"error": str(e)}

        # Multi-Timeframe Trend (returns StrategySignal)
        try:
            mt_signal: StrategySignal = self._multi_timeframe.generate_signal(
                prices=prices,
                calc_date=today,
                current_holding=current_holding,
            )
            signals["multi_timeframe"] = {
                "action": mt_signal.action.value,
                "asset_class": mt_signal.asset_class.value if mt_signal.asset_class else None,
                "confidence": round(mt_signal.confidence, 2),
                "reasoning": mt_signal.reasoning,
                "metadata": mt_signal.metadata,
            }
        except Exception as e:
            logger.error("multi_timeframe_signal_error", error=str(e))
            signals["multi_timeframe"] = {"error": str(e)}

        return {"signals": signals, "date": str(today)}

    def _get_market_context(self) -> dict[str, Any]:
        """Get comprehensive market context.

        Returns:
            Dictionary with regime, drawdown, and RSI.
        """
        prices = self._get_prices()
        symbol = "SPY"

        # Get regime
        regime_data = self._get_regime()

        # Get drawdown
        drawdown = calculate_drawdown(prices, symbol, lookback_days=252)
        if drawdown is not None:
            drawdown = round(drawdown, 4)

        # Get RSI
        rsi = calculate_rsi(prices, symbol, period=14)
        if rsi is not None:
            rsi = round(rsi, 2)

        return {
            "regime": regime_data["regime"],
            "spy_price": regime_data.get("spy_price"),
            "ma_200": regime_data.get("ma_200"),
            "pct_from_ma": regime_data.get("pct_from_ma"),
            "drawdown": drawdown,
            "drawdown_pct": f"{drawdown:.2%}" if drawdown is not None else None,
            "rsi": rsi,
            "rsi_interpretation": self._interpret_rsi(rsi),
        }

    def _execute_trade(
        self,
        symbol: str = "",
        action: str = "",
        shares: float = 0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Placeholder for trade execution.

        Args:
            symbol: Symbol to trade.
            action: 'buy' or 'sell'.
            shares: Number of shares.
            **kwargs: Additional arguments.

        Returns:
            Dictionary indicating trade execution is not implemented.
        """
        logger.warning(
            "execute_trade_called",
            symbol=symbol,
            action=action,
            shares=shares,
        )

        return {
            "status": "not_implemented",
            "message": "Trade execution is a placeholder. Manual execution required.",
            "requested_trade": {
                "symbol": symbol,
                "action": action,
                "shares": shares,
            },
        }
