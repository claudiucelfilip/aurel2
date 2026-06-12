#!/usr/bin/env python3
"""Lightweight SMA-200 comparison - one period at a time."""
import sys, gc, logging, os
os.environ["LOG_LEVEL"] = "CRITICAL"
logging.disable(logging.CRITICAL)
from datetime import date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from aurel2.core.assets import get_all_yahoo_symbols
from aurel2.data.providers.yahoo import YahooFinanceProvider
from aurel2.engine.backtest import BacktestEngine

PERIODS = [
    ("20y", date(2006, 2, 20), date(2026, 2, 15)),
    ("5y", date(2021, 2, 15), date(2026, 2, 15)),
    ("2009-2015", date(2009, 1, 1), date(2015, 12, 31)),
    ("2016-2022", date(2016, 1, 1), date(2022, 12, 31)),
    ("2019-2023", date(2019, 1, 1), date(2023, 12, 31)),
]

period_name = sys.argv[1] if len(sys.argv) > 1 else None

for name, start, end in PERIODS:
    if period_name and name != period_name:
        continue
    
    print(f"\n{'='*60}")
    print(f"Period: {name} ({start} to {end})")
    print(f"{'='*60}")
    
    # Fetch data
    provider = YahooFinanceProvider()
    symbols = get_all_yahoo_symbols()
    from datetime import timedelta
    prices = provider.get_multi_prices(symbols, start, end + timedelta(days=5))
    print(f"Loaded {len(prices)} price records")
    
    # Baseline
    engine_b = BacktestEngine(initial_capital=10000, trend_filter_enabled=False)
    result_b = engine_b.run(prices, start, end)
    print(f"\nBASELINE:  Return={result_b.total_return:>8.1f}%  CAGR={result_b.cagr:>6.1f}%  Sharpe={result_b.sharpe_ratio:>5.2f}  MaxDD={result_b.max_drawdown:>6.1f}%  Trades={result_b.num_trades}")
    
    del engine_b
    gc.collect()
    
    # With filter
    engine_f = BacktestEngine(initial_capital=10000, trend_filter_enabled=True)
    result_f = engine_f.run(prices, start, end)
    print(f"FILTERED:  Return={result_f.total_return:>8.1f}%  CAGR={result_f.cagr:>6.1f}%  Sharpe={result_f.sharpe_ratio:>5.2f}  MaxDD={result_f.max_drawdown:>6.1f}%  Trades={result_f.num_trades}")
    
    # Verdict
    better_return = result_f.total_return >= result_b.total_return
    better_sharpe = result_f.sharpe_ratio >= result_b.sharpe_ratio
    better_dd = result_f.max_drawdown <= result_b.max_drawdown
    score = sum([better_return, better_sharpe, better_dd])
    verdict = "✓ FILTER WINS" if score >= 2 else "✗ BASELINE WINS"
    print(f"VERDICT: {verdict} (return={'✓' if better_return else '✗'} sharpe={'✓' if better_sharpe else '✗'} drawdown={'✓' if better_dd else '✗'})")
    
    del engine_f, prices, provider
    gc.collect()
