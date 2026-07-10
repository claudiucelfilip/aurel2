"""Tilt file schema + validation.

Schema (docs/plans/2026-07-10-ai-overlay-design.md, "The tilt interface"):

    {
      "as_of": "2026-07-14",
      "expires": "2026-07-21",
      "regime_view": "risk_on|mixed|risk_off",
      "confidence": 0.0,
      "powers": {
        "accelerate_entry": {"symbol": "QQQ|null"},
        "lookback_override_months": null,   # 3|6|null
        "force_defensive_contest": false
      },
      "reasoning": "...",
      "samples": 5, "sample_agreement": 0.8
    }

Hard rule: malformed OR expired OR schema-invalid tilt = complete no-op, logged.
"""

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

import structlog

logger = structlog.get_logger()

REGIME_VIEWS = ("risk_on", "mixed", "risk_off")
LOOKBACK_OVERRIDE_MONTHS = (3, 6)
DEFENSIVE_CONTEST_SYMBOLS = ("GLD", "AGG", "SHY", "IEF", "TIP", "CASH")


def tilt_path_for_mode(mode: str = "paper") -> str:
    return f"data/{mode}/overlay_tilt.json"


@dataclass
class Tilt:
    """A validated, non-expired tilt. Construct only via validate_tilt/load_tilt."""

    as_of: date
    expires: date
    regime_view: str
    confidence: float
    accelerate_entry_symbol: Optional[str]
    lookback_override_months: Optional[int]
    force_defensive_contest: bool
    reasoning: str
    samples: int
    sample_agreement: float
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return dict(self.raw)


class TiltValidationError(Exception):
    """Raised internally to short-circuit to a no-op; never propagated to callers
    of validate_tilt/load_tilt, which return None instead."""


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise TiltValidationError(msg)


def _parse_iso_date(value: Any, field_name: str) -> date:
    _require(isinstance(value, str), f"{field_name} must be a string")
    try:
        return date.fromisoformat(value)
    except ValueError as e:
        raise TiltValidationError(f"{field_name} not a valid ISO date: {value}") from e


def validate_tilt(parsed: Any, now: Optional[date] = None) -> Optional[Tilt]:
    """Validate a parsed tilt dict against the schema. Returns None (logged) on
    ANY malformation or expiry — never raises to the caller.
    """
    if now is None:
        now = datetime.now().date()

    try:
        _require(isinstance(parsed, dict), "tilt is not a JSON object")

        as_of = _parse_iso_date(parsed.get("as_of"), "as_of")
        expires = _parse_iso_date(parsed.get("expires"), "expires")

        regime_view = parsed.get("regime_view")
        _require(regime_view in REGIME_VIEWS, f"regime_view invalid: {regime_view!r}")

        confidence = parsed.get("confidence")
        _require(isinstance(confidence, (int, float)) and not isinstance(confidence, bool), "confidence must be numeric")
        confidence = float(confidence)
        _require(0.0 <= confidence <= 1.0, f"confidence out of range: {confidence}")

        powers = parsed.get("powers")
        _require(isinstance(powers, dict), "powers must be an object")

        accel = powers.get("accelerate_entry")
        _require(isinstance(accel, dict), "powers.accelerate_entry must be an object")
        accel_symbol = accel.get("symbol")
        _require(accel_symbol is None or isinstance(accel_symbol, str), "accelerate_entry.symbol must be string or null")

        lookback = powers.get("lookback_override_months")
        _require(lookback is None or lookback in LOOKBACK_OVERRIDE_MONTHS, f"lookback_override_months invalid: {lookback!r}")

        force_defensive = powers.get("force_defensive_contest", False)
        _require(isinstance(force_defensive, bool), "force_defensive_contest must be boolean")

        reasoning = parsed.get("reasoning", "")
        _require(isinstance(reasoning, str), "reasoning must be a string")

        samples = parsed.get("samples")
        _require(isinstance(samples, int) and not isinstance(samples, bool), "samples must be an int")

        sample_agreement = parsed.get("sample_agreement")
        _require(
            isinstance(sample_agreement, (int, float)) and not isinstance(sample_agreement, bool),
            "sample_agreement must be numeric",
        )
        sample_agreement = float(sample_agreement)
        _require(0.0 <= sample_agreement <= 1.0, f"sample_agreement out of range: {sample_agreement}")

        # Hard TTL: stale tilt = no tilt (expiry is exclusive: 'as_of expires' day itself is still valid, day after is not).
        if now > expires:
            logger.warning("overlay_tilt_expired", as_of=str(as_of), expires=str(expires), now=str(now))
            return None

    except TiltValidationError as e:
        logger.warning("overlay_tilt_invalid", error=str(e), parsed=str(parsed)[:2000])
        return None

    return Tilt(
        as_of=as_of,
        expires=expires,
        regime_view=regime_view,
        confidence=confidence,
        accelerate_entry_symbol=accel_symbol,
        lookback_override_months=lookback,
        force_defensive_contest=force_defensive,
        reasoning=reasoning,
        samples=samples,
        sample_agreement=sample_agreement,
        raw=parsed,
    )


def load_tilt(path: str, now: Optional[date] = None) -> Optional[Tilt]:
    """Load + validate the tilt file. Missing file, unreadable JSON, or any
    schema/TTL violation => None (logged), which callers treat as a hard no-op.
    """
    p = Path(path)
    if not p.exists():
        return None
    try:
        parsed = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("overlay_tilt_unreadable", path=path, error=str(e))
        return None
    return validate_tilt(parsed, now=now)


def write_tilt(path: str, tilt_dict: dict) -> None:
    """Write the tilt file. Caller is responsible for git-versioning the write
    (see runner.py's git_commit_tilt) — this function only performs the write.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(tilt_dict, indent=2))
