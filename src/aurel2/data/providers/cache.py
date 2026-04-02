"""Cached data provider wrapping any underlying provider."""

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import structlog

from aurel2.data.providers.yahoo import YahooFinanceProvider

logger = structlog.get_logger()

CACHE_DIR = Path("data/price_cache")


class CachedPriceProvider:
    """Wraps YahooFinanceProvider with a Parquet disk cache.

    Historical prices are immutable — yesterday's close never changes.
    Only the most recent trading day is re-fetched (may be partial/intraday).
    """

    def __init__(
        self,
        provider: YahooFinanceProvider | None = None,
        cache_dir: Path = CACHE_DIR,
    ):
        self.provider = provider or YahooFinanceProvider()
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, symbol: str) -> Path:
        return self.cache_dir / f"{symbol}.parquet"

    def get_prices(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
    ) -> pd.DataFrame:
        """Get prices, using cache for historical data.

        Strategy:
        1. Load cached data if it exists
        2. Determine what's missing (before cached start, after cached end)
        3. Fetch only missing ranges from Yahoo
        4. Merge and re-save cache
        5. Re-fetch the last trading day (today's data may be partial)
        """
        cache_path = self._cache_path(symbol)
        cached_df = None

        if cache_path.exists():
            try:
                cached_df = pd.read_parquet(cache_path)
                cached_df["date"] = pd.to_datetime(cached_df["date"]).dt.date
                logger.debug("cache_hit", symbol=symbol, rows=len(cached_df))
            except Exception as e:
                logger.warning("cache_read_failed", symbol=symbol, error=str(e))
                cached_df = None

        if cached_df is not None and not cached_df.empty:
            cached_start = cached_df["date"].min()
            cached_end = cached_df["date"].max()

            fetched_parts = []

            # Fetch data before cached start if needed
            if start_date < cached_start:
                logger.info("cache_fetch_prefix", symbol=symbol, start=str(start_date), end=str(cached_start))
                try:
                    prefix = self.provider.get_prices(symbol, start_date, cached_start)
                    if not prefix.empty:
                        fetched_parts.append(prefix)
                except Exception as e:
                    # Offline / transient Yahoo failures: keep cached data.
                    logger.warning("cache_fetch_prefix_failed", symbol=symbol, error=str(e))

            # Fetch data from cached_end onward (includes re-fetch of last day)
            refetch_start = cached_end - timedelta(days=1)
            if refetch_start <= end_date:
                logger.info("cache_fetch_suffix", symbol=symbol, start=str(refetch_start), end=str(end_date))
                try:
                    suffix = self.provider.get_prices(symbol, refetch_start, end_date)
                    if not suffix.empty:
                        fetched_parts.append(suffix)
                except Exception as e:
                    logger.warning("cache_fetch_suffix_failed", symbol=symbol, error=str(e))

            if fetched_parts:
                new_data = pd.concat(fetched_parts, ignore_index=True)
                new_data["date"] = pd.to_datetime(new_data["date"]).dt.date
                # Merge: cached + newly fetched, deduplicate by date
                merged = pd.concat([cached_df, new_data], ignore_index=True)
                merged = merged.drop_duplicates(subset=["date"], keep="last")
                merged = merged.sort_values("date").reset_index(drop=True)
            else:
                merged = cached_df
        else:
            # No cache — full fetch
            logger.info("cache_miss", symbol=symbol)
            try:
                merged = self.provider.get_prices(symbol, start_date, end_date)
            except Exception as e:
                logger.warning("cache_full_fetch_failed", symbol=symbol, error=str(e))
                merged = pd.DataFrame(columns=["date", "close", "symbol"])
            if not merged.empty:
                merged["date"] = pd.to_datetime(merged["date"]).dt.date

        # Save to cache
        if not merged.empty:
            try:
                save_df = merged.copy()
                save_df["date"] = pd.to_datetime(save_df["date"])
                save_df.to_parquet(cache_path, index=False)
                logger.debug("cache_saved", symbol=symbol, rows=len(save_df))
            except Exception as e:
                logger.warning("cache_write_failed", symbol=symbol, error=str(e))

        # Filter to requested range
        if not merged.empty:
            # Include buffer start (provider already fetches with 400-day buffer)
            merged = merged[(merged["date"] >= start_date - timedelta(days=400)) & (merged["date"] <= end_date)]

        return merged

    def get_multi_prices(
        self,
        symbols: list[str],
        start_date: date,
        end_date: date,
    ) -> pd.DataFrame:
        """Fetch prices for multiple symbols with caching."""
        all_data = []

        for symbol in symbols:
            df = self.get_prices(symbol, start_date, end_date)
            if not df.empty:
                all_data.append(df)

        if not all_data:
            return pd.DataFrame(columns=["date", "close", "symbol"])

        return pd.concat(all_data, ignore_index=True)
