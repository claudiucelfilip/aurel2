"""Tests for settlement headroom guard in live execution path."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from aurel2.broker.base import AccountSummary, OrderResult
from aurel2.live.executor import ExecutionResult, Executor
from aurel2.live.trade_recorder import TradeRecorder


def test_executor_buy_reduces_shares_with_settlement_headroom():
    async def _run():
        connection = MagicMock()
        connection.is_connected = True
        connection.get_account_summary = AsyncMock(return_value=AccountSummary(
            total_value=1000.0,
            cash_balance=1000.0,
            buying_power=1000.0,
        ))
        connection.broker = MagicMock()
        connection.broker.get_market_price = AsyncMock(return_value=100.0)
        connection.broker.place_order = AsyncMock(return_value=OrderResult(
            order_id="o1",
            symbol="SPY",
            action="BUY",
            quantity=9,
            filled_quantity=9,
            avg_fill_price=100.0,
            status="FILLED",
            message="filled",
        ))
        connection.broker.verify_order = AsyncMock(return_value=MagicMock(
            verified=True,
            expected_shares=9,
            actual_shares=9,
            slippage_pct=0.0,
            message="ok",
        ))

        executor = Executor(connection, settlement_headroom_pct=0.10, settlement_min_cash_buffer=0.0)

        result = await executor.execute(action="buy", symbol="SPY")

        assert result.success is True
        assert result.settlement_guard_note
        assert "reduced BUY size" in result.settlement_guard_note
        order = connection.broker.place_order.call_args.args[0]
        assert order.quantity == 9

    asyncio.run(_run())


def test_executor_buy_blocked_by_settlement_guard_buffer():
    async def _run():
        connection = MagicMock()
        connection.is_connected = True
        connection.get_account_summary = AsyncMock(return_value=AccountSummary(
            total_value=1000.0,
            cash_balance=1000.0,
            buying_power=1000.0,
        ))
        connection.broker = MagicMock()
        connection.broker.get_market_price = AsyncMock(return_value=100.0)
        connection.broker.place_order = AsyncMock()
        connection.broker.verify_order = AsyncMock()

        executor = Executor(connection, settlement_headroom_pct=0.02, settlement_min_cash_buffer=980.0)

        result = await executor.execute(action="buy", symbol="SPY")

        assert result.success is False
        assert "Settlement guard blocked BUY" in result.message
        assert connection.broker.place_order.await_count == 0

    asyncio.run(_run())


def test_trade_recorder_notifies_settlement_guard_reduction():
    async def _run():
        executor = MagicMock()
        executor.execute = AsyncMock(return_value=ExecutionResult(
            success=True,
            action="buy",
            symbol="SPY",
            shares=9,
            fill_price=100.0,
            settlement_guard_note="Settlement guard reduced BUY size for SPY: 10 -> 9 shares",
        ))
        executor.get_current_holding = AsyncMock(return_value="SPY")

        connection = MagicMock()
        connection.get_account_summary = AsyncMock(return_value=AccountSummary(
            total_value=1000.0,
            cash_balance=100.0,
            buying_power=100.0,
        ))

        journal = MagicMock()
        notifier = MagicMock()

        recorder = TradeRecorder(executor=executor, connection=connection, journal=journal, notifier=notifier)

        await recorder.execute_and_record(action="buy", symbol="SPY", decision_id="d1")

        kwargs = notifier.send.call_args.kwargs
        assert "Settlement guard reduced BUY size" in kwargs["message"]

    asyncio.run(_run())
