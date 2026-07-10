"""The three transition powers, enforced against a validated Tilt + persisted cap state.

Single seam: apply_overlay(decision, tilt, ctx) -> OverlayResult. Both backtest
(engine/backtest.py) and live (live/checker.py) call this same function so the
fidelity rule (same code path live and backtest) holds for the overlay too.

Containment: this module NEVER mutates config, never introduces a symbol the
core didn't already know about (accelerate_entry is restricted to the core's
own projected next pick; force_defensive_contest is restricted to a fixed,
hardcoded defensive set already in ASSET_REGISTRY), and never places an order
directly — it only returns a (possibly modified) AgentDecision for the normal
execution path to act on, plus journal entries describing what happened.
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

import pandas as pd
import structlog

from aurel2.config.canonical import CANONICAL_CONFIG
from aurel2.core.assets import ASSET_REGISTRY
from aurel2.core.models import AssetClass
from aurel2.data.momentum import calculate_momentum_scores
from aurel2.overlay.schema import DEFENSIVE_CONTEST_SYMBOLS, Tilt
from aurel2.overlay.state import (
    OverlayState,
    can_accelerate_entry,
    can_activate_lookback_override,
    can_force_defensive_contest,
    clear_lookback_override,
    get_active_lookback_override,
    record_accelerate_entry,
    record_force_defensive_contest,
    record_lookback_activation,
)

logger = structlog.get_logger()

PROJECTION_TREND_MONTHS = 3  # short-term trend used to project the 12m ranking forward


@dataclass
class OverlayJournalEntry:
    """One row of overlay activity for the trade journal — applied or ignored,
    always with reasoning. Never contains order/execution fields; those stay
    solely in TradeJournal.record_execution's normal path.
    """

    power: str  # "accelerate_entry" | "lookback_override_months" | "force_defensive_contest"
    status: str  # "applied" | "ignored"
    reason: str
    detail: dict = field(default_factory=dict)


@dataclass
class OverlayResult:
    decision_overrides: dict[str, Any]  # e.g. {"action": "buy", "asset_symbol": "QQQ"} or {}
    journal_entries: list[OverlayJournalEntry] = field(default_factory=list)
    active_lookback_months: Optional[int] = None  # for the caller to re-run DM ranking at this lookback


def project_next_pick(
    prices: pd.DataFrame,
    calc_date: date,
    dm_assets: dict[AssetClass, Any],
    exclude_from_selection: set[AssetClass] | None = None,
) -> Optional[str]:
    """Project the core's OWN next pick forward using current short-term trend.

    Reuses calculate_momentum_scores (the same function DualMomentumStrategy
    itself calls) at a short lookback (PROJECTION_TREND_MONTHS) as a proxy for
    "if current short-term trends persist, who wins the 12m ranking next" —
    this can only ever surface a symbol already in dm_assets, never a novel one.
    """
    exclude_from_selection = exclude_from_selection or set()
    scores = calculate_momentum_scores(
        prices=prices,
        assets=dm_assets,
        calc_date=calc_date,
        lookback_months=PROJECTION_TREND_MONTHS,
        cash_rate=0.0,
    )
    risky_scores = {
        k: v for k, v in scores.items() if k != AssetClass.CASH and k not in exclude_from_selection
    }
    if not risky_scores:
        return None
    winner_class = max(risky_scores.keys(), key=lambda k: risky_scores[k].momentum_12m)
    winner_asset = dm_assets.get(winner_class)
    return winner_asset.yahoo_symbol if winner_asset else None


def apply_accelerate_entry(
    tilt: Tilt,
    state: OverlayState,
    today: date,
    prices: pd.DataFrame,
    dm_assets: dict[AssetClass, Any],
    current_holding_symbol: Optional[str],
    exclude_from_selection: set[AssetClass] | None = None,
) -> Optional[OverlayJournalEntry]:
    """Returns None if the tilt didn't request this power at all (nothing to
    journal); otherwise an OverlayJournalEntry describing applied/ignored.
    """
    symbol = tilt.accelerate_entry_symbol
    if not symbol:
        return None

    projected = project_next_pick(prices, today, dm_assets, exclude_from_selection)

    if not CANONICAL_CONFIG.overlay.accelerate_entry_enabled:
        # Shadow log: record what would have happened so the parallel run
        # produces evidence for re-enabling behind a worth-it hurdle.
        return OverlayJournalEntry(
            power="accelerate_entry",
            status="ignored",
            reason="power disabled (shadow-logged)",
            detail={"requested": symbol, "projected_next_pick": projected},
        )

    if projected is None or symbol != projected:
        return OverlayJournalEntry(
            power="accelerate_entry",
            status="ignored",
            reason=f"requested symbol {symbol!r} is not the core's own projected next pick ({projected!r})",
            detail={"requested_symbol": symbol, "projected_symbol": projected},
        )

    if symbol == current_holding_symbol:
        return OverlayJournalEntry(
            power="accelerate_entry",
            status="ignored",
            reason=f"already holding {symbol}, nothing to accelerate",
            detail={"requested_symbol": symbol},
        )

    if not can_accelerate_entry(state, today):
        return OverlayJournalEntry(
            power="accelerate_entry",
            status="ignored",
            reason="over cap: max 1 acceleration per 21 days",
            detail={"requested_symbol": symbol, "last_used": state.last_accelerate_entry_date},
        )

    record_accelerate_entry(state, today)
    return OverlayJournalEntry(
        power="accelerate_entry",
        status="applied",
        reason=f"accelerated entry into {symbol}, the core's own projected next pick",
        detail={"symbol": symbol},
    )


def apply_lookback_override(
    tilt: Tilt,
    state: OverlayState,
    today: date,
) -> Optional[OverlayJournalEntry]:
    """Decides whether an override lookback should be active TODAY. Handles
    both new activation requests and auto-revert of an existing activation.
    Sets state.active_lookback_override as a side effect when applied/reverted.
    Returns None only when the tilt makes no request AND nothing is active
    (true no-op, nothing to journal).
    """
    requested_months = tilt.lookback_override_months
    currently_active = get_active_lookback_override(state, today)  # auto-reverts on 30-day expiry as a side effect

    # Auto-revert on risk_on view, regardless of what the tilt currently requests.
    if currently_active is not None and tilt.regime_view == "risk_on":
        clear_lookback_override(state)
        return OverlayJournalEntry(
            power="lookback_override_months",
            status="ignored",
            reason=f"auto-reverted active {currently_active}m override: regime_view is risk_on",
            detail={"reverted_months": currently_active},
        )

    if currently_active is not None:
        return OverlayJournalEntry(
            power="lookback_override_months",
            status="applied",
            reason=f"{currently_active}m lookback override still active",
            detail={"months": currently_active},
        )

    if requested_months is None:
        return None

    if not can_activate_lookback_override(state, today):
        reason = (
            "over cap: an override is already active"
            if state.active_lookback_override is not None
            else "over cap: max 2 activations per quarter"
        )
        return OverlayJournalEntry(
            power="lookback_override_months",
            status="ignored",
            reason=reason,
            detail={"requested_months": requested_months},
        )

    record_lookback_activation(state, today, requested_months)
    return OverlayJournalEntry(
        power="lookback_override_months",
        status="applied",
        reason=f"activated {requested_months}m lookback override (regime_view={tilt.regime_view})",
        detail={"months": requested_months},
    )


def run_defensive_contest(
    prices: pd.DataFrame,
    calc_date: date,
    current_holding_symbol: Optional[str],
) -> Optional[str]:
    """Restricted ranking of the fixed defensive set vs the held asset, equal
    thresholds (highest 12m momentum wins outright — no asymmetric switch
    threshold). Returns the winning symbol if a defensive asset wins, else None
    (held asset wins or contest can't be computed).
    """
    defensive_classes = {
        ac: asset for ac, asset in ASSET_REGISTRY.items() if asset.yahoo_symbol in DEFENSIVE_CONTEST_SYMBOLS
    }
    # Represent CASH explicitly; ASSET_REGISTRY's CASH entry has yahoo_symbol=None
    # so the membership check above already includes it via AssetClass.CASH.
    if not any(ac == AssetClass.CASH for ac in defensive_classes):
        defensive_classes[AssetClass.CASH] = ASSET_REGISTRY[AssetClass.CASH]

    held_asset_class = None
    held_asset = None
    if current_holding_symbol and current_holding_symbol not in ("CASH", None):
        for ac, asset in ASSET_REGISTRY.items():
            if asset.yahoo_symbol == current_holding_symbol:
                held_asset_class = ac
                held_asset = asset
                break

    contest_assets = dict(defensive_classes)
    if held_asset_class is not None and held_asset_class not in contest_assets:
        contest_assets[held_asset_class] = held_asset

    scores = calculate_momentum_scores(
        prices=prices,
        assets=contest_assets,
        calc_date=calc_date,
        lookback_months=12,
        cash_rate=0.0,
    )
    if not scores:
        return None

    winner_class = max(scores.keys(), key=lambda k: scores[k].momentum_12m)
    winner_asset = contest_assets.get(winner_class)
    winner_symbol = winner_asset.yahoo_symbol if (winner_asset and winner_asset.yahoo_symbol) else (
        "CASH" if winner_class == AssetClass.CASH else None
    )

    if winner_class == held_asset_class:
        return None  # held asset wins; no rotation
    if winner_symbol in DEFENSIVE_CONTEST_SYMBOLS or winner_class == AssetClass.CASH:
        return winner_symbol
    return None  # shouldn't happen (held asset isn't in DEFENSIVE_CONTEST_SYMBOLS but won) — defensive-only rotation


def apply_force_defensive_contest(
    tilt: Tilt,
    state: OverlayState,
    today: date,
    prices: pd.DataFrame,
    current_holding_symbol: Optional[str],
) -> Optional[OverlayJournalEntry]:
    if not tilt.force_defensive_contest:
        return None

    if not can_force_defensive_contest(state, today):
        return OverlayJournalEntry(
            power="force_defensive_contest",
            status="ignored",
            reason="over cap: max 1 per 14 days",
            detail={"last_used": state.last_force_defensive_date},
        )

    winner_symbol = run_defensive_contest(prices, today, current_holding_symbol)
    record_force_defensive_contest(state, today)  # the contest itself consumes the cap, win or lose

    if winner_symbol is None:
        return OverlayJournalEntry(
            power="force_defensive_contest",
            status="applied",
            reason="ran restricted defensive contest; held asset won, no rotation",
            detail={"current_holding": current_holding_symbol},
        )

    return OverlayJournalEntry(
        power="force_defensive_contest",
        status="applied",
        reason=f"restricted defensive contest: {winner_symbol} beat held asset {current_holding_symbol}",
        detail={"winner": winner_symbol, "current_holding": current_holding_symbol},
    )


def apply_overlay(
    tilt: Optional[Tilt],
    state: OverlayState,
    today: date,
    prices: pd.DataFrame,
    dm_assets: dict[AssetClass, Any],
    current_holding_symbol: Optional[str],
    exclude_from_selection: set[AssetClass] | None = None,
) -> OverlayResult:
    """The single seam backtest and live share. tilt=None (malformed/expired/
    missing) is a hard no-op: returns an empty OverlayResult immediately, no
    state mutation, nothing journaled (there's nothing to journal — a bad
    tilt file was never a valid instruction). Callers apply decision_overrides
    (if any) into their AgentDecision and log journal_entries into TradeJournal.
    """
    if tilt is None:
        return OverlayResult(decision_overrides={})

    journal_entries: list[OverlayJournalEntry] = []
    decision_overrides: dict[str, Any] = {}

    accel_entry = apply_accelerate_entry(
        tilt, state, today, prices, dm_assets, current_holding_symbol, exclude_from_selection
    )
    if accel_entry is not None:
        journal_entries.append(accel_entry)
        if accel_entry.status == "applied":
            decision_overrides["action"] = "buy"
            decision_overrides["asset_symbol"] = accel_entry.detail["symbol"]

    lookback_entry = apply_lookback_override(tilt, state, today)
    active_lookback_months = None
    if lookback_entry is not None:
        journal_entries.append(lookback_entry)
        if lookback_entry.status == "applied":
            active_lookback_months = lookback_entry.detail.get("months")

    defensive_entry = apply_force_defensive_contest(tilt, state, today, prices, current_holding_symbol)
    if defensive_entry is not None:
        journal_entries.append(defensive_entry)
        if defensive_entry.status == "applied" and defensive_entry.detail.get("winner"):
            # Force-defensive-contest is one-off; it wins the day's decision outright
            # only if accelerate_entry didn't already set an override this same day
            # (accelerate_entry and force_defensive both firing same-day is not a
            # spec'd interaction — accelerate_entry, being capped tighter and
            # evaluated first, takes precedence to avoid ambiguous double-override).
            decision_overrides.setdefault("action", "buy")
            decision_overrides.setdefault("asset_symbol", defensive_entry.detail["winner"])

    return OverlayResult(
        decision_overrides=decision_overrides,
        journal_entries=journal_entries,
        active_lookback_months=active_lookback_months,
    )
