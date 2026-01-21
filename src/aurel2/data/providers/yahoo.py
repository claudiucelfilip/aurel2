"""Yahoo Finance data provider."""

from datetime import date, timedelta

import pandas as pd
import yfinance as yf
import structlog

logger = structlog.get_logger()


class YahooFinanceProvider:
    """Fetch historical price data from Yahoo Finance."""

    def get_prices(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> pd.DataFrame:
        """
        Fetch historical adjusted close prices.

        Args:
            symbol: Yahoo Finance symbol (e.g., 'CSPX.DE', 'SPY')
            start_date: Start date for data
            end_date: End date for data

        Returns:
            DataFrame with columns: date, close, symbol
        """
        logger.info("fetching_yahoo_data", symbol=symbol, start=str(start_date), end=str(end_date))

        # Add buffer for lookback calculation
        buffer_start = start_date - timedelta(days=400)

        ticker = yf.Ticker(symbol)
        hist = ticker.history(start=buffer_start, end=end_date + timedelta(days=1))

        if hist.empty:
            logger.warning("no_data_returned", symbol=symbol)
            return pd.DataFrame(columns=["date", "close", "symbol"])

        df = pd.DataFrame({
            "date": hist.index.date,
            "close": hist["Close"].values,
            "symbol": symbol,
        })

        logger.info("fetched_yahoo_data", symbol=symbol, rows=len(df))
        return df

    def get_multi_prices(
        self,
        symbols: list[str],
        start_date: date,
        end_date: date,
    ) -> pd.DataFrame:
        """
        Fetch prices for multiple symbols.

        Returns:
            DataFrame with columns: date, close, symbol
        """
        all_data = []

        for symbol in symbols:
            df = self.get_prices(symbol, start_date, end_date)
            if not df.empty:
                all_data.append(df)

        if not all_data:
            return pd.DataFrame(columns=["date", "close", "symbol"])

        return pd.concat(all_data, ignore_index=True)
