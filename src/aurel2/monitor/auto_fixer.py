"""Automatic fixes for daemon issues."""

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import psutil
import structlog

logger = structlog.get_logger()


@dataclass
class FixAttempt:
    """Record of a fix attempt."""

    timestamp: datetime
    action: str
    success: bool
    message: str


@dataclass
class FixResult:
    """Result of a fix operation."""

    success: bool
    message: str
    action_taken: str
    attempts_remaining: int


class AutoFixer:
    """Handles automatic fixes for daemon issues."""

    MAX_ATTEMPTS_PER_ERROR = 3
    COOLDOWN_MINUTES = 5

    def __init__(self, paper: bool = True, dry_run: bool = False):
        self.paper = paper
        self.dry_run = dry_run
        self._attempts: dict[str, list[FixAttempt]] = {}
        self._last_fix_time: Optional[datetime] = None

    def fix(self, action: str, context: Optional[dict] = None) -> FixResult:
        """Execute a fix action."""
        context = context or {}

        # Check cooldown
        if self._in_cooldown():
            return FixResult(
                success=False,
                message=f"In cooldown period (wait {self.COOLDOWN_MINUTES} min between fixes)",
                action_taken="none",
                attempts_remaining=self._get_remaining_attempts(action),
            )

        # Check attempt limit
        remaining = self._get_remaining_attempts(action)
        if remaining <= 0:
            return FixResult(
                success=False,
                message=f"Max fix attempts ({self.MAX_ATTEMPTS_PER_ERROR}) reached for {action}",
                action_taken="none",
                attempts_remaining=0,
            )

        # Route to appropriate fix method
        if action == "restart_daemon":
            result = self._restart_daemon(context)
        elif action == "wait_and_restart":
            result = self._wait_and_restart(context)
        elif action == "kill_daemon":
            result = self._kill_daemon()
        elif action == "wait":
            result = self._wait(context)
        else:
            result = FixResult(
                success=False,
                message=f"Unknown fix action: {action}",
                action_taken=action,
                attempts_remaining=remaining,
            )

        # Record attempt
        self._record_attempt(action, result.success, result.message)
        self._last_fix_time = datetime.now()

        return FixResult(
            success=result.success,
            message=result.message,
            action_taken=result.action_taken,
            attempts_remaining=remaining - 1,
        )

    def reset_attempts(self, action: Optional[str] = None) -> None:
        """Reset attempt counts after successful recovery."""
        if action:
            self._attempts.pop(action, None)
        else:
            self._attempts.clear()
        logger.info("auto_fixer_attempts_reset", action=action or "all")

    def get_stats(self) -> dict:
        """Get fix attempt statistics."""
        return {
            "attempts": {
                action: len(attempts)
                for action, attempts in self._attempts.items()
            },
            "last_fix": self._last_fix_time.isoformat() if self._last_fix_time else None,
            "in_cooldown": self._in_cooldown(),
        }

    def _restart_daemon(self, context: dict) -> FixResult:
        """Kill existing daemon and start a new one."""
        logger.info("auto_fixer_restarting_daemon", paper=self.paper, dry_run=self.dry_run)

        # Kill existing daemon
        kill_result = self._kill_daemon()
        if not kill_result.success:
            logger.warning("kill_daemon_failed", message=kill_result.message)
            # Continue anyway - process might already be dead

        # Wait for process to fully terminate
        time.sleep(2)

        # Ensure IB Gateway is running before starting daemon
        self._ensure_gateway_running()

        # Start new daemon
        return self._start_daemon()

    def _ensure_gateway_running(self) -> bool:
        """Check if IB Gateway is running, launch it if not."""
        # Check if IB Gateway process is running
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                name = proc.info.get("name", "").lower()
                cmdline = " ".join(proc.info.get("cmdline") or []).lower()
                if "ib gateway" in name or "ibgateway" in name or "ib gateway" in cmdline:
                    logger.info("gateway_already_running", pid=proc.info["pid"])
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        # IB Gateway not running, try to launch it
        logger.info("auto_fixer_launching_gateway")
        gateway_paths = [
            Path("/Applications/IB Gateway 10.19/IB Gateway 10.19.app"),
            Path("/Applications/IB Gateway/IB Gateway.app"),
            Path.home() / "Applications" / "IB Gateway" / "IB Gateway.app",
        ]

        for gateway_path in gateway_paths:
            if gateway_path.exists():
                try:
                    subprocess.Popen(["open", str(gateway_path)])
                    logger.info("gateway_launched", path=str(gateway_path))
                    # Give IB Gateway time to start
                    time.sleep(10)
                    return True
                except Exception as e:
                    logger.warning("gateway_launch_failed", path=str(gateway_path), error=str(e))

        logger.warning("gateway_not_found")
        return False

    def _wait(self, context: dict) -> FixResult:
        """Wait and observe - AI decided not to take action yet.

        This is used when the AI wants to gather more data before
        deciding on an action. No actual fix is performed.
        """
        reason = context.get("reason", "AI decided to wait and observe")

        logger.info("auto_fixer_waiting_observation", reason=reason)

        return FixResult(
            success=True,
            message=f"Waiting to observe: {reason}",
            action_taken="wait",
            attempts_remaining=0,  # Will be set by caller
        )

    def _wait_and_restart(self, context: dict) -> FixResult:
        """Wait for circuit breaker reset, then restart."""
        wait_seconds = context.get("wait_seconds", 300)  # 5 minutes default

        logger.info("auto_fixer_waiting", seconds=wait_seconds)

        # Wait for circuit breaker cooldown
        time.sleep(min(wait_seconds, 60))  # Cap at 60s for responsiveness

        return self._restart_daemon(context)

    def _kill_daemon(self) -> FixResult:
        """Kill any running daemon processes."""
        killed = 0

        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cmdline = proc.info.get("cmdline") or []
                cmdline_str = " ".join(cmdline)

                if "aurel2" in cmdline_str and "live" in cmdline_str:
                    pid = proc.info["pid"]

                    # Don't kill ourselves if we're somehow in the daemon
                    if pid == os.getpid():
                        continue

                    logger.info("killing_daemon_process", pid=pid)
                    os.kill(pid, signal.SIGTERM)
                    killed += 1

                    # Wait a moment for graceful shutdown
                    time.sleep(1)

                    # Force kill if still running
                    if psutil.pid_exists(pid):
                        os.kill(pid, signal.SIGKILL)

            except (psutil.NoSuchProcess, psutil.AccessDenied, ProcessLookupError):
                continue

        if killed > 0:
            return FixResult(
                success=True,
                message=f"Killed {killed} daemon process(es)",
                action_taken="kill_daemon",
                attempts_remaining=0,  # Will be set by caller
            )
        else:
            return FixResult(
                success=True,  # No daemon to kill is also success
                message="No daemon process found to kill",
                action_taken="kill_daemon",
                attempts_remaining=0,
            )

    def _start_daemon(self) -> FixResult:
        """Start a new daemon process."""
        logger.info("auto_fixer_starting_daemon", paper=self.paper, dry_run=self.dry_run)

        # Build command
        python_path = sys.executable
        cmd = [python_path, "-m", "aurel2.cli", "live"]

        if self.paper:
            cmd.append("--paper")
        else:
            cmd.append("--real")

        if self.dry_run:
            cmd.append("--dry-run")

        try:
            # Create log directory
            log_dir = Path.home() / ".aurel2"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / "daemon.log"

            # Start daemon with nohup-like behavior
            with open(log_file, "a") as f:
                process = subprocess.Popen(
                    cmd,
                    stdout=f,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,  # Detach from current session
                )

            logger.info("daemon_started", pid=process.pid)

            # Give it a moment to start
            time.sleep(3)

            # Check if it's still running
            if process.poll() is None:
                return FixResult(
                    success=True,
                    message=f"Daemon started successfully (PID: {process.pid})",
                    action_taken="start_daemon",
                    attempts_remaining=0,
                )
            else:
                return FixResult(
                    success=False,
                    message=f"Daemon exited immediately with code {process.returncode}",
                    action_taken="start_daemon",
                    attempts_remaining=0,
                )

        except Exception as e:
            logger.error("daemon_start_failed", error=str(e))
            return FixResult(
                success=False,
                message=f"Failed to start daemon: {e}",
                action_taken="start_daemon",
                attempts_remaining=0,
            )

    def _in_cooldown(self) -> bool:
        """Check if we're in cooldown period."""
        if self._last_fix_time is None:
            return False

        elapsed = datetime.now() - self._last_fix_time
        return elapsed < timedelta(minutes=self.COOLDOWN_MINUTES)

    def _get_remaining_attempts(self, action: str) -> int:
        """Get remaining attempts for an action."""
        attempts = len(self._attempts.get(action, []))
        return max(0, self.MAX_ATTEMPTS_PER_ERROR - attempts)

    def _record_attempt(self, action: str, success: bool, message: str) -> None:
        """Record a fix attempt."""
        if action not in self._attempts:
            self._attempts[action] = []

        self._attempts[action].append(FixAttempt(
            timestamp=datetime.now(),
            action=action,
            success=success,
            message=message,
        ))

        logger.info(
            "fix_attempt_recorded",
            action=action,
            success=success,
            total_attempts=len(self._attempts[action]),
        )
