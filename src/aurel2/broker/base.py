"""Base broker interface."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import structlog

logger = structlog.get_logger()


@dataclass
class BrokerPosition:
    """A position held at the broker."""
    symbol: str
    shares: float
    avg_cost: float
    market_price: float
    market_value: float
    unrealized_pnl: float
    unrealized_pnl_pct: float = 0.0
    currency: str = "USD"


@dataclass
class BrokerOrder:
    """An order to be placed."""
    symbol: str
    action: str  # "BUY" or "SELL"
    quantity: float
    order_type: str = "MKT"  # MKT, LMT
    limit_price: Optional[float] = None


@dataclass
class OrderResult:
    """Result of an order execution."""
    order_id: str
    symbol: str
    action: str
    quantity: float
    filled_quantity: float
    avg_fill_price: float
    status: str  # "FILLED", "PARTIAL", "REJECTED", "PENDING"
    message: str = ""


@dataclass
class AccountSummary:
    """Broker account summary."""
    total_value: float
    cash_balance: float
    buying_power: float
    currency: str = "USD"
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    gross_position_value: float = 0.0
    accrued_cash: float = 0.0


@dataclass
class OrderVerification:
    """Result of order verification."""
    verified: bool
    expected_shares: float
    actual_shares: float
    expected_action: str  # "BUY" or "SELL"
    slippage_pct: float  # (fill_price - expected_price) / expected_price
    message: str


@dataclass(frozen=True)
class MarketQuote:
    """One timestamped bid/ask observation."""

    symbol: str
    price: float
    bid_price: float
    ask_price: float
    observed_at: datetime


@dataclass(frozen=True)
class MarketClock:
    """Broker view of the current regular trading session."""

    is_open: bool
    timestamp: datetime
    next_open: datetime
    next_close: datetime


class BaseBroker(ABC):
    """Abstract base class for broker integrations."""

    @abstractmethod
    async def connect(self) -> bool:
        """Connect to the broker. Returns True if successful."""
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from the broker."""
        pass

    @abstractmethod
    async def get_account_summary(self) -> AccountSummary:
        """Get account summary (cash, total value, etc.)."""
        pass

    @abstractmethod
    async def get_positions(self) -> list[BrokerPosition]:
        """Get all current positions."""
        pass

    @abstractmethod
    async def get_position(self, symbol: str) -> Optional[BrokerPosition]:
        """Get a specific position by symbol."""
        pass

    @abstractmethod
    async def place_order(self, order: BrokerOrder) -> OrderResult:
        """Place an order. Returns the result."""
        pass

    @abstractmethod
    async def get_market_price(self, symbol: str) -> Optional[float]:
        """Get current market price for a symbol."""
        pass

    @abstractmethod
    async def get_market_quotes(self, symbols: list[str]) -> dict[str, MarketQuote]:
        """Get one batch of timestamped quotes for the requested symbols."""
        pass

    @abstractmethod
    async def get_market_clock(self) -> Optional[MarketClock]:
        """Get the broker's current regular-session clock."""
        pass

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Check if connected to broker."""
        pass

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
        """Execute a position switch: sell one ETF and buy another.

        If sell_quantity is None, sells entire position.
        Uses proceeds to buy the new symbol, scaled by position_size_pct.
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

        # Calculate shares to buy with settlement guard
        position_pct = min(max(position_size_pct, 0.0), 1.0)
        headroom_pct = min(max(settlement_headroom_pct, 0.0), 0.95)
        min_cash_buffer = max(settlement_min_cash_buffer, 0.0)

        gross_funds = proceeds * position_pct
        unguarded_qty = round(gross_funds / buy_price, 6)
        guarded_funds = max(0.0, gross_funds * (1.0 - headroom_pct) - min_cash_buffer)
        buy_quantity = round(guarded_funds / buy_price, 6)

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
