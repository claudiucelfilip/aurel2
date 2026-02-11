"""Tests for trade execution recording (daemon + checker post-trade state capture)."""

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("ib_insync", reason="ib_insync not installed")

from aurel2.live.journal import TradeJournal
from aurel2.live.executor import ExecutionResult


# --- Unit Tests: TradeJournal.record_execution ---

class TestRecordExecution:
    """Tests for TradeJournal.record_execution with post-trade state."""

    @pytest.fixture
    def journal(self, tmp_path):
        """Create a journal with a sample decision entry."""
        filepath = str(tmp_path / "journal.json")
        j = TradeJournal(filepath=filepath)
        j.record_decision(
            decision_id="test-001",
            action="buy",
            symbol="GLD",
            confidence=0.9,
            decision_type="non_routine",
            strategy_signals={},
            account_value=1000000.0,
            current_holding=None,
        )
        return j

    def test_records_account_value_after(self, journal):
        """record_execution should store account_value_after."""
        journal.record_execution(
            decision_id="test-001",
            success=True,
            shares=100,
            fill_price=450.0,
            account_value_after=1005000.0,
        )
        entry = journal.entries[-1]
        assert entry.account_value_after == 1005000.0

    def test_records_current_holding_after(self, journal):
        """record_execution should store current_holding_after."""
        journal.record_execution(
            decision_id="test-001",
            success=True,
            shares=100,
            fill_price=450.0,
            current_holding_after="GLD",
        )
        entry = journal.entries[-1]
        assert entry.current_holding_after == "GLD"

    def test_records_both_post_trade_fields(self, journal):
        """record_execution should store both post-trade fields."""
        journal.record_execution(
            decision_id="test-001",
            success=True,
            shares=100,
            fill_price=450.0,
            account_value_after=1005000.0,
            current_holding_after="GLD",
        )
        entry = journal.entries[-1]
        assert entry.account_value_after == 1005000.0
        assert entry.current_holding_after == "GLD"

    def test_none_when_not_provided(self, journal):
        """Post-trade fields should be None when not provided."""
        journal.record_execution(
            decision_id="test-001",
            success=True,
            shares=100,
            fill_price=450.0,
        )
        entry = journal.entries[-1]
        assert entry.account_value_after is None
        assert entry.current_holding_after is None

    def test_persists_to_file(self, journal):
        """Post-trade state should be persisted to disk."""
        journal.record_execution(
            decision_id="test-001",
            success=True,
            shares=100,
            fill_price=450.0,
            account_value_after=1005000.0,
            current_holding_after="GLD",
        )
        # Reload from disk
        reloaded = TradeJournal(filepath=journal.filepath)
        entry = reloaded.entries[-1]
        assert entry.account_value_after == 1005000.0
        assert entry.current_holding_after == "GLD"

    def test_records_execution_error(self, journal):
        """record_execution should store error message on failure."""
        journal.record_execution(
            decision_id="test-001",
            success=False,
            error="Order rejected: insufficient funds",
        )
        entry = journal.entries[-1]
        assert entry.executed is False
        assert entry.execution_error == "Order rejected: insufficient funds"

    def test_missing_decision_id_no_crash(self, journal):
        """record_execution with unknown ID should not crash."""
        journal.record_execution(
            decision_id="nonexistent-id",
            success=True,
            shares=100,
            fill_price=450.0,
            account_value_after=1000.0,
        )
        # Should not have modified any entry
        entry = journal.entries[-1]
        assert entry.id == "test-001"
        assert entry.account_value_after is None


# --- Tests: Daemon execution paths call record_execution with post-trade state ---

class TestDaemonExecuteApproved:
    """Tests that _execute_approved passes post-trade state to journal."""

    @pytest.fixture
    def mock_daemon(self):
        """Create a mock daemon with required dependencies."""
        from unittest.mock import MagicMock, AsyncMock

        daemon = MagicMock()
        daemon.dry_run = False

        # Mock connection
        daemon.connection = MagicMock()
        daemon.connection.is_connected = True

        account_summary = MagicMock()
        account_summary.total_value = 1005000.0
        daemon.connection.get_account_summary = AsyncMock(return_value=account_summary)

        # Mock executor
        daemon.executor = MagicMock()
        daemon.executor.execute = AsyncMock(return_value=ExecutionResult(
            success=True,
            action="buy",
            symbol="GLD",
            shares=100,
            fill_price=450.0,
        ))
        daemon.executor.get_current_holding = AsyncMock(return_value="GLD")

        # Mock journal
        daemon.checker = MagicMock()
        daemon.checker.journal = MagicMock()
        daemon.checker.journal.record_execution = MagicMock()

        # Mock pending manager
        daemon.pending_manager = MagicMock()

        # Mock notifier
        daemon.notifier = MagicMock()

        return daemon

    def test_execute_approved_passes_account_value(self, mock_daemon):
        """_execute_approved should pass account_value_after to record_execution."""
        from aurel2.live.daemon import LiveDaemon

        decision = MagicMock()
        decision.id = "d-001"
        decision.action = "buy"
        decision.symbol = "GLD"
        decision.current_holding = None
        decision.position_size_pct = 1.0
        decision.journal_decision_id = "j-001"

        asyncio.run(LiveDaemon._execute_approved(mock_daemon, decision))

        mock_daemon.checker.journal.record_execution.assert_called_once()
        call_kwargs = mock_daemon.checker.journal.record_execution.call_args
        assert call_kwargs.kwargs.get("account_value_after") == 1005000.0
        assert call_kwargs.kwargs.get("current_holding_after") == "GLD"

    def test_execute_approved_none_on_failed_trade(self, mock_daemon):
        """Post-trade state should be None when execution fails."""
        from aurel2.live.daemon import LiveDaemon

        mock_daemon.executor.execute = AsyncMock(return_value=ExecutionResult(
            success=False,
            action="buy",
            symbol="GLD",
            message="Order rejected",
        ))

        decision = MagicMock()
        decision.id = "d-002"
        decision.action = "buy"
        decision.symbol = "GLD"
        decision.current_holding = None
        decision.position_size_pct = 1.0
        decision.journal_decision_id = "j-002"

        asyncio.run(LiveDaemon._execute_approved(mock_daemon, decision))

        call_kwargs = mock_daemon.checker.journal.record_execution.call_args
        assert call_kwargs.kwargs.get("account_value_after") is None
        assert call_kwargs.kwargs.get("current_holding_after") is None


class TestDaemonExecuteTimeout:
    """Tests that _execute_timeout passes post-trade state to journal."""

    @pytest.fixture
    def mock_daemon(self):
        """Create a mock daemon for timeout execution."""
        daemon = MagicMock()
        daemon.dry_run = False

        daemon.connection = MagicMock()
        daemon.connection.is_connected = True
        daemon.connection.broker = MagicMock()
        daemon.connection.broker.get_market_price = AsyncMock(return_value=455.0)

        account_summary = MagicMock()
        account_summary.total_value = 1006000.0
        daemon.connection.get_account_summary = AsyncMock(return_value=account_summary)

        daemon.executor = MagicMock()
        daemon.executor.execute = AsyncMock(return_value=ExecutionResult(
            success=True,
            action="buy",
            symbol="EFA",
            shares=200,
            fill_price=80.0,
        ))
        daemon.executor.get_current_holding = AsyncMock(return_value="EFA")

        daemon.checker = MagicMock()
        daemon.checker.journal = MagicMock()
        daemon.checker.journal.record_execution = MagicMock()

        daemon.pending_manager = MagicMock()
        daemon.pending_manager.validate_decision_still_valid = MagicMock(return_value=(True, ""))

        daemon.notifier = MagicMock()

        return daemon

    def test_execute_timeout_passes_post_trade_state(self, mock_daemon):
        """_execute_timeout should pass account_value_after and current_holding_after."""
        from aurel2.live.daemon import LiveDaemon

        decision = MagicMock()
        decision.id = "d-003"
        decision.action = "buy"
        decision.symbol = "EFA"
        decision.current_holding = "GLD"
        decision.position_size_pct = 0.8
        decision.journal_decision_id = "j-003"

        asyncio.run(LiveDaemon._execute_timeout(mock_daemon, decision))

        call_kwargs = mock_daemon.checker.journal.record_execution.call_args
        assert call_kwargs.kwargs.get("account_value_after") == 1006000.0
        assert call_kwargs.kwargs.get("current_holding_after") == "EFA"


class TestCheckerExecuteDecision:
    """Tests that checker._execute_decision passes post-trade state."""

    @pytest.fixture
    def mock_checker(self):
        """Create a mock checker."""
        checker = MagicMock()
        checker.dry_run = False

        checker.connection = MagicMock()
        account_summary = MagicMock()
        account_summary.total_value = 1007000.0
        checker.connection.get_account_summary = AsyncMock(return_value=account_summary)

        checker.executor = MagicMock()
        checker.executor.execute = AsyncMock(return_value=ExecutionResult(
            success=True,
            action="buy",
            symbol="SPY",
            shares=50,
            fill_price=580.0,
        ))
        checker.executor.get_current_holding = AsyncMock(return_value="SPY")

        checker.journal = MagicMock()
        checker.journal.record_execution = MagicMock()

        checker.notifier = MagicMock()

        return checker

    def test_execute_decision_passes_post_trade_state(self, mock_checker):
        """_execute_decision should pass account_value_after and current_holding_after."""
        from aurel2.live.checker import Checker
        from aurel2.agent.orchestrator import AgentDecision, DecisionType, Urgency
        from aurel2.core.models import SignalAction

        decision = AgentDecision(
            decision_type=DecisionType.ROUTINE,
            action=SignalAction.BUY,
            asset_symbol="SPY",
            reasoning="Test",
            confidence=0.9,
            strategy_signals={},
            requires_approval=False,
            timeout_hours=1.0,
            urgency=Urgency.LOW,
        )

        asyncio.run(Checker._execute_decision(
            mock_checker, decision, None, None, 1000000.0, "test-id"
        ))

        call_kwargs = mock_checker.journal.record_execution.call_args
        assert call_kwargs.kwargs.get("account_value_after") == 1007000.0
        assert call_kwargs.kwargs.get("current_holding_after") == "SPY"
