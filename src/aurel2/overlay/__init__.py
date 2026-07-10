"""AI overlay engine: capped transition powers on top of Aurel2's dual-momentum core.

Containment (docs/plans/2026-07-10-ai-overlay-design.md, "Hard containment"):
the overlay's ONLY write surfaces are data/{mode}/overlay_tilt.json,
data/{mode}/overlay_state.json, data/{mode}/claw_shadow.jsonl, and trade
journal entries. It cannot place orders, touch config, or introduce symbols.
"""
