"""Error classification and analysis for daemon monitoring."""

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import structlog

logger = structlog.get_logger()


class ErrorSeverity(Enum):
    """Error severity levels."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class ErrorCategory(Enum):
    """Categories of errors for classification."""

    CONNECTION = "connection"  # IBKR connection issues
    CIRCUIT_BREAKER = "circuit_breaker"  # Circuit breaker tripped
    PRICE_FETCH = "price_fetch"  # Failed to get market data
    DAEMON_CRASH = "daemon_crash"  # Process not running
    EXECUTION = "execution"  # Trade execution errors
    NOTIFICATION = "notification"  # Notification delivery failures
    TIMEOUT = "timeout"  # Operation timeout
    UNKNOWN = "unknown"


@dataclass
class AnalyzedError:
    """Result of error analysis."""

    category: ErrorCategory
    severity: ErrorSeverity
    message: str
    is_auto_fixable: bool
    fix_action: Optional[str] = None
    details: Optional[str] = None


# Patterns for error classification
ERROR_PATTERNS = {
    ErrorCategory.CONNECTION: [
        r"connection.*failed",
        r"ibkr.*disconnect",
        r"connect_blocked",
        r"cannot connect",
        r"connection refused",
        r"socket.*error",
        r"gateway.*not.*running",
    ],
    ErrorCategory.CIRCUIT_BREAKER: [
        r"circuit_breaker_opened",
        r"circuit.*open",
        r"too many failures",
    ],
    ErrorCategory.PRICE_FETCH: [
        r"price.*fetch.*failed",
        r"market.*data.*error",
        r"no.*price.*data",
        r"failed.*get.*price",
    ],
    ErrorCategory.DAEMON_CRASH: [
        r"daemon.*not.*running",
        r"process.*died",
        r"heartbeat.*stale",
        r"unresponsive",
    ],
    ErrorCategory.EXECUTION: [
        r"execution.*failed",
        r"order.*rejected",
        r"trade.*error",
        r"insufficient.*funds",
        r"position.*error",
    ],
    ErrorCategory.NOTIFICATION: [
        r"notification.*error",
        r"notification.*failed",
        r"ascii.*codec.*can't encode",
    ],
    ErrorCategory.TIMEOUT: [
        r"timeout",
        r"timed.*out",
        r"deadline.*exceeded",
    ],
}

# Which error categories are auto-fixable
AUTO_FIXABLE_CATEGORIES = {
    ErrorCategory.CONNECTION: True,
    ErrorCategory.CIRCUIT_BREAKER: True,
    ErrorCategory.PRICE_FETCH: True,
    ErrorCategory.DAEMON_CRASH: True,
    ErrorCategory.EXECUTION: False,  # Money involved - never auto-fix
    ErrorCategory.NOTIFICATION: False,  # Code bug - needs human attention
    ErrorCategory.TIMEOUT: True,
    ErrorCategory.UNKNOWN: False,
}

# Fix actions for each category
FIX_ACTIONS = {
    ErrorCategory.CONNECTION: "restart_daemon",
    ErrorCategory.CIRCUIT_BREAKER: "wait_and_restart",
    ErrorCategory.PRICE_FETCH: "restart_daemon",
    ErrorCategory.DAEMON_CRASH: "restart_daemon",
    ErrorCategory.TIMEOUT: "restart_daemon",
}


class ErrorAnalyzer:
    """Analyzes and classifies daemon errors."""

    def __init__(self):
        # Track error occurrences for rate limiting
        self._error_counts: dict[ErrorCategory, int] = {cat: 0 for cat in ErrorCategory}
        self._last_errors: dict[ErrorCategory, str] = {}

    def analyze(self, error_message: str, context: Optional[dict] = None) -> AnalyzedError:
        """Analyze an error message and classify it."""
        error_lower = error_message.lower()
        context = context or {}

        # Try to match against known patterns
        category = self._classify_category(error_lower)
        severity = self._determine_severity(category, error_message, context)

        # Track this error
        self._error_counts[category] += 1
        self._last_errors[category] = error_message

        # Determine if auto-fixable
        is_auto_fixable = AUTO_FIXABLE_CATEGORIES.get(category, False)

        # Check if we've hit max fix attempts (3 per category)
        if self._error_counts[category] > 3:
            is_auto_fixable = False

        fix_action = FIX_ACTIONS.get(category) if is_auto_fixable else None

        return AnalyzedError(
            category=category,
            severity=severity,
            message=error_message,
            is_auto_fixable=is_auto_fixable,
            fix_action=fix_action,
            details=self._get_details(category, context),
        )

    def analyze_health_report(self, issues: list[str], context: Optional[dict] = None) -> list[AnalyzedError]:
        """Analyze issues from a health report."""
        errors = []
        for issue in issues:
            errors.append(self.analyze(issue, context))
        return errors

    def reset_counts(self) -> None:
        """Reset error counts (call after successful fix)."""
        self._error_counts = {cat: 0 for cat in ErrorCategory}

    def reset_category(self, category: ErrorCategory) -> None:
        """Reset count for a specific category after successful fix."""
        self._error_counts[category] = 0

    def get_error_counts(self) -> dict[str, int]:
        """Get current error counts by category."""
        return {cat.value: count for cat, count in self._error_counts.items()}

    def _classify_category(self, error_lower: str) -> ErrorCategory:
        """Classify error into a category based on patterns."""
        for category, patterns in ERROR_PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, error_lower):
                    return category
        return ErrorCategory.UNKNOWN

    def _determine_severity(
        self, category: ErrorCategory, message: str, context: dict
    ) -> ErrorSeverity:
        """Determine error severity based on category and context."""
        # Execution errors are always critical
        if category == ErrorCategory.EXECUTION:
            return ErrorSeverity.CRITICAL

        # Daemon crash is critical
        if category == ErrorCategory.DAEMON_CRASH:
            return ErrorSeverity.CRITICAL

        # Circuit breaker open is an error
        if category == ErrorCategory.CIRCUIT_BREAKER:
            return ErrorSeverity.ERROR

        # Connection issues are warnings initially, errors if repeated
        if category == ErrorCategory.CONNECTION:
            if self._error_counts[category] >= 2:
                return ErrorSeverity.ERROR
            return ErrorSeverity.WARNING

        # Price fetch issues are usually warnings
        if category == ErrorCategory.PRICE_FETCH:
            return ErrorSeverity.WARNING

        # Notification failures are warnings (system still works, but alerts are broken)
        if category == ErrorCategory.NOTIFICATION:
            return ErrorSeverity.WARNING

        # Unknown errors default to warning
        return ErrorSeverity.WARNING

    def _get_details(self, category: ErrorCategory, context: dict) -> Optional[str]:
        """Get additional details for the error."""
        details_parts = []

        if category == ErrorCategory.CONNECTION:
            details_parts.append("IBKR connection issue - may need to restart daemon or check IB Gateway")

        if category == ErrorCategory.CIRCUIT_BREAKER:
            details_parts.append("Circuit breaker tripped due to repeated failures")
            if "circuit_breaker" in context:
                cb = context["circuit_breaker"]
                details_parts.append(f"State: {cb.get('state', 'unknown')}")
                details_parts.append(f"Failures: {cb.get('failure_count', 0)}")

        if category == ErrorCategory.EXECUTION:
            details_parts.append("Trade execution error - MANUAL REVIEW REQUIRED")
            details_parts.append("This error involves real money and cannot be auto-fixed")

        if category == ErrorCategory.DAEMON_CRASH:
            details_parts.append("Daemon process has stopped or become unresponsive")
            if "process" in context:
                proc = context["process"]
                if proc.get("pid"):
                    details_parts.append(f"Last known PID: {proc['pid']}")

        if category == ErrorCategory.NOTIFICATION:
            details_parts.append("Notification delivery failed - trade alerts may not be reaching you")
            details_parts.append("Check ntfy configuration and daemon logs")

        return "; ".join(details_parts) if details_parts else None
