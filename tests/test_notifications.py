"""Tests for Ntfy notification service."""

from unittest.mock import MagicMock, patch

import pytest

from aurel2.notifications.ntfy import NtfyNotifier


class TestNtfyNotifierInit:
    """Tests for NtfyNotifier initialization."""

    def test_init_with_topic(self):
        """NtfyNotifier should accept a topic."""
        notifier = NtfyNotifier(topic="test-topic")
        assert notifier.topic == "test-topic"

    def test_init_with_default_server(self):
        """NtfyNotifier should use default ntfy.sh server."""
        notifier = NtfyNotifier(topic="test-topic")
        assert notifier.server == "https://ntfy.sh"

    def test_init_with_custom_server(self):
        """NtfyNotifier should accept custom server."""
        notifier = NtfyNotifier(topic="test-topic", server="https://ntfy.example.com")
        assert notifier.server == "https://ntfy.example.com"


class TestFormatApprovalMessage:
    """Tests for approval message formatting."""

    def test_format_approval_message_includes_action(self):
        """Approval message should include the action."""
        notifier = NtfyNotifier(topic="test-topic")
        message = notifier._format_approval_message(
            action="BUY",
            symbol="SPY",
            reasoning="All strategies agree",
            approval_url="https://example.com/approve/123",
        )
        assert "BUY" in message

    def test_format_approval_message_includes_symbol(self):
        """Approval message should include the symbol."""
        notifier = NtfyNotifier(topic="test-topic")
        message = notifier._format_approval_message(
            action="BUY",
            symbol="SPY",
            reasoning="All strategies agree",
            approval_url="https://example.com/approve/123",
        )
        assert "SPY" in message

    def test_format_approval_message_includes_approval_url(self):
        """Approval message should include the approval URL."""
        notifier = NtfyNotifier(topic="test-topic")
        message = notifier._format_approval_message(
            action="BUY",
            symbol="SPY",
            reasoning="All strategies agree",
            approval_url="https://example.com/approve/123",
        )
        assert "https://example.com/approve/123" in message

    def test_format_approval_message_includes_reasoning(self):
        """Approval message should include the reasoning."""
        notifier = NtfyNotifier(topic="test-topic")
        message = notifier._format_approval_message(
            action="SELL",
            symbol="EFA",
            reasoning="Momentum shifted to bonds",
            approval_url="https://example.com/approve/456",
        )
        assert "Momentum shifted to bonds" in message


class TestFormatExecutionMessage:
    """Tests for execution message formatting."""

    def test_format_execution_message_includes_action(self):
        """Execution message should include the action."""
        notifier = NtfyNotifier(topic="test-topic")
        message = notifier._format_execution_message(
            action="BUY",
            symbol="SPY",
            price=542.30,
            reasoning="Routine rebalance",
        )
        assert "BUY" in message

    def test_format_execution_message_includes_symbol(self):
        """Execution message should include the symbol."""
        notifier = NtfyNotifier(topic="test-topic")
        message = notifier._format_execution_message(
            action="BUY",
            symbol="SPY",
            price=542.30,
            reasoning="Routine rebalance",
        )
        assert "SPY" in message

    def test_format_execution_message_includes_price(self):
        """Execution message should include the price."""
        notifier = NtfyNotifier(topic="test-topic")
        message = notifier._format_execution_message(
            action="BUY",
            symbol="SPY",
            price=542.30,
            reasoning="Routine rebalance",
        )
        assert "542.30" in message

    def test_format_execution_message_includes_reasoning(self):
        """Execution message should include the reasoning."""
        notifier = NtfyNotifier(topic="test-topic")
        message = notifier._format_execution_message(
            action="SELL",
            symbol="EFA",
            price=75.50,
            reasoning="Taking profits",
        )
        assert "Taking profits" in message


class TestFormatAutonomousMessage:
    """Tests for autonomous action message formatting."""

    def test_format_autonomous_message_includes_action(self):
        """Autonomous message should include the action."""
        notifier = NtfyNotifier(topic="test-topic")
        message = notifier._format_autonomous_message(
            action="BUY",
            symbol="AGG",
            price=100.25,
            reasoning="Defensive position",
            wait_time=300,
        )
        assert "BUY" in message

    def test_format_autonomous_message_includes_symbol(self):
        """Autonomous message should include the symbol."""
        notifier = NtfyNotifier(topic="test-topic")
        message = notifier._format_autonomous_message(
            action="BUY",
            symbol="AGG",
            price=100.25,
            reasoning="Defensive position",
            wait_time=300,
        )
        assert "AGG" in message

    def test_format_autonomous_message_includes_wait_time(self):
        """Autonomous message should include the wait time."""
        notifier = NtfyNotifier(topic="test-topic")
        message = notifier._format_autonomous_message(
            action="BUY",
            symbol="AGG",
            price=100.25,
            reasoning="Defensive position",
            wait_time=300,
        )
        # Should mention 5 minutes (300 seconds) or 300 seconds
        assert "300" in message or "5" in message


class TestSendNotification:
    """Tests for sending notifications."""

    @patch("httpx.post")
    def test_send_notification_returns_true_on_success(self, mock_post):
        """send should return True when notification succeeds."""
        mock_post.return_value = MagicMock(status_code=200)
        notifier = NtfyNotifier(topic="test-topic")
        result = notifier.send("Test message", priority="high")
        assert result is True

    @patch("httpx.post")
    def test_send_notification_calls_correct_url(self, mock_post):
        """send should call the correct ntfy URL."""
        mock_post.return_value = MagicMock(status_code=200)
        notifier = NtfyNotifier(topic="test-topic")
        notifier.send("Test message")
        mock_post.assert_called_once()
        assert "ntfy.sh/test-topic" in mock_post.call_args[0][0]

    @patch("httpx.post")
    def test_send_notification_includes_message_in_body(self, mock_post):
        """send should include message in request body."""
        mock_post.return_value = MagicMock(status_code=200)
        notifier = NtfyNotifier(topic="test-topic")
        notifier.send("Test message body")
        call_kwargs = mock_post.call_args
        # Message should be in content or data
        assert "Test message body" in str(call_kwargs)

    @patch("httpx.post")
    def test_send_notification_includes_title_header(self, mock_post):
        """send should include title in headers."""
        mock_post.return_value = MagicMock(status_code=200)
        notifier = NtfyNotifier(topic="test-topic")
        notifier.send("Test message", title="Test Title")
        call_kwargs = mock_post.call_args
        headers = call_kwargs.kwargs.get("headers", {})
        assert headers.get("Title") == "Test Title"

    @patch("httpx.post")
    def test_send_notification_includes_priority_header(self, mock_post):
        """send should include priority in headers."""
        mock_post.return_value = MagicMock(status_code=200)
        notifier = NtfyNotifier(topic="test-topic")
        notifier.send("Test message", priority="urgent")
        call_kwargs = mock_post.call_args
        headers = call_kwargs.kwargs.get("headers", {})
        assert headers.get("Priority") == "urgent"

    @patch("httpx.post")
    def test_send_notification_includes_tags_header(self, mock_post):
        """send should include tags in headers."""
        mock_post.return_value = MagicMock(status_code=200)
        notifier = NtfyNotifier(topic="test-topic")
        notifier.send("Test message", tags=["chart_with_upwards_trend", "money_bag"])
        call_kwargs = mock_post.call_args
        headers = call_kwargs.kwargs.get("headers", {})
        assert "chart_with_upwards_trend" in headers.get("Tags", "")

    @patch("httpx.post")
    def test_send_notification_includes_click_url_header(self, mock_post):
        """send should include click URL in headers."""
        mock_post.return_value = MagicMock(status_code=200)
        notifier = NtfyNotifier(topic="test-topic")
        notifier.send("Test message", click_url="https://example.com/dashboard")
        call_kwargs = mock_post.call_args
        headers = call_kwargs.kwargs.get("headers", {})
        assert headers.get("Click") == "https://example.com/dashboard"

    @patch("httpx.post")
    def test_send_notification_returns_false_on_failure(self, mock_post):
        """send should return False when notification fails."""
        mock_post.return_value = MagicMock(status_code=500)
        notifier = NtfyNotifier(topic="test-topic")
        result = notifier.send("Test message")
        assert result is False

    @patch("httpx.post")
    def test_send_notification_handles_exception(self, mock_post):
        """send should handle exceptions gracefully."""
        mock_post.side_effect = Exception("Network error")
        notifier = NtfyNotifier(topic="test-topic")
        result = notifier.send("Test message")
        assert result is False


class TestSendApprovalRequest:
    """Tests for send_approval_request method."""

    @patch("httpx.post")
    def test_send_approval_request_returns_bool(self, mock_post):
        """send_approval_request should return a boolean."""
        mock_post.return_value = MagicMock(status_code=200)
        notifier = NtfyNotifier(topic="test-topic")
        result = notifier.send_approval_request(
            action="BUY",
            symbol="SPY",
            reasoning="Strong momentum",
            approval_url="https://example.com/approve/123",
            confidence=0.85,
        )
        assert isinstance(result, bool)

    @patch("httpx.post")
    def test_send_approval_request_uses_high_priority(self, mock_post):
        """send_approval_request should use high priority."""
        mock_post.return_value = MagicMock(status_code=200)
        notifier = NtfyNotifier(topic="test-topic")
        notifier.send_approval_request(
            action="BUY",
            symbol="SPY",
            reasoning="Strong momentum",
            approval_url="https://example.com/approve/123",
            confidence=0.85,
        )
        call_kwargs = mock_post.call_args
        headers = call_kwargs.kwargs.get("headers", {})
        assert headers.get("Priority") == "high"


class TestSendExecutionNotice:
    """Tests for send_execution_notice method."""

    @patch("httpx.post")
    def test_send_execution_notice_returns_bool(self, mock_post):
        """send_execution_notice should return a boolean."""
        mock_post.return_value = MagicMock(status_code=200)
        notifier = NtfyNotifier(topic="test-topic")
        result = notifier.send_execution_notice(
            action="BUY",
            symbol="SPY",
            price=542.30,
            reasoning="Momentum signal",
        )
        assert isinstance(result, bool)

    @patch("httpx.post")
    def test_send_execution_notice_uses_default_priority(self, mock_post):
        """send_execution_notice should use default priority."""
        mock_post.return_value = MagicMock(status_code=200)
        notifier = NtfyNotifier(topic="test-topic")
        notifier.send_execution_notice(
            action="BUY",
            symbol="SPY",
            price=542.30,
            reasoning="Momentum signal",
        )
        call_kwargs = mock_post.call_args
        headers = call_kwargs.kwargs.get("headers", {})
        # Default priority or no priority header
        assert headers.get("Priority", "default") in ["default", None, ""]


class TestSendAutonomousAction:
    """Tests for send_autonomous_action method."""

    @patch("httpx.post")
    def test_send_autonomous_action_returns_bool(self, mock_post):
        """send_autonomous_action should return a boolean."""
        mock_post.return_value = MagicMock(status_code=200)
        notifier = NtfyNotifier(topic="test-topic")
        result = notifier.send_autonomous_action(
            action="BUY",
            symbol="AGG",
            price=100.50,
            reasoning="Defensive rebalance",
            wait_time=300,
        )
        assert isinstance(result, bool)

    @patch("httpx.post")
    def test_send_autonomous_action_mentions_cancellation_window(self, mock_post):
        """send_autonomous_action should mention cancellation window."""
        mock_post.return_value = MagicMock(status_code=200)
        notifier = NtfyNotifier(topic="test-topic")
        notifier.send_autonomous_action(
            action="BUY",
            symbol="AGG",
            price=100.50,
            reasoning="Defensive rebalance",
            wait_time=300,
        )
        call_kwargs = mock_post.call_args
        # The message should mention the wait time or cancellation
        content = str(call_kwargs)
        assert "300" in content or "cancel" in content.lower() or "5" in content
