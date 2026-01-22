"""Trade executor - translates decisions into IBKR orders."""

from dataclasses import dataclass
from typing import Optional

import structlog

from aurel2.broker.base import BrokerOrder, OrderResult, BrokerPosition
from aurel2.live.connection import IBKRConnection

logger = structlog.get_logger()


@dataclass
class ExecutionResult:
    """Result of a trade execution."""

    success: bool
    action: str
    symbol: Optional[str]
    shares: float = 0
    fill_price: float = 0
    message: str = ""
    order_result: Optional[OrderResult] = None


class Executor:
    """
    Executes trading decisions via IBKR.

    Handles:
    - BUY: Purchase shares of the target symbol
    - SELL: Sell current position
    - HOLD: No action
    - SWITCH: Sell current, buy new (atomic operation)
    """

    def __init__(self, connection: IBKRConnection):
        self.connection = connection

    async def execute(
        self,
        action: str,
        symbol: Optional[str],
        current_holding: Optional[str] = None,
    ) -> ExecutionResult:
        """
        Execute a trading decision.

        Args:
            action: "buy", "sell", or "hold"
            symbol: Target symbol for buy, or symbol to sell
            current_holding: Current position symbol (for switch detection)

        Returns:
            ExecutionResult with success status and details
        """
        action = action.lower()

        if not self.connection.is_connected:
            return ExecutionResult(
                success=False,
                action=action,
                symbol=symbol,
                message="Not connected to IBKR",
            )

        if action == "hold":
            logger.info("executor_hold", symbol=current_holding)
            return ExecutionResult(
                success=True,
                action="hold",
                symbol=current_holding,
                message="No action taken - holding current position",
            )

        if action == "sell":
            return await self._execute_sell(symbol or current_holding)

        if action == "buy":
            # Check if this is a switch (sell current, buy new)
            if current_holding and current_holding.upper() != symbol.upper():
                return await self._execute_switch(current_holding, symbol)
            else:
                return await self._execute_buy(symbol)

        return ExecutionResult(
            success=False,
            action=action,
            symbol=symbol,
            message=f"Unknown action: {action}",
        )

    async def _execute_buy(self, symbol: str) -> ExecutionResult:
        """Execute a buy order using available cash."""
        logger.info("executor_buy_start", symbol=symbol)

        try:
            # Get account summary for available cash
            summary = await self.connection.get_account_summary()
            if not summary:
                return ExecutionResult(
                    success=False,
                    action="buy",
                    symbol=symbol,
                    message="Could not get account summary",
                )

            # Get market price
            price = await self.connection.broker.get_market_price(symbol)
            if not price:
                return ExecutionResult(
                    success=False,
                    action="buy",
                    symbol=symbol,
                    message=f"Could not get market price for {symbol}",
                )

            # Calculate shares to buy (use 99% of buying power to leave buffer)
            available = summary.buying_power * 0.99
            shares = int(available / price)

            if shares <= 0:
                return ExecutionResult(
                    success=False,
                    action="buy",
                    symbol=symbol,
                    message=f"Insufficient funds. Available: {available:.2f}, Price: {price:.2f}",
                )

            # Place order
            order = BrokerOrder(
                symbol=symbol,
                action="BUY",
                quantity=shares,
                order_type="MKT",
            )

            result = await self.connection.broker.place_order(order)

            success = result.status == "FILLED"
            return ExecutionResult(
                success=success,
                action="buy",
                symbol=symbol,
                shares=result.filled_quantity,
                fill_price=result.avg_fill_price,
                message=f"Order {result.status}: {result.message}",
                order_result=result,
            )

        except Exception as e:
            logger.error("executor_buy_error", symbol=symbol, error=str(e))
            return ExecutionResult(
                success=False,
                action="buy",
                symbol=symbol,
                message=f"Buy error: {str(e)}",
            )

    async def _execute_sell(self, symbol: str) -> ExecutionResult:
        """Execute a sell order for entire position."""
        logger.info("executor_sell_start", symbol=symbol)

        try:
            # Get current position
            position = await self.connection.get_position(symbol)
            if not position or position.shares <= 0:
                return ExecutionResult(
                    success=False,
                    action="sell",
                    symbol=symbol,
                    message=f"No position found for {symbol}",
                )

            # Place sell order
            order = BrokerOrder(
                symbol=symbol,
                action="SELL",
                quantity=position.shares,
                order_type="MKT",
            )

            result = await self.connection.broker.place_order(order)

            success = result.status == "FILLED"
            return ExecutionResult(
                success=success,
                action="sell",
                symbol=symbol,
                shares=result.filled_quantity,
                fill_price=result.avg_fill_price,
                message=f"Order {result.status}: {result.message}",
                order_result=result,
            )

        except Exception as e:
            logger.error("executor_sell_error", symbol=symbol, error=str(e))
            return ExecutionResult(
                success=False,
                action="sell",
                symbol=symbol,
                message=f"Sell error: {str(e)}",
            )

    async def _execute_switch(self, sell_symbol: str, buy_symbol: str) -> ExecutionResult:
        """Execute a position switch: sell current, buy new."""
        logger.info("executor_switch_start", sell=sell_symbol, buy=buy_symbol)

        try:
            # Use the broker's atomic switch operation
            sell_result, buy_result = await self.connection.broker.execute_switch(
                sell_symbol=sell_symbol,
                buy_symbol=buy_symbol,
            )

            # Check results
            if sell_result.status != "FILLED":
                return ExecutionResult(
                    success=False,
                    action="switch",
                    symbol=buy_symbol,
                    message=f"Sell leg failed: {sell_result.message}",
                    order_result=sell_result,
                )

            success = buy_result.status == "FILLED"
            return ExecutionResult(
                success=success,
                action="switch",
                symbol=buy_symbol,
                shares=buy_result.filled_quantity,
                fill_price=buy_result.avg_fill_price,
                message=f"Sold {sell_symbol}, bought {buy_symbol}. Status: {buy_result.status}",
                order_result=buy_result,
            )

        except Exception as e:
            logger.error(
                "executor_switch_error",
                sell=sell_symbol,
                buy=buy_symbol,
                error=str(e),
            )
            return ExecutionResult(
                success=False,
                action="switch",
                symbol=buy_symbol,
                message=f"Switch error: {str(e)}",
            )

    async def get_current_holding(self) -> Optional[str]:
        """Get the symbol of the current largest position."""
        positions = await self.connection.get_positions()

        if not positions:
            return None

        # Find largest position by market value
        largest = max(positions, key=lambda p: p.market_value)

        # Only return if it's a meaningful position
        if largest.market_value > 100:  # More than $100
            return largest.symbol

        return None
