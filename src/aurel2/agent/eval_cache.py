"""Evaluation cache for storing AI decisions and comparisons.

This module provides caching for:
- AI decisions (keyed by input hash)
- Comparison results between deterministic and AI approaches
- Outcome tracking for measuring which approach was better
"""

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger()


@dataclass
class DecisionRecord:
    """Record of a decision made by both deterministic and AI approaches.

    Attributes:
        date: Date the decision was made for.
        input_hash: Hash of the input (signals + context) for cache lookup.
        strategy_signals: What each strategy recommended.
        market_context_summary: Summary of market context.
        current_holding: What we currently hold.
        deterministic_decision: What weighted voting decided.
        ai_decision: What the AI decided (if evaluated).
        ai_reasoning: The AI's explanation.
        ai_model: Which model was used.
        agreed: Whether deterministic and AI agreed.
        outcome_1w: Return after 1 week (filled in later).
        outcome_deterministic_1w: Outcome if we followed deterministic.
        outcome_ai_1w: Outcome if we followed AI.
        created_at: When this record was created.
    """

    date: date
    input_hash: str
    strategy_signals: dict[str, Any]
    market_context_summary: str
    current_holding: str | None
    deterministic_decision: dict[str, Any]
    ai_decision: dict[str, Any] | None = None
    ai_reasoning: str | None = None
    ai_model: str | None = None
    agreed: bool | None = None
    outcome_1w: float | None = None
    outcome_deterministic_1w: float | None = None
    outcome_ai_1w: float | None = None
    created_at: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        d = asdict(self)
        d["date"] = self.date.isoformat()
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "DecisionRecord":
        """Create from dictionary."""
        data = data.copy()
        if isinstance(data.get("date"), str):
            data["date"] = date.fromisoformat(data["date"])
        return cls(**data)


@dataclass
class ComparisonSummary:
    """Summary of comparisons between deterministic and AI approaches.

    Attributes:
        period_start: Start of evaluation period.
        period_end: End of evaluation period.
        total_decisions: Total number of decisions evaluated.
        agreements: How many times they agreed.
        disagreements: How many times they disagreed.
        ai_better_when_disagreed: Times AI was better when they disagreed.
        deterministic_better_when_disagreed: Times deterministic was better.
        ai_cumulative_return: Cumulative return following AI decisions.
        deterministic_cumulative_return: Cumulative return following deterministic.
        decisions: List of individual decision records.
    """

    period_start: date | None = None
    period_end: date | None = None
    total_decisions: int = 0
    agreements: int = 0
    disagreements: int = 0
    ai_better_when_disagreed: int = 0
    deterministic_better_when_disagreed: int = 0
    ai_cumulative_return: float = 0.0
    deterministic_cumulative_return: float = 0.0
    decisions: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "total_decisions": self.total_decisions,
            "agreements": self.agreements,
            "disagreements": self.disagreements,
            "ai_better_when_disagreed": self.ai_better_when_disagreed,
            "deterministic_better_when_disagreed": self.deterministic_better_when_disagreed,
            "ai_cumulative_return": self.ai_cumulative_return,
            "deterministic_cumulative_return": self.deterministic_cumulative_return,
            "agreement_rate": self.agreements / self.total_decisions if self.total_decisions > 0 else 0,
            "ai_win_rate_on_disagreements": (
                self.ai_better_when_disagreed / self.disagreements
                if self.disagreements > 0
                else 0
            ),
            "decisions": self.decisions,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ComparisonSummary":
        """Create from dictionary."""
        return cls(
            period_start=date.fromisoformat(data["period_start"]) if data.get("period_start") else None,
            period_end=date.fromisoformat(data["period_end"]) if data.get("period_end") else None,
            total_decisions=data.get("total_decisions", 0),
            agreements=data.get("agreements", 0),
            disagreements=data.get("disagreements", 0),
            ai_better_when_disagreed=data.get("ai_better_when_disagreed", 0),
            deterministic_better_when_disagreed=data.get("deterministic_better_when_disagreed", 0),
            ai_cumulative_return=data.get("ai_cumulative_return", 0.0),
            deterministic_cumulative_return=data.get("deterministic_cumulative_return", 0.0),
            decisions=data.get("decisions", []),
        )


class EvalCache:
    """Cache for AI evaluation decisions and comparisons.

    Stores:
    - Individual decisions keyed by input hash
    - Comparison summaries
    - Outcome tracking
    """

    def __init__(self, cache_dir: Path | None = None):
        """Initialize the evaluation cache.

        Args:
            cache_dir: Directory to store cache files. If None, uses
                       data/ai_eval_cache/
        """
        if cache_dir is None:
            cache_dir = Path("data/ai_eval_cache")
        self.cache_dir = cache_dir
        self.decisions_dir = cache_dir / "decisions"
        self.results_dir = cache_dir / "results"

        # Create directories
        self.decisions_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def compute_input_hash(
        signals: dict[str, Any],
        context_summary: str,
        current_holding: str | None,
    ) -> str:
        """Compute a hash of the decision inputs.

        This allows us to cache decisions and reuse them when the
        same inputs are encountered again.

        Args:
            signals: Strategy signals dictionary.
            context_summary: Market context summary string.
            current_holding: Current holding symbol.

        Returns:
            SHA256 hash of the inputs (first 16 chars).
        """
        # Create a deterministic string representation
        input_str = json.dumps(
            {
                "signals": signals,
                "context": context_summary,
                "holding": current_holding,
            },
            sort_keys=True,
        )
        return hashlib.sha256(input_str.encode()).hexdigest()[:16]

    def _get_decision_path(self, target_date: date, input_hash: str) -> Path:
        """Get the path for a decision cache file."""
        return self.decisions_dir / f"{target_date.isoformat()}_{input_hash}.json"

    def get_cached_decision(
        self,
        target_date: date,
        input_hash: str,
    ) -> DecisionRecord | None:
        """Get a cached decision if it exists.

        Args:
            target_date: Date of the decision.
            input_hash: Hash of the inputs.

        Returns:
            DecisionRecord if found, None otherwise.
        """
        path = self._get_decision_path(target_date, input_hash)
        if path.exists():
            try:
                with open(path) as f:
                    data = json.load(f)
                logger.info("loaded_decision_from_cache", date=str(target_date), hash=input_hash)
                return DecisionRecord.from_dict(data)
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning("decision_cache_load_failed", error=str(e))
        return None

    def save_decision(self, record: DecisionRecord) -> None:
        """Save a decision record to cache.

        Args:
            record: The decision record to save.
        """
        path = self._get_decision_path(record.date, record.input_hash)
        with open(path, "w") as f:
            json.dump(record.to_dict(), f, indent=2)
        logger.info("saved_decision_to_cache", date=str(record.date), hash=record.input_hash)

    def get_all_decisions(self) -> list[DecisionRecord]:
        """Get all cached decisions.

        Returns:
            List of all decision records.
        """
        decisions = []
        for path in sorted(self.decisions_dir.glob("*.json")):
            try:
                with open(path) as f:
                    data = json.load(f)
                decisions.append(DecisionRecord.from_dict(data))
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning("decision_load_failed", path=str(path), error=str(e))
        return decisions

    def get_decisions_for_period(
        self,
        start_date: date,
        end_date: date,
    ) -> list[DecisionRecord]:
        """Get decisions for a specific period.

        Args:
            start_date: Start of period.
            end_date: End of period.

        Returns:
            List of decision records in the period.
        """
        all_decisions = self.get_all_decisions()
        return [d for d in all_decisions if start_date <= d.date <= end_date]

    def update_outcome(
        self,
        target_date: date,
        input_hash: str,
        outcome_1w: float,
        outcome_deterministic_1w: float | None = None,
        outcome_ai_1w: float | None = None,
    ) -> bool:
        """Update a decision record with outcome data.

        Args:
            target_date: Date of the original decision.
            input_hash: Hash of the inputs.
            outcome_1w: Return after 1 week.
            outcome_deterministic_1w: Return if followed deterministic.
            outcome_ai_1w: Return if followed AI.

        Returns:
            True if updated successfully, False if record not found.
        """
        record = self.get_cached_decision(target_date, input_hash)
        if record is None:
            return False

        record.outcome_1w = outcome_1w
        if outcome_deterministic_1w is not None:
            record.outcome_deterministic_1w = outcome_deterministic_1w
        if outcome_ai_1w is not None:
            record.outcome_ai_1w = outcome_ai_1w

        self.save_decision(record)
        return True

    def compute_comparison_summary(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> ComparisonSummary:
        """Compute a summary comparing deterministic vs AI decisions.

        Args:
            start_date: Start of period (None = all time).
            end_date: End of period (None = all time).

        Returns:
            ComparisonSummary with aggregated statistics.
        """
        if start_date and end_date:
            decisions = self.get_decisions_for_period(start_date, end_date)
        else:
            decisions = self.get_all_decisions()

        summary = ComparisonSummary(
            period_start=start_date,
            period_end=end_date,
        )

        for record in decisions:
            if record.ai_decision is None:
                continue  # Skip records without AI evaluation

            summary.total_decisions += 1

            if record.agreed:
                summary.agreements += 1
            else:
                summary.disagreements += 1

                # Check who was better (if outcomes available)
                if record.outcome_deterministic_1w is not None and record.outcome_ai_1w is not None:
                    if record.outcome_ai_1w > record.outcome_deterministic_1w:
                        summary.ai_better_when_disagreed += 1
                    elif record.outcome_deterministic_1w > record.outcome_ai_1w:
                        summary.deterministic_better_when_disagreed += 1

            # Track cumulative returns
            if record.outcome_deterministic_1w is not None:
                summary.deterministic_cumulative_return += record.outcome_deterministic_1w
            if record.outcome_ai_1w is not None:
                summary.ai_cumulative_return += record.outcome_ai_1w

            summary.decisions.append(record.to_dict())

        return summary

    def save_comparison_summary(self, summary: ComparisonSummary) -> None:
        """Save a comparison summary.

        Args:
            summary: The summary to save.
        """
        path = self.results_dir / "comparison.json"
        with open(path, "w") as f:
            json.dump(summary.to_dict(), f, indent=2)
        logger.info("saved_comparison_summary", path=str(path))

    def load_comparison_summary(self) -> ComparisonSummary | None:
        """Load the latest comparison summary.

        Returns:
            ComparisonSummary if found, None otherwise.
        """
        path = self.results_dir / "comparison.json"
        if path.exists():
            try:
                with open(path) as f:
                    data = json.load(f)
                return ComparisonSummary.from_dict(data)
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning("comparison_load_failed", error=str(e))
        return None
