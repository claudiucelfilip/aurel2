"""Trade executor - translates decisions into IBKR orders."""

from dataclasses import dataclass
from typing import Any, Optional

import structlog

from aurel2.broker.base import BrokerOrder, OrderResult, BrokerPosition
from aurel2.broker.ibkr import OrderVerification
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
        position_size_pct: float = 1.0,
    ) -> ExecutionResult:
        """
        Execute a trading decision.

        Args:
            action: "buy", "sell", or "hold"
            symbol: Target symbol for buy, or symbol to sell
            current_holding: Current position symbol (for switch detection)
            position_size_pct: Position size as percentage (0.0 to 1.0) based on
                regime detection and confidence. Defaults to 1.0 (100%).

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
                return await self._execute_switch(current_holding, symbol, position_size_pct)
            else:
                return await self._execute_buy(symbol, position_size_pct)

        return ExecutionResult(
            success=False,
            action=action,
            symbol=symbol,
            message=f"Unknown action: {action}",
        )

    async def _execute_buy(self, symbol: str, position_size_pct: float = 1.0) -> ExecutionResult:
        """Execute a buy order using available cash.

        Args:
            symbol: The symbol to buy.
            position_size_pct: Position size as percentage of cash balance (0.0 to 1.0).
                This is determined by regime detection and confidence scoring.
                Uses cash_balance (never margin/buying_power) to avoid leverage.
        """
        logger.info("executor_buy_start", symbol=symbol, position_size_pct=position_size_pct)

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

            # Apply position sizing: use position_size_pct of cash balance
            # Use cash_balance (not buying_power which includes margin leverage)
            # Then apply 98% buffer to leave room for commissions/settlement
            effective_pct = position_size_pct * 0.98
            available = summary.cash_balance * effective_pct
            shares = int(available / price)

            logger.info(
                "executor_position_sizing",
                symbol=symbol,
                position_size_pct=f"{position_size_pct:.0%}",
                cash_balance=summary.cash_balance,
                effective_amount=available,
                shares=shares,
            )

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

            order_result = await self.connection.broker.place_order(order)

            # Verify the order
            verification = await self.connection.broker.verify_order(
                order_result=order_result,
                expected_shares=shares,
                expected_price=price,
            )

            if not verification.verified:
                logger.error(
                    "order_verification_failed",
                    symbol=symbol,
                    expected_shares=verification.expected_shares,
                    actual_shares=verification.actual_shares,
                    message=verification.message,
                )
                return ExecutionResult(
                    success=False,
                    action="buy",
                    symbol=symbol,
                    shares=verification.actual_shares,
                    fill_price=order_result.avg_fill_price,
                    message=f"Verification failed: {verification.message}",
                    order_result=order_result,
                )

            if abs(verification.slippage_pct) > 0.01:
                logger.warning(
                    "high_slippage_detected",
                    symbol=symbol,
                    slippage_pct=f"{verification.slippage_pct:.2%}",
                    expected_price=price,
                    fill_price=order_result.avg_fill_price,
                )

            success = order_result.status in ("FILLED", "PARTIAL")
            return ExecutionResult(
                success=success,
                action="buy",
                symbol=symbol,
                shares=order_result.filled_quantity,
                fill_price=order_result.avg_fill_price,
                message=f"Order {order_result.status}: {order_result.message}",
                order_result=order_result,
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

            # Get market price for slippage calculation
            market_price = await self.connection.broker.get_market_price(symbol)

            # Place sell order
            order = BrokerOrder(
                symbol=symbol,
                action="SELL",
                quantity=position.shares,
                order_type="MKT",
            )

            order_result = await self.connection.broker.place_order(order)

            # Verify the order
            verification = await self.connection.broker.verify_order(
                order_result=order_result,
                expected_shares=position.shares,
                expected_price=market_price,
            )

            if not verification.verified:
                logger.error(
                    "order_verification_failed",
                    symbol=symbol,
                    expected_shares=verification.expected_shares,
                    actual_shares=verification.actual_shares,
                    message=verification.message,
                )
                return ExecutionResult(
                    success=False,
                    action="sell",
                    symbol=symbol,
                    shares=verification.actual_shares,
                    fill_price=order_result.avg_fill_price,
                    message=f"Verification failed: {verification.message}",
                    order_result=order_result,
                )

            if abs(verification.slippage_pct) > 0.01:
                logger.warning(
                    "high_slippage_detected",
                    symbol=symbol,
                    slippage_pct=f"{verification.slippage_pct:.2%}",
                    expected_price=market_price,
                    fill_price=order_result.avg_fill_price,
                )

            success = order_result.status in ("FILLED", "PARTIAL")
            return ExecutionResult(
                success=success,
                action="sell",
                symbol=symbol,
                shares=order_result.filled_quantity,
                fill_price=order_result.avg_fill_price,
                message=f"Order {order_result.status}: {order_result.message}",
                order_result=order_result,
            )

        except Exception as e:
            logger.error("executor_sell_error", symbol=symbol, error=str(e))
            return ExecutionResult(
                success=False,
                action="sell",
                symbol=symbol,
                message=f"Sell error: {str(e)}",
            )

    async def _execute_switch(
        self, sell_symbol: str, buy_symbol: str, position_size_pct: float = 1.0
    ) -> ExecutionResult:
        """Execute a position switch: sell current, buy new.

        Args:
            sell_symbol: Symbol to sell (current holding).
            buy_symbol: Symbol to buy (new position).
            position_size_pct: Position size as percentage for the buy leg (0.0 to 1.0).
                Note: Sell is always 100% of position, but buy may be reduced based on regime.
        """
        logger.info(
            "executor_switch_start",
            sell=sell_symbol,
            buy=buy_symbol,
            position_size_pct=position_size_pct,
        )

        try:
            # Use the broker's atomic switch operation with position sizing
            sell_result, buy_result = await self.connection.broker.execute_switch(
                sell_symbol=sell_symbol,
                buy_symbol=buy_symbol,
                position_size_pct=position_size_pct,
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

    async def reconcile_positions(self) -> dict[str, Any]:
        """Reconcile local state with broker positions."""
        broker_positions = await self.connection.get_positions()

        result = {
            "positions": [],
            "discrepancies": [],
            "total_value": 0,
        }

        for pos in broker_positions:
            result["positions"].append({
                "symbol": pos.symbol,
                "shares": pos.shares,
                "market_value": pos.market_value,
                "avg_cost": pos.avg_cost,
            })
            result["total_value"] += pos.market_value

        logger.info(
            "positions_reconciled",
            num_positions=len(broker_positions),
            total_value=result["total_value"],
        )

        return result
