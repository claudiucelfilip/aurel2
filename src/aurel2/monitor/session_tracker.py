"""Session tracking for paper trading progress evaluation."""

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()

SESSION_FILE = Path("data/session_progress.json")
MAX_DAYS = 90  # 3 months of history


def session_path_for_mode(mode: str = "paper") -> Path:
    """Return session progress path for the given trading mode."""
    return Path(f"data/{mode}/session_progress.json")


@dataclass
class DailySession:
    """Daily session metrics."""

    date: str
    uptime_minutes: int = 0
    restarts: int = 0
    errors: int = 0
    decisions: int = 0
    executions: int = 0
    account_value: Optional[float] = None
    starting_value: Optional[float] = None
    daily_return: Optional[float] = None
    max_drawdown: Optional[float] = None
    win_count: int = 0
    loss_count: int = 0
    ai_agreed: int = 0
    ai_disagreed: int = 0


@dataclass
class SessionProgress:
    """Overall session progress metrics."""

    sessions: list[DailySession] = field(default_factory=list)
    start_date: Optional[str] = None
    initial_value: Optional[float] = None
    current_value: Optional[float] = None
    total_return: Optional[float] = None
    max_drawdown: float = 0.0
    total_trades: int = 0
    win_rate: Optional[float] = None
    ai_agreement_rate: Optional[float] = None
    total_restarts: int = 0
    total_errors: int = 0
    days_active: int = 0


class SessionTracker:
    """Tracks paper trading progress over time."""

    def __init__(self, session_file: Path = SESSION_FILE):
        self.session_file = session_file
        self._progress = self._load()

    def _load(self) -> SessionProgress:
        """Load session progress from file."""
        if not self.session_file.exists():
            return SessionProgress()

        try:
            data = json.loads(self.session_file.read_text())
            sessions = [DailySession(**s) for s in data.get("sessions", [])]
            return SessionProgress(
                sessions=sessions,
                start_date=data.get("start_date"),
                initial_value=data.get("initial_value"),
                current_value=data.get("current_value"),
                total_return=data.get("total_return"),
                max_drawdown=data.get("max_drawdown", 0.0),
                total_trades=data.get("total_trades", 0),
                win_rate=data.get("win_rate"),
                ai_agreement_rate=data.get("ai_agreement_rate"),
                total_restarts=data.get("total_restarts", 0),
                total_errors=data.get("total_errors", 0),
                days_active=data.get("days_active", 0),
            )
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("session_load_error", error=str(e))
            return SessionProgress()

    def _save(self) -> None:
        """Save session progress to file."""
        self.session_file.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "sessions": [asdict(s) for s in self._progress.sessions],
            "start_date": self._progress.start_date,
            "initial_value": self._progress.initial_value,
            "current_value": self._progress.current_value,
            "total_return": self._progress.total_return,
            "max_drawdown": self._progress.max_drawdown,
            "total_trades": self._progress.total_trades,
            "win_rate": self._progress.win_rate,
            "ai_agreement_rate": self._progress.ai_agreement_rate,
            "total_restarts": self._progress.total_restarts,
            "total_errors": self._progress.total_errors,
            "days_active": self._progress.days_active,
        }

        self.session_file.write_text(json.dumps(data, indent=2))

    def get_or_create_today(self) -> DailySession:
        """Get or create today's session."""
        today = date.today().isoformat()

        for session in self._progress.sessions:
            if session.date == today:
                return session

        # Create new session
        session = DailySession(date=today)
        self._progress.sessions.append(session)
        return session

    def record_uptime(self, minutes: int) -> None:
        """Record daemon uptime for today."""
        session = self.get_or_create_today()
        session.uptime_minutes += minutes
        self._save()

    def record_restart(self) -> None:
        """Record a daemon restart."""
        session = self.get_or_create_today()
        session.restarts += 1
        self._progress.total_restarts += 1
        self._save()

    def record_error(self) -> None:
        """Record an error occurrence."""
        session = self.get_or_create_today()
        session.errors += 1
        self._progress.total_errors += 1
        self._save()

    def record_decision(self, ai_agreed: bool = True) -> None:
        """Record a trading decision."""
        session = self.get_or_create_today()
        session.decisions += 1

        if ai_agreed:
            session.ai_agreed += 1
        else:
            session.ai_disagreed += 1

        self._recalculate_ai_agreement()
        self._save()

    def record_execution(self, profit: Optional[float] = None) -> None:
        """Record a trade execution."""
        session = self.get_or_create_today()
        session.executions += 1
        self._progress.total_trades += 1

        if profit is not None:
            if profit > 0:
                session.win_count += 1
            elif profit < 0:
                session.loss_count += 1

        self._recalculate_win_rate()
        self._save()

    def record_account_value(self, value: float) -> None:
        """Record current account value."""
        session = self.get_or_create_today()

        # Set starting value if not set
        if session.starting_value is None:
            session.starting_value = value

        session.account_value = value

        # Calculate daily return
        if session.starting_value and session.starting_value > 0:
            session.daily_return = (value - session.starting_value) / session.starting_value

        # Update overall progress
        self._progress.current_value = value

        if self._progress.initial_value is None:
            self._progress.initial_value = value
            self._progress.start_date = date.today().isoformat()

        # Calculate total return
        if self._progress.initial_value and self._progress.initial_value > 0:
            self._progress.total_return = (value - self._progress.initial_value) / self._progress.initial_value

        # Update max drawdown
        self._update_max_drawdown(value)

        self._save()

    def _update_max_drawdown(self, current_value: float) -> None:
        """Update maximum drawdown tracking."""
        # Find peak value from sessions
        peak_value = self._progress.initial_value or current_value

        for session in self._progress.sessions:
            if session.account_value and session.account_value > peak_value:
                peak_value = session.account_value

        if peak_value > 0 and current_value < peak_value:
            drawdown = (peak_value - current_value) / peak_value
            if drawdown > self._progress.max_drawdown:
                self._progress.max_drawdown = drawdown

    def _recalculate_win_rate(self) -> None:
        """Recalculate overall win rate."""
        total_wins = sum(s.win_count for s in self._progress.sessions)
        total_losses = sum(s.loss_count for s in self._progress.sessions)
        total = total_wins + total_losses

        if total > 0:
            self._progress.win_rate = total_wins / total
        else:
            self._progress.win_rate = None

    def _recalculate_ai_agreement(self) -> None:
        """Recalculate AI agreement rate."""
        total_agreed = sum(s.ai_agreed for s in self._progress.sessions)
        total_disagreed = sum(s.ai_disagreed for s in self._progress.sessions)
        total = total_agreed + total_disagreed

        if total > 0:
            self._progress.ai_agreement_rate = total_agreed / total
        else:
            self._progress.ai_agreement_rate = None

    def cleanup_old_sessions(self) -> int:
        """Remove sessions older than MAX_DAYS. Returns count removed."""
        cutoff = date.today() - timedelta(days=MAX_DAYS)
        cutoff_str = cutoff.isoformat()

        original_count = len(self._progress.sessions)
        self._progress.sessions = [
            s for s in self._progress.sessions
            if s.date >= cutoff_str
        ]

        removed = original_count - len(self._progress.sessions)
        if removed > 0:
            self._progress.days_active = len(self._progress.sessions)
            self._save()
            logger.info("cleaned_old_sessions", removed=removed)

        return removed

    def get_progress(self) -> SessionProgress:
        """Get current progress metrics."""
        self._progress.days_active = len(self._progress.sessions)
        return self._progress

    def get_summary(self) -> dict:
        """Get a summary for display."""
        progress = self.get_progress()

        # Calculate recent averages (last 7 days)
        recent_sessions = self._progress.sessions[-7:]

        avg_daily_return = None
        if recent_sessions:
            returns = [s.daily_return for s in recent_sessions if s.daily_return is not None]
            if returns:
                avg_daily_return = sum(returns) / len(returns)

        avg_uptime = None
        if recent_sessions:
            uptimes = [s.uptime_minutes for s in recent_sessions]
            avg_uptime = sum(uptimes) / len(uptimes)

        return {
            "start_date": progress.start_date,
            "days_active": progress.days_active,
            "initial_value": progress.initial_value,
            "current_value": progress.current_value,
            "total_return_pct": progress.total_return * 100 if progress.total_return else None,
            "max_drawdown_pct": progress.max_drawdown * 100,
            "total_trades": progress.total_trades,
            "win_rate_pct": progress.win_rate * 100 if progress.win_rate else None,
            "ai_agreement_pct": progress.ai_agreement_rate * 100 if progress.ai_agreement_rate else None,
            "total_restarts": progress.total_restarts,
            "total_errors": progress.total_errors,
            "avg_daily_return_pct": avg_daily_return * 100 if avg_daily_return else None,
            "avg_uptime_hours": avg_uptime / 60 if avg_uptime else None,
        }

    def get_recent_sessions(self, days: int = 7) -> list[DailySession]:
        """Get recent sessions."""
        return self._progress.sessions[-days:]
