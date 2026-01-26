"""Main daemon monitor - watches health and auto-fixes issues."""

import asyncio
import signal
from datetime import datetime
from typing import Optional

import structlog

from aurel2.monitor.health_checker import HealthChecker, HealthStatus, HealthReport
from aurel2.monitor.error_analyzer import ErrorAnalyzer, ErrorSeverity, AnalyzedError
from aurel2.monitor.auto_fixer import AutoFixer
from aurel2.monitor.session_tracker import SessionTracker
from aurel2.notifications.ntfy import NtfyNotifier

logger = structlog.get_logger()


class DaemonMonitor:
    """
    Monitors the daemon for errors and health issues.

    Responsibilities:
    - Check daemon health every 60 seconds
    - Analyze errors and classify by severity
    - Auto-fix fixable issues (connection, circuit breaker, crashes)
    - Escalate non-fixable issues via ntfy notifications
    - Track session progress for evaluation
    """

    CHECK_INTERVAL_SECONDS = 60
    CONSECUTIVE_UNHEALTHY_THRESHOLD = 3

    def __init__(
        self,
        paper: bool = True,
        dry_run: bool = False,
        ntfy_topic: str = "aurel2",
    ):
        self.paper = paper
        self.dry_run = dry_run

        self.health_checker = HealthChecker()
        self.error_analyzer = ErrorAnalyzer()
        self.auto_fixer = AutoFixer(paper=paper, dry_run=dry_run)
        self.session_tracker = SessionTracker()
        self.notifier = NtfyNotifier(topic=ntfy_topic, rate_limit_seconds=3600)

        self._running = False
        self._consecutive_unhealthy = 0
        self._last_health_report: Optional[HealthReport] = None
        self._minutes_since_start = 0

    async def start(self) -> None:
        """Start the monitor."""
        logger.info(
            "monitor_starting",
            paper=self.paper,
            dry_run=self.dry_run,
        )

        print("\n" + "=" * 60)
        print("Aurel2 Daemon Monitor")
        print(f"Mode: {'PAPER' if self.paper else 'LIVE'}")
        print(f"Dry run: {self.dry_run}")
        print(f"Check interval: {self.CHECK_INTERVAL_SECONDS}s")
        print("=" * 60 + "\n")

        # Setup signal handlers
        self._setup_signals()

        # Send startup notification (no rate limit for startup)
        self.notifier.send(
            message="Daemon monitor started",
            title="Aurel2: Monitor Started",
            tags=["eyes"],
            priority="low",
        )

        self._running = True

        try:
            await self._run_loop()
        except asyncio.CancelledError:
            logger.info("monitor_cancelled")
        finally:
            await self._shutdown()

    async def _run_loop(self) -> None:
        """Main monitoring loop."""
        while self._running:
            try:
                await self._check_cycle()
            except Exception as e:
                logger.error("monitor_check_error", error=str(e))
                self.session_tracker.record_error()

            # Wait for next check
            await asyncio.sleep(self.CHECK_INTERVAL_SECONDS)
            self._minutes_since_start += 1

            # Record uptime every 5 minutes
            if self._minutes_since_start % 5 == 0:
                self.session_tracker.record_uptime(5)

    async def _check_cycle(self) -> None:
        """Perform one health check cycle."""
        # Check health
        report = self.health_checker.check()
        self._last_health_report = report

        logger.info(
            "health_check",
            status=report.status.value,
            issues=len(report.issues),
            process_running=report.process.running,
        )

        # Handle based on status
        if report.status == HealthStatus.HEALTHY:
            await self._handle_healthy(report)
        elif report.status == HealthStatus.DEGRADED:
            await self._handle_degraded(report)
        elif report.status == HealthStatus.UNHEALTHY:
            await self._handle_unhealthy(report)

        # Update session with account value if available
        if report.heartbeat:
            # We'd get account value from IBKR - for now just track connection
            pass

    async def _handle_healthy(self, report: HealthReport) -> None:
        """Handle healthy status."""
        # Reset consecutive unhealthy counter
        if self._consecutive_unhealthy > 0:
            logger.info("daemon_recovered", previous_unhealthy_count=self._consecutive_unhealthy)
            self._consecutive_unhealthy = 0

            # Reset auto-fixer attempts on recovery
            self.auto_fixer.reset_attempts()

            # Notify recovery (no rate limit - recovery is important)
            self.notifier.send(
                message="Daemon has recovered and is healthy",
                title="Aurel2: Daemon Recovered",
                tags=["white_check_mark"],
                priority="low",
            )

        # Print status occasionally
        if self._minutes_since_start % 10 == 0:
            print(f"[{datetime.now().strftime('%H:%M')}] Daemon healthy ✓")

    async def _handle_degraded(self, report: HealthReport) -> None:
        """Handle degraded status."""
        print(f"[{datetime.now().strftime('%H:%M')}] Daemon degraded: {', '.join(report.issues)}")

        # Analyze errors
        errors = self.error_analyzer.analyze_health_report(
            report.issues,
            context={
                "process": vars(report.process) if report.process else {},
                "circuit_breaker": report.heartbeat.circuit_breaker_state if report.heartbeat else {},
            }
        )

        # Handle each error
        for error in errors:
            await self._handle_error(error)

    async def _handle_unhealthy(self, report: HealthReport) -> None:
        """Handle unhealthy status."""
        self._consecutive_unhealthy += 1
        print(f"[{datetime.now().strftime('%H:%M')}] Daemon UNHEALTHY ({self._consecutive_unhealthy}x): {', '.join(report.issues)}")

        self.session_tracker.record_error()

        # Analyze errors
        errors = self.error_analyzer.analyze_health_report(report.issues)

        # If we've hit the threshold, attempt restart
        if self._consecutive_unhealthy >= self.CONSECUTIVE_UNHEALTHY_THRESHOLD:
            logger.warning(
                "consecutive_unhealthy_threshold_reached",
                count=self._consecutive_unhealthy,
            )

            # Find a fixable error to act on
            for error in errors:
                if error.is_auto_fixable and error.fix_action:
                    result = self.auto_fixer.fix(error.fix_action)

                    if result.success:
                        self.session_tracker.record_restart()
                        self._notify_fix_success(error, result.message)
                    else:
                        self._notify_fix_failure(error, result.message)

                    # Only try one fix per cycle
                    break
            else:
                # No fixable errors - escalate
                self._escalate(errors, report)

        else:
            # Not at threshold yet - just notify about issues
            for error in errors:
                await self._handle_error(error)

    async def _handle_error(self, error: AnalyzedError) -> None:
        """Handle a single analyzed error."""
        logger.info(
            "handling_error",
            category=error.category.value,
            severity=error.severity.value,
            auto_fixable=error.is_auto_fixable,
        )

        # Only notify for WARNING and above, and rate limit
        if error.severity in (ErrorSeverity.WARNING, ErrorSeverity.ERROR, ErrorSeverity.CRITICAL):
            self._notify_error(error)

        # Attempt auto-fix if possible
        if error.is_auto_fixable and error.fix_action:
            if error.severity in (ErrorSeverity.ERROR, ErrorSeverity.CRITICAL):
                result = self.auto_fixer.fix(error.fix_action)

                if result.success:
                    self.session_tracker.record_restart()
                    self._notify_fix_success(error, result.message)
                else:
                    self._notify_fix_failure(error, result.message)

    def _notify_error(self, error: AnalyzedError) -> None:
        """Send notification for an error (rate limited to 1 per hour per category)."""
        priority = {
            ErrorSeverity.INFO: "low",
            ErrorSeverity.WARNING: "default",
            ErrorSeverity.ERROR: "high",
            ErrorSeverity.CRITICAL: "urgent",
        }.get(error.severity, "default")

        tags = {
            ErrorSeverity.INFO: ["information_source"],
            ErrorSeverity.WARNING: ["warning"],
            ErrorSeverity.ERROR: ["x"],
            ErrorSeverity.CRITICAL: ["rotating_light"],
        }.get(error.severity, ["warning"])

        message = f"{error.message}"
        if error.details:
            message += f"\n\n{error.details}"
        if error.is_auto_fixable:
            message += f"\n\nAuto-fix available: {error.fix_action}"

        self.notifier.send(
            message=message,
            title=f"Aurel2: {error.severity.value.upper()} - {error.category.value}",
            priority=priority,
            tags=tags,
            category=f"error_{error.category.value}",
        )

    def _notify_fix_success(self, error: AnalyzedError, message: str) -> None:
        """Notify about successful auto-fix (rate limited)."""
        self.notifier.send(
            message=f"Auto-fixed: {error.category.value}\n\n{message}",
            title="Aurel2: Auto-Fix Applied",
            tags=["wrench", "white_check_mark"],
            priority="default",
            category=f"fix_success_{error.category.value}",
        )

    def _notify_fix_failure(self, error: AnalyzedError, message: str) -> None:
        """Notify about failed auto-fix (rate limited)."""
        self.notifier.send(
            message=f"Auto-fix FAILED: {error.category.value}\n\n{message}\n\nManual intervention required.",
            title="Aurel2: Auto-Fix Failed",
            tags=["x", "wrench"],
            priority="high",
            category=f"fix_failure_{error.category.value}",
        )

    def _escalate(self, errors: list[AnalyzedError], report: HealthReport) -> None:
        """Escalate issues that cannot be auto-fixed (rate limited)."""
        issue_summary = "\n".join(f"• {e.message}" for e in errors)

        self.notifier.send(
            message=(
                f"Daemon has been unhealthy for {self._consecutive_unhealthy} consecutive checks.\n\n"
                f"Issues:\n{issue_summary}\n\n"
                "Auto-fix attempts exhausted or not possible.\n"
                "Manual intervention required."
            ),
            title="Aurel2: ESCALATION - Manual Intervention Required",
            tags=["rotating_light", "sos"],
            priority="urgent",
            category="escalation",
        )

    def _setup_signals(self) -> None:
        """Setup signal handlers."""
        loop = asyncio.get_event_loop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._handle_signal)

    def _handle_signal(self) -> None:
        """Handle shutdown signal."""
        logger.info("monitor_shutdown_signal")
        print("\nShutdown signal received. Stopping monitor...")
        self._running = False

    async def _shutdown(self) -> None:
        """Graceful shutdown."""
        logger.info("monitor_shutting_down")
        print("Shutting down monitor...")

        # Clean up old sessions
        self.session_tracker.cleanup_old_sessions()

        # Send notification
        self.notifier.send(
            message="Daemon monitor stopped",
            title="Aurel2: Monitor Stopped",
            tags=["stop_sign"],
            priority="low",
        )

        print("Monitor stopped.")

    def get_status(self) -> dict:
        """Get current monitor status."""
        return {
            "running": self._running,
            "consecutive_unhealthy": self._consecutive_unhealthy,
            "last_health": self._last_health_report.status.value if self._last_health_report else None,
            "minutes_running": self._minutes_since_start,
            "auto_fixer_stats": self.auto_fixer.get_stats(),
            "error_counts": self.error_analyzer.get_error_counts(),
        }
