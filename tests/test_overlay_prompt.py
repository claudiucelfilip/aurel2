"""render_prompt is pack-agnostic: it must not assume any specific pack field."""

import json

from aurel2.overlay.prompt import render_prompt


class TestRenderPromptPackAgnostic:
    def test_serializes_arbitrary_pack_fields(self):
        pack = {"as_of": "2026-07-14", "some_v2_field": {"nested": [1, 2, 3]}, "another": "x"}
        prompt = render_prompt(pack)
        assert "some_v2_field" in prompt
        assert "2026-07-14" in prompt

    def test_includes_schema_and_power_names(self):
        prompt = render_prompt({"as_of": "2026-07-14"})
        assert "accelerate_entry" in prompt
        assert "lookback_override_months" in prompt
        assert "force_defensive_contest" in prompt
        assert "regime_view" in prompt

    def test_handles_non_json_native_values_via_default_str(self):
        from datetime import date

        pack = {"as_of": date(2026, 7, 14)}
        prompt = render_prompt(pack)
        assert "2026-07-14" in prompt
