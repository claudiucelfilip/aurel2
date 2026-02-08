"""Asset registry for Aurel2 multi-strategy trading system.

This module provides a central registry of all tradeable assets with their
metadata including symbols, ISINs, and UCITS equivalents for European investors.
"""

from aurel2.core.models import Asset, AssetClass, AssetCategory

# ==============================================================================
# ASSET REGISTRY
# ==============================================================================
# Maps AssetClass to Asset for all ~12 tradeable assets in the system.
# Each asset includes:
# - Primary US-listed symbol
# - UCITS equivalent symbol for European investors (where available)
# - ISIN identifiers
# - Asset category for filtering
# ==============================================================================

ASSET_REGISTRY: dict[AssetClass, Asset] = {
    # =========================================================================
    # CORE EQUITY
    # =========================================================================
    AssetClass.US_STOCKS: Asset(
        symbol="SPY",
        name="SPDR S&P 500 ETF Trust",
        asset_class=AssetClass.US_STOCKS,
        isin="US78462F1030",
        yahoo_symbol="SPY",
        category=AssetCategory.EQUITY,
        ucits_symbol="CSPX.L",  # iShares Core S&P 500 (IE00B5BMR087)
    ),
    AssetClass.INTL_DEVELOPED: Asset(
        symbol="EFA",
        name="iShares MSCI EAFE ETF",
        asset_class=AssetClass.INTL_DEVELOPED,
        isin="US4642874659",
        yahoo_symbol="EFA",
        category=AssetCategory.EQUITY,
        ucits_symbol="VWRA.L",  # Vanguard FTSE All-World (IE00BK5BQT80)
    ),
    AssetClass.EMERGING_MARKETS: Asset(
        symbol="EEM",
        name="iShares MSCI Emerging Markets ETF",
        asset_class=AssetClass.EMERGING_MARKETS,
        isin="US4642872349",
        yahoo_symbol="EEM",
        category=AssetCategory.EQUITY,
        ucits_symbol="EIMI.L",  # iShares Core MSCI EM (IE00BKM4GZ66)
    ),
    # =========================================================================
    # SECTORS
    # =========================================================================
    AssetClass.TECH_SECTOR: Asset(
        symbol="XLK",
        name="Technology Select Sector SPDR Fund",
        asset_class=AssetClass.TECH_SECTOR,
        isin="US81369Y8030",
        yahoo_symbol="XLK",
        category=AssetCategory.EQUITY,
        ucits_symbol=None,  # No direct UCITS equivalent
    ),
    AssetClass.FINANCIAL_SECTOR: Asset(
        symbol="XLF",
        name="Financial Select Sector SPDR Fund",
        asset_class=AssetClass.FINANCIAL_SECTOR,
        isin="US81369Y5069",
        yahoo_symbol="XLF",
        category=AssetCategory.EQUITY,
        ucits_symbol=None,  # No direct UCITS equivalent
    ),
    AssetClass.ENERGY_SECTOR: Asset(
        symbol="XLE",
        name="Energy Select Sector SPDR Fund",
        asset_class=AssetClass.ENERGY_SECTOR,
        isin="US81369Y2033",
        yahoo_symbol="XLE",
        category=AssetCategory.EQUITY,
        ucits_symbol=None,  # No direct UCITS equivalent
    ),
    AssetClass.HEALTHCARE_SECTOR: Asset(
        symbol="XLV",
        name="Health Care Select Sector SPDR Fund",
        asset_class=AssetClass.HEALTHCARE_SECTOR,
        isin="US81369Y4070",
        yahoo_symbol="XLV",
        category=AssetCategory.EQUITY,
        ucits_symbol=None,  # No direct UCITS equivalent
    ),
    # =========================================================================
    # FIXED INCOME
    # =========================================================================
    AssetClass.BONDS_AGGREGATE: Asset(
        symbol="AGG",
        name="iShares Core U.S. Aggregate Bond ETF",
        asset_class=AssetClass.BONDS_AGGREGATE,
        isin="US4642872265",
        yahoo_symbol="AGG",
        category=AssetCategory.FIXED_INCOME,
        ucits_symbol="AGGH.L",  # iShares Global Aggregate Bond (IE00BDBRDM35)
    ),
    AssetClass.BONDS_TREASURY: Asset(
        symbol="TLT",
        name="iShares 20+ Year Treasury Bond ETF",
        asset_class=AssetClass.BONDS_TREASURY,
        isin="US4642874576",
        yahoo_symbol="TLT",
        category=AssetCategory.FIXED_INCOME,
        ucits_symbol=None,  # IDTL.L is partial equivalent
    ),
    AssetClass.BONDS_SHORT_TERM: Asset(
        symbol="SHY",
        name="iShares 1-3 Year Treasury Bond ETF",
        asset_class=AssetClass.BONDS_SHORT_TERM,
        isin="US4642874329",
        yahoo_symbol="SHY",
        category=AssetCategory.FIXED_INCOME,
        ucits_symbol=None,
    ),
    AssetClass.BONDS_INTERMEDIATE: Asset(
        symbol="IEF",
        name="iShares 7-10 Year Treasury Bond ETF",
        asset_class=AssetClass.BONDS_INTERMEDIATE,
        isin="US4642874402",
        yahoo_symbol="IEF",
        category=AssetCategory.FIXED_INCOME,
        ucits_symbol=None,
    ),
    AssetClass.TIPS: Asset(
        symbol="TIP",
        name="iShares TIPS Bond ETF",
        asset_class=AssetClass.TIPS,
        isin="US4642871846",
        yahoo_symbol="TIP",
        category=AssetCategory.FIXED_INCOME,
        ucits_symbol=None,
    ),
    AssetClass.REITS: Asset(
        symbol="VNQ",
        name="Vanguard Real Estate ETF",
        asset_class=AssetClass.REITS,
        isin="US9229085538",
        yahoo_symbol="VNQ",
        category=AssetCategory.ALTERNATIVE,
        ucits_symbol=None,
    ),
    AssetClass.SMALL_CAP_VALUE: Asset(
        symbol="IJS",
        name="iShares S&P Small-Cap 600 Value ETF",
        asset_class=AssetClass.SMALL_CAP_VALUE,
        isin="US4642887149",
        yahoo_symbol="IJS",
        category=AssetCategory.EQUITY,
        ucits_symbol=None,
    ),
    # =========================================================================
    # ALTERNATIVES
    # =========================================================================
    AssetClass.GOLD: Asset(
        symbol="GLD",
        name="SPDR Gold Shares",
        asset_class=AssetClass.GOLD,
        isin="US78463V1070",
        yahoo_symbol="GLD",
        category=AssetCategory.ALTERNATIVE,
        ucits_symbol="SGLD.L",  # Invesco Physical Gold (IE00B4ND3602)
    ),
    AssetClass.COMMODITIES: Asset(
        symbol="DBC",
        name="Invesco DB Commodity Index Tracking Fund",
        asset_class=AssetClass.COMMODITIES,
        isin="US46137V1087",
        yahoo_symbol="DBC",
        category=AssetCategory.ALTERNATIVE,
        ucits_symbol=None,  # No direct UCITS equivalent
    ),
    # =========================================================================
    # CASH
    # =========================================================================
    AssetClass.CASH: Asset(
        symbol="CASH",
        name="Cash / Money Market",
        asset_class=AssetClass.CASH,
        isin=None,
        yahoo_symbol=None,
        category=AssetCategory.CASH,
        ucits_symbol=None,
    ),
}


# ==============================================================================
# HELPER FUNCTIONS
# ==============================================================================


def get_asset(asset_class: AssetClass) -> Asset:
    """Get an asset by its asset class.

    Args:
        asset_class: The AssetClass enum value.

    Returns:
        The Asset instance for the given class.

    Raises:
        KeyError: If the asset class is not in the registry.

    Example:
        >>> spy = get_asset(AssetClass.US_STOCKS)
        >>> spy.symbol
        'SPY'
    """
    return ASSET_REGISTRY[asset_class]


def get_assets_by_category(category: AssetCategory) -> list[Asset]:
    """Get all assets belonging to a specific category.

    Args:
        category: The AssetCategory to filter by.

    Returns:
        List of Asset instances matching the category.

    Example:
        >>> equities = get_assets_by_category(AssetCategory.EQUITY)
        >>> len(equities)
        7
    """
    return [asset for asset in ASSET_REGISTRY.values() if asset.category == category]


def get_all_yahoo_symbols() -> list[str]:
    """Get all Yahoo Finance symbols from the asset registry.

    Returns:
        List of Yahoo Finance symbols (excluding CASH and assets without yahoo_symbol).

    Example:
        >>> symbols = get_all_yahoo_symbols()
        >>> 'SPY' in symbols
        True
    """
    return [
        asset.yahoo_symbol
        for asset in ASSET_REGISTRY.values()
        if asset.yahoo_symbol is not None
    ]
