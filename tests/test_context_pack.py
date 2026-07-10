"""Tests for the frozen AI-overlay context-pack builders (v1/v2/v3)."""

import json
from datetime import date

import pytest

from aurel2.core.assets import ASSET_REGISTRY
from aurel2.core.models import AssetClass
from aurel2.overlay.context_pack import BUILDERS, clip_to_as_of
from aurel2.overlay.data_snapshot import SNAPSHOT_PATH, load_snapshot

pytestmark = pytest.mark.skipif(
    not SNAPSHOT_PATH.exists(), reason="pinned price snapshot not committed at this path"
)


@pytest.fixture(scope="module")
def prices():
    return load_snapshot()


@pytest.fixture(scope="module")
def dm_assets():
    return {ac: a for ac, a in ASSET_REGISTRY.items() if ac != AssetClass.CASH}


class TestClipToAsOf:
    def test_excludes_as_of_date_itself(self, prices):
        as_of = date(2026, 3, 2)
        clipped = clip_to_as_of(prices, as_of)
        assert (clipped["date"].dt.date < as_of).all()


class TestBuildersJsonSerializable:
    @pytest.mark.parametrize("version", ["v1", "v2", "v3"])
    def test_pack_is_json_serializable(self, prices, dm_assets, version):
        pack = BUILDERS[version](
            prices=prices,
            calc_date=date(2026, 3, 2),
            dm_assets=dm_assets,
            current_holding_symbol="GLD",
            days_held=15,
            deterministic_signal={"action": "hold", "symbol": "GLD", "reason": "test"},
        )
        json.dumps(pack)  # raises on numpy bool_/int64/float64 leaks


class TestV2AddsTechnicals:
    def test_v2_has_rsi_and_zscore_for_full_universe(self, prices, dm_assets):
        pack = BUILDERS["v2"](
            prices=prices, calc_date=date(2026, 3, 2), dm_assets=dm_assets,
            current_holding_symbol="GLD", days_held=15, deterministic_signal={},
        )
        assert "per_asset_technicals" in pack
        assert "momentum_spread_alarm" in pack
        assert "SPY" in pack["per_asset_technicals"]
        assert set(pack["per_asset_technicals"]["SPY"].keys()) == {"rsi_14", "zscore_60d"}

    def test_v1_lacks_v2_fields(self, prices, dm_assets):
        pack = BUILDERS["v1"](
            prices=prices, calc_date=date(2026, 3, 2), dm_assets=dm_assets,
            current_holding_symbol="GLD", days_held=15, deterministic_signal={},
        )
        assert "per_asset_technicals" not in pack


class TestV3AddsSenses:
    def test_v3_has_breadth_and_self_awareness(self, prices, dm_assets):
        pack = BUILDERS["v3"](
            prices=prices, calc_date=date(2026, 3, 2), dm_assets=dm_assets,
            current_holding_symbol="GLD", days_held=15, deterministic_signal={},
        )
        assert "breadth" in pack
        assert "vol_rate_of_change" in pack
        assert "relative_strength_vs_equity_3m_pp" in pack
        assert "bond_etf_spreads" in pack
        assert "self_awareness" in pack
        assert "core_projected_next_action" in pack["self_awareness"]


class TestNoLookahead:
    def test_last_20d_pct_change_never_includes_as_of_date(self, prices, dm_assets):
        as_of = date(2026, 3, 2)
        pack = BUILDERS["v1"](
            prices=prices, calc_date=as_of, dm_assets=dm_assets,
            current_holding_symbol="GLD", days_held=15, deterministic_signal={},
        )
        hist = clip_to_as_of(prices, as_of)
        max_hist_date = hist["date"].max().date()
        assert max_hist_date < as_of


class TestDeterminism:
    def test_same_inputs_produce_identical_pack(self, prices, dm_assets):
        kwargs = dict(
            prices=prices, calc_date=date(2026, 3, 2), dm_assets=dm_assets,
            current_holding_symbol="GLD", days_held=15,
            deterministic_signal={"action": "hold", "symbol": "GLD", "reason": "test"},
        )
        pack_a = BUILDERS["v3"](**kwargs)
        pack_b = BUILDERS["v3"](**kwargs)
        assert json.dumps(pack_a, sort_keys=True) == json.dumps(pack_b, sort_keys=True)
