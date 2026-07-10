"""Claw shadow-scorer: weekly, read-only comparison telemetry.

Sends the SAME context pack the overlay operator sees to the codex CLI
(gpt-5.5), asking what Claw would do, and logs the raw response to
data/{mode}/claw_shadow.jsonl. This is PURE comparison telemetry — its output
is never read by powers.py / apply_overlay, never feeds into the tilt file,
and never influences any decision. It exists only so the parallel-run
scorecard (docs/plans/2026-07-10-ai-overlay-design.md, "Parallel run &
graduation") can report Claw-agreement alongside the overlay's own record.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import structlog

from aurel2.overlay.cli_backends import call_codex_cli
from aurel2.overlay.prompt import render_prompt

logger = structlog.get_logger()

DEFAULT_CODEX_MODEL = "gpt-5.5"


def claw_shadow_path_for_mode(mode: str = "paper") -> str:
    return f"data/{mode}/claw_shadow.jsonl"


def run_claw_shadow(
    pack: dict[str, Any],
    mode: str = "paper",
    model: str = DEFAULT_CODEX_MODEL,
    log_path: Optional[str] = None,
) -> dict:
    """Single read-only capture of what Claw (via codex CLI) would do given
    the same context pack. Appends one JSONL record; returns that record.
    Never raises on a CLI failure -- records the failure instead, since this
    is telemetry, not a decision path.
    """
    path = log_path or claw_shadow_path_for_mode(mode)
    prompt = render_prompt(pack)
    parsed, latency, raw = call_codex_cli(prompt, model=model)

    record = {
        "timestamp": datetime.now().isoformat(),
        "as_of": pack.get("as_of"),
        "model": model,
        "latency_s": round(latency, 1),
        "parsed": parsed,
        "raw_response": raw[:2000] if isinstance(raw, str) else str(raw)[:2000],
    }

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps(record) + "\n")

    logger.info("claw_shadow_recorded", as_of=record["as_of"], parsed_ok=parsed is not None)
    return record


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Run one read-only Claw shadow-score capture from a context pack JSON file.")
    parser.add_argument("pack_path", help="Path to a JSON file containing the context pack")
    parser.add_argument("--mode", default="paper", choices=["paper", "live"])
    parser.add_argument("--model", default=DEFAULT_CODEX_MODEL)
    args = parser.parse_args()

    pack = json.loads(Path(args.pack_path).read_text())
    record = run_claw_shadow(pack, mode=args.mode, model=args.model)
    json.dump(record, sys.stdout, indent=2)
    print()
