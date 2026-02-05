"""Main daemon monitor - watches health and auto-fixes issues."""

import asyncio
import signal
from datetime import datetime, timedelta
from typing import Optional

import structlog

from aurel2.monitor.health_checker import HealthChecker, HealthStatus, HealthReport
from aurel2.monitor.error_analyzer import ErrorAnalyzer, ErrorSeverity, AnalyzedError
from aurel2.monitor.auto_fixer import AutoFixer
from aurel2.monitor.session_tracker import SessionTracker
from aurel2.monitor.incident_tracker import IncidentTracker
from aurel2.monitor.ai_analyzer import AIAnalyzer, AIAnalysis
from aurel2.notifications.ntfy import NtfyNotifier

logger = structlog.get_logger()


class DaemonMonitor:
    """
    Monitors the daemon for errors and health issues.

    Responsibilities:
    - Check daemon health every 60 seconds
    - Analyze errors and classify by severity
    - Auto-fix fixable issues (connection, circuit breaker, crashes)
    - Proactively restart daemon on connection loss
    - Escalate non-fixable issues via ntfy notifications
    - Track session progress for evaluation
    """

    CHECK_INTERVAL_SECONDS = 60
    CONSECUTIVE_UNHEALTHY_THRESHOLD = 3
    CONSECUTIVE_DISCONNECTED_THRESHOLD = 5  # 5 minutes of disconnection triggers restart
    MAX_RESTARTS_PER_HOUR = 3

    def __init__(
        self,
        paper: bool = True,
        dry_run: bool = False,
        ntfy_topic: str = "aurel2",
        ai_enabled: bool = False,
        ai_model: str = "sonnet",
    ):
        self.paper = paper
        self.dry_run = dry_run
        self.ai_enabled = ai_enabled

        self.health_checker = HealthChecker()
        self.error_analyzer = ErrorAnalyzer()
        self.auto_fixer = AutoFixer(paper=paper, dry_run=dry_run)
        self.session_tracker = SessionTracker()
        self.incident_tracker = IncidentTracker()
        self.notifier = NtfyNotifier(topic=ntfy_topic, rate_limit_seconds=3600)

        # AI analyzer (optional)
        self.ai_analyzer: Optional[AIAnalyzer] = None
        if ai_enabled:
            self.ai_analyzer = AIAnalyzer(model=ai_model)

        self._running = False
        self._consecutive_unhealthy = 0
        self._consecutive_disconnected = 0  # Track consecutive disconnected checks
        self._last_health_report: Optional[HealthReport] = None
        self._minutes_since_start = 0
        self._restarts_today = 0
        self._restarts_this_hour: list[datetime] = []  # Track restart timestamps
        self._errors_today = 0
        self._last_incident_id: Optional[str] = None

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
        print(f"AI enabled: {self.ai_enabled}")
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

        # Check for persistent disconnection (proactive recovery)
        if report.heartbeat and not report.heartbeat.connected:
            self._consecutive_disconnected += 1
            logger.info(
                "daemon_disconnected",
                consecutive_count=self._consecutive_disconnected,
                threshold=self.CONSECUTIVE_DISCONNECTED_THRESHOLD,
            )
        else:
            self._consecutive_disconnected = 0

        # Proactive restart if disconnected for too long
        if self._consecutive_disconnected >= self.CONSECUTIVE_DISCONNECTED_THRESHOLD:
            await self._handle_persistent_disconnection(report)
            return

        # Handle based on status
        if report.status == HealthStatus.HEALTHY:
            await self._handle_healthy(report)
        elif self.ai_enabled and self.ai_analyzer:
            # AI mode: analyze ALL non-healthy states with AI
            await self._handle_with_ai(report)
        elif report.status == HealthStatus.DEGRADED:
            await self._handle_degraded(report)
        elif report.status == HealthStatus.UNHEALTHY:
            await self._handle_unhealthy(report)

        # Update session with account value if available
        if report.heartbeat:
            # We'd get account value from IBKR - for now just track connection
            pass

    def _can_restart(self) -> tuple[bool, str]:
        """Check if we can perform another restart (rate limiting)."""
        # Clean up old restart timestamps (older than 1 hour)
        one_hour_ago = datetime.now() - timedelta(hours=1)
        self._restarts_this_hour = [ts for ts in self._restarts_this_hour if ts > one_hour_ago]

        if len(self._restarts_this_hour) >= self.MAX_RESTARTS_PER_HOUR:
            return False, f"Restart limit reached ({self.MAX_RESTARTS_PER_HOUR}/hour)"

        return True, ""

    def _record_restart(self) -> None:
        """Record a restart for rate limiting."""
        self._restarts_this_hour.append(datetime.now())
        self._restarts_today += 1
        self.session_tracker.record_restart()

    async def _handle_persistent_disconnection(self, report: HealthReport) -> None:
        """Handle persistent IBKR disconnection by restarting the daemon."""
        logger.warning(
            "persistent_disconnection_detected",
            consecutive_checks=self._consecutive_disconnected,
            threshold=self.CONSECUTIVE_DISCONNECTED_THRESHOLD,
        )

        print(f"[{datetime.now().strftime('%H:%M')}] Persistent disconnection detected ({self._consecutive_disconnected}x)")

        # Check if we can restart
        can_restart, reason = self._can_restart()
        if not can_restart:
            logger.warning("restart_blocked", reason=reason)
            print(f"  Cannot restart: {reason}")

            # Escalate since we can't fix it
            self.notifier.send(
                message=(
                    f"Daemon has been disconnected for {self._consecutive_disconnected} minutes.\n\n"
                    f"Auto-restart blocked: {reason}\n\n"
                    "Manual intervention required."
                ),
                title="Aurel2: Persistent Disconnection",
                tags=["warning", "electric_plug"],
                priority="high",
                category="persistent_disconnection",
            )
            return

        # Attempt restart
        print(f"  Attempting automatic restart...")

        result = self.auto_fixer.fix("restart_daemon", {"reason": "persistent IBKR disconnection"})

        if result.success:
            self._record_restart()
            self._consecutive_disconnected = 0

            logger.info("daemon_restarted_for_disconnection", message=result.message)

            self.notifier.send(
                message=(
                    f"Daemon automatically restarted due to persistent disconnection.\n\n"
                    f"Was disconnected for: {self._consecutive_disconnected} checks\n"
                    f"Result: {result.message}\n"
                    f"Restarts this hour: {len(self._restarts_this_hour)}/{self.MAX_RESTARTS_PER_HOUR}"
                ),
                title="Aurel2: Auto-Restart (Disconnection)",
                tags=["arrows_counterclockwise", "electric_plug"],
                priority="default",
                category="auto_restart_disconnection",
            )
        else:
            logger.error("daemon_restart_failed", message=result.message)

            self.notifier.send(
                message=(
                    f"Failed to restart daemon after persistent disconnection.\n\n"
                    f"Error: {result.message}\n\n"
                    "Manual intervention required."
                ),
                title="Aurel2: Restart Failed",
                tags=["x", "warning"],
                priority="high",
                category="restart_failed",
            )

    async def _handle_healthy(self, report: HealthReport) -> None:
        """Handle healthy status."""
        # Reset consecutive counters
        was_unhealthy = self._consecutive_unhealthy > 0
        was_disconnected = self._consecutive_disconnected > 0

        if was_unhealthy or was_disconnected:
            logger.info(
                "daemon_recovered",
                previous_unhealthy_count=self._consecutive_unhealthy,
                previous_disconnected_count=self._consecutive_disconnected,
            )
            self._consecutive_unhealthy = 0
            self._consecutive_disconnected = 0

            # Reset auto-fixer attempts on recovery
            self.auto_fixer.reset_attempts()

            # Mark previous incident as resolved if any
            if self._last_incident_id:
                resolution_time = self._minutes_since_start * 60  # Approximate
                self.incident_tracker.update_outcome(
                    self._last_incident_id,
                    outcome="resolved",
                    resolution_time_seconds=resolution_time,
                )
                self._last_incident_id = None

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

    async def _handle_with_ai(self, report: HealthReport) -> None:
        """Handle non-healthy status using AI analysis."""
        print(f"[{datetime.now().strftime('%H:%M')}] {report.status.value.upper()}: {', '.join(report.issues)}")

        # Track consecutive unhealthy
        if report.status == HealthStatus.UNHEALTHY:
            self._consecutive_unhealthy += 1
            self._errors_today += 1
            self.session_tracker.record_error()

        # Get AI analysis
        analysis = self.ai_analyzer.analyze(
            health_report=report,
            recent_incidents=self.incident_tracker.get_recent_incidents(hours=24),
            session_context=self._get_session_context(),
        )

        logger.info(
            "ai_analysis_result",
            root_cause=analysis.root_cause,
            severity=analysis.severity,
            action=analysis.recommended_action,
            confidence=analysis.confidence,
        )

        # Record incident
        similar_incidents = self.incident_tracker.get_similar_incidents(report.issues)
        similar_summaries = [
            f"{inc.timestamp[:10]}: {inc.ai_diagnosis}"
            for inc in similar_incidents[:3]
        ]

        incident_id = self.incident_tracker.record_incident(
            health_status=report.status.value,
            issues=report.issues,
            ai_diagnosis=analysis.root_cause,
            action_taken=analysis.recommended_action,
            ai_confidence=analysis.confidence,
            ai_reasoning=analysis.reasoning,
            similar_to_past=similar_summaries,
        )
        self._last_incident_id = incident_id

        # Execute AI's recommendation
        await self._execute_ai_recommendation(analysis, incident_id)

    def _get_session_context(self) -> dict:
        """Get session context for AI analysis."""
        return {
            "uptime_minutes": self._minutes_since_start,
            "restarts_today": self._restarts_today,
            "errors_today": self._errors_today,
            "paper": self.paper,
            "consecutive_unhealthy": self._consecutive_unhealthy,
            "auto_fixer_stats": self.auto_fixer.get_stats(),
        }

    async def _execute_ai_recommendation(self, analysis: AIAnalysis, incident_id: str) -> None:
        """Execute the AI's recommended action."""
        action = analysis.recommended_action

        # Log AI reasoning
        print(f"  AI diagnosis: {analysis.root_cause}")
        print(f"  AI recommends: {action} (confidence: {analysis.confidence:.0%})")
        if analysis.reasoning:
            print(f"  Reasoning: {analysis.reasoning[:100]}...")

        # Safety check: never auto-fix execution errors
        if analysis.severity == "critical" and "execution" in analysis.root_cause.lower():
            logger.warning("ai_blocked_execution_fix", reason="Execution errors require manual review")
            action = "escalate"

        # Execute the action
        if action == "wait":
            # AI decided to wait and observe
            logger.info("ai_action_wait", reasoning=analysis.reasoning)
            print("  Action: Waiting to observe...")
            # Don't count wait as a fix attempt
            return

        elif action == "escalate":
            # AI determined issue cannot be auto-fixed
            self._escalate_ai(analysis)

        elif action in ("restart_daemon", "wait_and_restart"):
            # Check restart limits (3 per hour)
            stats = self.auto_fixer.get_stats()
            total_attempts = sum(stats.get("attempts", {}).values())

            if total_attempts >= 3:
                logger.warning("ai_restart_limit_reached", attempts=total_attempts)
                self.incident_tracker.update_outcome(incident_id, "recurring")
                self._escalate_ai(analysis, reason="Restart limit reached (3/hour)")
                return

            # Execute the fix
            context = {"reason": analysis.reasoning}
            result = self.auto_fixer.fix(action, context)

            if result.success:
                self._restarts_today += 1
                self.session_tracker.record_restart()
                self._notify_ai_fix_success(analysis, result.message)
            else:
                self._notify_ai_fix_failure(analysis, result.message)
                self.incident_tracker.update_outcome(incident_id, "escalated")

    def _escalate_ai(self, analysis: AIAnalysis, reason: Optional[str] = None) -> None:
        """Escalate an issue that AI determined cannot be auto-fixed."""
        escalation_reason = reason or analysis.reasoning

        logger.warning(
            "ai_escalation",
            root_cause=analysis.root_cause,
            reason=escalation_reason,
        )

        self.notifier.send(
            message=(
                f"AI Analysis:\n"
                f"Root cause: {analysis.root_cause}\n"
                f"Severity: {analysis.severity}\n\n"
                f"Reason for escalation: {escalation_reason}\n\n"
                f"Manual intervention required."
            ),
            title="Aurel2: AI Escalation",
            tags=["rotating_light", "robot"],
            priority="urgent",
            category="ai_escalation",
        )

    def _notify_ai_fix_success(self, analysis: AIAnalysis, message: str) -> None:
        """Notify about successful AI-recommended fix."""
        self.notifier.send(
            message=(
                f"AI auto-fixed an issue:\n\n"
                f"Diagnosis: {analysis.root_cause}\n"
                f"Action: {analysis.recommended_action}\n"
                f"Result: {message}"
            ),
            title="Aurel2: AI Auto-Fix Applied",
            tags=["robot", "wrench", "white_check_mark"],
            priority="default",
            category="ai_fix_success",
        )

    def _notify_ai_fix_failure(self, analysis: AIAnalysis, message: str) -> None:
        """Notify about failed AI-recommended fix."""
        self.notifier.send(
            message=(
                f"AI auto-fix FAILED:\n\n"
                f"Diagnosis: {analysis.root_cause}\n"
                f"Attempted: {analysis.recommended_action}\n"
                f"Error: {message}\n\n"
                f"Manual intervention required."
            ),
            title="Aurel2: AI Auto-Fix Failed",
            tags=["robot", "x"],
            priority="high",
            category="ai_fix_failure",
        )

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

            # Check restart limit
            can_restart, reason = self._can_restart()
            if not can_restart:
                logger.warning("restart_blocked_unhealthy", reason=reason)
                self._escalate(errors, report, extra_info=reason)
                return

            # Find a fixable error to act on
            for error in errors:
                if error.is_auto_fixable and error.fix_action:
                    result = self.auto_fixer.fix(error.fix_action)

                    if result.success:
                        self._record_restart()
                        self._consecutive_unhealthy = 0
                        self._notify_fix_success(error, result.message)
                    else:
                        self._notify_fix_failure(error, result.message)

                    # Only try one fix per cycle
                    break
            else:
                # No fixable errors - try generic restart
                if "Daemon process not running" in report.issues:
                    result = self.auto_fixer.fix("restart_daemon", {"reason": "daemon not running"})
                    if result.success:
                        self._record_restart()
                        self._consecutive_unhealthy = 0
                        self.notifier.send(
                            message=f"Daemon was not running. Auto-restarted.\n\n{result.message}",
                            title="Aurel2: Auto-Restart (Process Down)",
                            tags=["arrows_counterclockwise"],
                            priority="default",
                            category="auto_restart_process_down",
                        )
                    else:
                        self._escalate(errors, report)
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

    def _escalate(self, errors: list[AnalyzedError], report: HealthReport, extra_info: Optional[str] = None) -> None:
        """Escalate issues that cannot be auto-fixed (rate limited)."""
        issue_summary = "\n".join(f"• {e.message}" for e in errors) if errors else "\n".join(f"• {i}" for i in report.issues)

        extra = f"\n\nNote: {extra_info}" if extra_info else ""

        self.notifier.send(
            message=(
                f"Daemon has been unhealthy for {self._consecutive_unhealthy} consecutive checks.\n\n"
                f"Issues:\n{issue_summary}{extra}\n\n"
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
        # Clean up old restart timestamps for accurate count
        one_hour_ago = datetime.now() - timedelta(hours=1)
        self._restarts_this_hour = [ts for ts in self._restarts_this_hour if ts > one_hour_ago]

        status = {
            "running": self._running,
            "consecutive_unhealthy": self._consecutive_unhealthy,
            "consecutive_disconnected": self._consecutive_disconnected,
            "last_health": self._last_health_report.status.value if self._last_health_report else None,
            "minutes_running": self._minutes_since_start,
            "auto_fixer_stats": self.auto_fixer.get_stats(),
            "error_counts": self.error_analyzer.get_error_counts(),
            "ai_enabled": self.ai_enabled,
            "restarts_this_hour": len(self._restarts_this_hour),
            "max_restarts_per_hour": self.MAX_RESTARTS_PER_HOUR,
            "restarts_today": self._restarts_today,
            "errors_today": self._errors_today,
        }

        if self.ai_enabled:
            status["incident_stats"] = self.incident_tracker.get_stats()

        return status
