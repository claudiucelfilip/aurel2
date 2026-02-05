"""Live trading daemon - main loop, scheduling, polling."""

import asyncio
import json
import signal
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Optional

import pytz
import structlog

from aurel2.live.connection import IBKRConnection
from aurel2.live.checker import Checker
from aurel2.live.executor import Executor
from aurel2.live.pending import PendingManager, PendingStatus
from aurel2.notifications.ntfy import NtfyNotifier

logger = structlog.get_logger()

HEARTBEAT_FILE = Path("/tmp/aurel2-heartbeat.json")


class LiveDaemon:
    """
    Live trading daemon that runs continuously.

    Schedule:
    - Daily check at configured time (default 4 PM Romania)
    - Poll for pending approvals every 5 minutes
    - Heartbeat to IBKR every 10 minutes

    Handles:
    - Graceful shutdown on SIGINT/SIGTERM
    - Automatic reconnection to IBKR
    - Pending decision timeouts
    """

    def __init__(
        self,
        paper: bool = True,
        check_time: dt_time = dt_time(16, 0),  # 4 PM
        timezone: str = "Europe/Bucharest",
        poll_interval_minutes: int = 5,
        ntfy_topic: str = "aurel2",
        dry_run: bool = False,
        ai_model: str = "sonnet",
        ai_lookback_years: int = 3,
        ibkr_host: str = "127.0.0.1",
        ibkr_port: int | None = None,
    ):
        self.paper = paper
        self.check_time = check_time
        self.timezone = pytz.timezone(timezone)
        self.poll_interval = timedelta(minutes=poll_interval_minutes)
        self.ntfy_topic = ntfy_topic
        self.dry_run = dry_run
        self.ai_model = ai_model
        self.ai_lookback_years = ai_lookback_years

        self.connection = IBKRConnection(paper=paper, host=ibkr_host, port=ibkr_port)
        self.pending_manager = PendingManager()
        self.checker = Checker(
            connection=self.connection,
            pending_manager=self.pending_manager,
            ntfy_topic=ntfy_topic,
            dry_run=dry_run,
            ai_model=ai_model,
            ai_lookback_years=ai_lookback_years,
        )
        self.executor = Executor(self.connection)
        self.notifier = NtfyNotifier(topic=ntfy_topic)

        self._running = False
        self._last_check: Optional[datetime] = None
        self._error_count = 0

    def _write_heartbeat(self) -> None:
        """Write heartbeat file with current daemon status."""
        try:
            pending = self.pending_manager.get_pending()
            heartbeat = {
                "timestamp": datetime.now().isoformat(),
                "connected": self.connection.is_connected,
                "circuit_breaker": self.connection.circuit_breaker.get_status(),
                "pending_count": len(pending),
                "last_check": self._last_check.isoformat() if self._last_check else None,
                "paper": self.paper,
                "dry_run": self.dry_run,
                "error_count": self._error_count,
            }
            HEARTBEAT_FILE.write_text(json.dumps(heartbeat, indent=2))
        except Exception as e:
            logger.warning("heartbeat_write_error", error=str(e))

    async def start(self) -> None:
        """Start the daemon."""
        logger.info(
            "daemon_starting",
            paper=self.paper,
            check_time=self.check_time.isoformat(),
            timezone=str(self.timezone),
            dry_run=self.dry_run,
        )

        print("\n" + "=" * 60)
        print(f"Aurel2 Live Trading Daemon")
        print(f"Mode: {'PAPER' if self.paper else 'LIVE'}")
        print(f"Check time: {self.check_time.isoformat()} {self.timezone}")
        print(f"AI Model: {self.ai_model} (lookback: {self.ai_lookback_years}yr)")
        print(f"Dry run: {self.dry_run}")
        print("=" * 60 + "\n")

        # Setup signal handlers
        self._setup_signals()

        # Connect to IBKR
        connected = await self.connection.connect(launch_tws_if_needed=True)
        if not connected:
            logger.error("daemon_connection_failed")
            print("Failed to connect to IBKR. Exiting.")
            return

        # Load pending decisions
        pending = self.pending_manager.get_pending()
        if pending:
            logger.info("daemon_pending_loaded", count=len(pending))
            print(f"Loaded {len(pending)} pending decisions from previous session.")

        # Send startup notification
        self.notifier.send(
            message=(
                f"Daemon started\n\n"
                f"Mode: {'PAPER' if self.paper else 'LIVE'}\n"
                f"Check time: {self.check_time.isoformat()}\n"
                f"Pending decisions: {len(pending)}"
            ),
            title="Aurel2: Daemon Started",
            tags=["rocket"],
            priority="low",
        )

        self._running = True

        # Main loop
        try:
            await self._run_loop()
        except asyncio.CancelledError:
            logger.info("daemon_cancelled")
        finally:
            await self._shutdown()

    async def _run_loop(self) -> None:
        """Main daemon loop."""
        heartbeat_interval = 60  # Write heartbeat every 60 seconds
        last_heartbeat = datetime.min

        while self._running:
            now = datetime.now(self.timezone)

            # Write heartbeat every minute
            if (datetime.now() - last_heartbeat).total_seconds() >= heartbeat_interval:
                self._write_heartbeat()
                last_heartbeat = datetime.now()

            # Check if it's time for daily check
            if self._should_run_check(now):
                logger.info("daemon_running_daily_check")
                print(f"\n[{now.strftime('%Y-%m-%d %H:%M')}] Running daily check...")

                try:
                    result = await self.checker.run()
                    self._last_check = now

                    if result.success:
                        print(f"Check complete: {result.message}")
                        if result.decision:
                            print(f"  Decision: {result.decision.action.value.upper()} {result.decision.asset_symbol or ''}")
                            print(f"  Type: {result.decision.decision_type.value}")
                    else:
                        print(f"Check failed: {result.message}")

                except Exception as e:
                    logger.error("daemon_check_error", error=str(e))
                    print(f"Check error: {e}")
                    self._error_count += 1

            # Poll for pending approvals
            await self._poll_pending()

            # Wait until next poll interval
            await asyncio.sleep(self.poll_interval.total_seconds())

    def _should_run_check(self, now: datetime) -> bool:
        """Determine if we should run the daily check."""
        # If we've already checked today, skip
        if self._last_check:
            if self._last_check.date() == now.date():
                return False

        # Check if it's past the scheduled check time
        scheduled = now.replace(
            hour=self.check_time.hour,
            minute=self.check_time.minute,
            second=0,
            microsecond=0,
        )

        return now >= scheduled

    async def _poll_pending(self) -> None:
        """Poll for pending decision status changes and timeouts."""
        results = await self.pending_manager.check_all_pending()

        for decision, new_status in results:
            logger.info(
                "daemon_pending_status_change",
                id=decision.id,
                status=new_status,
            )

            if new_status == PendingStatus.APPROVED.value:
                await self._execute_approved(decision)

            elif new_status == PendingStatus.REJECTED.value:
                print(f"Decision {decision.id} rejected by user.")
                self.notifier.send(
                    message=f"Decision rejected: {decision.action.upper()} {decision.symbol or ''}",
                    title="Aurel2: Decision Rejected",
                    tags=["x"],
                    priority="low",
                )
                self.pending_manager.remove_decision(decision.id)

            elif new_status == PendingStatus.TIMEOUT.value:
                print(f"Decision {decision.id} timed out - auto-executing...")
                await self._execute_timeout(decision)

    async def _execute_approved(self, decision) -> None:
        """Execute an approved decision."""
        logger.info("daemon_executing_approved", id=decision.id)
        print(f"Executing approved decision: {decision.action.upper()} {decision.symbol or ''}")

        if self.dry_run:
            print("  (Dry run - no actual execution)")
            self.pending_manager.mark_executed(decision.id)
            return

        result = await self.executor.execute(
            action=decision.action,
            symbol=decision.symbol,
            current_holding=decision.current_holding,
            position_size_pct=decision.position_size_pct,
        )

        # Get post-trade state for journal
        account_after = None
        holding_after = None
        if result.success:
            try:
                summary = await self.connection.get_account_summary()
                if summary:
                    account_after = summary.total_value
                holding_after = await self.executor.get_current_holding()
            except Exception:
                pass

        # Record execution in trade journal (use linked journal ID if available)
        journal_id = decision.journal_decision_id or decision.id
        self.checker.journal.record_execution(
            decision_id=journal_id,
            success=result.success,
            shares=result.shares,
            fill_price=result.fill_price,
            error=result.message if not result.success else None,
            account_value_after=account_after,
            current_holding_after=holding_after,
        )

        if result.success:
            self.notifier.send(
                message=(
                    f"Approved decision executed\n\n"
                    f"Action: {decision.action.upper()} {decision.symbol or ''}\n"
                    f"Position size: {decision.position_size_pct:.0%}\n"
                    f"Shares: {result.shares:.2f} @ ${result.fill_price:.2f}"
                ),
                title="Aurel2: Trade Executed",
                tags=["white_check_mark", "chart_with_upwards_trend"],
                priority="default",
            )
        else:
            self.notifier.send(
                message=f"Execution failed: {result.message}",
                title="Aurel2: Execution Failed",
                tags=["x", "warning"],
                priority="high",
            )

        self.pending_manager.mark_executed(decision.id)

    async def _execute_timeout(self, decision) -> None:
        """Execute a timed-out decision with validation."""
        logger.info(
            "daemon_handling_timed_out_decision",
            decision_id=decision.id,
            action=decision.action,
            symbol=decision.symbol,
        )

        # Get current market price for validation
        current_price = 0.0
        if decision.symbol and self.connection.is_connected:
            try:
                current_price = await self.connection.broker.get_market_price(decision.symbol)
            except Exception as e:
                logger.warning("failed_to_get_price_for_validation", error=str(e))

        # Validate decision is still appropriate
        is_valid, reason = self.pending_manager.validate_decision_still_valid(
            decision=decision,
            current_price=current_price,
        )

        if not is_valid:
            logger.warning(
                "timed_out_decision_invalidated",
                decision_id=decision.id,
                reason=reason,
            )

            self.notifier.send(
                message=(
                    f"Timed-out decision NOT auto-executed: {reason}\n\n"
                    f"Action: {decision.action.upper()} {decision.symbol or 'CASH'}\n"
                    f"Please review manually."
                ),
                title="Aurel2: Decision Invalidated",
                priority="high",
                tags=["warning", "clock"],
            )

            # Mark as rejected
            self.pending_manager.decisions[decision.id].status = "rejected"
            self.pending_manager._save()
            return

        # Proceed with auto-execution
        logger.info("daemon_executing_timeout", id=decision.id)
        print(f"Auto-executing timed-out decision: {decision.action.upper()} {decision.symbol or ''}")

        if self.dry_run:
            print("  (Dry run - no actual execution)")
            self.pending_manager.mark_executed(decision.id)
            return

        result = await self.executor.execute(
            action=decision.action,
            symbol=decision.symbol,
            current_holding=decision.current_holding,
            position_size_pct=decision.position_size_pct,
        )

        # Get post-trade state for journal
        account_after = None
        holding_after = None
        if result.success:
            try:
                summary = await self.connection.get_account_summary()
                if summary:
                    account_after = summary.total_value
                holding_after = await self.executor.get_current_holding()
            except Exception:
                pass

        # Record execution in trade journal (use linked journal ID if available)
        journal_id = decision.journal_decision_id or decision.id
        self.checker.journal.record_execution(
            decision_id=journal_id,
            success=result.success,
            shares=result.shares,
            fill_price=result.fill_price,
            error=result.message if not result.success else None,
            account_value_after=account_after,
            current_holding_after=holding_after,
        )

        if result.success:
            self.notifier.send(
                message=(
                    f"Timed-out decision auto-executed\n\n"
                    f"Action: {decision.action.upper()} {decision.symbol or ''}\n"
                    f"Position size: {decision.position_size_pct:.0%}\n"
                    f"Shares: {result.shares:.2f} @ ${result.fill_price:.2f}\n"
                    f"Note: Executed after 1-hour timeout"
                ),
                title="Aurel2: Auto-Executed (Timeout)",
                tags=["alarm_clock", "chart_with_upwards_trend"],
                priority="default",
            )
        else:
            self.notifier.send(
                message=f"Auto-execution failed: {result.message}",
                title="Aurel2: Execution Failed",
                tags=["x", "warning"],
                priority="high",
            )

        self.pending_manager.mark_executed(decision.id)

    def _setup_signals(self) -> None:
        """Setup signal handlers for graceful shutdown."""
        loop = asyncio.get_event_loop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._handle_signal)

    def _handle_signal(self) -> None:
        """Handle shutdown signal."""
        logger.info("daemon_shutdown_signal")
        print("\nShutdown signal received. Stopping daemon...")
        self._running = False

    async def _shutdown(self) -> None:
        """Graceful shutdown."""
        logger.info("daemon_shutting_down")
        print("Shutting down...")

        # Disconnect from IBKR
        await self.connection.disconnect()

        # Send notification
        self.notifier.send(
            message="Daemon stopped gracefully",
            title="Aurel2: Daemon Stopped",
            tags=["stop_sign"],
            priority="low",
        )

        print("Daemon stopped.")


async def run_single_check(
    paper: bool = True,
    ntfy_topic: str = "aurel2",
    dry_run: bool = False,
) -> None:
    """Run a single check (for manual testing)."""
    logger.info("single_check_start", paper=paper, dry_run=dry_run)

    print("\n" + "=" * 60)
    print(f"Aurel2 Single Check")
    print(f"Mode: {'PAPER' if paper else 'LIVE'}")
    print(f"Dry run: {dry_run}")
    print("=" * 60 + "\n")

    connection = IBKRConnection(paper=paper)
    pending_manager = PendingManager()
    checker = Checker(
        connection=connection,
        pending_manager=pending_manager,
        ntfy_topic=ntfy_topic,
        dry_run=dry_run,
    )

    try:
        # Connect
        connected = await connection.connect(launch_tws_if_needed=True)
        if not connected:
            print("Failed to connect to IBKR.")
            return

        # Run check
        print("Running check...")
        result = await checker.run()

        # Print results
        print("\n" + "-" * 40)
        print(f"Success: {result.success}")
        print(f"Message: {result.message}")
        print(f"Current holding: {result.current_holding}")
        print(f"Account value: ${result.account_value:,.2f}" if result.account_value else "Account value: N/A")

        if result.decision:
            print(f"\nDecision:")
            print(f"  Type: {result.decision.decision_type.value}")
            print(f"  Action: {result.decision.action.value.upper()}")
            print(f"  Asset: {result.decision.asset_symbol}")
            print(f"  Confidence: {result.decision.confidence:.0%}")
            print(f"  Requires approval: {result.decision.requires_approval}")
            print(f"  Reasoning: {result.decision.reasoning}")

        if result.execution_result:
            print(f"\nExecution:")
            print(f"  Success: {result.execution_result.success}")
            print(f"  Shares: {result.execution_result.shares}")
            print(f"  Price: ${result.execution_result.fill_price:.2f}")
            print(f"  Message: {result.execution_result.message}")

        if result.pending_decision:
            print(f"\nPending approval:")
            print(f"  URL: {result.pending_decision.approval_url}")
            print(f"  Timeout: {result.pending_decision.timeout_seconds() // 60} minutes")

        print("-" * 40 + "\n")

    finally:
        await connection.disconnect()
