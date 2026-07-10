"""Schema validation tests: malformed/expired/schema-invalid tilt = no-op."""

from datetime import date

from aurel2.overlay.schema import Tilt, load_tilt, validate_tilt, write_tilt

VALID_TILT = {
    "as_of": "2026-07-14",
    "expires": "2026-07-21",
    "regime_view": "risk_on",
    "confidence": 0.8,
    "powers": {
        "accelerate_entry": {"symbol": "QQQ"},
        "lookback_override_months": None,
        "force_defensive_contest": False,
    },
    "reasoning": "Strong uptrend.",
    "samples": 5,
    "sample_agreement": 0.8,
}


class TestValidateTiltValid:
    def test_valid_tilt_returns_tilt_object(self):
        tilt = validate_tilt(VALID_TILT, now=date(2026, 7, 15))
        assert isinstance(tilt, Tilt)
        assert tilt.regime_view == "risk_on"
        assert tilt.accelerate_entry_symbol == "QQQ"
        assert tilt.confidence == 0.8

    def test_valid_tilt_all_powers_null(self):
        t = dict(VALID_TILT)
        t["powers"] = {
            "accelerate_entry": {"symbol": None},
            "lookback_override_months": 6,
            "force_defensive_contest": True,
        }
        tilt = validate_tilt(t, now=date(2026, 7, 15))
        assert tilt is not None
        assert tilt.accelerate_entry_symbol is None
        assert tilt.lookback_override_months == 6
        assert tilt.force_defensive_contest is True

    def test_expiry_is_inclusive_on_expires_day(self):
        tilt = validate_tilt(VALID_TILT, now=date(2026, 7, 21))
        assert tilt is not None


class TestValidateTiltExpired:
    def test_expired_tilt_is_none(self):
        tilt = validate_tilt(VALID_TILT, now=date(2026, 7, 22))
        assert tilt is None


class TestValidateTiltMalformed:
    def test_not_a_dict(self):
        assert validate_tilt("not a dict", now=date(2026, 7, 15)) is None
        assert validate_tilt(None, now=date(2026, 7, 15)) is None
        assert validate_tilt([1, 2, 3], now=date(2026, 7, 15)) is None

    def test_missing_as_of(self):
        t = dict(VALID_TILT)
        del t["as_of"]
        assert validate_tilt(t, now=date(2026, 7, 15)) is None

    def test_bad_date_format(self):
        t = dict(VALID_TILT)
        t["as_of"] = "07/14/2026"
        assert validate_tilt(t, now=date(2026, 7, 15)) is None

    def test_invalid_regime_view(self):
        t = dict(VALID_TILT)
        t["regime_view"] = "super_bullish"
        assert validate_tilt(t, now=date(2026, 7, 15)) is None

    def test_confidence_out_of_range(self):
        t = dict(VALID_TILT)
        t["confidence"] = 1.5
        assert validate_tilt(t, now=date(2026, 7, 15)) is None

    def test_confidence_not_numeric(self):
        t = dict(VALID_TILT)
        t["confidence"] = "high"
        assert validate_tilt(t, now=date(2026, 7, 15)) is None

    def test_missing_powers(self):
        t = dict(VALID_TILT)
        del t["powers"]
        assert validate_tilt(t, now=date(2026, 7, 15)) is None

    def test_invalid_lookback_override_months(self):
        t = dict(VALID_TILT)
        t["powers"] = dict(VALID_TILT["powers"])
        t["powers"]["lookback_override_months"] = 12
        assert validate_tilt(t, now=date(2026, 7, 15)) is None

    def test_force_defensive_contest_not_bool(self):
        t = dict(VALID_TILT)
        t["powers"] = dict(VALID_TILT["powers"])
        t["powers"]["force_defensive_contest"] = "yes"
        assert validate_tilt(t, now=date(2026, 7, 15)) is None

    def test_accelerate_entry_not_dict(self):
        t = dict(VALID_TILT)
        t["powers"] = dict(VALID_TILT["powers"])
        t["powers"]["accelerate_entry"] = "QQQ"
        assert validate_tilt(t, now=date(2026, 7, 15)) is None

    def test_samples_not_int(self):
        t = dict(VALID_TILT)
        t["samples"] = "5"
        assert validate_tilt(t, now=date(2026, 7, 15)) is None

    def test_sample_agreement_out_of_range(self):
        t = dict(VALID_TILT)
        t["sample_agreement"] = 1.2
        assert validate_tilt(t, now=date(2026, 7, 15)) is None

    def test_confidence_bool_rejected(self):
        # bool is an int subclass in Python; must not silently pass as numeric.
        t = dict(VALID_TILT)
        t["confidence"] = True
        assert validate_tilt(t, now=date(2026, 7, 15)) is None


class TestLoadTilt:
    def test_missing_file_returns_none(self, tmp_path):
        path = tmp_path / "does_not_exist.json"
        assert load_tilt(str(path)) is None

    def test_unreadable_json_returns_none(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not valid json")
        assert load_tilt(str(path)) is None

    def test_valid_file_roundtrips(self, tmp_path):
        path = tmp_path / "overlay_tilt.json"
        write_tilt(str(path), VALID_TILT)
        tilt = load_tilt(str(path), now=date(2026, 7, 15))
        assert tilt is not None
        assert tilt.regime_view == "risk_on"

    def test_expired_file_returns_none(self, tmp_path):
        path = tmp_path / "overlay_tilt.json"
        write_tilt(str(path), VALID_TILT)
        tilt = load_tilt(str(path), now=date(2026, 8, 1))
        assert tilt is None
