"""Ntfy notification service for Aurel2."""

import time
from typing import Optional

import httpx
import structlog

logger = structlog.get_logger(__name__)

# Rate limiting: track last notification time per category
_last_notification_time: dict[str, float] = {}
_rate_limit_seconds: int = 3600  # 1 hour default


class NtfyNotifier:
    """Send push notifications via ntfy.sh."""

    def __init__(
        self,
        topic: str,
        server: str = "https://ntfy.sh",
        rate_limit_seconds: int = 3600,
    ) -> None:
        """Initialize the notifier.

        Args:
            topic: The ntfy topic to publish to.
            server: The ntfy server URL. Defaults to https://ntfy.sh.
            rate_limit_seconds: Minimum seconds between notifications of the same category.
                Defaults to 3600 (1 hour). Set to 0 to disable rate limiting.
        """
        self.topic = topic
        self.server = server.rstrip("/")
        self.rate_limit_seconds = rate_limit_seconds

    def _is_rate_limited(self, category: str) -> bool:
        """Check if a notification category is rate limited.

        Args:
            category: The notification category (e.g., 'monitor_issue', 'trade_approval').

        Returns:
            True if the notification should be skipped due to rate limiting.
        """
        if self.rate_limit_seconds <= 0:
            return False

        now = time.time()
        last_time = _last_notification_time.get(category, 0)

        if now - last_time < self.rate_limit_seconds:
            logger.debug(
                "notification_rate_limited",
                category=category,
                seconds_until_allowed=int(self.rate_limit_seconds - (now - last_time)),
            )
            return True

        return False

    def _record_notification(self, category: str) -> None:
        """Record that a notification was sent for rate limiting purposes."""
        _last_notification_time[category] = time.time()

    def send(
        self,
        message: str,
        title: Optional[str] = None,
        priority: Optional[str] = None,
        tags: Optional[list[str]] = None,
        click_url: Optional[str] = None,
        actions: Optional[list[str]] = None,
        category: Optional[str] = None,
    ) -> bool:
        """Send a notification to ntfy.

        Args:
            message: The notification body text.
            title: Optional title for the notification.
            priority: Priority level (min, low, default, high, urgent).
            tags: List of emoji tags (e.g., ['warning', 'chart_with_upwards_trend']).
            click_url: URL to open when notification is clicked.
            actions: List of action buttons in ntfy format.
            category: Optional category for rate limiting (e.g., 'monitor_issue').
                Notifications in the same category are rate limited.

        Returns:
            True if notification was sent successfully, False otherwise.
        """
        if category and self._is_rate_limited(category):
            return False
        url = f"{self.server}/{self.topic}"
        headers: dict[str, str] = {}

        if title:
            headers["Title"] = title
        if priority:
            headers["Priority"] = priority
        if tags:
            headers["Tags"] = ",".join(tags)
        if click_url:
            headers["Click"] = click_url
        if actions:
            headers["Actions"] = ";".join(actions)

        try:
            response = httpx.post(url, content=message, headers=headers)
            if response.status_code == 200:
                if category:
                    self._record_notification(category)
                logger.info(
                    "notification_sent",
                    topic=self.topic,
                    title=title,
                    priority=priority,
                    category=category,
                )
                return True
            else:
                logger.warning(
                    "notification_failed",
                    topic=self.topic,
                    status_code=response.status_code,
                    response=response.text,
                )
                return False
        except Exception as e:
            logger.error(
                "notification_error",
                topic=self.topic,
                error=str(e),
            )
            return False

    def _format_approval_message(
        self,
        action: str,
        symbol: str,
        reasoning: str,
        approval_url: str,
    ) -> str:
        """Format a message for trade approval requests.

        Args:
            action: The trade action (BUY/SELL).
            symbol: The asset symbol.
            reasoning: Explanation for the trade recommendation.
            approval_url: URL to approve or reject the trade.

        Returns:
            Formatted message string.
        """
        return (
            f"Trade Approval Required\n\n"
            f"Action: {action} {symbol}\n"
            f"Reasoning: {reasoning}\n\n"
            f"Approve/Reject: {approval_url}"
        )

    def _format_execution_message(
        self,
        action: str,
        symbol: str,
        price: float,
        reasoning: str,
    ) -> str:
        """Format a message for trade execution notices.

        Args:
            action: The trade action (BUY/SELL).
            symbol: The asset symbol.
            price: The execution price.
            reasoning: Explanation for the trade.

        Returns:
            Formatted message string.
        """
        return (
            f"Trade Executed\n\n"
            f"Action: {action} {symbol}\n"
            f"Price: {price:.2f}\n"
            f"Reasoning: {reasoning}"
        )

    def _format_autonomous_message(
        self,
        action: str,
        symbol: str,
        price: float,
        reasoning: str,
        wait_time: int,
    ) -> str:
        """Format a message for autonomous actions with cancellation window.

        Args:
            action: The trade action (BUY/SELL).
            symbol: The asset symbol.
            price: The target price.
            reasoning: Explanation for the trade.
            wait_time: Seconds to wait before executing (cancellation window).

        Returns:
            Formatted message string.
        """
        minutes = wait_time // 60
        seconds = wait_time % 60
        time_str = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"

        return (
            f"Autonomous Trade Pending\n\n"
            f"Action: {action} {symbol}\n"
            f"Price: {price:.2f}\n"
            f"Reasoning: {reasoning}\n\n"
            f"Executing in {time_str} ({wait_time} seconds)\n"
            f"Reply 'cancel' to stop this trade"
        )

    def send_approval_request(
        self,
        action: str,
        symbol: str,
        reasoning: str,
        approval_url: str,
        confidence: float,
    ) -> bool:
        """Send a trade approval request notification.

        Args:
            action: The trade action (BUY/SELL).
            symbol: The asset symbol.
            reasoning: Explanation for the trade recommendation.
            approval_url: URL to approve or reject the trade.
            confidence: Confidence score (0-1) for the recommendation.

        Returns:
            True if notification was sent successfully, False otherwise.
        """
        message = self._format_approval_message(action, symbol, reasoning, approval_url)
        confidence_pct = int(confidence * 100)

        return self.send(
            message=message,
            title=f"Aurel2: {action} {symbol} ({confidence_pct}% confidence)",
            priority="high",
            tags=["warning", "chart_with_upwards_trend"],
            click_url=approval_url,
            actions=[
                f"view, Approve, {approval_url}?action=approve",
                f"view, Reject, {approval_url}?action=reject",
            ],
        )

    def send_execution_notice(
        self,
        action: str,
        symbol: str,
        price: float,
        reasoning: str,
    ) -> bool:
        """Send a trade execution notification.

        Args:
            action: The trade action (BUY/SELL).
            symbol: The asset symbol.
            price: The execution price.
            reasoning: Explanation for the trade.

        Returns:
            True if notification was sent successfully, False otherwise.
        """
        message = self._format_execution_message(action, symbol, price, reasoning)
        tag = "chart_with_upwards_trend" if action == "BUY" else "chart_with_downwards_trend"

        return self.send(
            message=message,
            title=f"Aurel2: {action} {symbol} @ {price:.2f}",
            tags=[tag, "money_bag"],
        )

    def send_autonomous_action(
        self,
        action: str,
        symbol: str,
        price: float,
        reasoning: str,
        wait_time: int,
    ) -> bool:
        """Send a notification for an autonomous action with cancellation window.

        Args:
            action: The trade action (BUY/SELL).
            symbol: The asset symbol.
            price: The target price.
            reasoning: Explanation for the trade.
            wait_time: Seconds to wait before executing (cancellation window).

        Returns:
            True if notification was sent successfully, False otherwise.
        """
        message = self._format_autonomous_message(action, symbol, price, reasoning, wait_time)

        return self.send(
            message=message,
            title=f"Aurel2: {action} {symbol} in {wait_time}s",
            priority="high",
            tags=["hourglass", "robot"],
        )
