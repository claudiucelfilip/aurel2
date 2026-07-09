#!/usr/bin/env python3
"""
Arm 5: Buy-and-hold benchmarks for SPY and QQQ over 2026-02-11 to 2026-07-08.
Computes daily equity curves normalized to 100.0 at start date.
"""

import json
from datetime import datetime
import yfinance as yf
import pandas as pd


def compute_equity_curve(prices):
    """
    Compute normalized equity curve from price series.
    Returns list of (date_str, equity) tuples and metrics.
    """
    if not prices or len(prices) < 2:
        return [], 0.0, 0.0

    # Normalize to 100.0 at start
    start_price = prices[0]["close"]
    equity_curve = []
    max_equity = 100.0
    max_drawdown = 0.0

    for price_point in prices:
        equity = 100.0 * (price_point["close"] / start_price)
        equity_curve.append({"date": price_point["date"], "equity": round(equity, 2)})

        # Track max drawdown
        if equity > max_equity:
            max_equity = equity
        drawdown = ((max_equity - equity) / max_equity) * 100.0
        if drawdown > max_drawdown:
            max_drawdown = drawdown

    # Final return
    final_equity = equity_curve[-1]["equity"]
    final_return = round(final_equity - 100.0, 2)

    return equity_curve, final_return, round(max_drawdown, 2)


def fetch_benchmark_data(ticker, start_date, end_date):
    """
    Fetch auto-adjusted daily closes from yfinance.
    Returns list of {"date": "YYYY-MM-DD", "close": price} dicts.
    """
    df = yf.download(ticker, start=start_date, end=end_date, progress=False)

    prices = []
    # yfinance returns MultiIndex DataFrame with (Price, Ticker) columns
    # Extract the Close column for this ticker
    if isinstance(df, pd.DataFrame):
        if ("Close", ticker) in df.columns:
            close_series = df[("Close", ticker)]
        elif "Close" in df.columns:
            close_series = df["Close"]
            if isinstance(close_series, pd.DataFrame):
                close_series = close_series[ticker]
        else:
            close_series = df.iloc[:, 0]  # Fallback to first column

        for date, value in close_series.items():
            prices.append({
                "date": date.strftime("%Y-%m-%d"),
                "close": float(value)
            })
    return prices


def main():
    start_date = "2026-02-11"
    end_date = "2026-07-08"

    # Fetch data
    print(f"Fetching SPY and QQQ data from {start_date} to {end_date}...")
    spy_prices = fetch_benchmark_data("SPY", start_date, end_date)
    qqq_prices = fetch_benchmark_data("QQQ", start_date, end_date)

    # Compute equity curves
    spy_curve, spy_return, spy_dd = compute_equity_curve(spy_prices)
    qqq_curve, qqq_return, qqq_dd = compute_equity_curve(qqq_prices)

    # Build output
    result = {
        "arm": "benchmarks",
        "window": {
            "start": start_date,
            "end": end_date
        },
        "start_equity": 100.0,
        "series": {
            "SPY": {
                "daily_equity": spy_curve,
                "final_return_pct": spy_return,
                "max_drawdown_pct": spy_dd
            },
            "QQQ": {
                "daily_equity": qqq_curve,
                "final_return_pct": qqq_return,
                "max_drawdown_pct": qqq_dd
            }
        },
        "notes": ["data source: yfinance auto-adjusted"]
    }

    # Write output
    output_path = "/Users/claudiu/Sites/aurel2/data/edge_decomposition/benchmarks.json"
    with open(output_path, "w") as f:
        json.dump(result, f, indent=2)

    print(f"Wrote benchmarks to {output_path}")
    print(f"SPY: {spy_return:+.2f}% return, {spy_dd:.2f}% max drawdown")
    print(f"QQQ: {qqq_return:+.2f}% return, {qqq_dd:.2f}% max drawdown")


if __name__ == "__main__":
    main()
