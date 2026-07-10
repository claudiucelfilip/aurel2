#!/usr/bin/env python3
"""Model bake-off: fable5 vs gpt-5.5 vs sonnet on the AI tilt overlay (ai_tilt_harness.py).

Reuses the harness's context-pack builder, price cache, and baseline replay
unmodified -- only the CLI backend and cache file differ per model. Sonnet's
scores are loaded from the existing data/edge_decomposition/ai_tilt_replay.json
(already run) rather than re-called.

CLI-only, never HTTP/API: fable5 -> `claude --model claude-fable-5 -p ...`,
gpt55 -> `codex exec --model gpt-5.5 --json ...`.

Output: data/edge_decomposition/model_bakeoff.json
"""

import json
import sys
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

from aurel2.core.assets import get_all_yahoo_symbols
from aurel2.data.providers.cache import CachedPriceProvider

import ai_tilt_harness as h

DATA_DIR = REPO_ROOT / "data" / "edge_decomposition"
OUT_PATH = DATA_DIR / "model_bakeoff.json"
SONNET_REPLAY_PATH = DATA_DIR / "ai_tilt_replay.json"

# Only backends we actually re-call live; sonnet is loaded from its existing replay.
LIVE_BACKENDS = ["fable5", "gpt55", "gpt56sol"]
CHOP_WINDOW_START = date(2026, 2, 11)
CHOP_WINDOW_END = date(2026, 3, 31)
CLAW_RISK_ON_DATE = date(2026, 4, 23)  # Claw's QQQ entry


def cache_path_for(backend: str) -> Path:
    return DATA_DIR / f"ai_tilt_cache_{backend}.jsonl"


def run_backend(backend: str, prices: pd.DataFrame, week_starts: list[date],
                 decisions_by_date: dict, equity_by_date: dict, holding_since: dict,
                 tilt_engine_assets) -> dict:
    """Runs all 22 weekly calls for one backend. Returns {date: tilt} plus raw stats."""
    cache_path = cache_path_for(backend)
    cache = h.load_cache(cache_path)

    weekly_tilts: dict[date, dict] = {}
    latencies = []
    malformed_count = 0

    for i, wk_date in enumerate(week_starts, 1):
        ds = wk_date.isoformat()
        eq_row = equity_by_date.get(ds)
        dec_row = decisions_by_date.get(ds)
        cur_holding_symbol = eq_row["holding"] if eq_row else "CASH"
        since = holding_since.get(ds, ds)
        held_days = (date.fromisoformat(ds) - date.fromisoformat(since)).days

        context = h.build_context_pack(
            prices=prices,
            calc_date=wk_date,
            dm_assets=tilt_engine_assets,
            current_holding_symbol=None if cur_holding_symbol == "CASH" else cur_holding_symbol,
            days_held=held_days,
            deterministic_signal={
                "action": dec_row["action"] if dec_row else None,
                "symbol": dec_row["symbol"] if dec_row else None,
                "reason": dec_row["reason"] if dec_row else None,
            },
        )

        prompt_hash_before = len(cache)
        tilt, latency = h.get_ai_tilt(wk_date, context, cache, backend=backend, cache_path=cache_path)
        weekly_tilts[wk_date] = tilt
        latencies.append(latency)
        if tilt["reasoning"].startswith("PARSE_FAILURE"):
            malformed_count += 1

        top_bias = max(tilt["symbol_bias"].items(), key=lambda kv: abs(kv[1]), default=None)
        print(f"  [{backend}][{i}/{len(week_starts)}] {ds}  regime={tilt['regime_view']:9s}  "
              f"top_bias={top_bias}  conf={tilt['confidence']:.2f}  latency={latency:.1f}s")

    return {
        "weekly_tilts": weekly_tilts,
        "latencies": latencies,
        "malformed_count": malformed_count,
    }


def score_model(name: str, weekly_tilts: dict[date, dict]) -> dict:
    sorted_weeks = sorted(weekly_tilts.keys())

    # risk-on turn timing: first week flagged risk_on
    first_risk_on = None
    for wk in sorted_weeks:
        if weekly_tilts[wk]["regime_view"] == "risk_on":
            first_risk_on = wk
            break

    # chop defense: weeks in [CHOP_WINDOW_START, CHOP_WINDOW_END] flagged risk_off
    chop_weeks = [wk for wk in sorted_weeks if CHOP_WINDOW_START <= wk <= CHOP_WINDOW_END]
    chop_risk_off_count = sum(1 for wk in chop_weeks if weekly_tilts[wk]["regime_view"] == "risk_off")

    # decisiveness
    n = len(sorted_weeks)
    n_mixed = sum(1 for wk in sorted_weeks if weekly_tilts[wk]["regime_view"] == "mixed")
    pct_mixed = round(100 * n_mixed / n, 1) if n else None
    all_biases = [abs(v) for wk in sorted_weeks for v in weekly_tilts[wk]["symbol_bias"].values()]
    mean_abs_bias = round(sum(all_biases) / len(all_biases), 5) if all_biases else 0.0
    mean_confidence = round(sum(weekly_tilts[wk]["confidence"] for wk in sorted_weeks) / n, 3) if n else None

    # claw agreement
    claw_table = h.build_claw_calibration(weekly_tilts)
    agree_ct = sum(1 for r in claw_table if r["agreement"].startswith("agree"))
    disagree_ct = sum(1 for r in claw_table if r["agreement"].startswith("disagree"))
    neutral_ct = len(claw_table) - agree_ct - disagree_ct

    return {
        "model": name,
        "first_risk_on_date": first_risk_on.isoformat() if first_risk_on else None,
        "risk_on_before_claw_entry": (first_risk_on is not None and first_risk_on <= CLAW_RISK_ON_DATE),
        "chop_defense_risk_off_weeks": chop_risk_off_count,
        "chop_defense_total_weeks": len(chop_weeks),
        "pct_weeks_mixed": pct_mixed,
        "mean_abs_symbol_bias": mean_abs_bias,
        "mean_confidence": mean_confidence,
        "claw_agree": agree_ct,
        "claw_disagree": disagree_ct,
        "claw_neutral": neutral_ct,
        "claw_calibration_table": claw_table,
    }


def main():
    provider = CachedPriceProvider()
    symbols = get_all_yahoo_symbols()
    for extra in ("SPY", "QQQ"):
        if extra not in symbols:
            symbols.append(extra)

    fetch_start = date(h.WINDOW_START.year - 2, h.WINDOW_START.month, h.WINDOW_START.day)
    print(f"Fetching {len(symbols)} symbols {fetch_start} -> {h.WINDOW_END}...")
    prices = provider.get_multi_prices(symbols, fetch_start, h.WINDOW_END)
    if prices.empty:
        print("ERROR: no price data")
        sys.exit(1)
    print(f"{len(prices)} price rows fetched")

    # Same weekly decision-day cadence and baseline state the harness uses.
    all_days = pd.bdate_range(h.WINDOW_START, h.WINDOW_END).date.tolist()
    week_starts = []
    seen_weeks = set()
    for d in all_days:
        wk = d.isocalendar()[:2]
        if wk not in seen_weeks:
            seen_weeks.add(wk)
            week_starts.append(d)
    print(f"{len(week_starts)} weekly decision days")

    baseline_decisions, baseline_equity, _ = h.run_replay(prices, h.build_engine(), tilts_by_week=None)
    decisions_by_date = {d["date"]: d for d in baseline_decisions}
    equity_by_date = {d["date"]: d for d in baseline_equity}

    days_held = 0
    prev_holding = None
    holding_since = {}
    cur_since = None
    for d in baseline_equity:
        hld = d["holding"]
        if hld != prev_holding:
            cur_since = d["date"]
        holding_since[d["date"]] = cur_since
        prev_holding = hld

    tilt_engine_assets = h.build_engine().dual_momentum.assets

    results = {}
    backend_stats = {}
    for backend in LIVE_BACKENDS:
        print(f"\n=== Running backend: {backend} ({len(week_starts)} weeks) ===")
        r = run_backend(backend, prices, week_starts, decisions_by_date, equity_by_date, holding_since, tilt_engine_assets)
        results[backend] = r["weekly_tilts"]
        live_lat = [l for l in r["latencies"] if l > 0]
        backend_stats[backend] = {
            "n_live_calls": len(live_lat),
            "n_cache_hits": sum(1 for l in r["latencies"] if l == 0),
            "total_latency_s": round(sum(r["latencies"]), 1),
            "avg_live_latency_s": round(sum(live_lat) / len(live_lat), 1) if live_lat else None,
            "max_latency_s": round(max(r["latencies"]), 1) if r["latencies"] else 0,
            "malformed_json_count": r["malformed_count"],
            "malformed_json_rate": round(r["malformed_count"] / len(week_starts), 3),
        }
        print(f"  {backend} done: {backend_stats[backend]}")

    # Load sonnet's already-run results for comparison.
    print(f"\n=== Loading sonnet results from {SONNET_REPLAY_PATH} ===")
    sonnet_data = json.loads(SONNET_REPLAY_PATH.read_text())
    sonnet_weekly = {}
    for row in sonnet_data["weekly_ai_tilts"]:
        sonnet_weekly[date.fromisoformat(row["date"])] = {
            "regime_view": row["regime_view"],
            "symbol_bias": row["symbol_bias"],
            "confidence": row["confidence"],
            "reasoning": row["reasoning"],
        }
    results["sonnet"] = sonnet_weekly
    sonnet_cache = h.load_cache(h.CACHE_PATH)
    sonnet_malformed = sum(
        1 for rec in sonnet_cache.values()
        if rec["tilt"]["reasoning"].startswith("PARSE_FAILURE")
    )
    backend_stats["sonnet"] = {
        "n_live_calls": sonnet_data["cli_stats"]["n_live_calls"],
        "n_cache_hits": sonnet_data["cli_stats"]["n_cache_hits"],
        "total_latency_s": sonnet_data["cli_stats"]["total_latency_s"],
        "avg_live_latency_s": sonnet_data["cli_stats"]["avg_live_latency_s"],
        "max_latency_s": sonnet_data["cli_stats"]["max_latency_s"],
        "malformed_json_count": sonnet_malformed,
        "malformed_json_rate": round(sonnet_malformed / len(week_starts), 3),
        "note": "loaded from existing ai_tilt_replay.json, not re-called",
    }

    print("\n=== Scoring ===")
    scorecards = {}
    for name, weekly_tilts in results.items():
        scorecards[name] = score_model(name, weekly_tilts)

    # comparison table
    print(f"\n{'model':10s} {'first_risk_on':14s} {'chop_risk_off':14s} {'pct_mixed':10s} "
          f"{'mean_|bias|':12s} {'mean_conf':10s} {'claw_agree':11s} {'malformed':10s}")
    for name in ["sonnet", "fable5", "gpt55", "gpt56sol"]:
        sc = scorecards[name]
        st = backend_stats[name]
        chop_str = f"{sc['chop_defense_risk_off_weeks']}/{sc['chop_defense_total_weeks']}"
        claw_str = f"{sc['claw_agree']}/{sc['claw_disagree']}/{sc['claw_neutral']}"
        print(f"{name:10s} {str(sc['first_risk_on_date']):14s} "
              f"{chop_str:14s} "
              f"{str(sc['pct_weeks_mixed']):10s} {str(sc['mean_abs_symbol_bias']):12s} "
              f"{str(sc['mean_confidence']):10s} "
              f"{claw_str:11s} "
              f"{st['malformed_json_rate']:10.3f}")

    weekly_timelines = {}
    for name, weekly_tilts in results.items():
        weekly_timelines[name] = [
            {
                "date": wk.isoformat(),
                "regime_view": weekly_tilts[wk]["regime_view"],
                "symbol_bias": weekly_tilts[wk]["symbol_bias"],
                "confidence": weekly_tilts[wk]["confidence"],
                "reasoning": weekly_tilts[wk]["reasoning"],
            }
            for wk in sorted(weekly_tilts.keys())
        ]

    out = {
        "window": {"start": h.WINDOW_START.isoformat(), "end": h.WINDOW_END.isoformat()},
        "claw_risk_on_entry_date": CLAW_RISK_ON_DATE.isoformat(),
        "chop_window": {"start": CHOP_WINDOW_START.isoformat(), "end": CHOP_WINDOW_END.isoformat()},
        "scorecards": scorecards,
        "cli_stats": backend_stats,
        "weekly_timelines": weekly_timelines,
        "notes": [
            "sonnet scores are loaded from the existing data/edge_decomposition/ai_tilt_replay.json "
            "(already run with `claude --model sonnet`) -- not re-called here.",
            "fable5 backend: `claude --model claude-fable-5 -p ... --output-format json`.",
            "gpt55 backend: `codex exec --model gpt-5.5 --json ...` with stdin redirected from "
            "/dev/null (codex exec otherwise blocks waiting for piped input); parsed from the "
            "'item.completed' / agent_message event's 'text' field in the newline-delimited JSON "
            "stream on stdout. No markdown-fence stripping was needed in testing -- gpt-5.5 returned "
            "bare JSON consistently.",
            "gpt56sol backend: `codex exec --model gpt-5.6-sol --json ...`, same parsing as gpt55. "
            "Model identity was verified before running: plain `gpt-5.6` and an intentionally bogus "
            "model string both get a server-side 'Model metadata ... not found, defaulting to "
            "fallback metadata' item.completed error followed by an explicit 400 "
            "('not supported when using Codex with a ChatGPT account'); gpt-5.6-sol produces neither "
            "-- no metadata warning, no 400, normal agent turn -- identical behavior to the known-good "
            "gpt-5.5 control. That distinguishes 'resolves to a real allowed model' from 'unknown "
            "string silently falls back to account default'.",
            "Backend quirk (gpt55 and gpt56sol both): codex autonomously ran shell commands "
            "(reading ~/.codex/skills/using-superpowers/SKILL.md) in response to trivial verification "
            "prompts that did not ask for any tool use -- unrequested tool invocation worth weighing "
            "if codex is ever used as an unattended overlay operator.",
            "Cache files are per-backend: ai_tilt_cache_fable5.jsonl, ai_tilt_cache_gpt55.jsonl, "
            "ai_tilt_cache_gpt56sol.jsonl (sonnet's is the original ai_tilt_cache.jsonl, untouched).",
            "claw_agree/claw_disagree/claw_neutral counts include the 'neutral (...)' and "
            "'no_ai_data' rows in the neutral bucket.",
            "P&L replay was intentionally skipped -- tilts are known-inert in this window per the "
            "task brief; this is a pure judgment/format scorecard.",
        ],
    }

    OUT_PATH.write_text(json.dumps(out, indent=2))
    print(f"\nWrote {OUT_PATH}")


if __name__ == "__main__":
    main()
