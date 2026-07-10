"""Pinned, point-in-time-clean price snapshot for the context-pack ablation.

CRITICAL determinism rule (docs/plans/2026-07-10-ai-overlay-design.md, "Deterministic
context packs"): yfinance's default `auto_adjust=True` returns dividend/split-adjusted
closes that silently drift every time a dividend is paid between fetches -- the
bake-off found this broke cache reproducibility (same cache_key, different price
history depending on fetch date). RAW (unadjusted) closes are fixed at the time the
row was traded and never change on refetch.

This module fetches RAW closes once (auto_adjust=False) and snapshots them to a
single committed parquet file. All context-pack builders read ONLY from that
snapshot -- never from CachedPriceProvider/yfinance directly -- so packs are
byte-reproducible from a frozen input regardless of when they're rebuilt.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

REPO_ROOT = Path(__file__).resolve().parents[3]
# CSV, not parquet: no pyarrow/fastparquet dependency in this environment, and a
# committed CSV diffs cleanly in git review -- both wins for a frozen, pinned artifact.
SNAPSHOT_PATH = REPO_ROOT / "data" / "edge_decomposition" / "snapshots" / "price_snapshot_raw.csv"

# Extra symbols the context packs reference beyond the DM universe itself.
EXTRA_SYMBOLS = ["SPY", "QQQ", "TLT"]


def fetch_raw_snapshot(
    symbols: list[str],
    start_date: date,
    end_date: date,
    lookback_buffer_days: int = 450,
) -> pd.DataFrame:
    """Fetch RAW (unadjusted) closes for `symbols` over [start-buffer, end].

    Returns columns: date, symbol, close, volume. Raises if any symbol returns
    no data (fail loud -- a silent gap would make packs non-deterministic in a
    different way).
    """
    buffer_start = start_date - timedelta(days=lookback_buffer_days)
    frames = []
    for sym in symbols:
        ticker = yf.Ticker(sym)
        hist = ticker.history(start=buffer_start, end=end_date + timedelta(days=1), auto_adjust=False)
        if hist.empty:
            raise RuntimeError(f"No raw price data returned for {sym}")
        df = pd.DataFrame({
            "date": hist.index.date,
            "symbol": sym,
            "close": hist["Close"].values,
            "volume": hist["Volume"].values if "Volume" in hist.columns else None,
        })
        frames.append(df)
    out = pd.concat(frames, ignore_index=True)
    out["date"] = pd.to_datetime(out["date"])
    return out.sort_values(["symbol", "date"]).reset_index(drop=True)


def build_and_write_snapshot(
    symbols: list[str],
    start_date: date,
    end_date: date,
    out_path: Path = SNAPSHOT_PATH,
) -> pd.DataFrame:
    df = fetch_raw_snapshot(symbols, start_date, end_date)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_df = df.copy()
    write_df["date"] = write_df["date"].dt.strftime("%Y-%m-%d")
    write_df.to_csv(out_path, index=False)
    return df


def load_snapshot(path: Path = SNAPSHOT_PATH) -> pd.DataFrame:
    """Load the committed, pinned raw-close snapshot. Never re-fetches."""
    if not path.exists():
        raise FileNotFoundError(
            f"No pinned price snapshot at {path} -- run build_and_write_snapshot() once "
            "and commit the resulting CSV file before building context packs."
        )
    df = pd.read_csv(path)
    df["date"] = pd.to_datetime(df["date"])
    return df
