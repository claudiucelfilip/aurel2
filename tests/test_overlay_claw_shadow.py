"""Claw shadow-scorer tests: read-only, logged to JSONL, mocked codex CLI, never applied."""

import json
from unittest.mock import patch

from aurel2.overlay.claw_shadow import claw_shadow_path_for_mode, run_claw_shadow


class TestClawShadow:
    @patch("aurel2.overlay.claw_shadow.call_codex_cli")
    def test_logs_jsonl_record(self, mock_call, tmp_path):
        mock_call.return_value = ({"regime_view": "risk_on"}, 1.2, '{"regime_view": "risk_on"}')
        log_path = tmp_path / "claw_shadow.jsonl"

        record = run_claw_shadow({"as_of": "2026-07-14"}, log_path=str(log_path))

        assert record["parsed"] == {"regime_view": "risk_on"}
        assert log_path.exists()
        lines = log_path.read_text().splitlines()
        assert len(lines) == 1
        logged = json.loads(lines[0])
        assert logged["as_of"] == "2026-07-14"

    @patch("aurel2.overlay.claw_shadow.call_codex_cli")
    def test_appends_multiple_records(self, mock_call, tmp_path):
        mock_call.return_value = ({"regime_view": "mixed"}, 1.0, "{}")
        log_path = tmp_path / "claw_shadow.jsonl"

        run_claw_shadow({"as_of": "2026-07-14"}, log_path=str(log_path))
        run_claw_shadow({"as_of": "2026-07-21"}, log_path=str(log_path))

        lines = log_path.read_text().splitlines()
        assert len(lines) == 2

    @patch("aurel2.overlay.claw_shadow.call_codex_cli")
    def test_cli_failure_is_recorded_not_raised(self, mock_call, tmp_path):
        mock_call.return_value = (None, 0.5, "TIMEOUT")
        log_path = tmp_path / "claw_shadow.jsonl"

        record = run_claw_shadow({"as_of": "2026-07-14"}, log_path=str(log_path))
        assert record["parsed"] is None
        assert "TIMEOUT" in record["raw_response"]

    def test_default_path_uses_mode(self):
        assert claw_shadow_path_for_mode("paper") == "data/paper/claw_shadow.jsonl"
        assert claw_shadow_path_for_mode("live") == "data/live/claw_shadow.jsonl"
