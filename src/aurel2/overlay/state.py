"""Cap state persistence for the three transition powers.

Caps (docs/plans/2026-07-10-ai-overlay-design.md, "three transition powers + caps"):
- accelerate_entry: max 1 per 21 days.
- lookback_override_months: max 30 consecutive trading days per activation, max 2
  activations per quarter; auto-revert on expiry or risk_on view.
- force_defensive_contest: max 1 per 14 days.

State lives in data/{mode}/overlay_state.json and is the ONLY thing that gates
whether a power is allowed to activate — the tilt file's request is necessary
but never sufficient.
"""

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()

ACCELERATE_ENTRY_COOLDOWN_DAYS = 21
LOOKBACK_OVERRIDE_MAX_DAYS = 30
LOOKBACK_OVERRIDE_MAX_PER_QUARTER = 2
FORCE_DEFENSIVE_COOLDOWN_DAYS = 14


def state_path_for_mode(mode: str = "paper") -> str:
    return f"data/{mode}/overlay_state.json"


def _quarter_key(d: date) -> str:
    q = (d.month - 1) // 3 + 1
    return f"{d.year}Q{q}"


@dataclass
class LookbackActivation:
    started: str  # ISO date
    months: int
    quarter: str


@dataclass
class OverlayState:
    last_accelerate_entry_date: Optional[str] = None
    last_force_defensive_date: Optional[str] = None
    active_lookback_override: Optional[dict] = None  # {"started": iso, "months": int, "quarter": str}
    lookback_activations_by_quarter: dict = field(default_factory=dict)  # quarter -> count

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "OverlayState":
        return cls(
            last_accelerate_entry_date=d.get("last_accelerate_entry_date"),
            last_force_defensive_date=d.get("last_force_defensive_date"),
            active_lookback_override=d.get("active_lookback_override"),
            lookback_activations_by_quarter=d.get("lookback_activations_by_quarter", {}),
        )


def load_state(path: str) -> OverlayState:
    p = Path(path)
    if not p.exists():
        return OverlayState()
    try:
        return OverlayState.from_dict(json.loads(p.read_text()))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("overlay_state_unreadable", path=path, error=str(e))
        return OverlayState()


def save_state(path: str, state: OverlayState) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state.to_dict(), indent=2))


def can_accelerate_entry(state: OverlayState, today: date) -> bool:
    if state.last_accelerate_entry_date is None:
        return True
    last = date.fromisoformat(state.last_accelerate_entry_date)
    return (today - last).days >= ACCELERATE_ENTRY_COOLDOWN_DAYS


def record_accelerate_entry(state: OverlayState, today: date) -> None:
    state.last_accelerate_entry_date = today.isoformat()


def can_force_defensive_contest(state: OverlayState, today: date) -> bool:
    if state.last_force_defensive_date is None:
        return True
    last = date.fromisoformat(state.last_force_defensive_date)
    return (today - last).days >= FORCE_DEFENSIVE_COOLDOWN_DAYS


def record_force_defensive_contest(state: OverlayState, today: date) -> None:
    state.last_force_defensive_date = today.isoformat()


def get_active_lookback_override(state: OverlayState, today: date) -> Optional[int]:
    """Return the currently-active override lookback (3 or 6), auto-reverting
    (clearing state) if the 30-consecutive-trading-day window has elapsed.
    Trading days are approximated as calendar days here; the caller
    (powers.py) re-validates against the actual trading calendar when
    checking expiry precisely, since this module has no price data.
    """
    active = state.active_lookback_override
    if active is None:
        return None
    started = date.fromisoformat(active["started"])
    if (today - started).days > LOOKBACK_OVERRIDE_MAX_DAYS:
        state.active_lookback_override = None
        return None
    return active["months"]


def clear_lookback_override(state: OverlayState) -> None:
    state.active_lookback_override = None


def can_activate_lookback_override(state: OverlayState, today: date) -> bool:
    """A NEW activation is allowed only if no override is currently active and
    this quarter hasn't hit its cap of 2 activations."""
    if state.active_lookback_override is not None:
        return False
    quarter = _quarter_key(today)
    count = state.lookback_activations_by_quarter.get(quarter, 0)
    return count < LOOKBACK_OVERRIDE_MAX_PER_QUARTER


def record_lookback_activation(state: OverlayState, today: date, months: int) -> None:
    quarter = _quarter_key(today)
    state.active_lookback_override = {"started": today.isoformat(), "months": months, "quarter": quarter}
    state.lookback_activations_by_quarter[quarter] = state.lookback_activations_by_quarter.get(quarter, 0) + 1
