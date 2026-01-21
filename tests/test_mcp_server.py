"""Tests for MCP Server."""

from datetime import date
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from aurel2.core.models import AssetClass, SignalAction


@pytest.fixture
def mock_prices():
    """Create mock price data for SPY, EFA, and AGG."""
    dates = pd.date_range("2023-01-01", periods=300, freq="D")

    # Create realistic price patterns
    spy_prices = 400 + np.cumsum(np.random.randn(300) * 2)
    efa_prices = 70 + np.cumsum(np.random.randn(300) * 1)
    agg_prices = 100 + np.cumsum(np.random.randn(300) * 0.5)

    spy_df = pd.DataFrame({"date": dates, "close": spy_prices, "symbol": ["SPY"] * 300})
    efa_df = pd.DataFrame({"date": dates, "close": efa_prices, "symbol": ["EFA"] * 300})
    agg_df = pd.DataFrame({"date": dates, "close": agg_prices, "symbol": ["AGG"] * 300})

    return pd.concat([spy_df, efa_df, agg_df], ignore_index=True)


@pytest.fixture
def server_with_mock_data(mock_prices):
    """Create MCP server with mocked data provider."""
    with patch("aurel2.mcp.server.YahooFinanceProvider") as mock_provider_class:
        mock_provider = MagicMock()
        mock_provider.get_multi_prices.return_value = mock_prices
        mock_provider.get_prices.return_value = mock_prices[mock_prices["symbol"] == "SPY"]
        mock_provider_class.return_value = mock_provider

        from aurel2.mcp.server import Aurel2MCPServer
        server = Aurel2MCPServer()
        # Pre-populate the cache
        server._prices_cache = mock_prices
        server._cache_date = date.today()
        yield server


class TestMCPServerTools:
    """Test that the MCP server has required tools."""

    def test_server_has_required_tools(self, server_with_mock_data):
        """Server should list all 7 required tools."""
        server = server_with_mock_data
        tools = server.list_tools()
        tool_names = [t["name"] for t in tools]

        assert "get_momentum_scores" in tool_names
        assert "get_regime" in tool_names
        assert "get_rsi" in tool_names
        assert "get_portfolio" in tool_names
        assert "get_strategy_signals" in tool_names
        assert "get_market_context" in tool_names
        assert "execute_trade" in tool_names

    def test_list_tools_returns_list_of_dicts(self, server_with_mock_data):
        """list_tools should return list of tool definitions."""
        server = server_with_mock_data
        tools = server.list_tools()

        assert isinstance(tools, list)
        assert len(tools) == 7

        for tool in tools:
            assert "name" in tool
            assert "description" in tool


class TestGetRegime:
    """Tests for get_regime tool."""

    def test_get_regime_returns_valid_regime(self, server_with_mock_data):
        """get_regime should return bull, bear, or neutral."""
        server = server_with_mock_data
        result = server.call_tool("get_regime", {})

        assert result["regime"] in ["bull", "bear", "neutral"]
        assert "spy_price" in result
        assert "ma_200" in result

    def test_get_regime_includes_percentage(self, server_with_mock_data):
        """get_regime should include percentage above/below MA."""
        server = server_with_mock_data
        result = server.call_tool("get_regime", {})

        assert "pct_from_ma" in result
        assert isinstance(result["pct_from_ma"], float)


class TestGetRSI:
    """Tests for get_rsi tool."""

    def test_get_rsi_returns_value_for_symbol(self, server_with_mock_data):
        """get_rsi should return RSI for specified symbol."""
        server = server_with_mock_data
        result = server.call_tool("get_rsi", {"symbol": "SPY"})

        assert "rsi" in result
        assert "symbol" in result
        assert result["symbol"] == "SPY"

    def test_get_rsi_returns_value_in_valid_range(self, server_with_mock_data):
        """RSI should be between 0 and 100."""
        server = server_with_mock_data
        result = server.call_tool("get_rsi", {"symbol": "SPY"})

        if result["rsi"] is not None:
            assert 0 <= result["rsi"] <= 100

    def test_get_rsi_handles_missing_symbol(self, server_with_mock_data):
        """get_rsi should handle missing symbol gracefully."""
        server = server_with_mock_data
        result = server.call_tool("get_rsi", {"symbol": "INVALID_SYMBOL"})

        assert "rsi" in result
        # RSI should be None for invalid symbol


class TestGetMomentumScores:
    """Tests for get_momentum_scores tool."""

    def test_get_momentum_scores_returns_dict(self, server_with_mock_data):
        """get_momentum_scores should return momentum scores."""
        server = server_with_mock_data
        result = server.call_tool("get_momentum_scores", {})

        assert "scores" in result
        assert isinstance(result["scores"], dict)

    def test_get_momentum_scores_includes_asset_classes(self, server_with_mock_data):
        """Momentum scores should include asset classes."""
        server = server_with_mock_data
        result = server.call_tool("get_momentum_scores", {})

        # Should have at least one asset class
        assert len(result["scores"]) > 0


class TestGetPortfolio:
    """Tests for get_portfolio tool."""

    def test_get_portfolio_returns_holdings(self, server_with_mock_data):
        """get_portfolio should return portfolio holdings."""
        server = server_with_mock_data
        result = server.call_tool("get_portfolio", {})

        assert "cash" in result
        assert "holdings" in result
        assert isinstance(result["holdings"], list)

    def test_get_portfolio_includes_total_invested(self, server_with_mock_data):
        """get_portfolio should include total invested amount."""
        server = server_with_mock_data
        result = server.call_tool("get_portfolio", {})

        assert "total_invested" in result


class TestGetStrategySignals:
    """Tests for get_strategy_signals tool."""

    def test_get_strategy_signals_returns_signals(self, server_with_mock_data):
        """get_strategy_signals should return signals from all strategies."""
        server = server_with_mock_data
        result = server.call_tool("get_strategy_signals", {})

        assert "signals" in result
        assert isinstance(result["signals"], dict)

    def test_get_strategy_signals_includes_three_strategies(self, server_with_mock_data):
        """Should include signals from dual_momentum, mean_reversion, multi_timeframe."""
        server = server_with_mock_data
        result = server.call_tool("get_strategy_signals", {})

        signals = result["signals"]
        assert "dual_momentum" in signals
        assert "mean_reversion" in signals
        assert "multi_timeframe" in signals


class TestGetMarketContext:
    """Tests for get_market_context tool."""

    def test_get_market_context_returns_context(self, server_with_mock_data):
        """get_market_context should return market context data."""
        server = server_with_mock_data
        result = server.call_tool("get_market_context", {})

        assert "regime" in result
        assert "drawdown" in result
        assert "rsi" in result

    def test_get_market_context_regime_is_valid(self, server_with_mock_data):
        """Market context regime should be valid."""
        server = server_with_mock_data
        result = server.call_tool("get_market_context", {})

        assert result["regime"] in ["bull", "bear", "neutral"]


class TestExecuteTrade:
    """Tests for execute_trade tool."""

    def test_execute_trade_is_placeholder(self, server_with_mock_data):
        """execute_trade should return placeholder response."""
        server = server_with_mock_data
        result = server.call_tool("execute_trade", {
            "symbol": "SPY",
            "action": "buy",
            "shares": 10
        })

        # Should indicate it's a placeholder or not implemented
        assert "status" in result


class TestCallTool:
    """Tests for call_tool dispatcher."""

    def test_call_tool_handles_unknown_tool(self, server_with_mock_data):
        """call_tool should handle unknown tool names gracefully."""
        server = server_with_mock_data
        result = server.call_tool("unknown_tool", {})

        assert "error" in result


class TestPricesCaching:
    """Tests for price data caching."""

    def test_prices_are_cached(self, mock_prices):
        """Prices should be cached after first fetch."""
        with patch("aurel2.mcp.server.YahooFinanceProvider") as mock_provider_class:
            mock_provider = MagicMock()
            mock_provider.get_multi_prices.return_value = mock_prices
            mock_provider_class.return_value = mock_provider

            from aurel2.mcp.server import Aurel2MCPServer
            server = Aurel2MCPServer()

            # First call should fetch prices
            server._get_prices()
            assert mock_provider.get_multi_prices.call_count == 1

            # Second call should use cache (same day)
            server._get_prices()
            assert mock_provider.get_multi_prices.call_count == 1
