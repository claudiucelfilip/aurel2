"""Incident tracking for AI-enhanced monitoring.

Tracks past incidents, their diagnoses, actions taken, and outcomes
to provide pattern history for AI analysis.
"""

import json
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()

INCIDENTS_FILE = Path("data/monitor_incidents.json")
MAX_INCIDENTS = 500  # Keep last 500 incidents


@dataclass
class Incident:
    """Record of a monitoring incident."""

    id: str
    timestamp: str  # ISO format
    health_status: str  # healthy, degraded, unhealthy
    issues: list[str]
    ai_diagnosis: str
    action_taken: str  # restart_daemon, wait_and_restart, wait, escalate
    outcome: str = "pending"  # pending, resolved, escalated, recurring
    resolution_time_seconds: Optional[int] = None
    ai_confidence: float = 0.0
    ai_reasoning: str = ""
    similar_to_past: list[str] = field(default_factory=list)


class IncidentTracker:
    """Tracks and stores monitoring incidents for pattern learning."""

    def __init__(self, incidents_file: Path = INCIDENTS_FILE):
        self.incidents_file = incidents_file
        self._incidents: list[Incident] = []
        self._load()

    def _load(self) -> None:
        """Load incidents from file."""
        if not self.incidents_file.exists():
            self._incidents = []
            return

        try:
            data = json.loads(self.incidents_file.read_text())
            self._incidents = [
                Incident(
                    id=inc.get("id", str(uuid.uuid4())),
                    timestamp=inc["timestamp"],
                    health_status=inc["health_status"],
                    issues=inc.get("issues", []),
                    ai_diagnosis=inc.get("ai_diagnosis", ""),
                    action_taken=inc.get("action_taken", "unknown"),
                    outcome=inc.get("outcome", "pending"),
                    resolution_time_seconds=inc.get("resolution_time_seconds"),
                    ai_confidence=inc.get("ai_confidence", 0.0),
                    ai_reasoning=inc.get("ai_reasoning", ""),
                    similar_to_past=inc.get("similar_to_past", []),
                )
                for inc in data.get("incidents", [])
            ]
            logger.info("incidents_loaded", count=len(self._incidents))
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("incidents_load_error", error=str(e))
            self._incidents = []

    def _save(self) -> None:
        """Save incidents to file."""
        self.incidents_file.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "incidents": [asdict(inc) for inc in self._incidents],
            "last_updated": datetime.now().isoformat(),
        }

        self.incidents_file.write_text(json.dumps(data, indent=2))

    def record_incident(
        self,
        health_status: str,
        issues: list[str],
        ai_diagnosis: str,
        action_taken: str = "pending",
        ai_confidence: float = 0.0,
        ai_reasoning: str = "",
        similar_to_past: Optional[list[str]] = None,
    ) -> str:
        """Record a new incident.

        Args:
            health_status: The health status (degraded, unhealthy, etc.)
            issues: List of issue descriptions from health report
            ai_diagnosis: AI's root cause analysis
            action_taken: The fix action taken
            ai_confidence: AI's confidence in diagnosis (0.0-1.0)
            ai_reasoning: AI's reasoning explanation
            similar_to_past: List of similar past incident descriptions

        Returns:
            The incident ID
        """
        incident_id = str(uuid.uuid4())[:8]

        incident = Incident(
            id=incident_id,
            timestamp=datetime.now().isoformat(),
            health_status=health_status,
            issues=issues,
            ai_diagnosis=ai_diagnosis,
            action_taken=action_taken,
            ai_confidence=ai_confidence,
            ai_reasoning=ai_reasoning,
            similar_to_past=similar_to_past or [],
        )

        self._incidents.append(incident)

        # Prune old incidents if too many
        if len(self._incidents) > MAX_INCIDENTS:
            self._incidents = self._incidents[-MAX_INCIDENTS:]

        self._save()

        logger.info(
            "incident_recorded",
            incident_id=incident_id,
            health_status=health_status,
            issues=issues,
            action_taken=action_taken,
        )

        return incident_id

    def update_outcome(
        self,
        incident_id: str,
        outcome: str,
        resolution_time_seconds: Optional[int] = None,
    ) -> bool:
        """Update the outcome of an incident.

        Args:
            incident_id: The incident ID to update
            outcome: The outcome (resolved, escalated, recurring)
            resolution_time_seconds: Time to resolution in seconds

        Returns:
            True if incident was found and updated, False otherwise
        """
        for incident in self._incidents:
            if incident.id == incident_id:
                incident.outcome = outcome
                if resolution_time_seconds is not None:
                    incident.resolution_time_seconds = resolution_time_seconds
                self._save()
                logger.info(
                    "incident_outcome_updated",
                    incident_id=incident_id,
                    outcome=outcome,
                    resolution_time=resolution_time_seconds,
                )
                return True

        logger.warning("incident_not_found", incident_id=incident_id)
        return False

    def get_recent_incidents(self, hours: int = 24) -> list[Incident]:
        """Get incidents from the last N hours.

        Args:
            hours: Number of hours to look back

        Returns:
            List of recent incidents
        """
        cutoff = datetime.now() - timedelta(hours=hours)
        cutoff_str = cutoff.isoformat()

        return [
            inc for inc in self._incidents
            if inc.timestamp >= cutoff_str
        ]

    def get_similar_incidents(
        self,
        issues: list[str],
        limit: int = 5,
    ) -> list[Incident]:
        """Find incidents with similar issues.

        Uses simple keyword matching to find similar past incidents.

        Args:
            issues: Current issue descriptions
            limit: Maximum number of similar incidents to return

        Returns:
            List of similar past incidents, most recent first
        """
        if not issues:
            return []

        # Build keyword set from current issues
        current_keywords = set()
        for issue in issues:
            # Extract significant words (lowercase, 4+ chars)
            words = issue.lower().split()
            current_keywords.update(w for w in words if len(w) >= 4)

        # Score past incidents by keyword overlap
        scored_incidents: list[tuple[float, Incident]] = []

        for incident in self._incidents:
            incident_keywords = set()
            for issue in incident.issues:
                words = issue.lower().split()
                incident_keywords.update(w for w in words if len(w) >= 4)

            # Calculate Jaccard similarity
            if incident_keywords:
                intersection = len(current_keywords & incident_keywords)
                union = len(current_keywords | incident_keywords)
                similarity = intersection / union if union > 0 else 0

                if similarity > 0.3:  # Only include reasonably similar
                    scored_incidents.append((similarity, incident))

        # Sort by similarity (descending), then by timestamp (most recent first)
        scored_incidents.sort(key=lambda x: (-x[0], x[1].timestamp), reverse=True)

        return [inc for _, inc in scored_incidents[:limit]]

    def get_incident(self, incident_id: str) -> Optional[Incident]:
        """Get a specific incident by ID.

        Args:
            incident_id: The incident ID

        Returns:
            The incident if found, None otherwise
        """
        for incident in self._incidents:
            if incident.id == incident_id:
                return incident
        return None

    def get_pending_incidents(self) -> list[Incident]:
        """Get all incidents with pending outcome.

        Returns:
            List of pending incidents
        """
        return [inc for inc in self._incidents if inc.outcome == "pending"]

    def get_stats(self) -> dict:
        """Get incident statistics.

        Returns:
            Dictionary with incident stats
        """
        total = len(self._incidents)
        if total == 0:
            return {
                "total_incidents": 0,
                "resolved": 0,
                "escalated": 0,
                "recurring": 0,
                "pending": 0,
                "avg_resolution_time_seconds": None,
            }

        outcomes = {
            "resolved": 0,
            "escalated": 0,
            "recurring": 0,
            "pending": 0,
        }

        resolution_times = []

        for incident in self._incidents:
            outcomes[incident.outcome] = outcomes.get(incident.outcome, 0) + 1
            if incident.resolution_time_seconds is not None:
                resolution_times.append(incident.resolution_time_seconds)

        avg_resolution = None
        if resolution_times:
            avg_resolution = sum(resolution_times) / len(resolution_times)

        return {
            "total_incidents": total,
            "resolved": outcomes["resolved"],
            "escalated": outcomes["escalated"],
            "recurring": outcomes["recurring"],
            "pending": outcomes["pending"],
            "avg_resolution_time_seconds": avg_resolution,
        }

    def to_prompt_text(self, max_incidents: int = 10) -> str:
        """Format recent incidents as text for AI prompt.

        Args:
            max_incidents: Maximum number of incidents to include

        Returns:
            Formatted text for AI prompt
        """
        recent = self.get_recent_incidents(hours=48)[-max_incidents:]

        if not recent:
            return "No recent incidents recorded."

        lines = [f"RECENT INCIDENTS (last 48 hours, {len(recent)} total):"]

        for inc in recent:
            ts = datetime.fromisoformat(inc.timestamp)
            lines.append(f"\n[{ts.strftime('%Y-%m-%d %H:%M')}] {inc.health_status.upper()}")
            lines.append(f"  Issues: {', '.join(inc.issues)}")
            lines.append(f"  Diagnosis: {inc.ai_diagnosis}")
            lines.append(f"  Action: {inc.action_taken} -> Outcome: {inc.outcome}")
            if inc.resolution_time_seconds:
                lines.append(f"  Resolution time: {inc.resolution_time_seconds}s")

        return "\n".join(lines)

    def cleanup_old_incidents(self, days: int = 30) -> int:
        """Remove incidents older than N days.

        Args:
            days: Number of days to keep

        Returns:
            Number of incidents removed
        """
        cutoff = datetime.now() - timedelta(days=days)
        cutoff_str = cutoff.isoformat()

        original_count = len(self._incidents)
        self._incidents = [
            inc for inc in self._incidents
            if inc.timestamp >= cutoff_str
        ]

        removed = original_count - len(self._incidents)
        if removed > 0:
            self._save()
            logger.info("old_incidents_cleaned", removed=removed)

        return removed
