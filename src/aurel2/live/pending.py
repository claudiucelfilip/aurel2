"""Pending decisions manager - tracks approvals and handles timeouts."""

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from enum import Enum

import httpx
import structlog

logger = structlog.get_logger()

DEFAULT_PENDING_FILE = "data/pending_decisions.json"


class PendingStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    EXECUTED = "executed"


class DecisionUrgency(str, Enum):
    ROUTINE = "routine"
    NON_ROUTINE = "non_routine"
    URGENT = "urgent"


@dataclass
class PendingDecision:
    """A decision awaiting approval or execution."""

    id: str
    created_at: str  # ISO format
    urgency: str  # DecisionUrgency value
    action: str  # buy, sell, hold
    symbol: Optional[str]
    reasoning: str
    confidence: float
    status: str = PendingStatus.PENDING.value
    executed_at: Optional[str] = None
    approval_url: Optional[str] = None

    # Rich context for display
    deterministic_action: Optional[str] = None
    deterministic_asset: Optional[str] = None
    ai_agrees: bool = True
    ai_action: Optional[str] = None
    ai_asset: Optional[str] = None
    ai_reasoning: Optional[str] = None
    strategies: list = field(default_factory=list)
    market_regime: Optional[str] = None
    current_holding: Optional[str] = None

    def timeout_seconds(self) -> int:
        """Get timeout in seconds based on urgency."""
        # Both NON_ROUTINE and URGENT have 1 hour timeout
        return 3600  # 1 hour

    def is_timed_out(self) -> bool:
        """Check if this decision has timed out."""
        if self.status != PendingStatus.PENDING.value:
            return False

        created = datetime.fromisoformat(self.created_at)
        timeout = timedelta(seconds=self.timeout_seconds())
        return datetime.now() > created + timeout

    def time_remaining(self) -> timedelta:
        """Get time remaining before timeout."""
        created = datetime.fromisoformat(self.created_at)
        timeout = timedelta(seconds=self.timeout_seconds())
        deadline = created + timeout
        remaining = deadline - datetime.now()
        return max(remaining, timedelta(0))

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "PendingDecision":
        """Create from dictionary."""
        return cls(**data)


class PendingManager:
    """
    Manages pending decisions - creation, polling, timeout handling.

    Persists to JSON file for daemon restart resilience.
    """

    def __init__(
        self,
        pending_file: str = DEFAULT_PENDING_FILE,
        approval_base_url: str = "https://approval-endpoint.vercel.app/api/decision",
    ):
        self.pending_file = Path(pending_file)
        self.approval_base_url = approval_base_url
        self.decisions: dict[str, PendingDecision] = {}

        # Ensure data directory exists
        self.pending_file.parent.mkdir(parents=True, exist_ok=True)

        # Load existing decisions
        self._load()

    def create_decision(
        self,
        urgency: DecisionUrgency,
        action: str,
        symbol: Optional[str],
        reasoning: str,
        confidence: float,
        deterministic_action: Optional[str] = None,
        deterministic_asset: Optional[str] = None,
        ai_agrees: bool = True,
        ai_action: Optional[str] = None,
        ai_asset: Optional[str] = None,
        ai_reasoning: Optional[str] = None,
        strategies: Optional[list] = None,
        market_regime: Optional[str] = None,
        current_holding: Optional[str] = None,
    ) -> PendingDecision:
        """Create a new pending decision."""
        decision_id = str(uuid.uuid4())[:8]

        decision = PendingDecision(
            id=decision_id,
            created_at=datetime.now().isoformat(),
            urgency=urgency.value,
            action=action,
            symbol=symbol,
            reasoning=reasoning,
            confidence=confidence,
            status=PendingStatus.PENDING.value,
            approval_url=f"{self.approval_base_url}/{decision_id}",
            deterministic_action=deterministic_action,
            deterministic_asset=deterministic_asset,
            ai_agrees=ai_agrees,
            ai_action=ai_action,
            ai_asset=ai_asset,
            ai_reasoning=ai_reasoning,
            strategies=strategies or [],
            market_regime=market_regime,
            current_holding=current_holding,
        )

        self.decisions[decision_id] = decision
        self._save()

        logger.info(
            "pending_decision_created",
            id=decision_id,
            urgency=urgency.value,
            action=action,
            symbol=symbol,
            timeout_seconds=decision.timeout_seconds(),
        )

        return decision

    async def post_to_approval_endpoint(self, decision: PendingDecision) -> bool:
        """Post decision to Vercel approval endpoint."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    decision.approval_url,
                    json={
                        "action": decision.action,
                        "symbol": decision.symbol or "HOLD",
                        "reasoning": decision.reasoning[:500],
                        "confidence": decision.confidence,
                        "deterministic_action": decision.deterministic_action,
                        "deterministic_asset": decision.deterministic_asset,
                        "ai_agrees": decision.ai_agrees,
                        "ai_action": decision.ai_action,
                        "ai_asset": decision.ai_asset,
                        "ai_reasoning": decision.ai_reasoning[:300] if decision.ai_reasoning else None,
                        "strategies_agree": all(
                            s.get("action") == decision.action.upper()
                            for s in decision.strategies
                        ) if decision.strategies else True,
                        "strategies": decision.strategies,
                        "market_regime": decision.market_regime,
                        "current_holding": decision.current_holding,
                    },
                    timeout=10.0,
                )

                if response.status_code == 201:
                    logger.info("pending_posted_to_endpoint", id=decision.id)
                    return True
                else:
                    logger.warning(
                        "pending_post_failed",
                        id=decision.id,
                        status=response.status_code,
                        body=response.text[:200],
                    )
                    return False

        except Exception as e:
            logger.error("pending_post_error", id=decision.id, error=str(e))
            return False

    async def poll_approval_status(self, decision: PendingDecision) -> Optional[str]:
        """
        Poll Vercel endpoint for approval status.

        Returns: "approved", "rejected", or None if still pending.
        """
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    decision.approval_url,
                    headers={"Accept": "application/json"},
                    timeout=10.0,
                )

                if response.status_code == 200:
                    data = response.json()
                    status = data.get("status")

                    if status in ["approved", "rejected"]:
                        logger.info(
                            "pending_status_changed",
                            id=decision.id,
                            status=status,
                        )
                        return status

                return None

        except Exception as e:
            logger.warning("pending_poll_error", id=decision.id, error=str(e))
            return None

    async def check_all_pending(self) -> list[tuple[PendingDecision, str]]:
        """
        Check all pending decisions for status changes or timeouts.

        Returns list of (decision, new_status) tuples for decisions that need action.
        """
        results = []

        for decision in list(self.decisions.values()):
            if decision.status != PendingStatus.PENDING.value:
                continue

            # Check for timeout first
            if decision.is_timed_out():
                decision.status = PendingStatus.TIMEOUT.value
                self._save()
                results.append((decision, PendingStatus.TIMEOUT.value))
                logger.info("pending_timed_out", id=decision.id)
                continue

            # Poll endpoint for approval/rejection
            new_status = await self.poll_approval_status(decision)
            if new_status:
                decision.status = new_status
                self._save()
                results.append((decision, new_status))

        return results

    def mark_executed(self, decision_id: str) -> None:
        """Mark a decision as executed."""
        if decision_id in self.decisions:
            self.decisions[decision_id].status = PendingStatus.EXECUTED.value
            self.decisions[decision_id].executed_at = datetime.now().isoformat()
            self._save()
            logger.info("pending_marked_executed", id=decision_id)

    def remove_decision(self, decision_id: str) -> None:
        """Remove a decision from tracking."""
        if decision_id in self.decisions:
            del self.decisions[decision_id]
            self._save()
            logger.info("pending_removed", id=decision_id)

    def get_pending(self) -> list[PendingDecision]:
        """Get all pending decisions."""
        return [
            d for d in self.decisions.values()
            if d.status == PendingStatus.PENDING.value
        ]

    def _save(self) -> None:
        """Save decisions to file."""
        data = {
            "decisions": {
                id: d.to_dict() for id, d in self.decisions.items()
            }
        }

        with open(self.pending_file, "w") as f:
            json.dump(data, f, indent=2)

    def _load(self) -> None:
        """Load decisions from file."""
        if not self.pending_file.exists():
            self.decisions = {}
            return

        try:
            with open(self.pending_file) as f:
                data = json.load(f)

            self.decisions = {
                id: PendingDecision.from_dict(d)
                for id, d in data.get("decisions", {}).items()
            }

            logger.info("pending_loaded", count=len(self.decisions))

        except Exception as e:
            logger.error("pending_load_error", error=str(e))
            self.decisions = {}
