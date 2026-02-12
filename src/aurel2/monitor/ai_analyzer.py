"""AI-powered analysis for daemon monitoring.

Uses Claude Code CLI to provide intelligent root cause analysis
and action recommendations for daemon health issues.
"""

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import structlog

from aurel2.monitor.error_analyzer import ErrorAnalyzer, ErrorSeverity
from aurel2.monitor.health_checker import HealthReport, HealthStatus
from aurel2.monitor.incident_tracker import Incident

logger = structlog.get_logger()

DAEMON_LOG_FILE = Path.home() / ".aurel2" / "daemon.log"


@dataclass
class AIAnalysis:
    """Result of AI analysis of a health issue."""

    root_cause: str  # AI's diagnosis
    severity: str  # critical, error, warning, info
    recommended_action: str  # restart_daemon, wait_and_restart, wait, escalate
    confidence: float  # 0.0-1.0
    reasoning: str  # Explanation
    is_anomaly: bool  # Proactive detection
    similar_past_incidents: list[str]  # Matched patterns


AI_MONITOR_SYSTEM_PROMPT = """You are an autonomous monitoring agent for the Aurel2 trading system.
Your job is to diagnose issues and decide on fixes WITHOUT human intervention.

## About Aurel2
Aurel2 is an automated trading system that:
- Runs as a daemon process connecting to Alpaca Markets API
- Executes momentum-based ETF rotation strategies
- Uses a heartbeat file to signal health
- Has a circuit breaker that trips on repeated failures

## Common Issues and Patterns

### Connection Issues (restart_daemon)
- Alpaca API unreachable or returning errors
- Broker API connection dropped
- Socket errors or timeouts
- Usually fixed by restarting the daemon

### Circuit Breaker Issues (wait_and_restart)
- Circuit breaker opened due to repeated failures
- Need to wait for cooldown period before restart
- Often caused by market data issues or API rate limits

### Process Crashes (restart_daemon)
- Daemon process not running
- Heartbeat file stale (no updates)
- Process killed or crashed unexpectedly

### Execution Errors (escalate)
- Order rejected by broker
- Insufficient funds
- Position errors
- NEVER auto-fix these - money is involved

## Available Actions
- restart_daemon: Kill and restart the trading daemon
- wait_and_restart: Wait 60s (circuit breaker cooldown), then restart
- wait: Do nothing now, observe on next check (use when unsure or if issue may self-resolve)
- escalate: Issue cannot be auto-fixed, notify for manual review

## Safety Rules (CRITICAL)
1. NEVER auto-fix execution/order errors - these involve real money
2. Limit restarts to 3 per hour maximum
3. If same issue recurs 3+ times after fixes, escalate
4. When in doubt, use 'wait' to gather more data
5. In paper trading mode, be more willing to restart

## Output Format
Respond with ONLY valid JSON (no markdown code blocks):
{
    "root_cause": "brief diagnosis of what's wrong",
    "severity": "critical|error|warning|info",
    "recommended_action": "restart_daemon|wait_and_restart|wait|escalate",
    "confidence": 0.0 to 1.0,
    "reasoning": "why this action is appropriate",
    "is_anomaly": true or false,
    "similar_past_incidents": ["brief summaries of similar past issues if any"]
}"""


class AIAnalyzer:
    """AI-powered analyzer for daemon health issues.

    Uses Claude Code CLI to analyze health reports and recommend actions.
    Falls back to deterministic ErrorAnalyzer on failure.
    """

    def __init__(
        self,
        model: str = "sonnet",
        log_file: Path = DAEMON_LOG_FILE,
        timeout_seconds: int = 60,
    ):
        """Initialize the AI analyzer.

        Args:
            model: Claude model to use (sonnet, opus, haiku)
            log_file: Path to daemon log file for context
            timeout_seconds: Timeout for Claude CLI calls
        """
        self.model = model
        self.log_file = log_file
        self.timeout_seconds = timeout_seconds

        # Fallback analyzer
        self._error_analyzer = ErrorAnalyzer()

    def analyze(
        self,
        health_report: HealthReport,
        recent_incidents: Optional[list[Incident]] = None,
        session_context: Optional[dict] = None,
    ) -> AIAnalysis:
        """Analyze a health report and recommend an action.

        Args:
            health_report: The health report to analyze
            recent_incidents: Recent incidents for pattern matching
            session_context: Session information (uptime, restarts, etc.)

        Returns:
            AIAnalysis with diagnosis and recommendation
        """
        recent_incidents = recent_incidents or []
        session_context = session_context or {}

        # Build prompt
        prompt = self._build_prompt(health_report, recent_incidents, session_context)

        logger.info(
            "ai_analyzer_starting",
            status=health_report.status.value,
            issues=health_report.issues,
        )

        try:
            # Call Claude Code CLI
            result = subprocess.run(
                [
                    "claude",
                    "-p", prompt,
                    "--output-format", "text",
                    "--model", self.model,
                ],
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )

            if result.returncode != 0:
                logger.warning("ai_analyzer_cli_failed", stderr=result.stderr)
                return self._fallback_analysis(health_report)

            response_text = result.stdout.strip()

            # Parse JSON response
            analysis = self._parse_response(response_text)

            logger.info(
                "ai_analysis_complete",
                root_cause=analysis.root_cause,
                action=analysis.recommended_action,
                confidence=analysis.confidence,
            )

            return analysis

        except subprocess.TimeoutExpired:
            logger.warning("ai_analyzer_timeout", timeout=self.timeout_seconds)
            return self._fallback_analysis(health_report)

        except Exception as e:
            logger.error("ai_analyzer_error", error=str(e))
            return self._fallback_analysis(health_report)

    def _build_prompt(
        self,
        health_report: HealthReport,
        recent_incidents: list[Incident],
        session_context: dict,
    ) -> str:
        """Build the prompt for AI analysis."""
        lines = [AI_MONITOR_SYSTEM_PROMPT, "\n---\n"]

        # Current health status
        lines.append("## Current Health Status")
        lines.append(f"Status: {health_report.status.value.upper()}")
        lines.append(f"Timestamp: {health_report.timestamp.isoformat()}")

        if health_report.issues:
            lines.append(f"Issues: {', '.join(health_report.issues)}")
        else:
            lines.append("Issues: None")

        # Process info
        if health_report.process:
            lines.append("\n## Process Info")
            lines.append(f"Running: {health_report.process.running}")
            if health_report.process.pid:
                lines.append(f"PID: {health_report.process.pid}")
            if health_report.process.uptime_seconds:
                uptime_min = health_report.process.uptime_seconds / 60
                lines.append(f"Uptime: {uptime_min:.1f} minutes")
            if health_report.process.memory_mb:
                lines.append(f"Memory: {health_report.process.memory_mb:.1f} MB")

        # Heartbeat info
        if health_report.heartbeat:
            lines.append("\n## Heartbeat Info")
            lines.append(f"Connected to broker: {health_report.heartbeat.connected}")
            lines.append(f"Circuit breaker: {health_report.heartbeat.circuit_breaker_state}")
            lines.append(f"Pending orders: {health_report.heartbeat.pending_count}")
            lines.append(f"Error count: {health_report.heartbeat.error_count}")
            lines.append(f"Paper trading: {health_report.heartbeat.paper}")
        else:
            lines.append("\n## Heartbeat Info")
            lines.append("No heartbeat file found (daemon may not be running)")

        # Recent log entries
        log_lines = self._get_recent_logs(50)
        if log_lines:
            lines.append("\n## Recent Log Entries (last 50 lines)")
            lines.append("```")
            lines.extend(log_lines)
            lines.append("```")

        # Recent incidents
        if recent_incidents:
            lines.append("\n## Recent Incidents (last 24h)")
            for inc in recent_incidents[-10:]:  # Last 10
                lines.append(
                    f"- [{inc.timestamp[:16]}] {inc.health_status}: "
                    f"{inc.ai_diagnosis} -> {inc.action_taken} ({inc.outcome})"
                )
        else:
            lines.append("\n## Recent Incidents (last 24h)")
            lines.append("No recent incidents recorded.")

        # Session context
        lines.append("\n## Session Context")
        lines.append(f"Monitor uptime: {session_context.get('uptime_minutes', 0)} minutes")
        lines.append(f"Restarts today: {session_context.get('restarts_today', 0)}")
        lines.append(f"Errors today: {session_context.get('errors_today', 0)}")
        lines.append(f"Mode: {'paper' if session_context.get('paper', True) else 'live'}")
        lines.append(f"Consecutive unhealthy checks: {session_context.get('consecutive_unhealthy', 0)}")

        # Final instruction
        lines.append("\n## Your Task")
        lines.append("Analyze the situation and decide on the best action.")
        lines.append("Respond with ONLY valid JSON matching the specified format.")

        return "\n".join(lines)

    def _get_recent_logs(self, num_lines: int = 50) -> list[str]:
        """Get recent log lines."""
        if not self.log_file.exists():
            return []

        try:
            content = self.log_file.read_text()
            lines = content.strip().split("\n")
            return lines[-num_lines:]
        except Exception as e:
            logger.warning("log_read_error", error=str(e))
            return []

    def _parse_response(self, response_text: str) -> AIAnalysis:
        """Parse AI response into AIAnalysis."""
        # Handle markdown code blocks
        if "```json" in response_text:
            json_start = response_text.find("```json") + 7
            json_end = response_text.find("```", json_start)
            response_text = response_text[json_start:json_end].strip()
        elif "```" in response_text:
            json_start = response_text.find("```") + 3
            json_end = response_text.find("```", json_start)
            response_text = response_text[json_start:json_end].strip()

        try:
            data = json.loads(response_text)

            return AIAnalysis(
                root_cause=data.get("root_cause", "Unknown"),
                severity=data.get("severity", "error"),
                recommended_action=data.get("recommended_action", "wait"),
                confidence=float(data.get("confidence", 0.5)),
                reasoning=data.get("reasoning", ""),
                is_anomaly=data.get("is_anomaly", False),
                similar_past_incidents=data.get("similar_past_incidents", []),
            )

        except json.JSONDecodeError as e:
            logger.error("ai_response_parse_error", error=str(e), response=response_text[:200])
            # Return a safe default
            return AIAnalysis(
                root_cause="Failed to parse AI response",
                severity="warning",
                recommended_action="wait",
                confidence=0.0,
                reasoning=f"JSON parse error: {e}",
                is_anomaly=False,
                similar_past_incidents=[],
            )

    def _fallback_analysis(self, health_report: HealthReport) -> AIAnalysis:
        """Fall back to deterministic analysis when AI fails."""
        logger.info("ai_analyzer_fallback", reason="Using deterministic ErrorAnalyzer")

        # Use existing ErrorAnalyzer for classification
        errors = self._error_analyzer.analyze_health_report(health_report.issues)

        if not errors:
            return AIAnalysis(
                root_cause="No specific issues identified",
                severity="info",
                recommended_action="wait",
                confidence=0.5,
                reasoning="No errors detected, will continue monitoring",
                is_anomaly=False,
                similar_past_incidents=[],
            )

        # Use the most severe error
        most_severe = max(errors, key=lambda e: self._severity_rank(e.severity))

        # Map ErrorSeverity to string
        severity_map = {
            ErrorSeverity.INFO: "info",
            ErrorSeverity.WARNING: "warning",
            ErrorSeverity.ERROR: "error",
            ErrorSeverity.CRITICAL: "critical",
        }

        action = most_severe.fix_action or "wait"
        if not most_severe.is_auto_fixable:
            action = "escalate"

        return AIAnalysis(
            root_cause=most_severe.message,
            severity=severity_map.get(most_severe.severity, "error"),
            recommended_action=action,
            confidence=0.7,  # Deterministic is reasonably confident
            reasoning=most_severe.details or "Deterministic analysis (AI fallback)",
            is_anomaly=False,
            similar_past_incidents=[],
        )

    def _severity_rank(self, severity: ErrorSeverity) -> int:
        """Rank severity for comparison."""
        return {
            ErrorSeverity.INFO: 0,
            ErrorSeverity.WARNING: 1,
            ErrorSeverity.ERROR: 2,
            ErrorSeverity.CRITICAL: 3,
        }.get(severity, 1)
