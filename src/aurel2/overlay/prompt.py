"""Pack-agnostic prompt rendering for the overlay operator.

Track B (docs/plans/2026-07-10-ai-overlay-design.md, "Build tracks") owns the
exact context-pack fields (src/aurel2/overlay/context_pack.py, frozen after
the single ablation step). This module does NOT assume any specific field
beyond "a JSON-serializable dict" — render_prompt just serializes whatever
pack it's given under the fixed instruction/schema wrapper, so dropping in
the frozen v2/v3 pack spec requires no change here.
"""

import json
from typing import Any

TILT_JSON_SCHEMA = (
    '{"as_of": "YYYY-MM-DD", "expires": "YYYY-MM-DD", '
    '"regime_view": "risk_on|mixed|risk_off", "confidence": 0..1, '
    '"powers": {'
    '"accelerate_entry": {"symbol": "<TICKER already in the data below>|null"}, '
    '"lookback_override_months": "3|6|null", '
    '"force_defensive_contest": true|false'
    '}, '
    '"reasoning": "<2-4 sentences>"}'
)

PROMPT_TEMPLATE = """You are a discretionary overlay on a mechanical dual-momentum trading strategy. \
You may activate capped, pre-defined "transition powers" — you may NEVER introduce a new asset, \
place an order, or change any configuration. Assume you know NOTHING about markets after the \
"as_of" date in the data below — do not use any memorized knowledge of what happened after that \
date, reason only from the data given.

Context (all data strictly as of the close before as_of):
{context_json}

The three powers available to you, all optional:
1. accelerate_entry.symbol — enter EARLY into whatever asset the core strategy's own ranking \
would pick next if current short-term trends persist. You may only name a symbol that already \
appears in the data below as a current holding, momentum-ranked, or watchlist asset — never a \
symbol the data doesn't mention. Use null to decline.
2. lookback_override_months — request the core's ranking run at a 3-month or 6-month lookback \
instead of 12 months, for a sustained risk_off regime. Use null to decline.
3. force_defensive_contest — request a one-off restricted ranking of defensive assets (gold, \
aggregate/short/intermediate treasuries, TIPS, cash) against the currently held asset. Use false \
to decline.

Given ONLY this data, return STRICT JSON with no prose, no markdown fences, matching exactly this \
schema:
{tilt_schema}

Decline every power (all null/false) unless the data gives you real conviction."""


def render_prompt(pack: dict[str, Any]) -> str:
    """Render the full prompt for a single independent CLI sample given a
    context pack. `pack` is treated as an opaque, JSON-serializable dict —
    no field access beyond json.dumps.
    """
    return PROMPT_TEMPLATE.format(
        context_json=json.dumps(pack, indent=2, default=str),
        tilt_schema=TILT_JSON_SCHEMA,
    )
