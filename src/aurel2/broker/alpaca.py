"""Alpaca Markets broker integration using alpaca-py SDK."""

import asyncio
from typing import Optional

import structlog

from aurel2.broker.base import (
    BaseBroker,
    BrokerPosition,
    BrokerOrder,
    OrderResult,
    AccountSummary,
)

logger = structlog.get_logger()

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import (
        MarketOrderRequest,
        LimitOrderRequest,
        GetPortfolioHistoryRequest,
    )
    from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockLatestTradeRequest, StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    HAS_ALPACA = True
except ImportError:
    HAS_ALPACA = False


class AlpacaBroker(BaseBroker):
    """Alpaca Markets broker integration.

    Supports fractional shares natively via REST API.
    No gateway process needed - direct API access.

    Usage:
        broker = AlpacaBroker(api_key="...", api_secret="...", paper=True)
        await broker.connect()
        positions = await broker.get_positions()
        await broker.disconnect()
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        paper: bool = True,
    ):
        if not HAS_ALPACA:
            raise ImportError("alpaca-py is required. Install with: pip install alpaca-py")

        self.api_key = api_key
        self.api_secret = api_secret
        self.paper = paper
        self._client: Optional[TradingClient] = None
        self._data_client: Optional[StockHistoricalDataClient] = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected and self._client is not None

    async def connect(self) -> bool:
        """Validate credentials by fetching account info."""
        try:
            logger.info("connecting_to_alpaca", paper=self.paper)
            self._client = TradingClient(
                api_key=self.api_key,
                secret_key=self.api_secret,
                paper=self.paper,
            )
            self._data_client = StockHistoricalDataClient(
                api_key=self.api_key,
                secret_key=self.api_secret,
            )
            # Validate credentials
            loop = asyncio.get_event_loop()
            account = await loop.run_in_executor(None, self._client.get_account)
            self._connected = True
            logger.info(
                "connected_to_alpaca",
                account_id=account.id,
                status=account.status,
                equity=str(account.equity),
            )
            return True
        except Exception as e:
            self._connected = False
            logger.error("alpaca_connection_failed", error=str(e))
            return False

    async def disconnect(self) -> None:
        """Clear client state."""
        self._client = None
        self._data_client = None
        self._connected = False
        logger.info("disconnected_from_alpaca")

    async def get_account_summary(self) -> AccountSummary:
        """Get account summary from Alpaca."""
        if not self.is_connected:
            raise ConnectionError("Not connected to Alpaca")

        loop = asyncio.get_event_loop()
        account = await loop.run_in_executor(None, self._client.get_account)

        return AccountSummary(
            total_value=float(account.equity),
            cash_balance=float(account.cash),
            buying_power=float(account.buying_power),
            currency="USD",
            unrealized_pnl=float(getattr(account, 'unrealized_pl', 0) or 0),
            realized_pnl=float(getattr(account, 'realized_pl', 0) or 0),
            gross_position_value=float(account.long_market_value or 0),
        )

    async def get_portfolio_history(self, period: str = "1M", timeframe: str = "1D") -> dict:
        """Get portfolio equity history from Alpaca.

        Args:
            period: Duration string like 1D, 1W, 1M, 6M, 1A, 5A.
            timeframe: Resolution — '15Min' for intraday, '1D' for daily.

        Returns:
            dict with 'dates' (list[str]), 'equity' (list[float]),
            'profit_loss' (list[float]), 'profit_loss_pct' (list[float]).
        """
        if not self.is_connected:
            raise ConnectionError("Not connected to Alpaca")

        from datetime import datetime, timezone

        request = GetPortfolioHistoryRequest(
            period=period,
            timeframe=timeframe,
            extended_hours=True,
        )
        loop = asyncio.get_event_loop()
        history = await loop.run_in_executor(
            None, self._client.get_portfolio_history, request
        )

        # Format timestamps based on timeframe
        fmt = "%Y-%m-%d %H:%M" if timeframe != "1D" else "%Y-%m-%d"
        dates = [
            datetime.fromtimestamp(ts, tz=timezone.utc).strftime(fmt)
            for ts in history.timestamp
        ]
        return {
            "dates": dates,
            "equity": [round(v, 2) for v in history.equity],
            "profit_loss": [round(v, 2) for v in history.profit_loss],
            "profit_loss_pct": [
                round(v * 100, 2) if v is not None else 0
                for v in history.profit_loss_pct
            ],
            "base_value": round(history.base_value, 2) if history.base_value else 0,
        }

    async def get_positions(self) -> list[BrokerPosition]:
        """Get all current positions."""
        if not self.is_connected:
            raise ConnectionError("Not connected to Alpaca")

        loop = asyncio.get_event_loop()
        positions = await loop.run_in_executor(None, self._client.get_all_positions)

        result = []
        for pos in positions:
            shares = float(pos.qty)
            if shares == 0:
                continue
            result.append(BrokerPosition(
                symbol=pos.symbol,
                shares=shares,
                avg_cost=float(pos.avg_entry_price),
                market_price=float(pos.current_price),
                market_value=float(pos.market_value),
                unrealized_pnl=float(pos.unrealized_pl or 0),
                unrealized_pnl_pct=float(pos.unrealized_plpc or 0) * 100,
                currency="USD",
            ))

        return result

    async def get_position(self, symbol: str) -> Optional[BrokerPosition]:
        """Get a specific position by symbol."""
        if not self.is_connected:
            return None

        try:
            loop = asyncio.get_event_loop()
            pos = await loop.run_in_executor(
                None, self._client.get_open_position, symbol.upper()
            )
            shares = float(pos.qty)
            if shares == 0:
                return None
            return BrokerPosition(
                symbol=pos.symbol,
                shares=shares,
                avg_cost=float(pos.avg_entry_price),
                market_price=float(pos.current_price),
                market_value=float(pos.market_value),
                unrealized_pnl=float(pos.unrealized_pl or 0),
                currency="USD",
            )
        except Exception:
            # Position not found
            return None

    async def place_order(self, order: BrokerOrder) -> OrderResult:
        """Place an order via Alpaca. Supports fractional shares."""
        if not self.is_connected:
            raise ConnectionError("Not connected to Alpaca")

        side = OrderSide.BUY if order.action == "BUY" else OrderSide.SELL

        try:
            if order.order_type == "MKT":
                request = MarketOrderRequest(
                    symbol=order.symbol.upper(),
                    qty=order.quantity,
                    side=side,
                    time_in_force=TimeInForce.DAY,
                )
            elif order.order_type == "LMT" and order.limit_price:
                request = LimitOrderRequest(
                    symbol=order.symbol.upper(),
                    qty=order.quantity,
                    side=side,
                    time_in_force=TimeInForce.DAY,
                    limit_price=order.limit_price,
                )
            else:
                raise ValueError(f"Unsupported order type: {order.order_type}")

            logger.info(
                "placing_order",
                symbol=order.symbol,
                action=order.action,
                quantity=order.quantity,
                order_type=order.order_type,
            )

            loop = asyncio.get_event_loop()
            alpaca_order = await loop.run_in_executor(
                None, self._client.submit_order, request
            )

            # Poll for fill (60s timeout)
            filled_order = await self._wait_for_fill(alpaca_order.id, timeout=60)

            if filled_order.status.value == "filled":
                status = "FILLED"
            elif filled_order.status.value in ("canceled", "expired", "rejected"):
                status = "REJECTED"
            elif filled_order.filled_qty and float(filled_order.filled_qty) > 0:
                status = "PARTIAL"
            else:
                status = "PENDING"

            return OrderResult(
                order_id=str(filled_order.id),
                symbol=order.symbol,
                action=order.action,
                quantity=order.quantity,
                filled_quantity=float(filled_order.filled_qty or 0),
                avg_fill_price=float(filled_order.filled_avg_price or 0),
                status=status,
                message=filled_order.status.value,
            )

        except Exception as e:
            logger.error("place_order_failed", symbol=order.symbol, error=str(e))
            return OrderResult(
                order_id="",
                symbol=order.symbol,
                action=order.action,
                quantity=order.quantity,
                filled_quantity=0,
                avg_fill_price=0,
                status="REJECTED",
                message=str(e),
            )

    async def _wait_for_fill(self, order_id: str, timeout: int = 60):
        """Poll order status until filled or timeout."""
        loop = asyncio.get_event_loop()
        for _ in range(timeout * 2):  # Check every 0.5s
            order = await loop.run_in_executor(
                None, self._client.get_order_by_id, order_id
            )
            if order.status.value in ("filled", "canceled", "expired", "rejected"):
                return order
            await asyncio.sleep(0.5)
        # Return last known state on timeout
        return await loop.run_in_executor(
            None, self._client.get_order_by_id, order_id
        )

    async def get_market_price(self, symbol: str) -> Optional[float]:
        """Get latest trade price from Alpaca market data."""
        if not self._data_client:
            return None

        try:
            loop = asyncio.get_event_loop()
            request = StockLatestTradeRequest(symbol_or_symbols=symbol.upper())
            trades = await loop.run_in_executor(
                None, self._data_client.get_stock_latest_trade, request
            )
            trade = trades.get(symbol.upper())
            if trade:
                return float(trade.price)
            return None
        except Exception as e:
            logger.error("get_market_price_failed", symbol=symbol, error=str(e))
            return None

    async def get_price_history(self, symbol: str, period: str = "1M", timeframe: str = "1D") -> dict:
        """Get historical bars for a symbol from Alpaca market data.

        Args:
            symbol: Ticker symbol (e.g. SPY)
            period: Duration string like 1D, 1W, 1M, 6M, 1A, 5A.
            timeframe: Resolution — '15Min' for intraday, '1D' for daily.

        Returns:
            dict with 'dates' and 'close'.
        """
        if not self._data_client:
            return {"dates": [], "close": []}

        from datetime import datetime, timezone, timedelta

        tf = TimeFrame.Day if timeframe == "1D" else TimeFrame.Minute
        delta_map = {
            "1D": timedelta(days=1),
            "1W": timedelta(days=7),
            "1M": timedelta(days=30),
            "6M": timedelta(days=180),
            "1A": timedelta(days=365),
            "5A": timedelta(days=365 * 5),
        }
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - delta_map.get(period, timedelta(days=30))

        request = StockBarsRequest(
            symbol_or_symbols=symbol.upper(),
            timeframe=tf,
            start=start_dt,
            end=end_dt,
            feed="iex",
        )

        loop = asyncio.get_event_loop()
        bars = await loop.run_in_executor(None, self._data_client.get_stock_bars, request)
        rows = bars.data.get(symbol.upper(), []) if hasattr(bars, "data") else []

        fmt = "%Y-%m-%d %H:%M" if timeframe != "1D" else "%Y-%m-%d"
        dates = [b.timestamp.astimezone(timezone.utc).strftime(fmt) for b in rows]
        close = [float(b.close) for b in rows]
        return {"dates": dates, "close": close}
