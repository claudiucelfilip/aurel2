"""IBKR connection management and IB Gateway launcher."""

import asyncio
import subprocess
import sys
import platform
import time
from pathlib import Path
from typing import Optional

import structlog

from aurel2.broker.ibkr import IBKRBroker, ClientIdConflictError
from aurel2.live.circuit_breaker import CircuitBreaker
from aurel2.broker.base import AccountSummary, BrokerPosition

logger = structlog.get_logger()

# Default IB Gateway paths
GATEWAY_PATHS = {
    "Darwin": [
        "/Applications/IB Gateway 10.19/IB Gateway 10.19.app",
        "/Applications/IB Gateway/IB Gateway.app",
    ],
    "Linux": [
        "~/Jts/ibgateway.sh",
        "~/IBJts/ibgateway.sh",
    ],
    "Windows": [
        r"C:\Jts\ibgateway.exe",
    ],
}


class IBKRConnection:
    """
    Manages IBKR connection lifecycle.

    Handles:
    - Connecting to IB Gateway
    - Launching IB Gateway if not running
    - Reconnecting on disconnect
    - Heartbeat to keep connection alive
    """

    def __init__(
        self,
        paper: bool = True,
        host: str = "127.0.0.1",
        port: int | None = None,
        client_id: int | None = None,
    ):
        self.paper = paper
        # Default ports: IB Gateway paper=4002, live=4001
        # Can be overridden (e.g., Docker uses 4004/4003 inside container)
        self.port = port if port is not None else (4002 if paper else 4001)
        self.host = host
        # Auto-detect Docker: if host is "ib-gateway", we're in Docker
        self.docker_mode = (host == "ib-gateway")
        # Use random client ID if not specified to avoid conflicts
        if client_id is None:
            import random
            client_id = random.randint(100, 999)
        self.client_id = client_id
        self.broker: Optional[IBKRBroker] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=3,
            reset_timeout_seconds=300,
        )

    @property
    def is_connected(self) -> bool:
        return self.broker is not None and self.broker.is_connected

    def _on_connectivity_restored(self) -> None:
        """Called when IBKR upstream connectivity is restored (error 1102)."""
        logger.info("ibkr_connectivity_restored_resetting_circuit_breaker")
        self.circuit_breaker.record_success()

    async def connect(self, launch_gateway_if_needed: bool = True, max_retries: int = 3) -> bool:
        """
        Connect to IBKR.

        If connection fails and launch_gateway_if_needed is True, attempts to launch
        IB Gateway and waits for user to log in.

        Returns True if connected successfully.
        """
        # Check circuit breaker before attempting connection
        if not self.circuit_breaker.can_execute():
            status = self.circuit_breaker.get_status()
            logger.warning(
                "ibkr_connect_blocked_circuit_open",
                circuit_state=status["state"],
                failure_count=status["failure_count"],
            )
            return False

        # Try to connect
        for attempt in range(max_retries):
            logger.info(
                "ibkr_connect_attempt",
                attempt=attempt + 1,
                max_retries=max_retries,
                port=self.port,
                paper=self.paper,
            )

            try:
                self.broker = IBKRBroker(
                    host=self.host,
                    port=self.port,
                    client_id=self.client_id,
                )
                connected = await self.broker.connect()
                if connected:
                    logger.info("ibkr_connected", port=self.port, paper=self.paper)
                    # Reset circuit breaker when IBKR upstream connectivity restores (error 1102)
                    self.broker.on_connectivity_restored = self._on_connectivity_restored
                    self._start_heartbeat()
                    self.circuit_breaker.record_success()
                    return True
            except ClientIdConflictError:
                # Stale connection holding our client ID — pick a new one and retry
                import random
                old_id = self.client_id
                self.client_id = random.randint(100, 999)
                logger.warning(
                    "ibkr_client_id_conflict_retry",
                    old_client_id=old_id,
                    new_client_id=self.client_id,
                )
                # Don't count this as a normal failure or sleep — retry immediately
                continue
            except Exception as e:
                logger.warning("ibkr_connect_failed", error=str(e), attempt=attempt + 1)

            if attempt < max_retries - 1:
                await asyncio.sleep(10)

        # Connection failed - try to launch IB Gateway (skip in Docker mode)
        if launch_gateway_if_needed and not self.docker_mode:
            logger.info("ibkr_launching_gateway")
            launched = self._launch_gateway()

            if launched:
                result = await self._wait_for_login()
                if result:
                    self.circuit_breaker.record_success()
                else:
                    self.circuit_breaker.record_failure("IB Gateway login timeout")
                return result

        # All retries failed
        self.circuit_breaker.record_failure("Connection failed after all retries")
        return False

    async def disconnect(self) -> None:
        """Disconnect from IBKR."""
        self._stop_heartbeat()

        if self.broker:
            try:
                await self.broker.disconnect()
            except Exception as e:
                logger.warning("ibkr_disconnect_error", error=str(e))
            self.broker = None

        logger.info("ibkr_disconnected")

    async def ensure_connected(self) -> bool:
        """Ensure connection is active, reconnect if needed."""
        if self.is_connected:
            return True

        logger.info("ibkr_reconnecting")
        return await self.connect(launch_gateway_if_needed=False, max_retries=3)

    async def get_account_summary(self) -> Optional[AccountSummary]:
        """Get account summary from IBKR."""
        if not self.is_connected:
            return None

        try:
            return await self.broker.get_account_summary()
        except Exception as e:
            logger.error("ibkr_account_summary_error", error=str(e))
            return None

    async def get_positions(self) -> list[BrokerPosition]:
        """Get all positions from IBKR."""
        if not self.is_connected:
            return []

        try:
            return await self.broker.get_positions()
        except Exception as e:
            logger.error("ibkr_get_positions_error", error=str(e))
            return []

    async def get_position(self, symbol: str) -> Optional[BrokerPosition]:
        """Get a specific position."""
        if not self.is_connected:
            return None

        try:
            return await self.broker.get_position(symbol)
        except Exception as e:
            logger.error("ibkr_get_position_error", symbol=symbol, error=str(e))
            return None

    def _launch_gateway(self) -> bool:
        """Launch IB Gateway application."""
        system = platform.system()
        paths = GATEWAY_PATHS.get(system, [])

        for path_str in paths:
            path = Path(path_str).expanduser()

            if path.exists():
                logger.info("ibkr_launching_gateway", path=str(path))

                try:
                    if system == "Darwin":
                        subprocess.Popen(["open", str(path)])
                    elif system == "Linux":
                        subprocess.Popen([str(path)], start_new_session=True)
                    else:
                        subprocess.Popen([str(path)], creationflags=subprocess.DETACHED_PROCESS)

                    return True
                except Exception as e:
                    logger.warning("ibkr_gateway_launch_failed", path=str(path), error=str(e))

        logger.error("ibkr_gateway_not_found", searched_paths=paths)
        print("\n" + "=" * 60)
        print("Could not find IB Gateway. Please start it manually.")
        print(f"Expected locations: {paths}")
        print("=" * 60 + "\n")
        return False

    async def _wait_for_login(self, timeout_minutes: int = 5) -> bool:
        """Wait for user to log in to IB Gateway."""
        print("\n" + "=" * 60)
        print("IB Gateway launched. Please log in to your account.")
        print(f"Waiting up to {timeout_minutes} minutes for connection...")
        print("=" * 60 + "\n")

        start_time = time.time()
        timeout_seconds = timeout_minutes * 60
        check_interval = 10  # seconds

        while time.time() - start_time < timeout_seconds:
            elapsed = int(time.time() - start_time)
            remaining = timeout_seconds - elapsed

            logger.info(
                "ibkr_waiting_for_login",
                elapsed_seconds=elapsed,
                remaining_seconds=remaining,
            )

            try:
                self.broker = IBKRBroker(
                    host=self.host,
                    port=self.port,
                    client_id=self.client_id,
                )
                connected = await self.broker.connect()
                if connected:
                    logger.info("ibkr_connected_after_login")
                    print("\nConnected to IBKR!")
                    self._start_heartbeat()
                    return True
            except Exception:
                pass

            await asyncio.sleep(check_interval)

        logger.error("ibkr_login_timeout", timeout_minutes=timeout_minutes)
        print(f"\nTimeout waiting for IB Gateway login after {timeout_minutes} minutes.")
        return False

    def _start_heartbeat(self) -> None:
        """Start heartbeat task to keep connection alive."""
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    def _stop_heartbeat(self) -> None:
        """Stop heartbeat task."""
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

    async def _heartbeat_loop(self, interval_seconds: int = 60) -> None:
        """Send periodic heartbeat to keep IBKR connection alive and reconnect if needed."""
        reconnect_attempts = 0
        max_reconnect_attempts = 3
        total_consecutive_failures = 0
        max_total_failures = 10  # ~10 min at 60s intervals → fatal exit
        upstream_down_consecutive = 0
        max_upstream_down_wait = 3  # Wait max 3 min for error 1102 before hard reconnect

        while True:
            try:
                await asyncio.sleep(interval_seconds)

                if self.is_connected:
                    # Request account summary as a heartbeat
                    await self.broker.get_account_summary()
                    logger.debug("ibkr_heartbeat_sent")
                    reconnect_attempts = 0
                    total_consecutive_failures = 0
                    upstream_down_consecutive = 0
                elif (
                    self.broker
                    and self.broker.ib.isConnected()
                    and not self.broker._server_connected
                ):
                    # Gateway TCP is up but IBKR upstream is down (error 1100).
                    upstream_down_consecutive += 1
                    total_consecutive_failures += 1
                    logger.warning(
                        "ibkr_heartbeat_upstream_down",
                        total_failures=total_consecutive_failures,
                        upstream_down_minutes=upstream_down_consecutive,
                        max_wait=max_upstream_down_wait,
                    )

                    # If upstream has been down too long, stop waiting for 1102
                    # and attempt a hard disconnect/reconnect
                    if upstream_down_consecutive >= max_upstream_down_wait:
                        logger.warning(
                            "ibkr_upstream_down_timeout",
                            message="Giving up waiting for error 1102, attempting hard reconnect",
                        )
                        upstream_down_consecutive = 0
                        # Disconnect and try fresh connection
                        try:
                            await self.broker.disconnect()
                        except Exception:
                            pass
                        self.broker = None
                        # Fall through to fatal exit check
                else:
                    total_consecutive_failures += 1
                    upstream_down_consecutive = 0
                    logger.warning(
                        "ibkr_heartbeat_connection_lost",
                        reconnect_attempts=reconnect_attempts,
                        total_failures=total_consecutive_failures,
                    )

                    # Attempt to reconnect
                    if reconnect_attempts < max_reconnect_attempts:
                        reconnect_attempts += 1
                        logger.info("ibkr_heartbeat_reconnecting", attempt=reconnect_attempts)

                        connected = await self.connect(launch_gateway_if_needed=False, max_retries=1)
                        if connected:
                            logger.info("ibkr_heartbeat_reconnected")
                            reconnect_attempts = 0
                            total_consecutive_failures = 0
                        else:
                            await asyncio.sleep(30)
                    else:
                        logger.error("ibkr_heartbeat_max_reconnects_exceeded")
                        await asyncio.sleep(300)
                        reconnect_attempts = 0  # Reset to allow retry cycle

                # Fatal exit after sustained failure so Docker restarts the container
                if total_consecutive_failures >= max_total_failures:
                    logger.critical(
                        "ibkr_heartbeat_fatal_exit",
                        total_failures=total_consecutive_failures,
                        message="Exiting after sustained connection failure for Docker restart",
                    )
                    sys.exit(78)

            except asyncio.CancelledError:
                break
            except SystemExit:
                raise
            except Exception as e:
                total_consecutive_failures += 1
                logger.warning("ibkr_heartbeat_error", error=str(e), total_failures=total_consecutive_failures)
