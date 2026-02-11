"""Tests for dashboard application logic."""

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# --- Fixtures ---

@pytest.fixture
def tmp_data_dir(tmp_path):
    """Create a temporary data directory and patch DATA_DIR + MODE_DATA_DIR."""
    with patch("aurel2.dashboard.app.DATA_DIR", tmp_path), \
         patch("aurel2.dashboard.app.MODE_DATA_DIR", tmp_path):
        yield tmp_path


@pytest.fixture
def journal_entries():
    """Sample trade journal entries covering all status scenarios."""
    return [
        {
            "id": "exec-001",
            "timestamp": "2026-01-23T18:37:18.602924",
            "action": "buy",
            "symbol": "GLD",
            "confidence": 0.9,
            "decision_type": "non_routine",
            "strategy_signals": {
                "dual_momentum": {"action": "buy", "confidence": 0.8, "asset_symbol": "GLD"},
                "mean_reversion": {"action": "hold", "confidence": 0.5, "asset_symbol": None},
                "multi_timeframe": {"action": "buy", "confidence": 0.9, "asset_symbol": "EFA"},
            },
            "ai_agrees": True,
            "ai_action": "buy",
            "ai_asset": "GLD",
            "executed": True,
            "shares": 1870.0,
            "fill_price": 453.86,
            "execution_error": None,
            "account_value_before": 1004835.04,
            "account_value_after": 1005000.00,
            "current_holding_before": None,
            "current_holding_after": "GLD",
        },
        {
            "id": "hold-001",
            "timestamp": "2026-01-24T16:03:39.377643",
            "action": "hold",
            "symbol": None,
            "confidence": 0.6,
            "decision_type": "non_routine",
            "strategy_signals": {},
            "ai_agrees": True,
            "ai_action": "hold",
            "ai_asset": "GLD",
            "executed": False,
            "shares": 0.0,
            "fill_price": 0.0,
            "execution_error": None,
            "account_value_before": 1012647.56,
        },
        {
            "id": "failed-001",
            "timestamp": "2026-02-03T00:15:36.002619",
            "action": "buy",
            "symbol": "EFA",
            "confidence": 0.85,
            "decision_type": "non_routine",
            "strategy_signals": {
                "dual_momentum": {"action": "hold", "confidence": 0.8, "asset_symbol": "GLD"},
                "mean_reversion": {"action": "hold", "confidence": 0.5, "asset_symbol": "GLD"},
                "multi_timeframe": {"action": "buy", "confidence": 0.98, "asset_symbol": "EFA"},
            },
            "ai_agrees": False,
            "ai_action": "buy",
            "ai_asset": "EFA",
            "executed": False,
            "shares": 0.0,
            "fill_price": 0.0,
            "execution_error": "Switch partially failed: GLD sold but EFA buy did not fill.",
            "account_value_before": None,
            "current_holding_before": "GLD",
        },
        {
            "id": "finalized-001",
            "timestamp": "2026-01-22T16:14:15.906540",
            "action": "buy",
            "symbol": "GLD",
            "confidence": 0.9,
            "decision_type": "non_routine",
            "strategy_signals": {},
            "ai_agrees": True,
            "ai_action": "buy",
            "ai_asset": "GLD",
            "executed": True,
            "shares": 0.0,
            "fill_price": 0.0,
            "execution_error": None,
            "account_value_before": None,
        },
        {
            "id": "noaction-001",
            "timestamp": "2026-01-22T16:32:38.723860",
            "action": "buy",
            "symbol": "GLD",
            "confidence": 0.9,
            "decision_type": "non_routine",
            "strategy_signals": {},
            "ai_agrees": True,
            "ai_action": "buy",
            "ai_asset": "GLD",
            "executed": False,
            "shares": 0.0,
            "fill_price": 0.0,
            "execution_error": None,
            "account_value_before": None,
        },
    ]


@pytest.fixture
def pending_decisions_data():
    """Sample pending decisions for cross-referencing."""
    return {
        "decisions": {
            "pend-abc": {
                "id": "pend-abc",
                "journal_decision_id": "noaction-001",
                "status": "pending",
                "action": "buy",
                "symbol": "GLD",
                "created_at": (datetime.now() - timedelta(minutes=30)).isoformat(),
                "confidence": 0.9,
                "approval_url": "https://example.com/api/decision/pend-abc",
            },
            "pend-executed": {
                "id": "pend-executed",
                "journal_decision_id": "finalized-001",
                "status": "executed",
                "action": "buy",
                "symbol": "GLD",
                "created_at": (datetime.now() - timedelta(hours=2)).isoformat(),
                "confidence": 0.9,
                "approval_url": "https://example.com/api/decision/pend-executed",
            },
        }
    }


def write_journal(tmp_dir: Path, entries: list):
    """Write journal entries to the tmp data dir."""
    (tmp_dir / "trade_journal.json").write_text(json.dumps(entries))


def write_pending(tmp_dir: Path, data: dict):
    """Write pending decisions to the tmp data dir."""
    (tmp_dir / "pending_decisions.json").write_text(json.dumps(data))


# --- Unit Tests: load_trade_history ---

class TestLoadTradeHistoryStatus:
    """Tests for status determination in load_trade_history."""

    def test_executed_status_requires_shares(self, tmp_data_dir, journal_entries):
        """Entry with executed=True and shares>0 should be 'executed'."""
        from aurel2.dashboard.app import load_trade_history
        write_journal(tmp_data_dir, journal_entries)
        result = load_trade_history(page=1, per_page=50)
        statuses = {d["id"]: d["status"] for d in result["all_decisions"]}
        assert statuses["exec-001"] == "executed"

    def test_finalized_status_when_executed_no_shares(self, tmp_data_dir, journal_entries):
        """Entry with executed=True but shares=0 should be 'finalized'."""
        from aurel2.dashboard.app import load_trade_history
        write_journal(tmp_data_dir, journal_entries)
        result = load_trade_history(page=1, per_page=50)
        statuses = {d["id"]: d["status"] for d in result["all_decisions"]}
        assert statuses["finalized-001"] == "finalized"

    def test_failed_status_on_execution_error(self, tmp_data_dir, journal_entries):
        """Entry with execution_error should be 'failed'."""
        from aurel2.dashboard.app import load_trade_history
        write_journal(tmp_data_dir, journal_entries)
        result = load_trade_history(page=1, per_page=50)
        statuses = {d["id"]: d["status"] for d in result["all_decisions"]}
        assert statuses["failed-001"] == "failed"

    def test_pending_status_from_cross_reference(self, tmp_data_dir, journal_entries, pending_decisions_data):
        """Entry linked to a pending decision should show 'pending'."""
        from aurel2.dashboard.app import load_trade_history
        write_journal(tmp_data_dir, journal_entries)
        write_pending(tmp_data_dir, pending_decisions_data)
        result = load_trade_history(page=1, per_page=50)
        statuses = {d["id"]: d["status"] for d in result["all_decisions"]}
        assert statuses["noaction-001"] == "pending"

    def test_finalized_status_from_pending_executed(self, tmp_data_dir, journal_entries, pending_decisions_data):
        """Entry linked to an executed pending decision with no shares should be 'finalized'."""
        from aurel2.dashboard.app import load_trade_history
        write_journal(tmp_data_dir, journal_entries)
        write_pending(tmp_data_dir, pending_decisions_data)
        result = load_trade_history(page=1, per_page=50)
        statuses = {d["id"]: d["status"] for d in result["all_decisions"]}
        assert statuses["finalized-001"] == "finalized"

    def test_failed_takes_precedence_over_executed(self, tmp_data_dir):
        """execution_error should take precedence over executed flag."""
        from aurel2.dashboard.app import load_trade_history
        entry = {
            "id": "fail-exec",
            "timestamp": "2026-02-01T10:00:00",
            "action": "buy",
            "symbol": "EFA",
            "executed": True,
            "shares": 100.0,
            "fill_price": 50.0,
            "execution_error": "Partial fill",
            "ai_agrees": True,
        }
        write_journal(tmp_data_dir, [entry])
        result = load_trade_history(page=1, per_page=50)
        assert result["all_decisions"][0]["status"] == "failed"


class TestLoadTradeHistoryFiltering:
    """Tests for hold filtering in load_trade_history."""

    def test_holds_are_filtered_out(self, tmp_data_dir, journal_entries):
        """Hold decisions should not appear in all_decisions."""
        from aurel2.dashboard.app import load_trade_history
        write_journal(tmp_data_dir, journal_entries)
        result = load_trade_history(page=1, per_page=50)
        actions = [d["action"] for d in result["all_decisions"]]
        assert "hold" not in actions

    def test_holds_still_counted_in_total(self, tmp_data_dir, journal_entries):
        """Hold decisions should be counted in total_decisions."""
        from aurel2.dashboard.app import load_trade_history
        write_journal(tmp_data_dir, journal_entries)
        result = load_trade_history(page=1, per_page=50)
        assert result["total_decisions"] == 5  # includes the hold entry


class TestLoadTradeHistoryPagination:
    """Tests for pagination in load_trade_history."""

    def test_page_1_returns_first_entries(self, tmp_data_dir, journal_entries):
        """Page 1 with per_page=2 should return 2 entries."""
        from aurel2.dashboard.app import load_trade_history
        write_journal(tmp_data_dir, journal_entries)
        result = load_trade_history(page=1, per_page=2)
        assert len(result["all_decisions"]) == 2

    def test_page_2_returns_remaining(self, tmp_data_dir, journal_entries):
        """Page 2 should return the remaining entries."""
        from aurel2.dashboard.app import load_trade_history
        write_journal(tmp_data_dir, journal_entries)
        result = load_trade_history(page=2, per_page=2)
        # 4 non-hold entries, page 2 of 2-per-page = 2 remaining
        assert len(result["all_decisions"]) == 2

    def test_total_pages_calculated_correctly(self, tmp_data_dir, journal_entries):
        """total_pages should be ceil(non_hold_count / per_page)."""
        from aurel2.dashboard.app import load_trade_history
        write_journal(tmp_data_dir, journal_entries)
        result = load_trade_history(page=1, per_page=3)
        # 4 non-hold entries, 3 per page = 2 pages
        assert result["total_pages"] == 2

    def test_empty_page_beyond_range(self, tmp_data_dir, journal_entries):
        """Page beyond range should return empty list."""
        from aurel2.dashboard.app import load_trade_history
        write_journal(tmp_data_dir, journal_entries)
        result = load_trade_history(page=99, per_page=10)
        assert result["all_decisions"] == []

    def test_default_pagination(self, tmp_data_dir, journal_entries):
        """Default should be page 1, 10 per page."""
        from aurel2.dashboard.app import load_trade_history
        write_journal(tmp_data_dir, journal_entries)
        result = load_trade_history()
        assert result["page"] == 1
        assert result["per_page"] == 10

    def test_sorted_newest_first(self, tmp_data_dir, journal_entries):
        """Decisions should be sorted newest first."""
        from aurel2.dashboard.app import load_trade_history
        write_journal(tmp_data_dir, journal_entries)
        result = load_trade_history(page=1, per_page=50)
        timestamps = [d.get("timestamp", "") for d in result["all_decisions"]]
        assert timestamps == sorted(timestamps, reverse=True)


class TestLoadTradeHistoryAIOverride:
    """Tests for AI override detection."""

    def test_ai_override_computes_deterministic_action(self, tmp_data_dir, journal_entries):
        """When AI disagrees, deterministic_action should be derived from strategy majority."""
        from aurel2.dashboard.app import load_trade_history
        write_journal(tmp_data_dir, journal_entries)
        result = load_trade_history(page=1, per_page=50)
        # Find the EFA override entry
        efa = next(d for d in result["all_decisions"] if d["id"] == "failed-001")
        assert efa["deterministic_action"] == "hold"  # 2 out of 3 strategies said hold
        assert efa["deterministic_asset"] == "GLD"

    def test_ai_agrees_no_deterministic_fields(self, tmp_data_dir, journal_entries):
        """When AI agrees, deterministic_action should NOT be set."""
        from aurel2.dashboard.app import load_trade_history
        write_journal(tmp_data_dir, journal_entries)
        result = load_trade_history(page=1, per_page=50)
        agreed = next(d for d in result["all_decisions"] if d["id"] == "exec-001")
        assert "deterministic_action" not in agreed


class TestLoadTradeHistoryAccountValues:
    """Tests for account value tracking."""

    def test_total_decisions_counted(self, tmp_data_dir, journal_entries):
        """total_decisions should count all journal entries."""
        from aurel2.dashboard.app import load_trade_history
        write_journal(tmp_data_dir, journal_entries)
        result = load_trade_history()
        assert result["total_decisions"] == len(journal_entries)

    def test_executed_trades_count(self, tmp_data_dir, journal_entries):
        """executed_trades should count entries with executed=True."""
        from aurel2.dashboard.app import load_trade_history
        write_journal(tmp_data_dir, journal_entries)
        result = load_trade_history()
        assert result["executed_trades"] == 2  # exec-001 and finalized-001

    def test_empty_journal(self, tmp_data_dir):
        """Empty journal should return defaults."""
        from aurel2.dashboard.app import load_trade_history
        write_journal(tmp_data_dir, [])
        result = load_trade_history()
        assert result["total_decisions"] == 0
        assert result["all_decisions"] == []

    def test_missing_journal_file(self, tmp_data_dir, monkeypatch):
        """Missing journal should return defaults without error."""
        from aurel2.dashboard.app import load_trade_history
        # Change cwd so the fallback data/trade_journal.json isn't found
        monkeypatch.chdir(tmp_data_dir)
        result = load_trade_history()
        assert result["total_decisions"] == 0


class TestLoadTradeHistoryFormatting:
    """Tests for timestamp formatting."""

    def test_formatted_time_added(self, tmp_data_dir, journal_entries):
        """Entries should have formatted_time field."""
        from aurel2.dashboard.app import load_trade_history
        write_journal(tmp_data_dir, journal_entries)
        result = load_trade_history(page=1, per_page=50)
        for d in result["all_decisions"]:
            assert "formatted_time" in d

    def test_formatted_time_format(self, tmp_data_dir):
        """Formatted time should be 'Mon DD, HH:MM'."""
        from aurel2.dashboard.app import load_trade_history
        entry = {
            "id": "fmt-001",
            "timestamp": "2026-03-15T09:30:00",
            "action": "buy",
            "symbol": "SPY",
            "ai_agrees": True,
            "executed": False,
        }
        write_journal(tmp_data_dir, [entry])
        result = load_trade_history()
        assert result["all_decisions"][0]["formatted_time"] == "Mar 15, 09:30"


# --- Unit Tests: load_pending_decisions ---

class TestLoadPendingDecisions:
    """Tests for load_pending_decisions."""

    def test_returns_only_pending_status(self, tmp_data_dir, pending_decisions_data):
        """Should only return decisions with status=pending."""
        from aurel2.dashboard.app import load_pending_decisions
        write_pending(tmp_data_dir, pending_decisions_data)
        result = load_pending_decisions()
        assert len(result) == 1
        assert result[0]["id"] == "pend-abc"

    def test_calculates_time_remaining(self, tmp_data_dir):
        """Should calculate time remaining in minutes."""
        from aurel2.dashboard.app import load_pending_decisions
        data = {
            "decisions": {
                "p1": {
                    "id": "p1",
                    "status": "pending",
                    "action": "buy",
                    "symbol": "GLD",
                    "created_at": (datetime.now() - timedelta(minutes=10)).isoformat(),
                    "confidence": 0.9,
                    "approval_url": "https://example.com/api/decision/p1",
                }
            }
        }
        write_pending(tmp_data_dir, data)
        result = load_pending_decisions()
        assert result[0]["time_remaining_mins"] == pytest.approx(49, abs=2)

    def test_timed_out_decision_gets_zero_remaining(self, tmp_data_dir):
        """Expired decisions should have time_remaining_mins=0."""
        from aurel2.dashboard.app import load_pending_decisions
        data = {
            "decisions": {
                "p1": {
                    "id": "p1",
                    "status": "pending",
                    "action": "buy",
                    "symbol": "GLD",
                    "created_at": (datetime.now() - timedelta(hours=2)).isoformat(),
                    "confidence": 0.9,
                    "approval_url": "https://example.com/api/decision/p1",
                }
            }
        }
        write_pending(tmp_data_dir, data)
        result = load_pending_decisions()
        assert result[0]["time_remaining_mins"] == 0

    def test_no_pending_file_returns_empty(self, tmp_data_dir):
        """Missing file should return empty list."""
        from aurel2.dashboard.app import load_pending_decisions
        result = load_pending_decisions()
        assert result == []


# --- Unit Tests: get_heartbeat ---

class TestGetHeartbeat:
    """Tests for get_heartbeat."""

    def test_healthy_heartbeat(self, tmp_data_dir):
        """Recent heartbeat should be 'healthy'."""
        from aurel2.dashboard.app import get_heartbeat
        hb = {"timestamp": datetime.now().timestamp()}
        (tmp_data_dir / "heartbeat.json").write_text(json.dumps(hb))
        result = get_heartbeat()
        assert result["status"] == "healthy"
        assert result["seconds_ago"] < 10

    def test_stale_heartbeat(self, tmp_data_dir):
        """Old heartbeat should be 'stale'."""
        from aurel2.dashboard.app import get_heartbeat
        hb = {"timestamp": (datetime.now() - timedelta(minutes=10)).timestamp()}
        (tmp_data_dir / "heartbeat.json").write_text(json.dumps(hb))
        result = get_heartbeat()
        assert result["status"] == "stale"

    def test_missing_heartbeat(self, tmp_data_dir):
        """Missing heartbeat file should return None."""
        from aurel2.dashboard.app import get_heartbeat
        result = get_heartbeat()
        assert result is None


# --- Integration Tests: Dashboard Routes ---

class TestDashboardRoutes:
    """Integration tests for dashboard HTTP routes."""

    @pytest.fixture
    def client(self, tmp_data_dir, journal_entries, pending_decisions_data):
        """Create a test client with mock broker data."""
        from fastapi.testclient import TestClient
        from aurel2.dashboard.app import app

        write_journal(tmp_data_dir, journal_entries)
        write_pending(tmp_data_dir, pending_decisions_data)

        mock_broker_data = {
            "connected": True,
            "positions": [],
            "account": {
                "total_value": 1005374.59,
                "cash_balance": 1004826.0,
                "buying_power": 2000000.0,
                "unrealized_pnl": 0.0,
                "realized_pnl": 0.0,
                "gross_position_value": 0.0,
            },
        }

        with patch("aurel2.dashboard.app.get_broker_data", return_value=mock_broker_data):
            yield TestClient(app)

    def test_dashboard_returns_200(self, client):
        """Dashboard should return HTTP 200."""
        response = client.get("/")
        assert response.status_code == 200

    def test_dashboard_contains_total_value(self, client):
        """Dashboard should show account total value."""
        response = client.get("/")
        assert "$1,005,375" in response.text or "1,005,374" in response.text

    def test_dashboard_contains_activity_table(self, client):
        """Dashboard should have activity table."""
        response = client.get("/")
        assert "Activity" in response.text

    def test_dashboard_shows_status_badges(self, client):
        """Dashboard should show status badges."""
        response = client.get("/")
        assert "status-executed" in response.text or "status-failed" in response.text

    def test_dashboard_no_hold_rows_in_activity(self, client):
        """Activity table should not contain hold data rows."""
        response = client.get("/")
        # The hold entry from journal_entries should be filtered out
        # Check that the hold entry's timestamp doesn't appear in activity
        assert "Jan 24, 16:03" not in response.text

    def test_dashboard_shows_ai_override(self, client):
        """Dashboard should show AI override details."""
        response = client.get("/")
        assert "Override" in response.text
        assert "HOLD" in response.text  # the overridden action

    def test_dashboard_period_param(self, client):
        """Dashboard should accept period parameter."""
        response = client.get("/?period=1y")
        assert response.status_code == 200

    def test_dashboard_invalid_period_defaults(self, client):
        """Invalid period should default to 1m."""
        response = client.get("/?period=invalid")
        assert response.status_code == 200

    def test_dashboard_page_param(self, client):
        """Dashboard should accept page parameter."""
        response = client.get("/?page=1")
        assert response.status_code == 200

    def test_dashboard_negative_page_defaults(self, client):
        """Negative page should default to 1."""
        response = client.get("/?page=-1")
        assert response.status_code == 200

    def test_dashboard_shows_pending_decisions(self, client):
        """Dashboard should show pending decisions section."""
        response = client.get("/")
        assert "Pending Decisions" in response.text
        assert "btn-approve" in response.text

    def test_dashboard_shows_failed_entry(self, client):
        """Dashboard should show failed entries with error."""
        response = client.get("/")
        assert "Failed" in response.text

    def test_api_status_returns_json(self, client):
        """Status endpoint should return JSON."""
        response = client.get("/api/status")
        assert response.status_code == 200
        data = response.json()
        assert "heartbeat" in data
        assert "snapshots_count" in data
