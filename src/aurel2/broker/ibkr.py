"""Interactive Brokers integration using ib_insync."""

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

# Try to import ib_insync, but don't fail if not installed
try:
    from ib_insync import IB, Stock, MarketOrder, LimitOrder, Contract
    HAS_IB_INSYNC = True
except ImportError:
    HAS_IB_INSYNC = False
    logger.warning("ib_insync not installed. Run: pip install ib_insync")


class IBKRBroker(BaseBroker):
    """
    Interactive Brokers integration.

    Requires:
    - IB Gateway or TWS running locally
    - API connections enabled in IB Gateway/TWS settings
    - pip install ib_insync

    Usage:
        broker = IBKRBroker(host="127.0.0.1", port=7497)  # 7497=paper, 7496=live
        await broker.connect()
        positions = await broker.get_positions()
        await broker.disconnect()
    """

    # Symbol mappings: our symbols -> IBKR contract details
    SYMBOL_MAP = {
        # UCITS ETFs on European exchanges
        "VWRA": {"symbol": "VWRA", "exchange": "SBF", "currency": "EUR"},  # Vanguard All-World
        "CSPX": {"symbol": "CSPX", "exchange": "SBF", "currency": "EUR"},  # iShares S&P 500
        "AGGH": {"symbol": "AGGH", "exchange": "SBF", "currency": "EUR"},  # iShares Global Agg Bond
        # US ETFs
        "SPY": {"symbol": "SPY", "exchange": "ARCA", "currency": "USD"},
        "EFA": {"symbol": "EFA", "exchange": "ARCA", "currency": "USD"},
        "AGG": {"symbol": "AGG", "exchange": "ARCA", "currency": "USD"},
    }

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7497,  # 7497 = paper trading, 7496 = live
        client_id: int = 1,
    ):
        if not HAS_IB_INSYNC:
            raise ImportError("ib_insync is required. Install with: pip install ib_insync")

        self.host = host
        self.port = port
        self.client_id = client_id
        self.ib = IB()
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected and self.ib.isConnected()

    async def connect(self) -> bool:
        """Connect to IB Gateway/TWS."""
        try:
            logger.info("connecting_to_ibkr", host=self.host, port=self.port)
            await self.ib.connectAsync(
                host=self.host,
                port=self.port,
                clientId=self.client_id,
            )
            self._connected = True
            logger.info("connected_to_ibkr")
            return True
        except Exception as e:
            logger.error("ibkr_connection_failed", error=str(e))
            self._connected = False
            return False

    async def disconnect(self) -> None:
        """Disconnect from IB."""
        if self.ib.isConnected():
            self.ib.disconnect()
            self._connected = False
            logger.info("disconnected_from_ibkr")

    async def get_account_summary(self) -> AccountSummary:
        """Get account summary."""
        if not self.is_connected:
            raise ConnectionError("Not connected to IBKR")

        # Request account values
        account_values = self.ib.accountSummary()

        total_value = 0.0
        cash_balance = 0.0
        buying_power = 0.0
        currency = "EUR"

        for av in account_values:
            if av.tag == "NetLiquidation" and av.currency == "EUR":
                total_value = float(av.value)
            elif av.tag == "TotalCashValue" and av.currency == "EUR":
                cash_balance = float(av.value)
            elif av.tag == "BuyingPower":
                buying_power = float(av.value)

        return AccountSummary(
            total_value=total_value,
            cash_balance=cash_balance,
            buying_power=buying_power,
            currency=currency,
        )

    async def get_positions(self) -> list[BrokerPosition]:
        """Get all positions."""
        if not self.is_connected:
            raise ConnectionError("Not connected to IBKR")

        positions = []
        for pos in self.ib.positions():
            contract = pos.contract

            # Get market price
            market_price = await self.get_market_price(contract.symbol)
            if market_price is None:
                market_price = pos.avgCost  # Fallback to avg cost

            market_value = pos.position * market_price
            unrealized_pnl = market_value - (pos.position * pos.avgCost)

            positions.append(BrokerPosition(
                symbol=contract.symbol,
                shares=float(pos.position),
                avg_cost=float(pos.avgCost),
                market_price=market_price,
                market_value=market_value,
                unrealized_pnl=unrealized_pnl,
                currency=contract.currency,
            ))

        return positions

    async def get_position(self, symbol: str) -> Optional[BrokerPosition]:
        """Get a specific position."""
        positions = await self.get_positions()
        for pos in positions:
            if pos.symbol.upper() == symbol.upper():
                return pos
        return None

    def _get_contract(self, symbol: str) -> Contract:
        """Get IBKR contract for a symbol."""
        symbol = symbol.upper()

        if symbol in self.SYMBOL_MAP:
            info = self.SYMBOL_MAP[symbol]
            return Stock(
                symbol=info["symbol"],
                exchange=info["exchange"],
                currency=info["currency"],
            )
        else:
            # Default to US stock
            return Stock(symbol=symbol, exchange="SMART", currency="USD")

    async def get_market_price(self, symbol: str) -> Optional[float]:
        """Get current market price."""
        if not self.is_connected:
            return None

        try:
            contract = self._get_contract(symbol)
            self.ib.qualifyContracts(contract)

            # Request market data
            ticker = self.ib.reqMktData(contract)
            await asyncio.sleep(2)  # Wait for data

            price = ticker.marketPrice()
            self.ib.cancelMktData(contract)

            if price and price > 0:
                return float(price)
            return None
        except Exception as e:
            logger.error("get_market_price_failed", symbol=symbol, error=str(e))
            return None

    async def place_order(self, order: BrokerOrder) -> OrderResult:
        """Place an order."""
        if not self.is_connected:
            raise ConnectionError("Not connected to IBKR")

        contract = self._get_contract(order.symbol)
        self.ib.qualifyContracts(contract)

        # Create order
        if order.order_type == "MKT":
            ib_order = MarketOrder(
                action=order.action,
                totalQuantity=order.quantity,
            )
        elif order.order_type == "LMT" and order.limit_price:
            ib_order = LimitOrder(
                action=order.action,
                totalQuantity=order.quantity,
                lmtPrice=order.limit_price,
            )
        else:
            raise ValueError(f"Unsupported order type: {order.order_type}")

        # Place order
        logger.info(
            "placing_order",
            symbol=order.symbol,
            action=order.action,
            quantity=order.quantity,
            order_type=order.order_type,
        )

        trade = self.ib.placeOrder(contract, ib_order)

        # Wait for fill (with timeout)
        timeout = 30  # seconds
        for _ in range(timeout * 10):
            if trade.isDone():
                break
            await asyncio.sleep(0.1)

        # Get result
        if trade.orderStatus.status == "Filled":
            status = "FILLED"
        elif trade.orderStatus.status == "Cancelled":
            status = "REJECTED"
        elif trade.orderStatus.filled > 0:
            status = "PARTIAL"
        else:
            status = "PENDING"

        result = OrderResult(
            order_id=str(trade.order.orderId),
            symbol=order.symbol,
            action=order.action,
            quantity=order.quantity,
            filled_quantity=float(trade.orderStatus.filled),
            avg_fill_price=float(trade.orderStatus.avgFillPrice or 0),
            status=status,
            message=trade.orderStatus.status,
        )

        logger.info(
            "order_result",
            order_id=result.order_id,
            status=result.status,
            filled=result.filled_quantity,
            avg_price=result.avg_fill_price,
        )

        return result

    async def execute_switch(
        self,
        sell_symbol: str,
        buy_symbol: str,
        sell_quantity: Optional[float] = None,
    ) -> tuple[OrderResult, OrderResult]:
        """
        Execute a position switch: sell one ETF and buy another.

        If sell_quantity is None, sells entire position.
        Uses all proceeds to buy the new symbol.
        """
        # Get current position to sell
        if sell_quantity is None:
            position = await self.get_position(sell_symbol)
            if position is None:
                raise ValueError(f"No position found for {sell_symbol}")
            sell_quantity = position.shares

        # Sell
        sell_order = BrokerOrder(
            symbol=sell_symbol,
            action="SELL",
            quantity=sell_quantity,
            order_type="MKT",
        )
        sell_result = await self.place_order(sell_order)

        if sell_result.status != "FILLED":
            logger.error("sell_order_not_filled", result=sell_result)
            raise RuntimeError(f"Sell order not filled: {sell_result.message}")

        # Calculate proceeds
        proceeds = sell_result.filled_quantity * sell_result.avg_fill_price

        # Get buy price
        buy_price = await self.get_market_price(buy_symbol)
        if buy_price is None:
            raise RuntimeError(f"Could not get price for {buy_symbol}")

        # Calculate shares to buy (accounting for some slippage)
        buy_quantity = int(proceeds * 0.99 / buy_price)  # Leave 1% buffer

        # Buy
        buy_order = BrokerOrder(
            symbol=buy_symbol,
            action="BUY",
            quantity=buy_quantity,
            order_type="MKT",
        )
        buy_result = await self.place_order(buy_order)

        return sell_result, buy_result
