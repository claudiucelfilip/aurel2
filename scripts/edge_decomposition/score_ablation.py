#!/usr/bin/env python3
"""Score the v1/v2/v3 context-pack ablation on judgment metrics (not P&L).

docs/plans/2026-07-10-ai-overlay-design.md, "Context pack" + "Decision protocol":
  (a) first risk_off call in the Feb chop -- earlier is better.
  (b) Apr risk_on call preserved -- the blind harness called risk_on Apr 20, 3
      days before Claw's real EEM->QQQ move (2026-04-23); must not regress.
  (c) fewer "mixed" verdicts.
  (d) resample stability -- majority agreement across the 5 samples per week
      (v1 chop weeks flipped 4/11 on resample in the model bake-off).
  (e) agreement with Claw's actual documented calls (CLAW_ACTIONS in
      ai_tilt_harness.py) where known.

Reads data/edge_decomposition/ablation_raw.jsonl (post-retry, all fallbacks
recovered where possible) and produces data/edge_decomposition/ablation_scorecard.json.
"""

import json
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai_tilt_harness as harness

RAW_CACHE_PATH = REPO_ROOT / "data" / "edge_decomposition" / "ablation_raw.jsonl"
SCORECARD_PATH = REPO_ROOT / "data" / "edge_decomposition" / "ablation_scorecard.json"

CHOP_START = date(2026, 2, 11)
CHOP_END = date(2026, 3, 31)
CLAW_APR_ENTRY = date(2026, 4, 23)
VERSIONS = ["v1", "v2", "v3"]


def load_records() -> list[dict]:
    recs = []
    with RAW_CACHE_PATH.open() as f:
        for line in f:
            if line.strip():
                recs.append(json.loads(line))
    return recs


def majority_regime(tilts: list[dict]) -> tuple[str, float]:
    """Majority-of-5 regime_view + agreement fraction (docs/plans, Decision protocol)."""
    views = [t["regime_view"] for t in tilts if t is not None]
    if not views:
        return "mixed", 0.0
    counts = Counter(views)
    winner, n = counts.most_common(1)[0]
    return winner, round(n / len(views), 3)


def score_version(version: str, weekly: dict[str, dict[int, dict]]) -> dict:
    """weekly: {date_str: {sample_idx: tilt_or_None}}"""
    sorted_dates = sorted(weekly.keys())

    # (a) first risk_off call in the Feb-Mar chop window (majority-of-5 view).
    first_risk_off = None
    for ds in sorted_dates:
        d = date.fromisoformat(ds)
        if not (CHOP_START <= d <= CHOP_END):
            continue
        tilts = [weekly[ds][i]["tilt"] if isinstance(weekly[ds][i], dict) and "tilt" in weekly[ds][i] else weekly[ds][i] for i in sorted(weekly[ds])]
        regime, _ = majority_regime(tilts)
        if regime == "risk_off":
            first_risk_off = ds
            break

    # (b) Apr risk_on call preserved: majority-of-5 regime_view = risk_on on or
    # before 2026-04-20 (the original blind single-sample harness's call date),
    # and in any case strictly before Claw's real 2026-04-23 entry.
    first_risk_on_in_april = None
    for ds in sorted_dates:
        d = date.fromisoformat(ds)
        if d.month != 4:
            continue
        tilts = [weekly[ds][i]["tilt"] if isinstance(weekly[ds][i], dict) and "tilt" in weekly[ds][i] else weekly[ds][i] for i in sorted(weekly[ds])]
        regime, _ = majority_regime(tilts)
        if regime == "risk_on":
            first_risk_on_in_april = ds
            break
    apr_call_preserved = (
        first_risk_on_in_april is not None
        and date.fromisoformat(first_risk_on_in_april) < CLAW_APR_ENTRY
    )

    # (c) mixed-verdict rate (majority-of-5 view across all 22 weeks).
    n_mixed = 0
    n_weeks = 0
    per_week_majority = {}
    for ds in sorted_dates:
        tilts = [weekly[ds][i]["tilt"] if isinstance(weekly[ds][i], dict) and "tilt" in weekly[ds][i] else weekly[ds][i] for i in sorted(weekly[ds])]
        regime, agreement = majority_regime(tilts)
        per_week_majority[ds] = {"regime_view": regime, "sample_agreement": agreement}
        n_weeks += 1
        if regime == "mixed":
            n_mixed += 1
    pct_mixed = round(n_mixed / n_weeks * 100, 1) if n_weeks else None

    # (d) resample stability: fraction of weeks where the majority view holds
    # >=4/5 (strong) vs weeks that are a bare 3/5 split (fragile) -- and,
    # matching the bake-off's language, how many CHOP weeks "flip" i.e. have
    # sample_agreement < 0.6 (no reliable majority, a near coin-flip).
    chop_weeks = [ds for ds in sorted_dates if CHOP_START <= date.fromisoformat(ds) <= CHOP_END]
    fragile_chop_weeks = sum(1 for ds in chop_weeks if per_week_majority[ds]["sample_agreement"] < 0.6)
    mean_agreement_all = round(sum(per_week_majority[ds]["sample_agreement"] for ds in sorted_dates) / len(sorted_dates), 3)
    mean_agreement_chop = round(sum(per_week_majority[ds]["sample_agreement"] for ds in chop_weeks) / len(chop_weeks), 3) if chop_weeks else None

    # (e) Claw agreement -- reuse the harness's calibration builder against the
    # majority-of-5 weekly view (symbol_bias unioned/averaged across samples
    # that agreed with the majority regime, for a representative bias dict).
    weekly_tilts_for_claw = {}
    for ds in sorted_dates:
        tilts = [weekly[ds][i]["tilt"] if isinstance(weekly[ds][i], dict) and "tilt" in weekly[ds][i] else weekly[ds][i] for i in sorted(weekly[ds])]
        regime, _ = majority_regime(tilts)
        agreeing = [t for t in tilts if t is not None and t["regime_view"] == regime]
        bias_union = {}
        bias_counts = defaultdict(list)
        for t in agreeing:
            for sym, v in t.get("symbol_bias", {}).items():
                bias_counts[sym].append(v)
        for sym, vs in bias_counts.items():
            bias_union[sym] = round(sum(vs) / len(vs), 4)
        reasoning = agreeing[0]["reasoning"] if agreeing else ""
        weekly_tilts_for_claw[date.fromisoformat(ds)] = {
            "regime_view": regime, "symbol_bias": bias_union, "reasoning": reasoning,
        }
    claw_table = harness.build_claw_calibration(weekly_tilts_for_claw)
    claw_agree = sum(1 for r in claw_table if r["agreement"].startswith("agree"))
    claw_disagree = sum(1 for r in claw_table if r["agreement"].startswith("disagree"))
    claw_neutral = len(claw_table) - claw_agree - claw_disagree

    n_fallback_unrecovered = sum(
        1 for ds in sorted_dates for i in weekly[ds]
        if weekly[ds][i] is None
    )

    return {
        "version": version,
        "first_risk_off_date_in_chop": first_risk_off,
        "first_risk_on_date_in_april": first_risk_on_in_april,
        "apr_risk_on_preserved_before_claw_entry": apr_call_preserved,
        "pct_weeks_mixed": pct_mixed,
        "n_weeks_mixed": n_mixed,
        "n_weeks_total": n_weeks,
        "mean_sample_agreement_all_weeks": mean_agreement_all,
        "mean_sample_agreement_chop_weeks": mean_agreement_chop,
        "n_fragile_chop_weeks_lt_60pct_agreement": fragile_chop_weeks,
        "n_chop_weeks": len(chop_weeks),
        "claw_agree": claw_agree,
        "claw_disagree": claw_disagree,
        "claw_neutral": claw_neutral,
        "claw_calibration_table": claw_table,
        "per_week_majority": per_week_majority,
        "n_unrecovered_fallback_cells": n_fallback_unrecovered,
    }


def main():
    recs = load_records()
    print(f"Loaded {len(recs)} raw records")

    by_version = {v: defaultdict(dict) for v in VERSIONS}
    n_fallback = 0
    for rec in recs:
        v = rec["version"]
        ds = rec["date"]
        s = rec["sample_idx"]
        if rec.get("attempt") == "fallback":
            n_fallback += 1
            by_version[v][ds][s] = None
        else:
            by_version[v][ds][s] = rec
    print(f"Unrecovered fallback cells remaining: {n_fallback}")

    scorecards = {}
    for v in VERSIONS:
        scorecards[v] = score_version(v, by_version[v])
        sc = scorecards[v]
        print(f"\n=== {v} ===")
        print(f"  first_risk_off (chop): {sc['first_risk_off_date_in_chop']}")
        print(f"  first_risk_on (Apr):   {sc['first_risk_on_date_in_april']}  (preserved={sc['apr_risk_on_preserved_before_claw_entry']})")
        print(f"  pct_mixed:             {sc['pct_weeks_mixed']}%  ({sc['n_weeks_mixed']}/{sc['n_weeks_total']})")
        print(f"  mean_agreement (all):  {sc['mean_sample_agreement_all_weeks']}")
        print(f"  mean_agreement (chop): {sc['mean_sample_agreement_chop_weeks']}  fragile_chop_weeks={sc['n_fragile_chop_weeks_lt_60pct_agreement']}/{sc['n_chop_weeks']}")
        print(f"  claw: agree={sc['claw_agree']} disagree={sc['claw_disagree']} neutral={sc['claw_neutral']}")
        print(f"  unrecovered cells:     {sc['n_unrecovered_fallback_cells']}")

    out = {"versions": VERSIONS, "scorecards": scorecards}
    SCORECARD_PATH.write_text(json.dumps(out, indent=2))
    print(f"\nWrote {SCORECARD_PATH}")


if __name__ == "__main__":
    main()
