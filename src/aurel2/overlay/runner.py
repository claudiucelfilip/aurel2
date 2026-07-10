"""Majority-of-5 runner: 5 independent Fable 5 CLI calls -> one tilt file.

Rules (docs/plans/2026-07-10-ai-overlay-design.md, "Decision protocol"):
- 5 independent CLI calls per decision.
- regime_view decided by majority.
- a power activates ONLY if >=3/5 samples agree on it.
- numeric params averaged across the samples that agreed.
- write the tilt file with samples/sample_agreement metadata.
- every tilt write git-auto-commits the tilt file (versioning).

Cadence (weekly + event-triggered on a >5% held-asset move in 3 days) is
implemented as invokable entrypoints only — nothing here schedules anything
on any host.
"""

import subprocess
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import structlog

from aurel2.overlay.cli_backends import call_claude_cli
from aurel2.overlay.prompt import render_prompt
from aurel2.overlay.schema import (
    LOOKBACK_OVERRIDE_MONTHS,
    REGIME_VIEWS,
    tilt_path_for_mode,
    write_tilt,
)

from aurel2.config.canonical import CANONICAL_CONFIG

logger = structlog.get_logger()

N_SAMPLES = CANONICAL_CONFIG.overlay.sample_count
MAJORITY_THRESHOLD = CANONICAL_CONFIG.overlay.sample_agreement_threshold
DEFAULT_TTL_DAYS = 7
EVENT_TRIGGER_MOVE_PCT = 5.0
EVENT_TRIGGER_WINDOW_DAYS = 3


def _sample_one(pack: dict[str, Any], model: str = "claude-fable-5") -> Optional[dict]:
    prompt = render_prompt(pack)
    parsed, _latency, raw = call_claude_cli(prompt, model=model)
    if parsed is None:
        logger.warning("overlay_sample_parse_failed", raw=raw[:500])
        return None
    return parsed


def _valid_raw_sample(parsed: dict) -> bool:
    """Loose per-sample sanity check (not the full Tilt schema — that's
    applied to the AGGREGATED result). A sample that fails this is dropped
    from the vote entirely, same as a CLI failure."""
    if not isinstance(parsed, dict):
        return False
    if parsed.get("regime_view") not in REGIME_VIEWS:
        return False
    powers = parsed.get("powers")
    if not isinstance(powers, dict):
        return False
    return True


def collect_samples(pack: dict[str, Any], model: str = "claude-fable-5", n: int = N_SAMPLES) -> list[dict]:
    """Make n independent CLI calls, return the parsed+sane ones (dropped
    calls/parses simply reduce the vote denominator, per the spec's use of
    ">=3/5 samples agree" — a smaller-than-5 valid pool is handled by
    aggregate_samples using len(valid_samples) as the denominator).
    """
    samples = []
    for i in range(n):
        parsed = _sample_one(pack, model=model)
        if parsed is not None and _valid_raw_sample(parsed):
            samples.append(parsed)
        else:
            logger.warning("overlay_sample_dropped", index=i)
    return samples


def _majority_regime_view(samples: list[dict]) -> str:
    counts = Counter(s["regime_view"] for s in samples)
    top_count = max(counts.values())
    winners = [v for v, c in counts.items() if c == top_count]
    if len(winners) > 1:
        # Tie: fall back to the most conservative view among the tied winners.
        conservatism = {"risk_off": 0, "mixed": 1, "risk_on": 2}
        winners.sort(key=lambda v: conservatism.get(v, 1))
    return winners[0]


def _power_agreement(samples: list[dict], predicate) -> tuple[int, list[dict]]:
    """Returns (count_agreeing, list_of_agreeing_samples)."""
    agreeing = [s for s in samples if predicate(s)]
    return len(agreeing), agreeing


def aggregate_samples(samples: list[dict], as_of: Optional[date] = None, ttl_days: int = DEFAULT_TTL_DAYS) -> dict:
    """Majority-vote regime_view; a power activates only if >=3/5 (of the
    valid sample pool) agree; numeric params averaged across agreeing samples.
    Returns a raw tilt dict ready for schema.validate_tilt (this function does
    not itself validate — that happens on read, per the containment design).
    """
    if as_of is None:
        as_of = datetime.now().date()
    expires = as_of + timedelta(days=ttl_days)

    n = len(samples)
    if n == 0:
        return {
            "as_of": as_of.isoformat(),
            "expires": expires.isoformat(),
            "regime_view": "mixed",
            "confidence": 0.0,
            "powers": {
                "accelerate_entry": {"symbol": None},
                "lookback_override_months": None,
                "force_defensive_contest": False,
            },
            "reasoning": "All samples failed or were dropped; no-tilt fallback.",
            "samples": 0,
            "sample_agreement": 0.0,
        }

    regime_view = _majority_regime_view(samples)
    regime_agree_count = sum(1 for s in samples if s["regime_view"] == regime_view)

    def _accel_symbol(s: dict) -> Optional[str]:
        sym = s.get("powers", {}).get("accelerate_entry", {}).get("symbol")
        return sym if sym else None

    accel_counts = Counter(_accel_symbol(s) for s in samples if _accel_symbol(s) is not None)
    accel_symbol = None
    if accel_counts:
        top_symbol, top_count = accel_counts.most_common(1)[0]
        if top_count >= MAJORITY_THRESHOLD:
            accel_symbol = top_symbol

    def _lookback_months(s: dict) -> Optional[int]:
        v = s.get("powers", {}).get("lookback_override_months")
        try:
            v = int(v) if v is not None else None
        except (TypeError, ValueError):
            return None
        return v if v in LOOKBACK_OVERRIDE_MONTHS else None

    lookback_counts = Counter(_lookback_months(s) for s in samples if _lookback_months(s) is not None)
    lookback_months = None
    if lookback_counts:
        top_months, top_count = lookback_counts.most_common(1)[0]
        if top_count >= MAJORITY_THRESHOLD:
            lookback_months = top_months

    defensive_count, _ = _power_agreement(
        samples, lambda s: bool(s.get("powers", {}).get("force_defensive_contest"))
    )
    force_defensive = defensive_count >= MAJORITY_THRESHOLD

    confidences = [float(s["confidence"]) for s in samples if isinstance(s.get("confidence"), (int, float))]
    avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0

    reasonings = [str(s.get("reasoning", "")) for s in samples if s.get("reasoning")]
    combined_reasoning = " | ".join(reasonings)[:2000]

    sample_agreement = round(regime_agree_count / n, 4)

    return {
        "as_of": as_of.isoformat(),
        "expires": expires.isoformat(),
        "regime_view": regime_view,
        "confidence": round(avg_confidence, 4),
        "powers": {
            "accelerate_entry": {"symbol": accel_symbol},
            "lookback_override_months": lookback_months,
            "force_defensive_contest": force_defensive,
        },
        "reasoning": combined_reasoning,
        "samples": n,
        "sample_agreement": sample_agreement,
    }


def git_commit_tilt(tilt_path: str, repo_root: Optional[str] = None) -> bool:
    """Auto-commit the tilt file write for versioning. Best-effort: logs and
    returns False on any git failure rather than raising (a failed commit must
    never block the tilt write itself from taking effect)."""
    cwd = repo_root or str(Path(tilt_path).resolve().parents[2])
    rel_path = str(Path(tilt_path))
    try:
        subprocess.run(["git", "add", rel_path], cwd=cwd, check=True, capture_output=True, text=True)
        result = subprocess.run(
            ["git", "commit", "-m", f"Overlay: update tilt {rel_path}"],
            cwd=cwd,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 and "nothing to commit" not in result.stdout.lower():
            logger.warning("overlay_tilt_commit_failed", stdout=result.stdout, stderr=result.stderr)
            return False
        return True
    except (subprocess.CalledProcessError, OSError) as e:
        logger.warning("overlay_tilt_commit_error", error=str(e))
        return False


def run_overlay_decision(
    pack: dict[str, Any],
    mode: str = "paper",
    model: str = "claude-fable-5",
    n_samples: int = N_SAMPLES,
    ttl_days: int = DEFAULT_TTL_DAYS,
    auto_commit: bool = True,
    repo_root: Optional[str] = None,
) -> dict:
    """Full weekly/event-triggered entrypoint: sample n times, aggregate,
    write the tilt file, git-auto-commit it. Returns the written tilt dict.
    This function does NOT schedule itself — call it from a cron/cadence
    layer outside this package.
    """
    samples = collect_samples(pack, model=model, n=n_samples)
    tilt_dict = aggregate_samples(samples, ttl_days=ttl_days)
    tilt_path = tilt_path_for_mode(mode)
    write_tilt(tilt_path, tilt_dict)
    logger.info(
        "overlay_tilt_written",
        path=tilt_path,
        regime_view=tilt_dict["regime_view"],
        samples=tilt_dict["samples"],
        sample_agreement=tilt_dict["sample_agreement"],
    )
    if auto_commit:
        git_commit_tilt(tilt_path, repo_root=repo_root)
    return tilt_dict


def should_event_trigger(pct_move_3d: Optional[float]) -> bool:
    """True if the held asset moved >5% in 3 days -- the event-trigger
    condition for an out-of-cadence re-run. Pure predicate; the caller
    supplies pct_move_3d from its own price data (no scheduling here)."""
    if pct_move_3d is None:
        return False
    return abs(pct_move_3d) > EVENT_TRIGGER_MOVE_PCT


if __name__ == "__main__":
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(description="Run one majority-of-5 overlay decision from a context pack JSON file.")
    parser.add_argument("pack_path", help="Path to a JSON file containing the context pack")
    parser.add_argument("--mode", default="paper", choices=["paper", "live"])
    parser.add_argument("--model", default="claude-fable-5")
    parser.add_argument("--n-samples", type=int, default=N_SAMPLES)
    parser.add_argument("--no-commit", action="store_true")
    args = parser.parse_args()

    pack = json.loads(Path(args.pack_path).read_text())
    tilt = run_overlay_decision(
        pack, mode=args.mode, model=args.model, n_samples=args.n_samples, auto_commit=not args.no_commit
    )
    json.dump(tilt, sys.stdout, indent=2)
    print()
