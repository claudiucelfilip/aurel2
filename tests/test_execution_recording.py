"""Tests for trade execution recording (daemon + checker post-trade state capture)."""

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


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


# --- Tests: Daemon execution paths delegate to trade_recorder ---

class TestDaemonExecuteApproved:
    """Tests that _execute_approved delegates to trade_recorder correctly."""

    @pytest.fixture
    def mock_daemon(self):
        """Create a mock daemon with required dependencies."""
        daemon = MagicMock()
        daemon.dry_run = False

        # Mock trade_recorder on checker
        daemon.checker = MagicMock()
        daemon.checker.trade_recorder = MagicMock()
        daemon.checker.trade_recorder.execute_and_record = AsyncMock(
            return_value=ExecutionResult(
                success=True, action="buy", symbol="GLD",
                shares=100, fill_price=450.0,
            )
        )

        daemon.pending_manager = MagicMock()
        daemon.notifier = MagicMock()

        return daemon

    def test_execute_approved_calls_trade_recorder(self, mock_daemon):
        """_execute_approved should delegate to trade_recorder.execute_and_record."""
        from aurel2.live.daemon import LiveDaemon

        decision = MagicMock()
        decision.id = "d-001"
        decision.action = "buy"
        decision.symbol = "GLD"
        decision.current_holding = None
        decision.position_size_pct = 1.0
        decision.journal_decision_id = "j-001"

        asyncio.run(LiveDaemon._execute_approved(mock_daemon, decision))

        mock_daemon.checker.trade_recorder.execute_and_record.assert_called_once()
        call_kwargs = mock_daemon.checker.trade_recorder.execute_and_record.call_args
        assert call_kwargs.kwargs.get("action") == "buy"
        assert call_kwargs.kwargs.get("symbol") == "GLD"
        assert call_kwargs.kwargs.get("decision_id") == "j-001"

    def test_execute_approved_skips_on_dry_run(self, mock_daemon):
        """_execute_approved should skip execution in dry run mode."""
        from aurel2.live.daemon import LiveDaemon

        mock_daemon.dry_run = True

        decision = MagicMock()
        decision.id = "d-002"
        decision.action = "buy"
        decision.symbol = "GLD"
        decision.journal_decision_id = "j-002"

        asyncio.run(LiveDaemon._execute_approved(mock_daemon, decision))

        mock_daemon.checker.trade_recorder.execute_and_record.assert_not_called()


class TestDaemonExecuteTimeout:
    """Tests that _execute_timeout delegates to trade_recorder correctly."""

    @pytest.fixture
    def mock_daemon(self):
        """Create a mock daemon for timeout execution."""
        daemon = MagicMock()
        daemon.dry_run = False

        daemon.connection = MagicMock()
        daemon.connection.is_connected = True
        daemon.connection.broker = MagicMock()
        daemon.connection.broker.get_market_price = AsyncMock(return_value=455.0)

        daemon.checker = MagicMock()
        daemon.checker.trade_recorder = MagicMock()
        daemon.checker.trade_recorder.execute_and_record = AsyncMock(
            return_value=ExecutionResult(
                success=True, action="buy", symbol="EFA",
                shares=200, fill_price=80.0,
            )
        )

        daemon.pending_manager = MagicMock()
        daemon.pending_manager.validate_decision_still_valid = MagicMock(return_value=(True, ""))

        daemon.notifier = MagicMock()

        return daemon

    def test_execute_timeout_calls_trade_recorder(self, mock_daemon):
        """_execute_timeout should delegate to trade_recorder.execute_and_record."""
        from aurel2.live.daemon import LiveDaemon

        decision = MagicMock()
        decision.id = "d-003"
        decision.action = "buy"
        decision.symbol = "EFA"
        decision.current_holding = "GLD"
        decision.position_size_pct = 0.8
        decision.journal_decision_id = "j-003"

        asyncio.run(LiveDaemon._execute_timeout(mock_daemon, decision))

        mock_daemon.checker.trade_recorder.execute_and_record.assert_called_once()
        call_kwargs = mock_daemon.checker.trade_recorder.execute_and_record.call_args
        assert call_kwargs.kwargs.get("action") == "buy"
        assert call_kwargs.kwargs.get("symbol") == "EFA"
        assert call_kwargs.kwargs.get("decision_id") == "j-003"


class TestCheckerExecuteDecision:
    """Tests that checker._execute_decision delegates to trade_recorder."""

    @pytest.fixture
    def mock_checker(self):
        """Create a mock checker."""
        checker = MagicMock()
        checker.dry_run = False

        checker.trade_recorder = MagicMock()
        checker.trade_recorder.execute_and_record = AsyncMock(
            return_value=ExecutionResult(
                success=True, action="buy", symbol="SPY",
                shares=50, fill_price=580.0,
            )
        )

        checker.notifier = MagicMock()

        return checker

    def test_execute_decision_calls_trade_recorder(self, mock_checker):
        """_execute_decision should delegate to trade_recorder.execute_and_record."""
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

        mock_checker.trade_recorder.execute_and_record.assert_called_once()
        call_kwargs = mock_checker.trade_recorder.execute_and_record.call_args
        assert call_kwargs.kwargs.get("action") == "buy"
        assert call_kwargs.kwargs.get("symbol") == "SPY"
        assert call_kwargs.kwargs.get("decision_id") == "test-id"


class TestCheckerDispatchAutonomy:
    """The autonomy guarantee: a ROUTINE decision auto-executes and NEVER
    touches the approval path; only NON_ROUTINE/URGENT gets parked for approval.
    """

    def _mock_checker(self):
        checker = MagicMock()
        checker._execute_decision = AsyncMock(return_value="EXECUTED")
        checker._create_pending_decision = AsyncMock(return_value="PENDING")
        return checker

    def _decision(self, decision_type, requires_approval):
        from aurel2.agent.orchestrator import AgentDecision, Urgency
        from aurel2.core.models import SignalAction
        return AgentDecision(
            decision_type=decision_type,
            action=SignalAction.BUY,
            asset_symbol="XLK",
            reasoning="Test switch",
            confidence=0.9,
            strategy_signals={},
            requires_approval=requires_approval,
            timeout_hours=1.0,
            urgency=Urgency.LOW,
        )

    def test_routine_switch_auto_executes_not_parked(self):
        """A ROUTINE switch must auto-execute and never create a pending approval."""
        from aurel2.live.checker import Checker
        from aurel2.agent.orchestrator import DecisionType

        checker = self._mock_checker()
        decision = self._decision(DecisionType.ROUTINE, requires_approval=False)

        result = asyncio.run(Checker._dispatch_decision(
            checker, decision, None, {}, {}, "GLD", 100000.0, "did-1"
        ))

        assert result == "EXECUTED"
        checker._execute_decision.assert_called_once()
        checker._create_pending_decision.assert_not_called()

    def test_non_routine_switch_is_parked_for_approval(self):
        """A NON_ROUTINE switch must go to the approval path, not auto-execute."""
        from aurel2.live.checker import Checker
        from aurel2.agent.orchestrator import DecisionType

        checker = self._mock_checker()
        decision = self._decision(DecisionType.NON_ROUTINE, requires_approval=True)

        result = asyncio.run(Checker._dispatch_decision(
            checker, decision, None, {}, {}, "GLD", 100000.0, "did-2"
        ))

        assert result == "PENDING"
        checker._create_pending_decision.assert_called_once()
        checker._execute_decision.assert_not_called()
