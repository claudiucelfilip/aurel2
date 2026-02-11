"""Alpaca connection management."""

import asyncio
import os
import sys
from typing import Optional

import structlog

from aurel2.broker.alpaca import AlpacaBroker
from aurel2.live.circuit_breaker import CircuitBreaker
from aurel2.broker.base import AccountSummary, BrokerPosition

logger = structlog.get_logger()


class AlpacaConnection:
    """Manages Alpaca broker connection lifecycle.

    Much simpler than a gateway-based broker — no gateway process,
    no heartbeat loop, no Docker detection. Just REST API credentials.
    """

    def __init__(
        self,
        paper: bool = True,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
    ):
        self.paper = paper
        self.api_key = api_key or os.environ.get("APCA_API_KEY_ID", "")
        self.api_secret = api_secret or os.environ.get("APCA_API_SECRET_KEY", "")
        self.broker: Optional[AlpacaBroker] = None
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=3,
            reset_timeout_seconds=300,
        )

    @property
    def is_connected(self) -> bool:
        return self.broker is not None and self.broker.is_connected

    async def connect(self, max_retries: int = 3) -> bool:
        """Connect to Alpaca. Returns True if successful."""
        if not self.api_key or not self.api_secret:
            logger.error("alpaca_missing_credentials")
            return False

        if not self.circuit_breaker.can_execute():
            status = self.circuit_breaker.get_status()
            logger.warning(
                "alpaca_connect_blocked_circuit_open",
                circuit_state=status["state"],
                failure_count=status["failure_count"],
            )
            return False

        for attempt in range(max_retries):
            logger.info(
                "alpaca_connect_attempt",
                attempt=attempt + 1,
                max_retries=max_retries,
                paper=self.paper,
            )
            try:
                self.broker = AlpacaBroker(
                    api_key=self.api_key,
                    api_secret=self.api_secret,
                    paper=self.paper,
                )
                connected = await self.broker.connect()
                if connected:
                    logger.info("alpaca_connected", paper=self.paper)
                    self.circuit_breaker.record_success()
                    return True
            except Exception as e:
                logger.warning("alpaca_connect_failed", error=str(e), attempt=attempt + 1)

            if attempt < max_retries - 1:
                await asyncio.sleep(10)

        self.circuit_breaker.record_failure("Connection failed after all retries")
        return False

    async def disconnect(self) -> None:
        """Disconnect from Alpaca."""
        if self.broker:
            try:
                await self.broker.disconnect()
            except Exception as e:
                logger.warning("alpaca_disconnect_error", error=str(e))
            self.broker = None
        logger.info("alpaca_disconnected")

    async def ensure_connected(self) -> bool:
        """Ensure connection is active, reconnect if needed."""
        if self.is_connected:
            # Validate credentials are still valid
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self.broker._client.get_account)
                return True
            except Exception:
                logger.warning("alpaca_connection_stale_reconnecting")
                self.broker = None

        logger.info("alpaca_reconnecting")
        return await self.connect(max_retries=3)

    async def get_account_summary(self) -> Optional[AccountSummary]:
        """Get account summary."""
        if not self.is_connected:
            return None
        try:
            return await self.broker.get_account_summary()
        except Exception as e:
            logger.error("alpaca_account_summary_error", error=str(e))
            return None

    async def get_positions(self) -> list[BrokerPosition]:
        """Get all positions."""
        if not self.is_connected:
            return []
        try:
            return await self.broker.get_positions()
        except Exception as e:
            logger.error("alpaca_get_positions_error", error=str(e))
            return []

    async def get_position(self, symbol: str) -> Optional[BrokerPosition]:
        """Get a specific position."""
        if not self.is_connected:
            return None
        try:
            return await self.broker.get_position(symbol)
        except Exception as e:
            logger.error("alpaca_get_position_error", symbol=symbol, error=str(e))
            return None
