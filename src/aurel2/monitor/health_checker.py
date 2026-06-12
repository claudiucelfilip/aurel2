"""Health checking for the daemon process."""

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional

import psutil
import structlog

logger = structlog.get_logger()

HEARTBEAT_FILE = Path.home() / ".aurel2" / "heartbeat.json"
DAEMON_LOG_FILE = Path.home() / ".aurel2" / "daemon.log"
STALE_THRESHOLD_MINUTES = 10


class HealthStatus(Enum):
    """Overall daemon health status."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"  # Running but with issues
    UNHEALTHY = "unhealthy"  # Not running or unresponsive
    UNKNOWN = "unknown"


@dataclass
class HeartbeatInfo:
    """Information from the heartbeat file."""

    timestamp: datetime
    connected: bool
    circuit_breaker_state: str
    pending_count: int
    last_check: Optional[datetime]
    paper: bool
    dry_run: bool
    error_count: int


@dataclass
class ProcessInfo:
    """Information about the daemon process."""

    running: bool
    pid: Optional[int] = None
    cpu_percent: Optional[float] = None
    memory_mb: Optional[float] = None
    uptime_seconds: Optional[float] = None


@dataclass
class LogErrors:
    """Recent errors from log files."""

    recent_errors: list[str] = field(default_factory=list)
    error_count: int = 0
    last_error_time: Optional[datetime] = None


@dataclass
class HealthReport:
    """Complete health report for the daemon."""

    status: HealthStatus
    timestamp: datetime
    process: ProcessInfo
    heartbeat: Optional[HeartbeatInfo]
    log_errors: LogErrors
    issues: list[str] = field(default_factory=list)


class HealthChecker:
    """Checks daemon health via process monitoring, heartbeat, and logs."""

    def __init__(
        self,
        heartbeat_file: Path = HEARTBEAT_FILE,
        log_file: Path = DAEMON_LOG_FILE,
        stale_threshold_minutes: int = STALE_THRESHOLD_MINUTES,
    ):
        self.heartbeat_file = heartbeat_file
        self.log_file = log_file
        self.stale_threshold = timedelta(minutes=stale_threshold_minutes)

    def check(self) -> HealthReport:
        """Perform a complete health check."""
        timestamp = datetime.now()
        issues: list[str] = []

        # Check process
        process = self._check_process()
        if not process.running:
            issues.append("Daemon process not running")

        # Check heartbeat
        heartbeat = self._check_heartbeat()
        if heartbeat is None:
            issues.append("No heartbeat file found")
        elif self._is_heartbeat_stale(heartbeat):
            issues.append(f"Heartbeat stale (>{self.stale_threshold.total_seconds() // 60} min)")
        elif not heartbeat.connected:
            issues.append("Broker disconnected")
        elif heartbeat.circuit_breaker_state == "open":
            issues.append("Circuit breaker is OPEN")

        # Check logs - only report if there are significant errors
        log_errors = self._check_logs()
        if log_errors.error_count >= 3:
            issues.append(f"{log_errors.error_count} recent errors in log")

        # Determine overall status
        status = self._determine_status(process, heartbeat, log_errors, issues)

        return HealthReport(
            status=status,
            timestamp=timestamp,
            process=process,
            heartbeat=heartbeat,
            log_errors=log_errors,
            issues=issues,
        )

    def _check_process(self) -> ProcessInfo:
        """Check if daemon process is running.

        First tries local process scan (when monitor runs on same host as daemon).
        Falls back to Docker container health check (when running as separate container).
        """
        # Try local process scan first
        for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
            try:
                cmdline = proc.info.get("cmdline") or []
                cmdline_str = " ".join(cmdline)

                # Look for the daemon process
                if "aurel2" in cmdline_str and "live" in cmdline_str:
                    # Get detailed info
                    with proc.oneshot():
                        cpu = proc.cpu_percent(interval=0.1)
                        mem = proc.memory_info().rss / (1024 * 1024)
                        create_time = proc.info.get("create_time", 0)
                        uptime = datetime.now().timestamp() - create_time

                    return ProcessInfo(
                        running=True,
                        pid=proc.info["pid"],
                        cpu_percent=cpu,
                        memory_mb=mem,
                        uptime_seconds=uptime,
                    )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        # Fall back to Docker container check (when running as separate container)
        return self._check_docker_container()

    def _check_docker_container(self) -> ProcessInfo:
        """Check if the aurel2 daemon container is running via Docker."""
        try:
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Status}} {{.State.Health.Status}}", "aurel2-trading-aurel2-1"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                parts = result.stdout.strip().split()
                status = parts[0] if parts else ""
                health = parts[1] if len(parts) > 1 else ""
                if status == "running":
                    return ProcessInfo(running=True)
                logger.warning("daemon_container_not_running", status=status, health=health)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        # If neither local process nor Docker container found, check heartbeat freshness
        # as a last resort — if heartbeat is recent, daemon is probably running somewhere
        if self.heartbeat_file.exists():
            try:
                data = json.loads(self.heartbeat_file.read_text())
                age = datetime.now().timestamp() - data.get("timestamp", 0)
                if age < 120:  # Heartbeat updated within last 2 minutes
                    return ProcessInfo(running=True)
            except (json.JSONDecodeError, KeyError):
                pass

        return ProcessInfo(running=False)

    def _check_heartbeat(self) -> Optional[HeartbeatInfo]:
        """Read and parse heartbeat file."""
        if not self.heartbeat_file.exists():
            return None

        try:
            data = json.loads(self.heartbeat_file.read_text())
            return HeartbeatInfo(
                timestamp=datetime.fromtimestamp(data["timestamp"]),
                connected=data.get("connected", False),
                circuit_breaker_state=data.get("circuit_breaker", {}).get("state", "unknown"),
                pending_count=data.get("pending_count", 0),
                last_check=datetime.fromisoformat(data["last_check"]) if data.get("last_check") else None,
                paper=data.get("paper", True),
                dry_run=data.get("dry_run", False),
                error_count=data.get("error_count", 0),
            )
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning("heartbeat_parse_error", error=str(e))
            return None

    def _is_heartbeat_stale(self, heartbeat: HeartbeatInfo) -> bool:
        """Check if heartbeat is older than threshold."""
        age = datetime.now() - heartbeat.timestamp
        return age > self.stale_threshold

    def _check_logs(self, lines_to_check: int = 100) -> LogErrors:
        """Check recent log entries for errors."""
        if not self.log_file.exists():
            return LogErrors()

        try:
            # Read last N lines
            content = self.log_file.read_text()
            lines = content.strip().split("\n")[-lines_to_check:]

            # Patterns that indicate real errors (not benign warnings)
            error_patterns = [
                r"circuit_breaker_opened",
                r"daemon.*crash",
                r"execution.*failed",
                r"order.*rejected",
                r"connection.*lost",
                r"Traceback",
                r"CRITICAL",
                r"notification_error",
                r"notification_failed",
                r"executor_buy_error",
                r"executor_sell_error",
                r"order_verification_failed",
            ]
            combined_pattern = "|".join(error_patterns)

            # Patterns to ignore (benign warnings, deprecation warnings, etc.)
            ignore_patterns = [
                r"DeprecationWarning",
                r"Pandas4Warning",
                r"FutureWarning",
            ]
            ignore_pattern = "|".join(ignore_patterns)

            recent_errors = []
            last_error_time = None

            for line in lines:
                # Skip benign warnings
                if re.search(ignore_pattern, line, re.IGNORECASE):
                    continue

                if re.search(combined_pattern, line, re.IGNORECASE):
                    recent_errors.append(line[:200])  # Truncate long lines

                    # Try to extract timestamp
                    ts_match = re.search(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}", line)
                    if ts_match:
                        try:
                            ts = datetime.fromisoformat(ts_match.group().replace(" ", "T"))
                            if last_error_time is None or ts > last_error_time:
                                last_error_time = ts
                        except ValueError:
                            pass

            return LogErrors(
                recent_errors=recent_errors[-10:],  # Keep last 10
                error_count=len(recent_errors),
                last_error_time=last_error_time,
            )
        except Exception as e:
            logger.warning("log_check_error", error=str(e))
            return LogErrors()

    def _determine_status(
        self,
        process: ProcessInfo,
        heartbeat: Optional[HeartbeatInfo],
        log_errors: LogErrors,
        issues: list[str],
    ) -> HealthStatus:
        """Determine overall health status."""
        if not process.running:
            return HealthStatus.UNHEALTHY

        if heartbeat is None:
            return HealthStatus.UNHEALTHY

        if self._is_heartbeat_stale(heartbeat):
            return HealthStatus.UNHEALTHY

        if not heartbeat.connected:
            return HealthStatus.DEGRADED

        if heartbeat.circuit_breaker_state == "open":
            return HealthStatus.DEGRADED

        if log_errors.error_count > 5:
            return HealthStatus.DEGRADED

        if len(issues) > 0:
            return HealthStatus.DEGRADED

        return HealthStatus.HEALTHY
