"""Tests for core domain models."""

import pytest
from aurel2.core.models import Asset, AssetClass, AssetCategory


class TestAssetCategory:
    """Tests for AssetCategory enum."""

    def test_has_equity_value(self):
        """AssetCategory should have EQUITY value."""
        assert AssetCategory.EQUITY.value == "equity"

    def test_has_fixed_income_value(self):
        """AssetCategory should have FIXED_INCOME value."""
        assert AssetCategory.FIXED_INCOME.value == "fixed_income"

    def test_has_alternative_value(self):
        """AssetCategory should have ALTERNATIVE value."""
        assert AssetCategory.ALTERNATIVE.value == "alternative"

    def test_has_cash_value(self):
        """AssetCategory should have CASH value."""
        assert AssetCategory.CASH.value == "cash"

    def test_all_categories_exist(self):
        """All required categories should exist."""
        expected_categories = {"EQUITY", "FIXED_INCOME", "ALTERNATIVE", "CASH"}
        actual_categories = {c.name for c in AssetCategory}
        assert expected_categories == actual_categories


class TestAssetClass:
    """Tests for extended AssetClass enum."""

    def test_has_us_stocks(self):
        """AssetClass should have US_STOCKS (core equity)."""
        assert AssetClass.US_STOCKS.value == "us_stocks"

    def test_has_intl_developed(self):
        """AssetClass should have INTL_DEVELOPED (core equity)."""
        assert AssetClass.INTL_DEVELOPED.value == "intl_developed"

    def test_has_emerging_markets(self):
        """AssetClass should have EMERGING_MARKETS (core equity)."""
        assert AssetClass.EMERGING_MARKETS.value == "emerging_markets"

    def test_has_tech_sector(self):
        """AssetClass should have TECH_SECTOR."""
        assert AssetClass.TECH_SECTOR.value == "tech_sector"

    def test_has_financial_sector(self):
        """AssetClass should have FINANCIAL_SECTOR."""
        assert AssetClass.FINANCIAL_SECTOR.value == "financial_sector"

    def test_has_energy_sector(self):
        """AssetClass should have ENERGY_SECTOR."""
        assert AssetClass.ENERGY_SECTOR.value == "energy_sector"

    def test_has_healthcare_sector(self):
        """AssetClass should have HEALTHCARE_SECTOR."""
        assert AssetClass.HEALTHCARE_SECTOR.value == "healthcare_sector"

    def test_has_bonds_aggregate(self):
        """AssetClass should have BONDS_AGGREGATE (fixed income)."""
        assert AssetClass.BONDS_AGGREGATE.value == "bonds_aggregate"

    def test_has_bonds_treasury(self):
        """AssetClass should have BONDS_TREASURY (fixed income)."""
        assert AssetClass.BONDS_TREASURY.value == "bonds_treasury"

    def test_has_gold(self):
        """AssetClass should have GOLD (alternatives)."""
        assert AssetClass.GOLD.value == "gold"

    def test_has_commodities(self):
        """AssetClass should have COMMODITIES (alternatives)."""
        assert AssetClass.COMMODITIES.value == "commodities"

    def test_has_cash(self):
        """AssetClass should have CASH."""
        assert AssetClass.CASH.value == "cash"

    def test_global_stocks_alias(self):
        """GLOBAL_STOCKS should be alias for INTL_DEVELOPED (backward compatibility)."""
        assert AssetClass.GLOBAL_STOCKS.value == "intl_developed"

    def test_bonds_alias(self):
        """BONDS should be alias for BONDS_AGGREGATE (backward compatibility)."""
        assert AssetClass.BONDS.value == "bonds_aggregate"

    def test_all_asset_classes_exist(self):
        """All required asset classes should exist."""
        # Note: GLOBAL_STOCKS and BONDS are aliases and don't appear in iteration
        # They share the same value as INTL_DEVELOPED and BONDS_AGGREGATE respectively
        expected_classes = {
            # Core equity
            "US_STOCKS",
            "INTL_DEVELOPED",
            "EMERGING_MARKETS",
            # Sectors
            "TECH_SECTOR",
            "FINANCIAL_SECTOR",
            "ENERGY_SECTOR",
            "HEALTHCARE_SECTOR",
            # Fixed income
            "BONDS_AGGREGATE",
            "BONDS_TREASURY",
            "BONDS_SHORT_TERM",
            "BONDS_INTERMEDIATE",
            "TIPS",
            # Real assets
            "REITS",
            # Alternatives
            "GOLD",
            "COMMODITIES",
            # Size/style
            "SMALL_CAP_VALUE",
            # Cash
            "CASH",
        }
        actual_classes = {c.name for c in AssetClass}
        assert expected_classes == actual_classes

    def test_aliases_are_accessible(self):
        """Aliases should be accessible by name even though they don't appear in iteration."""
        # GLOBAL_STOCKS is an alias for INTL_DEVELOPED
        assert AssetClass.GLOBAL_STOCKS is AssetClass.INTL_DEVELOPED
        # BONDS is an alias for BONDS_AGGREGATE
        assert AssetClass.BONDS is AssetClass.BONDS_AGGREGATE


class TestAsset:
    """Tests for Asset dataclass with extended fields."""

    def test_asset_has_category_field(self):
        """Asset should have category field."""
        asset = Asset(
            symbol="SPY",
            name="SPDR S&P 500 ETF",
            asset_class=AssetClass.US_STOCKS,
            category=AssetCategory.EQUITY,
        )
        assert asset.category == AssetCategory.EQUITY

    def test_asset_category_defaults_to_equity(self):
        """Asset category should default to EQUITY."""
        asset = Asset(
            symbol="SPY",
            name="SPDR S&P 500 ETF",
            asset_class=AssetClass.US_STOCKS,
        )
        assert asset.category == AssetCategory.EQUITY

    def test_asset_has_ucits_symbol_field(self):
        """Asset should have ucits_symbol field for European equivalents."""
        asset = Asset(
            symbol="SPY",
            name="SPDR S&P 500 ETF",
            asset_class=AssetClass.US_STOCKS,
            ucits_symbol="CSPX.L",
        )
        assert asset.ucits_symbol == "CSPX.L"

    def test_asset_ucits_symbol_defaults_to_none(self):
        """Asset ucits_symbol should default to None."""
        asset = Asset(
            symbol="SPY",
            name="SPDR S&P 500 ETF",
            asset_class=AssetClass.US_STOCKS,
        )
        assert asset.ucits_symbol is None

    def test_asset_remains_frozen(self):
        """Asset should remain immutable (frozen=True)."""
        asset = Asset(
            symbol="SPY",
            name="SPDR S&P 500 ETF",
            asset_class=AssetClass.US_STOCKS,
        )
        with pytest.raises(AttributeError):
            asset.symbol = "QQQ"

    def test_asset_with_all_fields(self):
        """Asset should work with all fields specified."""
        asset = Asset(
            symbol="SPY",
            name="SPDR S&P 500 ETF",
            asset_class=AssetClass.US_STOCKS,
            isin="US78462F1030",
            yahoo_symbol="SPY",
            category=AssetCategory.EQUITY,
            ucits_symbol="CSPX.L",
        )
        assert asset.symbol == "SPY"
        assert asset.name == "SPDR S&P 500 ETF"
        assert asset.asset_class == AssetClass.US_STOCKS
        assert asset.isin == "US78462F1030"
        assert asset.yahoo_symbol == "SPY"
        assert asset.category == AssetCategory.EQUITY
        assert asset.ucits_symbol == "CSPX.L"
