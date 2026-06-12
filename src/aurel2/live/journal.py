"""Trade journal for audit trail and analysis."""

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import structlog

logger = structlog.get_logger()

DEFAULT_JOURNAL_PATH = "data/trade_journal.json"


def journal_path_for_mode(mode: str = "paper") -> str:
    """Return journal path for the given trading mode."""
    return f"data/{mode}/trade_journal.json"


@dataclass
class JournalEntry:
    """A single entry in the trade journal."""

    id: str
    timestamp: str
    entry_type: str  # "decision", "execution", "approval", "error"

    # Decision context
    action: Optional[str] = None
    symbol: Optional[str] = None
    decision_symbol: Optional[str] = None
    current_holding_symbol: Optional[str] = None
    confidence: float = 0.0
    decision_type: Optional[str] = None

    # Strategy signals
    strategy_signals: dict = field(default_factory=dict)

    # AI advisor
    ai_agrees: bool = True
    ai_action: Optional[str] = None
    ai_asset: Optional[str] = None
    ai_reasoning: Optional[str] = None
    ai_confidence: float = 0.0
    ai_commentary: Optional[str] = None
    failure_patterns: list = field(default_factory=list)

    # Market context
    market_regime: Optional[str] = None

    # Execution details
    executed: bool = False
    shares: float = 0.0
    fill_price: float = 0.0
    slippage_pct: float = 0.0
    execution_error: Optional[str] = None

    # Account state
    account_value_before: Optional[float] = None
    account_value_after: Optional[float] = None
    current_holding_before: Optional[str] = None
    current_holding_after: Optional[str] = None


class TradeJournal:
    """Persistent trade journal for audit and analysis."""

    def __init__(self, filepath: str = DEFAULT_JOURNAL_PATH):
        self.filepath = filepath
        self.entries: list[JournalEntry] = []
        self._load()

    def _load(self) -> None:
        """Load journal from file."""
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r") as f:
                    data = json.load(f)
                    self.entries = [JournalEntry(**e) for e in data]
                logger.info("journal_loaded", entries=len(self.entries))
            except Exception as e:
                logger.warning("journal_load_failed", error=str(e))
                self.entries = []
        else:
            self.entries = []

    def _save(self) -> None:
        """Save journal to file."""
        Path(self.filepath).parent.mkdir(parents=True, exist_ok=True)
        with open(self.filepath, "w") as f:
            json.dump([asdict(e) for e in self.entries], f, indent=2)

    def record_decision(
        self,
        decision_id: str,
        action: str,
        symbol: Optional[str],
        confidence: float,
        decision_type: str,
        strategy_signals: dict,
        ai_agrees: bool = True,
        ai_action: Optional[str] = None,
        ai_asset: Optional[str] = None,
        ai_reasoning: Optional[str] = None,
        ai_confidence: float = 0.0,
        ai_commentary: Optional[str] = None,
        failure_patterns: list = None,
        market_regime: Optional[str] = None,
        account_value: Optional[float] = None,
        current_holding: Optional[str] = None,
        decision_symbol: Optional[str] = None,
        current_holding_symbol: Optional[str] = None,
    ) -> JournalEntry:
        """Record a trading decision."""
        entry = JournalEntry(
            id=decision_id,
            timestamp=datetime.now().isoformat(),
            entry_type="decision",
            action=action,
            symbol=symbol,
            decision_symbol=decision_symbol if decision_symbol is not None else symbol,
            current_holding_symbol=current_holding_symbol if current_holding_symbol is not None else current_holding,
            confidence=confidence,
            decision_type=decision_type,
            strategy_signals=strategy_signals,
            ai_agrees=ai_agrees,
            ai_action=ai_action,
            ai_asset=ai_asset,
            ai_reasoning=ai_reasoning,
            ai_confidence=ai_confidence,
            ai_commentary=ai_commentary,
            failure_patterns=failure_patterns or [],
            market_regime=market_regime,
            account_value_before=account_value,
            current_holding_before=current_holding,
        )

        self.entries.append(entry)
        self._save()

        logger.info(
            "journal_decision_recorded",
            id=decision_id,
            action=action,
            symbol=symbol,
        )

        return entry

    def record_execution(
        self,
        decision_id: str,
        success: bool,
        shares: float = 0.0,
        fill_price: float = 0.0,
        slippage_pct: float = 0.0,
        error: Optional[str] = None,
        account_value_after: Optional[float] = None,
        current_holding_after: Optional[str] = None,
    ) -> None:
        """Record execution result for a decision."""
        for entry in reversed(self.entries):
            if entry.id == decision_id:
                entry.executed = success
                entry.shares = shares
                entry.fill_price = fill_price
                entry.slippage_pct = slippage_pct
                entry.execution_error = error
                entry.account_value_after = account_value_after
                entry.current_holding_after = current_holding_after

                self._save()

                logger.info(
                    "journal_execution_recorded",
                    id=decision_id,
                    success=success,
                    shares=shares,
                )
                return

        logger.warning("journal_decision_not_found", id=decision_id)

    def get_recent_entries(self, days: int = 30) -> list[JournalEntry]:
        """Get entries from the last N days."""
        from datetime import timedelta

        cutoff = datetime.now() - timedelta(days=days)
        return [
            e for e in self.entries
            if datetime.fromisoformat(e.timestamp) > cutoff
        ]

    def get_statistics(self) -> dict:
        """Get summary statistics from journal."""
        if not self.entries:
            return {"total_entries": 0}

        decisions = [e for e in self.entries if e.entry_type == "decision"]
        executions = [e for e in decisions if e.executed]

        return {
            "total_entries": len(self.entries),
            "total_decisions": len(decisions),
            "total_executions": len(executions),
            "ai_agreement_rate": sum(1 for e in decisions if e.ai_agrees) / len(decisions) if decisions else 0,
            "avg_confidence": sum(e.confidence for e in decisions) / len(decisions) if decisions else 0,
        }
