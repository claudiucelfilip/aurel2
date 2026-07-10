#!/usr/bin/env python3
"""Retry pass for cells that fell back to PARSE_FAILURE in the main ablation run.

The main run (context_pack_ablation.py) hit 80/330 fallbacks clustered on a
handful of weeks (2026-05-11, 05-18, 05-25, 06-22, 06-29, 07-06) spread evenly
across v1/v2/v3 -- not a data or prompt-size bug (a standalone reproduction of
one failing cell succeeded immediately), but concurrency contention: 5 worker
threads launching `claude` CLI subprocesses simultaneously overloaded something
(local process/resource limits) on exactly the weeks whose jobs landed in the
same submission burst. This script re-runs only the failed cells at
concurrency=2 with real error capture (the original fallback path discarded the
actual CLI error text), and REPLACES the fallback records in the raw cache with
successful results.
"""

import concurrent.futures
import json
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import structlog
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(50))
import logging
logging.disable(logging.CRITICAL)

import pandas as pd
from datetime import date

import ai_tilt_harness as harness
import context_pack_ablation as cpa
from aurel2.overlay.context_pack import BUILDERS
from aurel2.overlay.data_snapshot import load_snapshot

RAW_CACHE_PATH = cpa.RAW_CACHE_PATH
MAX_WORKERS = 2

_lock = threading.Lock()


def load_all_records() -> list[dict]:
    recs = []
    with RAW_CACHE_PATH.open() as f:
        for line in f:
            if line.strip():
                recs.append(json.loads(line))
    return recs


def retry_one(wk_date, version, sample_idx, prompt) -> dict:
    for attempt in range(3):
        parsed, latency, raw = harness.call_claude_cli(prompt, model=cpa.MODEL)
        tilt = harness.validate_tilt(parsed) if parsed is not None else None
        if tilt is not None:
            import hashlib
            prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:16]
            return {
                "cache_key": cpa.cache_key_for(wk_date, version, sample_idx, prompt_hash),
                "date": wk_date.isoformat(), "version": version, "sample_idx": sample_idx,
                "prompt_hash": prompt_hash, "attempt": f"retry{attempt + 1}",
                "raw_response": raw, "tilt": tilt, "latency_s": round(latency, 1),
            }
        print(f"    [{wk_date} {version} s{sample_idx}] retry {attempt+1} failed: {raw[:200]}")
        time.sleep(2)
    return None  # give up, caller keeps the fallback


def main():
    print("Loading raw cache and finding fallback records...")
    all_recs = load_all_records()
    fallback_recs = [r for r in all_recs if r.get("attempt") == "fallback"]
    print(f"{len(fallback_recs)} fallback cells to retry")
    if not fallback_recs:
        print("Nothing to retry.")
        return

    print("Rebuilding context packs and prompts for failed cells...")
    prices = load_snapshot()
    baseline_engine = harness.build_engine()
    baseline_decisions, baseline_equity, _ = harness.run_replay(prices, baseline_engine, tilts_by_week=None)
    decisions_by_date = {d["date"]: d for d in baseline_decisions}
    equity_by_date = {d["date"]: d for d in baseline_equity}

    days_held_map = {}
    prev_holding, cur_since = None, None
    for d in baseline_equity:
        h = d["holding"]
        if h != prev_holding:
            cur_since = d["date"]
        days_held_map[d["date"]] = cur_since
        prev_holding = h

    tilt_engine = harness.build_engine()
    dm_assets = tilt_engine.dual_momentum.assets

    jobs = []
    for rec in fallback_recs:
        wk_date = date.fromisoformat(rec["date"])
        version = rec["version"]
        sample_idx = rec["sample_idx"]
        ds = rec["date"]
        eq_row = equity_by_date.get(ds)
        dec_row = decisions_by_date.get(ds)
        cur_holding_symbol = eq_row["holding"] if eq_row else "CASH"
        since = days_held_map.get(ds, ds)
        held_days = (date.fromisoformat(ds) - date.fromisoformat(since)).days
        pack = BUILDERS[version](
            prices=prices, calc_date=wk_date, dm_assets=dm_assets,
            current_holding_symbol=None if cur_holding_symbol == "CASH" else cur_holding_symbol,
            days_held=held_days,
            deterministic_signal={
                "action": dec_row["action"] if dec_row else None,
                "symbol": dec_row["symbol"] if dec_row else None,
                "reason": dec_row["reason"] if dec_row else None,
            },
        )
        prompt = cpa.make_prompt(wk_date, pack)
        jobs.append((wk_date, version, sample_idx, prompt))

    print(f"Retrying {len(jobs)} cells at concurrency={MAX_WORKERS}...")
    new_records = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(retry_one, wk, v, s, p): (wk, v, s) for wk, v, s, p in jobs}
        done = 0
        for fut in concurrent.futures.as_completed(futs):
            wk, v, s = futs[fut]
            result = fut.result()
            done += 1
            if result is not None:
                new_records[(wk.isoformat(), v, s)] = result
                print(f"  [{done}/{len(jobs)}] RECOVERED {wk} {v} s{s}")
            else:
                print(f"  [{done}/{len(jobs)}] STILL FAILING {wk} {v} s{s}")

    print(f"\nRecovered {len(new_records)}/{len(jobs)} cells")

    # Rewrite the raw cache: replace fallback records with recovered ones where we got them.
    out_lines = []
    for rec in all_recs:
        key = (rec["date"], rec["version"], rec["sample_idx"])
        if rec.get("attempt") == "fallback" and key in new_records:
            out_lines.append(json.dumps(new_records[key]))
        else:
            out_lines.append(json.dumps(rec))
    RAW_CACHE_PATH.write_text("\n".join(out_lines) + "\n")
    print(f"Rewrote {RAW_CACHE_PATH}")

    still_failed = len(jobs) - len(new_records)
    print(f"Still failing after retry: {still_failed}")


if __name__ == "__main__":
    main()
