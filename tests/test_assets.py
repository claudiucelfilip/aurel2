"""Tests for asset registry."""

import pytest
from aurel2.core.assets import ASSET_REGISTRY, get_asset, get_assets_by_category
from aurel2.core.models import AssetClass, AssetCategory


class TestAssetRegistry:
    """Tests for the ASSET_REGISTRY dictionary."""

    def test_registry_has_core_assets(self):
        """Registry should have all core assets."""
        assert AssetClass.US_STOCKS in ASSET_REGISTRY
        assert AssetClass.INTL_DEVELOPED in ASSET_REGISTRY
        assert AssetClass.EMERGING_MARKETS in ASSET_REGISTRY
        assert AssetClass.GOLD in ASSET_REGISTRY

    def test_registry_has_sector_assets(self):
        """Registry should have all sector assets."""
        assert AssetClass.TECH_SECTOR in ASSET_REGISTRY
        assert AssetClass.FINANCIAL_SECTOR in ASSET_REGISTRY
        assert AssetClass.ENERGY_SECTOR in ASSET_REGISTRY
        assert AssetClass.HEALTHCARE_SECTOR in ASSET_REGISTRY

    def test_registry_has_fixed_income_assets(self):
        """Registry should have fixed income assets."""
        assert AssetClass.BONDS_AGGREGATE in ASSET_REGISTRY
        assert AssetClass.BONDS_TREASURY in ASSET_REGISTRY

    def test_registry_has_alternative_assets(self):
        """Registry should have alternative assets."""
        assert AssetClass.GOLD in ASSET_REGISTRY
        assert AssetClass.COMMODITIES in ASSET_REGISTRY

    def test_registry_has_cash(self):
        """Registry should have cash asset."""
        assert AssetClass.CASH in ASSET_REGISTRY

    def test_registry_asset_count(self):
        """Registry should have all assets (excluding aliases)."""
        # 3 core equity + 1 small-cap value + 4 sectors + 5 fixed income + 1 REIT + 2 alternatives + 1 cash = 17
        assert len(ASSET_REGISTRY) == 17

    def test_assets_have_correct_asset_class(self):
        """Each asset should have matching asset_class."""
        for asset_class, asset in ASSET_REGISTRY.items():
            assert asset.asset_class == asset_class


class TestGetAsset:
    """Tests for get_asset function."""

    def test_get_asset_us_stocks(self):
        """get_asset returns correct asset for US_STOCKS."""
        spy = get_asset(AssetClass.US_STOCKS)
        assert spy.symbol == "SPY"
        assert spy.yahoo_symbol == "SPY"
        assert spy.category == AssetCategory.EQUITY

    def test_get_asset_intl_developed(self):
        """get_asset returns correct asset for INTL_DEVELOPED."""
        efa = get_asset(AssetClass.INTL_DEVELOPED)
        assert efa.symbol == "EFA"
        assert efa.yahoo_symbol == "EFA"

    def test_get_asset_gold(self):
        """get_asset returns correct asset for GOLD."""
        gld = get_asset(AssetClass.GOLD)
        assert gld.symbol == "GLD"
        assert gld.category == AssetCategory.ALTERNATIVE

    def test_get_asset_bonds(self):
        """get_asset returns correct asset for BONDS_AGGREGATE."""
        agg = get_asset(AssetClass.BONDS_AGGREGATE)
        assert agg.symbol == "AGG"
        assert agg.category == AssetCategory.FIXED_INCOME

    def test_get_asset_cash(self):
        """get_asset returns correct asset for CASH."""
        cash = get_asset(AssetClass.CASH)
        assert cash.symbol == "CASH"
        assert cash.category == AssetCategory.CASH

    def test_get_asset_invalid_raises(self):
        """get_asset raises KeyError for invalid asset class."""
        with pytest.raises(KeyError):
            get_asset("INVALID")


class TestGetAssetsByCategory:
    """Tests for get_assets_by_category function."""

    def test_get_assets_by_category_equity(self):
        """get_assets_by_category filters correctly for EQUITY."""
        equities = get_assets_by_category(AssetCategory.EQUITY)
        # 3 core equity + 4 sectors = 7
        assert len(equities) >= 7

        for asset in equities:
            assert asset.category == AssetCategory.EQUITY

    def test_get_assets_by_category_fixed_income(self):
        """get_assets_by_category filters correctly for FIXED_INCOME."""
        fixed_income = get_assets_by_category(AssetCategory.FIXED_INCOME)
        assert len(fixed_income) == 5  # AGG, TLT, SHY, IEF, TIP

        for asset in fixed_income:
            assert asset.category == AssetCategory.FIXED_INCOME

    def test_get_assets_by_category_alternative(self):
        """get_assets_by_category filters correctly for ALTERNATIVE."""
        alternatives = get_assets_by_category(AssetCategory.ALTERNATIVE)
        assert len(alternatives) == 3  # GLD, DBC, VNQ

        for asset in alternatives:
            assert asset.category == AssetCategory.ALTERNATIVE

    def test_get_assets_by_category_cash(self):
        """get_assets_by_category filters correctly for CASH."""
        cash_assets = get_assets_by_category(AssetCategory.CASH)
        assert len(cash_assets) == 1  # CASH only

        for asset in cash_assets:
            assert asset.category == AssetCategory.CASH


class TestAssetDetails:
    """Tests for specific asset details."""

    def test_spy_has_ucits_equivalent(self):
        """SPY should have CSPX as UCITS equivalent."""
        spy = get_asset(AssetClass.US_STOCKS)
        assert spy.ucits_symbol == "CSPX.L"
        assert spy.isin == "US78462F1030"

    def test_efa_has_ucits_equivalent(self):
        """EFA should have VWRA as UCITS equivalent."""
        efa = get_asset(AssetClass.INTL_DEVELOPED)
        assert efa.ucits_symbol == "VWRA.L"

    def test_eem_has_ucits_equivalent(self):
        """EEM should have EIMI as UCITS equivalent."""
        eem = get_asset(AssetClass.EMERGING_MARKETS)
        assert eem.ucits_symbol == "EIMI.L"

    def test_agg_has_ucits_equivalent(self):
        """AGG should have AGGH as UCITS equivalent."""
        agg = get_asset(AssetClass.BONDS_AGGREGATE)
        assert agg.ucits_symbol == "AGGH.L"

    def test_gld_has_ucits_equivalent(self):
        """GLD should have SGLD as UCITS equivalent."""
        gld = get_asset(AssetClass.GOLD)
        assert gld.ucits_symbol == "SGLD.L"
