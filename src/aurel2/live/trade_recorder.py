"""Shared execute → record → notify pipeline for trade execution."""

from typing import Optional, TYPE_CHECKING

import structlog

from aurel2.live.executor import Executor, ExecutionResult
from aurel2.live.journal import TradeJournal
from aurel2.notifications.ntfy import NtfyNotifier

if TYPE_CHECKING:
    from aurel2.live.connection import AlpacaConnection

logger = structlog.get_logger()


class TradeRecorder:
    """Shared execute → record → notify pipeline.

    Eliminates duplication between checker._execute_decision(),
    daemon._execute_approved(), and daemon._execute_timeout().
    """

    def __init__(
        self,
        executor: Executor,
        connection: "AlpacaConnection",
        journal: TradeJournal,
        notifier: NtfyNotifier,
    ):
        self.executor = executor
        self.connection = connection
        self.journal = journal
        self.notifier = notifier

    async def execute_and_record(
        self,
        action: str,
        symbol: Optional[str],
        decision_id: str,
        current_holding: Optional[str] = None,
        position_size_pct: float = 1.0,
        notify_title: str = "Aurel2: Trade Executed",
        notify_context: str = "",
        notify_tags_success: list[str] | None = None,
        notify_priority: str = "default",
    ) -> ExecutionResult:
        """Execute a trade, record in journal, and send notification.

        Args:
            action: Trade action (buy/sell/hold)
            symbol: Asset symbol
            decision_id: Journal decision ID for linking execution
            current_holding: Current position symbol
            position_size_pct: Position sizing (0.0-1.0)
            notify_title: Notification title on success
            notify_context: Extra context for notification message
            notify_tags_success: Tags for success notification
            notify_priority: Notification priority

        Returns:
            ExecutionResult from the executor
        """
        if notify_tags_success is None:
            notify_tags_success = ["white_check_mark", "chart_with_upwards_trend"]

        # 1. Execute via broker
        result = await self.executor.execute(
            action=action,
            symbol=symbol,
            current_holding=current_holding,
            position_size_pct=position_size_pct,
        )

        # 2. Get post-trade account state
        account_after = None
        holding_after = None
        if result.success:
            try:
                summary = await self.connection.get_account_summary()
                if summary:
                    account_after = summary.total_value
                holding_after = await self.executor.get_current_holding()
            except Exception:
                pass

        # 3. Record in journal
        self.journal.record_execution(
            decision_id=decision_id,
            success=result.success,
            shares=result.shares,
            fill_price=result.fill_price,
            error=result.message if not result.success else None,
            account_value_after=account_after,
            current_holding_after=holding_after,
        )

        # 4. Send notification
        if result.success:
            total_cost = result.shares * result.fill_price
            acct_str = f"Account: ${account_after:,.0f}\n" if account_after else ""
            holding_str = f"Holding: {holding_after}\n" if holding_after else ""
            guard_str = f"\n{result.settlement_guard_note}\n" if result.settlement_guard_note else ""
            msg = (
                f"{action.upper()} {symbol or ''}\n"
                f"Shares: {result.shares:.0f} @ ${result.fill_price:.2f} "
                f"(${total_cost:,.0f})\n"
                f"{acct_str}"
                f"{holding_str}"
                f"{guard_str}"
                f"\n{notify_context}"
            ).strip()
            self.notifier.send(
                message=msg,
                title=notify_title,
                tags=notify_tags_success,
                priority=notify_priority,
            )
        else:
            self.notifier.send(
                message=(
                    f"Failed: {action.upper()} {symbol or ''}\n\n"
                    f"Error: {result.message}"
                ),
                title="Aurel2: Execution Failed",
                tags=["x", "warning"],
                priority="high",
            )

        return result
