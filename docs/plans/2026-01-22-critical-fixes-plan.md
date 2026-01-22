# Critical Fixes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix the 5 critical issues identified in the live trading implementation review before paper trading.

**Architecture:** Add verification, validation, and safety layers to existing execution flow without changing the core decision-making logic.

**Tech Stack:** Python, asyncio, structlog, ib_insync

---

## Task 1: Trade Execution Verification

**Goal:** Ensure orders are verified after placement and positions reconciled.

**Files:**
- Modify: `src/aurel2/live/executor.py`
- Modify: `src/aurel2/broker/ibkr.py`

**Step 1: Add OrderVerification dataclass to broker**

In `src/aurel2/broker/ibkr.py`, add after the `OrderResult` class:

```python
@dataclass
class OrderVerification:
    """Result of order verification."""
    verified: bool
    expected_shares: float
    actual_shares: float
    expected_action: str  # "BUY" or "SELL"
    slippage_pct: float  # (fill_price - expected_price) / expected_price
    message: str
```

**Step 2: Add verify_order method to IBKRBroker**

In `src/aurel2/broker/ibkr.py`, add method to `IBKRBroker` class:

```python
async def verify_order(
    self,
    order_result: OrderResult,
    expected_shares: float,
    expected_price: float | None = None,
) -> OrderVerification:
    """Verify order was filled as expected.

    Args:
        order_result: Result from place_order
        expected_shares: How many shares we expected to fill
        expected_price: Optional expected price for slippage calculation

    Returns:
        OrderVerification with verification results
    """
    # Check fill status
    if order_result.status == "REJECTED":
        return OrderVerification(
            verified=False,
            expected_shares=expected_shares,
            actual_shares=0,
            expected_action=order_result.action,
            slippage_pct=0,
            message=f"Order rejected: {order_result.message}",
        )

    if order_result.status == "PENDING":
        return OrderVerification(
            verified=False,
            expected_shares=expected_shares,
            actual_shares=order_result.filled_quantity,
            expected_action=order_result.action,
            slippage_pct=0,
            message="Order still pending after timeout",
        )

    # Check partial fill
    fill_ratio = order_result.filled_quantity / expected_shares if expected_shares > 0 else 0
    if fill_ratio < 0.95:  # Less than 95% filled
        return OrderVerification(
            verified=False,
            expected_shares=expected_shares,
            actual_shares=order_result.filled_quantity,
            expected_action=order_result.action,
            slippage_pct=0,
            message=f"Partial fill: {fill_ratio:.1%} of expected",
        )

    # Calculate slippage if expected price provided
    slippage_pct = 0.0
    if expected_price and expected_price > 0 and order_result.avg_fill_price > 0:
        slippage_pct = (order_result.avg_fill_price - expected_price) / expected_price
        # For sells, negative slippage is bad (sold lower than expected)
        if order_result.action == "SELL":
            slippage_pct = -slippage_pct

    return OrderVerification(
        verified=True,
        expected_shares=expected_shares,
        actual_shares=order_result.filled_quantity,
        expected_action=order_result.action,
        slippage_pct=slippage_pct,
        message="Order verified successfully",
    )
```

**Step 3: Update executor to verify orders**

In `src/aurel2/live/executor.py`, update `_execute_buy()` to verify after placement:

After the `order_result = await self.broker.place_order(order)` line, add:

```python
# Verify the order
verification = await self.broker.verify_order(
    order_result=order_result,
    expected_shares=shares,
    expected_price=market_price,
)

if not verification.verified:
    logger.error(
        "order_verification_failed",
        symbol=symbol,
        expected_shares=verification.expected_shares,
        actual_shares=verification.actual_shares,
        message=verification.message,
    )
    return ExecutionResult(
        success=False,
        action="buy",
        symbol=symbol,
        shares=verification.actual_shares,
        fill_price=order_result.avg_fill_price,
        message=f"Verification failed: {verification.message}",
        order_result=order_result,
    )

if abs(verification.slippage_pct) > 0.01:  # More than 1% slippage
    logger.warning(
        "high_slippage_detected",
        symbol=symbol,
        slippage_pct=f"{verification.slippage_pct:.2%}",
        expected_price=market_price,
        fill_price=order_result.avg_fill_price,
    )
```

**Step 4: Update _execute_sell() similarly**

Add the same verification pattern after the sell order placement.

**Step 5: Add position reconciliation method to executor**

```python
async def reconcile_positions(self) -> dict[str, Any]:
    """Reconcile local state with broker positions.

    Returns:
        Dict with reconciliation results
    """
    broker_positions = await self.connection.get_positions()

    result = {
        "positions": [],
        "discrepancies": [],
        "total_value": 0,
    }

    for pos in broker_positions:
        result["positions"].append({
            "symbol": pos.symbol,
            "shares": pos.quantity,
            "market_value": pos.market_value,
            "avg_cost": pos.avg_cost,
        })
        result["total_value"] += pos.market_value

    logger.info(
        "positions_reconciled",
        num_positions=len(broker_positions),
        total_value=result["total_value"],
    )

    return result
```

**Step 6: Run tests**

```bash
python3 -m pytest tests/live/ -v -k "executor or ibkr"
```

**Step 7: Commit**

```bash
git add src/aurel2/live/executor.py src/aurel2/broker/ibkr.py
git commit -m "feat: add order verification and position reconciliation"
```

---

## Task 2: Circuit Breaker for Broker Failures

**Goal:** Automatically pause trading after repeated connection failures.

**Files:**
- Create: `src/aurel2/live/circuit_breaker.py`
- Modify: `src/aurel2/live/connection.py`
- Modify: `src/aurel2/live/checker.py`

**Step 1: Create circuit breaker module**

Create `src/aurel2/live/circuit_breaker.py`:

```python
"""Circuit breaker for broker connection failures."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

import structlog

logger = structlog.get_logger()


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, rejecting requests
    HALF_OPEN = "half_open"  # Testing if recovered


@dataclass
class CircuitBreaker:
    """Circuit breaker to prevent cascading failures.

    States:
    - CLOSED: Normal operation, requests pass through
    - OPEN: Too many failures, requests rejected immediately
    - HALF_OPEN: Testing recovery, limited requests allowed

    Transitions:
    - CLOSED -> OPEN: After failure_threshold consecutive failures
    - OPEN -> HALF_OPEN: After reset_timeout seconds
    - HALF_OPEN -> CLOSED: On successful request
    - HALF_OPEN -> OPEN: On failed request
    """

    failure_threshold: int = 3
    reset_timeout_seconds: int = 300  # 5 minutes

    state: CircuitState = field(default=CircuitState.CLOSED)
    failure_count: int = field(default=0)
    last_failure_time: Optional[datetime] = field(default=None)
    last_success_time: Optional[datetime] = field(default=None)

    def record_success(self) -> None:
        """Record a successful operation."""
        self.failure_count = 0
        self.last_success_time = datetime.now()

        if self.state == CircuitState.HALF_OPEN:
            logger.info("circuit_breaker_closed", previous_state="half_open")
            self.state = CircuitState.CLOSED

    def record_failure(self, error: str = "") -> None:
        """Record a failed operation."""
        self.failure_count += 1
        self.last_failure_time = datetime.now()

        logger.warning(
            "circuit_breaker_failure",
            failure_count=self.failure_count,
            threshold=self.failure_threshold,
            error=error,
        )

        if self.state == CircuitState.HALF_OPEN:
            logger.warning("circuit_breaker_reopened")
            self.state = CircuitState.OPEN
        elif self.failure_count >= self.failure_threshold:
            logger.error(
                "circuit_breaker_opened",
                failure_count=self.failure_count,
                reset_timeout=self.reset_timeout_seconds,
            )
            self.state = CircuitState.OPEN

    def can_execute(self) -> bool:
        """Check if operation should be allowed.

        Returns:
            True if operation should proceed, False if circuit is open
        """
        if self.state == CircuitState.CLOSED:
            return True

        if self.state == CircuitState.OPEN:
            # Check if reset timeout has passed
            if self.last_failure_time:
                elapsed = datetime.now() - self.last_failure_time
                if elapsed > timedelta(seconds=self.reset_timeout_seconds):
                    logger.info("circuit_breaker_half_open")
                    self.state = CircuitState.HALF_OPEN
                    return True
            return False

        # HALF_OPEN - allow one request to test
        return True

    def get_status(self) -> dict:
        """Get current circuit breaker status."""
        return {
            "state": self.state.value,
            "failure_count": self.failure_count,
            "last_failure": self.last_failure_time.isoformat() if self.last_failure_time else None,
            "last_success": self.last_success_time.isoformat() if self.last_success_time else None,
        }
```

**Step 2: Integrate circuit breaker into connection**

In `src/aurel2/live/connection.py`, add import and attribute:

```python
from aurel2.live.circuit_breaker import CircuitBreaker

class IBKRConnection:
    def __init__(self, ...):
        # ... existing code ...
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=3,
            reset_timeout_seconds=300,
        )
```

Update the `connect()` method to use circuit breaker:

```python
async def connect(self, launch_tws_if_needed: bool = True, max_retries: int = 3) -> bool:
    """Connect to IBKR with circuit breaker protection."""

    # Check circuit breaker first
    if not self.circuit_breaker.can_execute():
        logger.error(
            "connection_blocked_by_circuit_breaker",
            status=self.circuit_breaker.get_status(),
        )
        return False

    # ... existing connection logic ...

    # On success:
    if connected:
        self.circuit_breaker.record_success()
        # ... rest of success handling ...

    # On failure (after all retries exhausted):
    self.circuit_breaker.record_failure("Connection failed after retries")
    return False
```

**Step 3: Add circuit breaker check to checker**

In `src/aurel2/live/checker.py`, update `run()` to check circuit breaker before execution:

```python
# Before executing any trades, check circuit breaker
if not self.connection.circuit_breaker.can_execute():
    status = self.connection.circuit_breaker.get_status()
    logger.error("execution_blocked_circuit_open", status=status)

    # Notify about circuit breaker state
    if self.notifier:
        self.notifier.send(
            message=f"Trading paused - circuit breaker open after {status['failure_count']} failures",
            title="Aurel2: Circuit Breaker Open",
            priority="urgent",
            tags=["warning", "stop_sign"],
        )

    return CheckResult(
        success=False,
        message=f"Circuit breaker open: {status['failure_count']} consecutive failures",
    )
```

**Step 4: Run tests**

```bash
python3 -m pytest tests/live/ -v
```

**Step 5: Commit**

```bash
git add src/aurel2/live/circuit_breaker.py src/aurel2/live/connection.py src/aurel2/live/checker.py
git commit -m "feat: add circuit breaker for broker connection failures"
```

---

## Task 3: Safe Timeout Auto-Execution

**Goal:** Re-validate market conditions before auto-executing timed-out decisions.

**Files:**
- Modify: `src/aurel2/live/pending.py`
- Modify: `src/aurel2/live/daemon.py`

**Step 1: Add market validation to pending module**

In `src/aurel2/live/pending.py`, add a validation method:

```python
from datetime import date

async def validate_decision_still_valid(
    self,
    decision: PendingDecision,
    current_price: float,
    original_price: float | None = None,
) -> tuple[bool, str]:
    """Validate that a pending decision is still valid for execution.

    Checks:
    - Market hasn't moved more than 3% since decision
    - Decision is not too stale (> 4 hours)

    Args:
        decision: The pending decision to validate
        current_price: Current market price of the asset
        original_price: Price when decision was made (if available)

    Returns:
        Tuple of (is_valid, reason)
    """
    from datetime import datetime, timedelta

    # Check staleness
    created = datetime.fromisoformat(decision.created_at)
    age = datetime.now() - created
    if age > timedelta(hours=4):
        return False, f"Decision too stale: {age.total_seconds() / 3600:.1f} hours old"

    # Check market movement if we have original price
    if original_price and original_price > 0:
        price_change = abs(current_price - original_price) / original_price
        if price_change > 0.03:  # 3% threshold
            return False, f"Market moved {price_change:.1%} since decision"

    return True, "Decision still valid"
```

**Step 2: Update daemon to validate before auto-execution**

In `src/aurel2/live/daemon.py`, update the timeout handling:

```python
async def _handle_timed_out_decision(
    self,
    decision: PendingDecision,
) -> None:
    """Handle a decision that has timed out.

    Re-validates market conditions before auto-executing.
    """
    logger.info(
        "handling_timed_out_decision",
        decision_id=decision.id,
        action=decision.action,
        symbol=decision.symbol,
    )

    # Get current market price for validation
    current_price = None
    if decision.symbol:
        try:
            current_price = await self.connection.broker.get_market_price(decision.symbol)
        except Exception as e:
            logger.warning("failed_to_get_price_for_validation", error=str(e))

    # Validate decision is still appropriate
    # Note: We don't have original price stored, so we only check staleness
    is_valid, reason = await self.pending_manager.validate_decision_still_valid(
        decision=decision,
        current_price=current_price or 0,
        original_price=None,  # TODO: Store original price in PendingDecision
    )

    if not is_valid:
        logger.warning(
            "timed_out_decision_invalidated",
            decision_id=decision.id,
            reason=reason,
        )

        # Notify about invalidation
        if self.notifier:
            self.notifier.send(
                message=f"Timed-out decision NOT auto-executed: {reason}\n\n"
                        f"Action: {decision.action.upper()} {decision.symbol or 'CASH'}\n"
                        f"Please review manually.",
                title="Aurel2: Decision Invalidated",
                priority="high",
                tags=["warning", "clock"],
            )

        # Mark as rejected instead of executing
        self.pending_manager.decisions[decision.id].status = PendingStatus.REJECTED.value
        self.pending_manager._save()
        return

    # Proceed with execution
    logger.info("auto_executing_timed_out_decision", decision_id=decision.id)

    # ... existing execution logic ...
```

**Step 3: Store original price in PendingDecision**

In `src/aurel2/live/pending.py`, add field to PendingDecision:

```python
@dataclass
class PendingDecision:
    # ... existing fields ...
    original_price: float | None = None  # Price when decision was made
```

**Step 4: Update checker to store original price**

In `src/aurel2/live/checker.py`, when creating pending decision, add the price:

```python
# Get current price for the decision
original_price = None
if decision.asset_symbol:
    try:
        original_price = await self.connection.broker.get_market_price(decision.asset_symbol)
    except Exception:
        pass

pending = self.pending_manager.create_decision(
    # ... existing params ...
    original_price=original_price,
)
```

**Step 5: Run tests**

```bash
python3 -m pytest tests/live/ -v -k "pending or daemon"
```

**Step 6: Commit**

```bash
git add src/aurel2/live/pending.py src/aurel2/live/daemon.py src/aurel2/live/checker.py
git commit -m "feat: validate market conditions before auto-executing timed-out decisions"
```

---

## Task 4: Reload Failure Learnings Before Each Check

**Goal:** Ensure AI advisor uses fresh failure data for each decision.

**Files:**
- Modify: `src/aurel2/agent/advisor.py`
- Modify: `src/aurel2/live/checker.py`

**Step 1: Add reload method to AIAdvisor**

In `src/aurel2/agent/advisor.py`, add:

```python
def reload_failure_analysis(self) -> bool:
    """Reload failure analysis from file.

    Returns:
        True if successfully loaded, False otherwise
    """
    previous_count = len(self.failure_analysis.failure_events) if self.failure_analysis else 0

    self._load_failure_analysis()

    current_count = len(self.failure_analysis.failure_events) if self.failure_analysis else 0

    if current_count != previous_count:
        logger.info(
            "failure_analysis_reloaded",
            previous_count=previous_count,
            current_count=current_count,
        )

    return self.failure_analysis is not None
```

**Step 2: Add staleness check**

```python
def is_failure_data_stale(self, max_age_hours: int = 24) -> bool:
    """Check if failure data is stale.

    Args:
        max_age_hours: Maximum age in hours before considered stale

    Returns:
        True if data is stale or missing
    """
    import os
    from datetime import datetime, timedelta

    if not os.path.exists(self.failure_file):
        return True

    file_mtime = datetime.fromtimestamp(os.path.getmtime(self.failure_file))
    age = datetime.now() - file_mtime

    return age > timedelta(hours=max_age_hours)
```

**Step 3: Update checker to reload before AI review**

In `src/aurel2/live/checker.py`, before calling AI advisor:

```python
# Reload failure learnings if stale
if self.ai_advisor:
    if self.ai_advisor.is_failure_data_stale(max_age_hours=24):
        logger.warning(
            "failure_data_stale",
            file=self.ai_advisor.failure_file,
        )

    # Always reload to get latest
    self.ai_advisor.reload_failure_analysis()
```

**Step 4: Run tests**

```bash
python3 -m pytest tests/agent/ -v -k "advisor"
```

**Step 5: Commit**

```bash
git add src/aurel2/agent/advisor.py src/aurel2/live/checker.py
git commit -m "feat: reload failure learnings before each AI review"
```

---

## Task 5: Add Trade Journal for Audit Trail

**Goal:** Create persistent structured record of all decisions and executions.

**Files:**
- Create: `src/aurel2/live/journal.py`
- Modify: `src/aurel2/live/checker.py`
- Modify: `src/aurel2/live/executor.py`

**Step 1: Create trade journal module**

Create `src/aurel2/live/journal.py`:

```python
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


@dataclass
class JournalEntry:
    """A single entry in the trade journal."""

    id: str  # Unique ID
    timestamp: str  # ISO format
    entry_type: str  # "decision", "execution", "approval", "error"

    # Decision context
    action: Optional[str] = None  # buy, sell, hold
    symbol: Optional[str] = None
    confidence: float = 0.0
    decision_type: Optional[str] = None  # ROUTINE, NON_ROUTINE, URGENT

    # Strategy signals
    strategy_signals: dict = field(default_factory=dict)

    # AI advisor
    ai_agrees: bool = True
    ai_action: Optional[str] = None
    ai_asset: Optional[str] = None
    ai_reasoning: Optional[str] = None
    ai_confidence: float = 0.0
    failure_patterns: list = field(default_factory=list)

    # Market context
    market_regime: Optional[str] = None
    spy_price: Optional[float] = None
    spy_drawdown: Optional[float] = None

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

    # Outcome (filled in later)
    forward_return_1d: Optional[float] = None
    forward_return_1w: Optional[float] = None
    forward_return_1m: Optional[float] = None


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
        failure_patterns: list = None,
        market_regime: Optional[str] = None,
        account_value: Optional[float] = None,
        current_holding: Optional[str] = None,
    ) -> JournalEntry:
        """Record a trading decision."""
        entry = JournalEntry(
            id=decision_id,
            timestamp=datetime.now().isoformat(),
            entry_type="decision",
            action=action,
            symbol=symbol,
            confidence=confidence,
            decision_type=decision_type,
            strategy_signals=strategy_signals,
            ai_agrees=ai_agrees,
            ai_action=ai_action,
            ai_asset=ai_asset,
            ai_reasoning=ai_reasoning,
            ai_confidence=ai_confidence,
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
        # Find the decision entry
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

        ai_overrides = [e for e in decisions if not e.ai_agrees]

        return {
            "total_entries": len(self.entries),
            "total_decisions": len(decisions),
            "total_executions": len(executions),
            "ai_agreement_rate": sum(1 for e in decisions if e.ai_agrees) / len(decisions) if decisions else 0,
            "ai_overrides": len(ai_overrides),
            "avg_confidence": sum(e.confidence for e in decisions) / len(decisions) if decisions else 0,
        }
```

**Step 2: Integrate journal into checker**

In `src/aurel2/live/checker.py`, add journal:

```python
from aurel2.live.journal import TradeJournal

class LiveChecker:
    def __init__(self, ...):
        # ... existing code ...
        self.journal = TradeJournal()
```

After making a decision, record it:

```python
# Record decision in journal
import uuid
decision_id = str(uuid.uuid4())[:8]

self.journal.record_decision(
    decision_id=decision_id,
    action=decision.action.value,
    symbol=decision.asset_symbol,
    confidence=decision.confidence,
    decision_type=decision.decision_type.value,
    strategy_signals=signals,
    ai_agrees=ai_advice.agrees_with_deterministic if ai_advice else True,
    ai_action=ai_advice.recommended_action if ai_advice else None,
    ai_asset=ai_advice.recommended_asset if ai_advice else None,
    ai_reasoning=ai_advice.reasoning if ai_advice else None,
    ai_confidence=ai_advice.confidence if ai_advice else 0.0,
    failure_patterns=ai_advice.failure_patterns_detected if ai_advice else [],
    market_regime=market_context.get("regime"),
    account_value=summary.total_value if summary else None,
    current_holding=current_holding,
)
```

After execution, record result:

```python
# Record execution in journal
self.journal.record_execution(
    decision_id=decision_id,
    success=execution_result.success,
    shares=execution_result.shares,
    fill_price=execution_result.fill_price,
    slippage_pct=0,  # TODO: Calculate from verification
    error=execution_result.message if not execution_result.success else None,
    account_value_after=summary.total_value if summary else None,
    current_holding_after=execution_result.symbol if execution_result.success else current_holding,
)
```

**Step 3: Run tests**

```bash
python3 -m pytest tests/live/ -v
```

**Step 4: Commit**

```bash
git add src/aurel2/live/journal.py src/aurel2/live/checker.py
git commit -m "feat: add trade journal for audit trail"
```

---

## Task 6: Final Integration Test

**Goal:** Verify all critical fixes work together.

**Step 1: Run full test suite**

```bash
python3 -m pytest tests/ -v
```

**Step 2: Run a dry-run check**

```bash
python3 -m aurel2.cli check --paper --dry-run
```

**Step 3: Verify journal was created**

```bash
cat data/trade_journal.json | python3 -m json.tool
```

**Step 4: Verify no regressions**

```bash
python3 -m aurel2.cli compare --start 2024-01-01 --end 2025-01-01 --capital 10000
```

**Step 5: Commit final changes if needed**

```bash
git status
git add -A
git commit -m "chore: integration test cleanup"
```

---

## Summary

After completing all tasks:

1. **Order Verification** - Orders are verified after placement, slippage tracked
2. **Circuit Breaker** - Trading pauses after 3 consecutive failures, resumes after 5 min
3. **Safe Timeout** - Timed-out decisions are validated before auto-execution
4. **Fresh Failure Data** - AI advisor reloads failure learnings before each review
5. **Trade Journal** - All decisions and executions are recorded for audit

These fixes address the 5 critical issues identified in the implementation review.
