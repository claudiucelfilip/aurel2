"""Interactive Brokers integration using ib_insync."""

import asyncio
from dataclasses import dataclass
from typing import Any, Optional
import structlog

from aurel2.broker.base import (
    BaseBroker,
    BrokerPosition,
    BrokerOrder,
    OrderResult,
    AccountSummary,
)


@dataclass
class OrderVerification:
    """Result of order verification."""
    verified: bool
    expected_shares: float
    actual_shares: float
    expected_action: str  # "BUY" or "SELL"
    slippage_pct: float  # (fill_price - expected_price) / expected_price
    message: str

logger = structlog.get_logger()

# Try to import ib_insync, but don't fail if not installed
try:
    from ib_insync import IB, Stock, MarketOrder, LimitOrder, Contract
    HAS_IB_INSYNC = True
except ImportError:
    HAS_IB_INSYNC = False
    logger.warning("ib_insync not installed. Run: pip install ib_insync")


class ClientIdConflictError(Exception):
    """Raised when the IBKR client ID is already in use."""
    pass


class IBKRBroker(BaseBroker):
    """
    Interactive Brokers integration.

    Requires:
    - IB Gateway running locally
    - API connections enabled in IB Gateway settings
    - pip install ib_insync

    Usage:
        broker = IBKRBroker(host="127.0.0.1", port=4002)  # IB Gateway: 4002=paper, 4001=live
        await broker.connect()
        positions = await broker.get_positions()
        await broker.disconnect()

    Port reference:
        IB Gateway: 4001 (live), 4002 (paper)
    """

    # Symbol mappings: our symbols -> IBKR contract details
    SYMBOL_MAP = {
        # UCITS ETFs on European exchanges
        "VWRA": {"symbol": "VWRA", "exchange": "SBF", "currency": "EUR"},  # Vanguard All-World
        "CSPX": {"symbol": "CSPX", "exchange": "SBF", "currency": "EUR"},  # iShares S&P 500
        "AGGH": {"symbol": "AGGH", "exchange": "SBF", "currency": "EUR"},  # iShares Global Agg Bond
        # US ETFs — use SMART routing for best execution
        "SPY": {"symbol": "SPY", "exchange": "SMART", "currency": "USD"},
        "EFA": {"symbol": "EFA", "exchange": "SMART", "currency": "USD"},
        "EEM": {"symbol": "EEM", "exchange": "SMART", "currency": "USD"},
        "XLK": {"symbol": "XLK", "exchange": "SMART", "currency": "USD"},
        "XLF": {"symbol": "XLF", "exchange": "SMART", "currency": "USD"},
        "XLE": {"symbol": "XLE", "exchange": "SMART", "currency": "USD"},
        "XLV": {"symbol": "XLV", "exchange": "SMART", "currency": "USD"},
        "AGG": {"symbol": "AGG", "exchange": "SMART", "currency": "USD"},
        "TLT": {"symbol": "TLT", "exchange": "SMART", "currency": "USD"},
        "SHY": {"symbol": "SHY", "exchange": "SMART", "currency": "USD"},
        "IEF": {"symbol": "IEF", "exchange": "SMART", "currency": "USD"},
        "TIP": {"symbol": "TIP", "exchange": "SMART", "currency": "USD"},
        "VNQ": {"symbol": "VNQ", "exchange": "SMART", "currency": "USD"},
        "IJS": {"symbol": "IJS", "exchange": "SMART", "currency": "USD"},
        "GLD": {"symbol": "GLD", "exchange": "SMART", "currency": "USD"},
        "DBC": {"symbol": "DBC", "exchange": "SMART", "currency": "USD"},
    }

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4002,  # IB Gateway: 4002 = paper, 4001 = live
        client_id: int = 1,
    ):
        if not HAS_IB_INSYNC:
            raise ImportError("ib_insync is required. Install with: pip install ib_insync")

        self.host = host
        self.port = port
        self.client_id = client_id
        self.ib = IB()
        self._connected = False
        self._server_connected = True  # Track IBKR server connectivity (Error 1100/1102)
        self._client_id_conflict = False
        self.on_connectivity_restored: Optional[callable] = None  # Callback for error 1102

    @property
    def is_connected(self) -> bool:
        return self._connected and self.ib.isConnected() and self._server_connected

    async def connect(self) -> bool:
        """Connect to IB Gateway.

        Raises ClientIdConflictError if the client ID is already in use,
        allowing the caller to retry with a different ID.
        """
        self._client_id_conflict = False

        # Listen for error 326 (client ID conflict) during connection
        def on_connect_error(reqId, errorCode, errorString, contract=None):
            if errorCode == 326:
                self._client_id_conflict = True

        self.ib.errorEvent += on_connect_error

        try:
            logger.info("connecting_to_ibkr", host=self.host, port=self.port)
            await self.ib.connectAsync(
                host=self.host,
                port=self.port,
                clientId=self.client_id,
            )
            self._connected = True
            self._server_connected = True

            # Accept delayed market data when live subscription isn't available
            self.ib.reqMarketDataType(3)

            # Register permanent error handler for server connectivity
            self.ib.errorEvent -= on_connect_error
            self.ib.errorEvent += self._on_error

            logger.info("connected_to_ibkr")
            return True
        except Exception as e:
            self.ib.errorEvent -= on_connect_error
            self._connected = False
            if self._client_id_conflict:
                logger.warning("ibkr_client_id_conflict", client_id=self.client_id)
                raise ClientIdConflictError(
                    f"Client ID {self.client_id} already in use"
                ) from e
            logger.error("ibkr_connection_failed", error=str(e))
            return False

    def _on_error(self, reqId: int, errorCode: int, errorString: str, contract: Any = None) -> None:
        """Handle IBKR error events to track server connectivity."""
        # Error 1100: Connectivity between IBKR and server lost
        if errorCode == 1100:
            logger.warning("ibkr_server_connectivity_lost", error_code=errorCode, message=errorString)
            self._server_connected = False
        # Error 1101/1102: Connectivity restored
        elif errorCode in (1101, 1102):
            logger.info("ibkr_server_connectivity_restored", error_code=errorCode, message=errorString)
            self._server_connected = True
            if self.on_connectivity_restored:
                self.on_connectivity_restored()

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

        # Request account values - use async wrapper
        account_values = await self.ib.accountSummaryAsync()

        total_value = 0.0
        cash_balance = 0.0
        buying_power = 0.0
        unrealized_pnl = 0.0
        realized_pnl = 0.0
        gross_position_value = 0.0
        accrued_cash = 0.0
        currency = "EUR"

        for av in account_values:
            if av.currency == "EUR":
                if av.tag == "NetLiquidation":
                    total_value = float(av.value)
                elif av.tag == "TotalCashValue":
                    cash_balance = float(av.value)
                elif av.tag == "BuyingPower":
                    buying_power = float(av.value)
                elif av.tag == "UnrealizedPnL":
                    unrealized_pnl = float(av.value)
                elif av.tag == "RealizedPnL":
                    realized_pnl = float(av.value)
                elif av.tag == "GrossPositionValue":
                    gross_position_value = float(av.value)
                elif av.tag == "AccruedCash":
                    accrued_cash = float(av.value)
            elif av.tag == "BuyingPower" and buying_power == 0.0:
                # BuyingPower sometimes only appears without currency filter
                buying_power = float(av.value)

        return AccountSummary(
            total_value=total_value,
            cash_balance=cash_balance,
            buying_power=buying_power,
            currency=currency,
            unrealized_pnl=unrealized_pnl,
            realized_pnl=realized_pnl,
            gross_position_value=gross_position_value,
            accrued_cash=accrued_cash,
        )

    async def get_positions(self) -> list[BrokerPosition]:
        """Get all positions."""
        if not self.is_connected:
            raise ConnectionError("Not connected to IBKR")

        positions = []
        ib_positions = await self.ib.reqPositionsAsync()
        for pos in ib_positions:
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
        """Get current market price (live or delayed)."""
        if not self.is_connected:
            return None

        try:
            contract = self._get_contract(symbol)
            await self.ib.qualifyContractsAsync(contract)

            # Request market data
            ticker = self.ib.reqMktData(contract)
            await asyncio.sleep(2)  # Wait for data

            price = ticker.marketPrice()

            # Delayed data can take longer to arrive — retry once
            if not price or not (price > 0):
                await asyncio.sleep(3)
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
        await self.ib.qualifyContractsAsync(contract)

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
        timeout = 60  # seconds
        for _ in range(timeout * 10):
            if trade.isDone():
                # Small delay to let IBKR error messages arrive via callback
                await asyncio.sleep(0.3)
                break
            await asyncio.sleep(0.1)

        # Get result
        if trade.orderStatus.status == "Filled":
            status = "FILLED"
        elif trade.orderStatus.status in ("Cancelled", "ApiCancelled", "Inactive"):
            status = "REJECTED"
        elif trade.orderStatus.filled > 0:
            status = "PARTIAL"
        else:
            status = "PENDING"

        # Extract the real error reason from trade log entries
        error_reason = ""
        for entry in reversed(trade.log):
            if entry.message:
                error_reason = entry.message
                break

        # Build a useful message: prefer the error reason over raw status
        if status in ("REJECTED", "PENDING") and error_reason:
            message = error_reason
        elif status in ("REJECTED", "PENDING"):
            message = trade.orderStatus.status
        else:
            message = trade.orderStatus.status

        result = OrderResult(
            order_id=str(trade.order.orderId),
            symbol=order.symbol,
            action=order.action,
            quantity=order.quantity,
            filled_quantity=float(trade.orderStatus.filled),
            avg_fill_price=float(trade.orderStatus.avgFillPrice or 0),
            status=status,
            message=message,
        )

        logger.info(
            "order_result",
            order_id=result.order_id,
            status=result.status,
            filled=result.filled_quantity,
            avg_price=result.avg_fill_price,
        )

        return result

    async def verify_order(
        self,
        order_result: OrderResult,
        expected_shares: float,
        expected_price: float | None = None,
    ) -> OrderVerification:
        """Verify order was filled as expected."""
        if order_result.status == "REJECTED":
            return OrderVerification(
                verified=False,
                expected_shares=expected_shares,
                actual_shares=0,
                expected_action=order_result.action,
                slippage_pct=0,
                message=f"Order rejected: {order_result.message}",
            )

        if order_result.status == "PENDING":
            return OrderVerification(
                verified=False,
                expected_shares=expected_shares,
                actual_shares=order_result.filled_quantity,
                expected_action=order_result.action,
                slippage_pct=0,
                message="Order still pending after timeout",
            )

        fill_ratio = order_result.filled_quantity / expected_shares if expected_shares > 0 else 0
        if fill_ratio < 0.80:
            return OrderVerification(
                verified=False,
                expected_shares=expected_shares,
                actual_shares=order_result.filled_quantity,
                expected_action=order_result.action,
                slippage_pct=0,
                message=f"Partial fill: {fill_ratio:.1%} of expected",
            )

        slippage_pct = 0.0
        if expected_price and expected_price > 0 and order_result.avg_fill_price > 0:
            slippage_pct = (order_result.avg_fill_price - expected_price) / expected_price
            if order_result.action == "SELL":
                slippage_pct = -slippage_pct

        return OrderVerification(
            verified=True,
            expected_shares=expected_shares,
            actual_shares=order_result.filled_quantity,
            expected_action=order_result.action,
            slippage_pct=slippage_pct,
            message="Order verified successfully",
        )

    async def execute_switch(
        self,
        sell_symbol: str,
        buy_symbol: str,
        sell_quantity: Optional[float] = None,
        position_size_pct: float = 1.0,
        settlement_headroom_pct: float = 0.02,
        settlement_min_cash_buffer: float = 0.0,
    ) -> tuple[OrderResult, OrderResult]:
        """
        Execute a position switch: sell one ETF and buy another.

        If sell_quantity is None, sells entire position.
        Uses proceeds to buy the new symbol, scaled by position_size_pct.

        Args:
            sell_symbol: Symbol to sell.
            buy_symbol: Symbol to buy.
            sell_quantity: Quantity to sell (None = entire position).
            position_size_pct: Position size percentage for buy leg (0.0 to 1.0).
                In volatile/bear markets, this may be reduced to hold more cash.
            settlement_headroom_pct: Additional settlement reserve percentage for buy leg.
            settlement_min_cash_buffer: Absolute cash buffer to keep unspent after buy leg.
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

        # Calculate shares to buy with settlement guard:
        # - Apply position_size_pct (regime-based sizing)
        # - Reserve settlement headroom and optional absolute cash buffer
        position_pct = min(max(position_size_pct, 0.0), 1.0)
        headroom_pct = min(max(settlement_headroom_pct, 0.0), 0.95)
        min_cash_buffer = max(settlement_min_cash_buffer, 0.0)

        gross_funds = proceeds * position_pct
        unguarded_qty = int(gross_funds / buy_price)
        guarded_funds = max(0.0, gross_funds * (1.0 - headroom_pct) - min_cash_buffer)
        buy_quantity = int(guarded_funds / buy_price)

        guard_note = ""
        if buy_quantity < unguarded_qty:
            guard_note = (
                f"Settlement guard reduced switch BUY size {buy_symbol}: "
                f"{unguarded_qty} -> {buy_quantity} shares "
                f"(headroom={headroom_pct:.1%}, cash_buffer=${min_cash_buffer:,.2f})"
            )
            logger.warning(
                "settlement_guard_reduced_switch_buy",
                sell_symbol=sell_symbol,
                buy_symbol=buy_symbol,
                proceeds=proceeds,
                gross_funds=gross_funds,
                guarded_funds=guarded_funds,
                unguarded_qty=unguarded_qty,
                buy_quantity=buy_quantity,
                headroom_pct=headroom_pct,
                min_cash_buffer=min_cash_buffer,
            )

        if buy_quantity <= 0:
            reason = (
                f"Settlement guard blocked switch BUY {buy_symbol}: "
                f"proceeds={proceeds:.2f}, position_pct={position_pct:.0%}, "
                f"headroom={headroom_pct:.1%}, min_buffer=${min_cash_buffer:,.2f}, "
                f"price={buy_price:.2f}"
            )
            logger.warning("settlement_guard_blocked_switch_buy", reason=reason)
            raise RuntimeError(reason)

        logger.info(
            "execute_switch_sizing",
            sell_symbol=sell_symbol,
            buy_symbol=buy_symbol,
            proceeds=proceeds,
            position_size_pct=f"{position_pct:.0%}",
            settlement_headroom_pct=headroom_pct,
            settlement_min_cash_buffer=min_cash_buffer,
            buy_quantity=buy_quantity,
        )

        # Buy
        buy_order = BrokerOrder(
            symbol=buy_symbol,
            action="BUY",
            quantity=buy_quantity,
            order_type="MKT",
        )
        buy_result = await self.place_order(buy_order)
        if guard_note:
            buy_result.message = f"{buy_result.message} | {guard_note}".strip(" |")

        return sell_result, buy_result
