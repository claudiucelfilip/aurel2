"""Circuit breaker for broker connection failures."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

import structlog

logger = structlog.get_logger()


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, rejecting requests
    HALF_OPEN = "half_open"  # Testing if recovered


@dataclass
class CircuitBreaker:
    """Circuit breaker to prevent cascading failures.

    States:
    - CLOSED: Normal operation, requests pass through
    - OPEN: Too many failures, requests rejected immediately
    - HALF_OPEN: Testing recovery, limited requests allowed
    """

    failure_threshold: int = 3
    reset_timeout_seconds: int = 300  # 5 minutes

    state: CircuitState = field(default=CircuitState.CLOSED)
    failure_count: int = field(default=0)
    last_failure_time: Optional[datetime] = field(default=None)
    last_success_time: Optional[datetime] = field(default=None)

    def record_success(self) -> None:
        """Record a successful operation."""
        self.failure_count = 0
        self.last_success_time = datetime.now()

        if self.state == CircuitState.HALF_OPEN:
            logger.info("circuit_breaker_closed", previous_state="half_open")
            self.state = CircuitState.CLOSED

    def record_failure(self, error: str = "") -> None:
        """Record a failed operation."""
        self.failure_count += 1
        self.last_failure_time = datetime.now()

        logger.warning(
            "circuit_breaker_failure",
            failure_count=self.failure_count,
            threshold=self.failure_threshold,
            error=error,
        )

        if self.state == CircuitState.HALF_OPEN:
            logger.warning("circuit_breaker_reopened")
            self.state = CircuitState.OPEN
        elif self.failure_count >= self.failure_threshold:
            logger.error(
                "circuit_breaker_opened",
                failure_count=self.failure_count,
                reset_timeout=self.reset_timeout_seconds,
            )
            self.state = CircuitState.OPEN

    def can_execute(self) -> bool:
        """Check if operation should be allowed."""
        if self.state == CircuitState.CLOSED:
            return True

        if self.state == CircuitState.OPEN:
            if self.last_failure_time:
                elapsed = datetime.now() - self.last_failure_time
                if elapsed > timedelta(seconds=self.reset_timeout_seconds):
                    logger.info("circuit_breaker_half_open")
                    self.state = CircuitState.HALF_OPEN
                    return True
            return False

        return True  # HALF_OPEN allows one request

    def get_status(self) -> dict:
        """Get current circuit breaker status."""
        return {
            "state": self.state.value,
            "failure_count": self.failure_count,
            "last_failure": self.last_failure_time.isoformat() if self.last_failure_time else None,
            "last_success": self.last_success_time.isoformat() if self.last_success_time else None,
        }
