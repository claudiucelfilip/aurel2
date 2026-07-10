#!/usr/bin/env python3
"""Fetch and pin a raw-close price snapshot for the Track A acceptance test.

Pulls the live universe (get_all_yahoo_symbols()) + SPY (benchmark) with
auto_adjust=False so the snapshot is reproducible across pre/post-cleanup
replay runs. Writes a single parquet file consumed by
scripts/acceptance_replay.py.
"""

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pandas as pd
import yfinance as yf

from aurel2.core.assets import get_all_yahoo_symbols
from aurel2.data.validation import validate_prices

SNAPSHOT_PATH = Path(__file__).parent.parent / "data" / "acceptance" / "price_snapshot_2026-02-11_2026-07-09.csv"


def fetch_symbol(symbol: str, start: date, end: date) -> pd.DataFrame:
    buffer_start = start - timedelta(days=400)
    ticker = yf.Ticker(symbol)
    hist = ticker.history(start=buffer_start, end=end + timedelta(days=1), auto_adjust=False)
    if hist.empty:
        print(f"WARNING: no data for {symbol}")
        return pd.DataFrame(columns=["date", "close", "symbol"])
    df = pd.DataFrame({
        "date": hist.index.date,
        "close": hist["Close"].values,
        "symbol": symbol,
    })
    return validate_prices(df, symbol=symbol)


def main():
    start = date(2026, 2, 11)
    end = date(2026, 7, 9)

    symbols = get_all_yahoo_symbols()
    if "SPY" not in symbols:
        symbols = symbols + ["SPY"]

    all_data = []
    for sym in symbols:
        print(f"Fetching {sym}...")
        df = fetch_symbol(sym, start, end)
        if not df.empty:
            all_data.append(df)

    prices = pd.concat(all_data, ignore_index=True)
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    prices.to_csv(SNAPSHOT_PATH, index=False)
    print(f"Saved {len(prices)} rows for {prices['symbol'].nunique()} symbols to {SNAPSHOT_PATH}")


if __name__ == "__main__":
    main()
