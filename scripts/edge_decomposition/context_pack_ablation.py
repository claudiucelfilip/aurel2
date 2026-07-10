#!/usr/bin/env python3
"""Track B: single context-pack ablation step (v1 vs v2 vs v3), majority-of-5.

docs/plans/2026-07-10-ai-overlay-design.md, "Context pack (single ablation step
BEFORE build freeze)": run the 22-week blind harness with v1/v2/v3, pick the
sharpest on judgment metrics, freeze. No iteration after this step.

Reuses the existing 22-week window and weekly cadence from
scripts/edge_decomposition/ai_tilt_harness.py (baseline replay for
current_position/days_held/deterministic_signal context), but:
  - reads prices from the PINNED raw-close snapshot (data/edge_decomposition/
    snapshots/price_snapshot_raw.csv), never yfinance/CachedPriceProvider, so
    every pack is byte-reproducible on rerun.
  - builds v1/v2/v3 packs via src/aurel2/overlay/context_pack.py (the same
    module production will import).
  - samples the CLI 5x per (week, version) for majority-vote judgment metrics
    (docs/plans, "Decision protocol", majority-of-5).
  - runs CLI calls concurrently (bounded pool) and caches every raw response by
    content hash in an append-only jsonl BEFORE any aggregation, so a crash
    mid-run never loses completed work -- rerunning the script resumes from
    cache with zero re-calls for already-cached (week, version, sample) cells.

Model: claude-fable-5 via the `claude` CLI ONLY (never the anthropic SDK/API --
see CLAUDE.md). Output: data/edge_decomposition/ablation_raw.jsonl (append-only
cache) and data/edge_decomposition/ablation_results.json (aggregated).
"""

import concurrent.futures
import hashlib
import json
import sys
import threading
import time
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import structlog
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(50))
import logging
logging.disable(logging.CRITICAL)

import pandas as pd

import ai_tilt_harness as harness
from aurel2.overlay.context_pack import BUILDERS
from aurel2.overlay.data_snapshot import load_snapshot

VERSIONS = ["v1", "v2", "v3"]
N_SAMPLES = 5
MODEL = "claude-fable-5"
MAX_WORKERS = 5

RAW_CACHE_PATH = REPO_ROOT / "data" / "edge_decomposition" / "ablation_raw.jsonl"
RESULTS_PATH = REPO_ROOT / "data" / "edge_decomposition" / "ablation_results.json"

WINDOW_START = harness.WINDOW_START
WINDOW_END = harness.WINDOW_END

_cache_lock = threading.Lock()


def load_raw_cache() -> dict:
    cache = {}
    if RAW_CACHE_PATH.exists():
        for line in RAW_CACHE_PATH.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            cache[rec["cache_key"]] = rec
    return cache


def append_raw_cache(rec: dict):
    with _cache_lock:
        RAW_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with RAW_CACHE_PATH.open("a") as f:
            f.write(json.dumps(rec) + "\n")


def make_prompt(as_of: date, context_pack: dict) -> str:
    return harness.PROMPT_TEMPLATE.format(as_of=as_of.isoformat(), context_json=json.dumps(context_pack, indent=2))


def cache_key_for(wk_date: date, version: str, sample_idx: int, prompt_hash: str) -> str:
    return f"{wk_date.isoformat()}:{version}:s{sample_idx}:{prompt_hash}"


def call_one(wk_date: date, version: str, sample_idx: int, prompt: str, cache: dict) -> dict:
    """Returns the cache record (already validated tilt or a fallback), always
    appended to the raw cache file exactly once per unique cache_key.
    """
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:16]
    key = cache_key_for(wk_date, version, sample_idx, prompt_hash)

    with _cache_lock:
        existing = cache.get(key)
    if existing is not None:
        return existing

    for attempt in range(2):
        parsed, latency, raw = harness.call_claude_cli(prompt, model=MODEL)
        tilt = harness.validate_tilt(parsed) if parsed is not None else None
        if tilt is not None:
            rec = {
                "cache_key": key, "date": wk_date.isoformat(), "version": version,
                "sample_idx": sample_idx, "prompt_hash": prompt_hash, "attempt": attempt + 1,
                "raw_response": raw, "tilt": tilt, "latency_s": round(latency, 1),
            }
            append_raw_cache(rec)
            with _cache_lock:
                cache[key] = rec
            return rec

    fallback_tilt = {"regime_view": "mixed", "symbol_bias": {}, "confidence": 0.0, "reasoning": "PARSE_FAILURE: recorded as no-tilt"}
    rec = {
        "cache_key": key, "date": wk_date.isoformat(), "version": version,
        "sample_idx": sample_idx, "prompt_hash": prompt_hash, "attempt": "fallback",
        "raw_response": "BOTH_ATTEMPTS_FAILED", "tilt": fallback_tilt, "latency_s": 0.0,
    }
    append_raw_cache(rec)
    with _cache_lock:
        cache[key] = rec
    return rec


def build_week_context(prices: pd.DataFrame, week_starts: list, decisions_by_date: dict, equity_by_date: dict, holding_since: dict) -> list[dict]:
    """Per-week ground truth (position/days-held/deterministic signal), shared
    across v1/v2/v3 so the only thing that varies between versions is the pack's
    feature set -- not the underlying position state (this IS the ablation).
    """
    tilt_engine = harness.build_engine()
    dm_assets = tilt_engine.dual_momentum.assets

    rows = []
    for wk_date in week_starts:
        ds = wk_date.isoformat()
        eq_row = equity_by_date.get(ds)
        dec_row = decisions_by_date.get(ds)
        cur_holding_symbol = eq_row["holding"] if eq_row else "CASH"
        since = holding_since.get(ds, ds)
        held_days = (date.fromisoformat(ds) - date.fromisoformat(since)).days
        rows.append({
            "wk_date": wk_date,
            "dm_assets": dm_assets,
            "current_holding_symbol": None if cur_holding_symbol == "CASH" else cur_holding_symbol,
            "days_held": held_days,
            "deterministic_signal": {
                "action": dec_row["action"] if dec_row else None,
                "symbol": dec_row["symbol"] if dec_row else None,
                "reason": dec_row["reason"] if dec_row else None,
            },
        })
    return rows


def main():
    print("Loading pinned raw-close snapshot...")
    prices = load_snapshot()
    print(f"{len(prices)} rows, symbols={sorted(prices['symbol'].unique())}")

    print("\nBuilding baseline (no-AI) replay for shared position context...")
    baseline_engine = harness.build_engine()
    baseline_decisions, baseline_equity, _ = harness.run_replay(prices.rename(columns={}), baseline_engine, tilts_by_week=None)
    decisions_by_date = {d["date"]: d for d in baseline_decisions}
    equity_by_date = {d["date"]: d for d in baseline_equity}

    days_held = 0
    prev_holding = None
    holding_since = {}
    cur_since = None
    for d in baseline_equity:
        h = d["holding"]
        if h != prev_holding:
            cur_since = d["date"]
        holding_since[d["date"]] = cur_since
        prev_holding = h

    all_days = pd.bdate_range(WINDOW_START, WINDOW_END).date.tolist()
    week_starts = []
    seen_weeks = set()
    for d in all_days:
        wk = d.isocalendar()[:2]
        if wk not in seen_weeks:
            seen_weeks.add(wk)
            week_starts.append(d)
    print(f"{len(week_starts)} weekly decision days")

    week_ctx_rows = build_week_context(prices, week_starts, decisions_by_date, equity_by_date, holding_since)

    print(f"\nBuilding context packs for {len(week_ctx_rows)} weeks x {len(VERSIONS)} versions...")
    jobs = []  # (wk_date, version, sample_idx, prompt)
    for row in week_ctx_rows:
        for version in VERSIONS:
            pack = BUILDERS[version](
                prices=prices,
                calc_date=row["wk_date"],
                dm_assets=row["dm_assets"],
                current_holding_symbol=row["current_holding_symbol"],
                days_held=row["days_held"],
                deterministic_signal=row["deterministic_signal"],
            )
            prompt = make_prompt(row["wk_date"], pack)
            for sample_idx in range(1, N_SAMPLES + 1):
                jobs.append((row["wk_date"], version, sample_idx, prompt))

    print(f"Total (week, version, sample) cells: {len(jobs)}")

    cache = load_raw_cache()
    already_cached = sum(1 for wk, v, s, p in jobs if cache_key_for(wk, v, s, hashlib.sha256(p.encode()).hexdigest()[:16]) in cache)
    print(f"Already cached: {already_cached}/{len(jobs)}; {len(jobs) - already_cached} calls to make")

    results = [None] * len(jobs)
    start_time = time.monotonic()
    done_count = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        future_to_idx = {
            ex.submit(call_one, wk, v, s, p, cache): i
            for i, (wk, v, s, p) in enumerate(jobs)
        }
        for fut in concurrent.futures.as_completed(future_to_idx):
            i = future_to_idx[fut]
            try:
                results[i] = fut.result()
            except Exception as e:
                wk, v, s, p = jobs[i]
                print(f"  [ERROR] {wk} {v} sample{s}: {e}")
                results[i] = {"cache_key": None, "date": wk.isoformat(), "version": v, "sample_idx": s, "tilt": None, "error": str(e)}
            done_count += 1
            if done_count % 10 == 0 or done_count == len(jobs):
                elapsed = time.monotonic() - start_time
                print(f"  [{done_count}/{len(jobs)}] elapsed={elapsed:.0f}s")

    print(f"\nAll {len(jobs)} cells resolved in {time.monotonic() - start_time:.0f}s")

    # Aggregate into weekly_by_version[version][date] = list of 5 tilts
    weekly_by_version = {v: {} for v in VERSIONS}
    for (wk, v, s, _p), rec in zip(jobs, results):
        ds = wk.isoformat()
        weekly_by_version[v].setdefault(ds, {})[s] = rec.get("tilt") if rec else None

    out = {
        "window": {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()},
        "model": MODEL,
        "n_samples": N_SAMPLES,
        "versions": VERSIONS,
        "week_starts": [w.isoformat() for w in week_starts],
        "weekly_by_version": weekly_by_version,
        "n_cells": len(jobs),
        "n_already_cached_at_start": already_cached,
    }
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(json.dumps(out, indent=2))
    print(f"\nWrote {RESULTS_PATH}")


if __name__ == "__main__":
    main()
