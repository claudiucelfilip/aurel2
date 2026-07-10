"""Integration seam: wires apply_overlay into a live/backtest decision cycle.

Both src/aurel2/live/checker.py and the backtest engine call
run_overlay_for_decision so the fidelity rule (same code path live and
backtest) holds for the overlay exactly as it does for the core strategy.
This module owns ONLY the plumbing (load tilt -> apply -> persist state ->
translate into decision overrides + journal-ready dicts); the actual power
logic lives in overlay/powers.py.
"""

from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

import pandas as pd
import structlog

from aurel2.core.models import AssetClass, SignalAction
from aurel2.overlay.powers import OverlayResult, apply_overlay
from aurel2.overlay.schema import load_tilt, tilt_path_for_mode
from aurel2.overlay.state import load_state, save_state, state_path_for_mode

logger = structlog.get_logger()


@dataclass
class OverlayOutcome:
    action: Optional[SignalAction]  # non-None only if the overlay overrides the decision
    asset_symbol: Optional[str]
    active_lookback_months: Optional[int]
    journal_rows: list[dict]  # ready to attach to the trade journal entry


def run_overlay_for_decision(
    mode: str,
    today: date,
    prices: pd.DataFrame,
    dm_assets: dict[AssetClass, Any],
    current_holding_symbol: Optional[str],
    exclude_from_selection: set[AssetClass] | None = None,
) -> OverlayOutcome:
    """Load + validate the tilt file, apply the three powers against persisted
    cap state, save updated state, and return a plain-data outcome the caller
    can fold into its own AgentDecision / journal call.

    A malformed/expired/missing tilt is a hard no-op: this function still
    loads+saves state (a no-op tilt cannot mutate state, since apply_overlay
    short-circuits before any cap check), and returns an outcome with no
    overrides and no journal rows.
    """
    tilt_path = tilt_path_for_mode(mode)
    state_path = state_path_for_mode(mode)

    tilt = load_tilt(tilt_path, now=today)
    state = load_state(state_path)

    result: OverlayResult = apply_overlay(
        tilt=tilt,
        state=state,
        today=today,
        prices=prices,
        dm_assets=dm_assets,
        current_holding_symbol=current_holding_symbol,
        exclude_from_selection=exclude_from_selection,
    )

    save_state(state_path, state)

    journal_rows = [
        {
            "power": e.power,
            "status": e.status,
            "reason": e.reason,
            "detail": e.detail,
        }
        for e in result.journal_entries
    ]

    action = None
    asset_symbol = None
    if result.decision_overrides:
        action = SignalAction(result.decision_overrides.get("action", "hold"))
        asset_symbol = result.decision_overrides.get("asset_symbol")

    if journal_rows:
        logger.info("overlay_applied_for_decision", mode=mode, as_of=today.isoformat(), rows=journal_rows)

    return OverlayOutcome(
        action=action,
        asset_symbol=asset_symbol,
        active_lookback_months=result.active_lookback_months,
        journal_rows=journal_rows,
    )
