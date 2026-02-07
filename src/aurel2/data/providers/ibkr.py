"""IBKR historical data provider for live trading.

Uses the existing IBKR connection to fetch historical price data,
providing a single source of truth for live trading decisions
(instead of mixing Yahoo data with IBKR execution).
"""

import asyncio
from datetime import date, timedelta

import pandas as pd
import structlog

from aurel2.data.validation import validate_prices

logger = structlog.get_logger()

# Try to import ib_insync
try:
    from ib_insync import IB, Stock, Contract
    HAS_IB_INSYNC = True
except ImportError:
    HAS_IB_INSYNC = False


class IBKRDataProvider:
    """Fetch historical price data from IBKR.

    Matches the YahooFinanceProvider interface:
        get_prices(symbol, start, end) -> DataFrame[date, close, symbol]

    Uses reqHistoricalData() for daily adjusted close prices.
    Requires an active IB connection (passed in, not created).
    """

    # Map our symbols to IBKR contracts
    SYMBOL_CONTRACTS = {
        "SPY": {"exchange": "ARCA", "currency": "USD"},
        "EFA": {"exchange": "ARCA", "currency": "USD"},
        "EEM": {"exchange": "ARCA", "currency": "USD"},
        "XLK": {"exchange": "ARCA", "currency": "USD"},
        "XLF": {"exchange": "ARCA", "currency": "USD"},
        "XLE": {"exchange": "ARCA", "currency": "USD"},
        "XLV": {"exchange": "ARCA", "currency": "USD"},
        "AGG": {"exchange": "ARCA", "currency": "USD"},
        "TLT": {"exchange": "ARCA", "currency": "USD"},
        "GLD": {"exchange": "ARCA", "currency": "USD"},
        "DBC": {"exchange": "ARCA", "currency": "USD"},
        # UCITS
        "CSPX": {"exchange": "SBF", "currency": "EUR"},
        "VWRA": {"exchange": "SBF", "currency": "EUR"},
        "AGGH": {"exchange": "SBF", "currency": "EUR"},
    }

    def __init__(self, ib: "IB"):
        """Initialize with an active IB connection.

        Args:
            ib: Connected ib_insync.IB instance
        """
        if not HAS_IB_INSYNC:
            raise ImportError("ib_insync is required for IBKRDataProvider")
        self.ib = ib

    def _make_contract(self, symbol: str) -> "Contract":
        info = self.SYMBOL_CONTRACTS.get(symbol, {"exchange": "SMART", "currency": "USD"})
        return Stock(symbol=symbol, exchange=info["exchange"], currency=info["currency"])

    async def get_prices_async(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> pd.DataFrame:
        """Fetch historical daily close prices from IBKR.

        Uses 400 calendar days of lookback for momentum calculations.
        """
        logger.info("fetching_ibkr_data", symbol=symbol, start=str(start_date), end=str(end_date))

        contract = self._make_contract(symbol)

        try:
            await self.ib.qualifyContractsAsync(contract)
        except Exception as e:
            logger.warning("ibkr_qualify_failed", symbol=symbol, error=str(e))
            return pd.DataFrame(columns=["date", "close", "symbol"])

        # Calculate duration
        days = (end_date - start_date).days + 400  # Buffer for lookback
        duration = f"{min(days, 365 * 2)} D"  # IBKR max is ~2 years per request

        try:
            bars = await self.ib.reqHistoricalDataAsync(
                contract,
                endDateTime=end_date.strftime("%Y%m%d 23:59:59"),
                durationStr=duration,
                barSizeSetting="1 day",
                whatToShow="ADJUSTED_LAST",
                useRTH=True,
                formatDate=1,
            )
        except Exception as e:
            logger.warning("ibkr_historical_data_failed", symbol=symbol, error=str(e))
            return pd.DataFrame(columns=["date", "close", "symbol"])

        if not bars:
            logger.warning("no_ibkr_data_returned", symbol=symbol)
            return pd.DataFrame(columns=["date", "close", "symbol"])

        df = pd.DataFrame({
            "date": [b.date for b in bars],
            "close": [b.close for b in bars],
            "symbol": symbol,
        })

        # Validate
        df = validate_prices(df, symbol=symbol)

        logger.info("fetched_ibkr_data", symbol=symbol, rows=len(df))
        return df

    def get_prices(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> pd.DataFrame:
        """Synchronous wrapper for get_prices_async."""
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Already in an async context — create a task
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(
                    asyncio.run, self.get_prices_async(symbol, start_date, end_date)
                )
                return future.result()
        return loop.run_until_complete(self.get_prices_async(symbol, start_date, end_date))

    async def get_multi_prices_async(
        self,
        symbols: list[str],
        start_date: date,
        end_date: date,
    ) -> pd.DataFrame:
        """Fetch prices for multiple symbols."""
        all_data = []

        for symbol in symbols:
            df = await self.get_prices_async(symbol, start_date, end_date)
            if not df.empty:
                all_data.append(df)
            # Small delay to respect IBKR rate limits
            await asyncio.sleep(0.5)

        if not all_data:
            return pd.DataFrame(columns=["date", "close", "symbol"])

        return pd.concat(all_data, ignore_index=True)

    def get_multi_prices(
        self,
        symbols: list[str],
        start_date: date,
        end_date: date,
    ) -> pd.DataFrame:
        """Synchronous wrapper for get_multi_prices_async."""
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(
                    asyncio.run, self.get_multi_prices_async(symbols, start_date, end_date)
                )
                return future.result()
        return loop.run_until_complete(self.get_multi_prices_async(symbols, start_date, end_date))
