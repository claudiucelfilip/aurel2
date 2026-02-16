#!/usr/bin/env python3
"""Show every trade difference between baseline and filtered."""
import sys, logging, os
os.environ["LOG_LEVEL"] = "CRITICAL"
logging.disable(logging.CRITICAL)
from datetime import date, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from aurel2.core.assets import get_all_yahoo_symbols
from aurel2.data.providers.yahoo import YahooFinanceProvider
from aurel2.engine.backtest import BacktestEngine

periods = {
    "5y": (date(2021, 2, 15), date(2026, 2, 15)),
    "2016-2022": (date(2016, 1, 1), date(2022, 12, 31)),
    "2019-2023": (date(2019, 1, 1), date(2023, 12, 31)),
    "2009-2015": (date(2009, 1, 1), date(2015, 12, 31)),
}

name = sys.argv[1] if len(sys.argv) > 1 else "5y"
start, end = periods[name]

provider = YahooFinanceProvider()
symbols = get_all_yahoo_symbols()
prices = provider.get_multi_prices(symbols, start, end + timedelta(days=5))

# Run both
engine_b = BacktestEngine(initial_capital=10000, trend_filter_enabled=False)
result_b = engine_b.run(prices, start, end)

engine_f = BacktestEngine(initial_capital=10000, trend_filter_enabled=True)
result_f = engine_f.run(prices, start, end)

print(f"Period: {name} ({start} to {end})")
print(f"BASELINE:  Return={result_b.total_return:.1f}%  CAGR={result_b.cagr:.1f}%  Sharpe={result_b.sharpe_ratio:.2f}  MaxDD={result_b.max_drawdown:.1f}%  Trades={result_b.num_trades}")
print(f"FILTERED:  Return={result_f.total_return:.1f}%  CAGR={result_f.cagr:.1f}%  Sharpe={result_f.sharpe_ratio:.2f}  MaxDD={result_f.max_drawdown:.1f}%  Trades={result_f.num_trades}")
print()

# Compare trade logs
trades_b = result_b.trades if hasattr(result_b, 'trades') else []
trades_f = result_f.trades if hasattr(result_f, 'trades') else []

print(f"=== BASELINE TRADES ({len(trades_b)}) ===")
for t in trades_b:
    print(f"  {t}")

print(f"\n=== FILTERED TRADES ({len(trades_f)}) ===")
for t in trades_f:
    print(f"  {t}")

# Check if there's a trade history or rebalance log
if hasattr(result_b, 'rebalance_log'):
    print(f"\n=== BASELINE REBALANCES ({len(result_b.rebalance_log)}) ===")
    for r in result_b.rebalance_log:
        print(f"  {r}")
if hasattr(result_f, 'rebalance_log'):
    print(f"\n=== FILTERED REBALANCES ({len(result_f.rebalance_log)}) ===")
    for r in result_f.rebalance_log:
        print(f"  {r}")

# Check portfolio values over time
if hasattr(result_b, 'portfolio_values') and hasattr(result_f, 'portfolio_values'):
    vals_b = result_b.portfolio_values
    vals_f = result_f.portfolio_values
    print(f"\n=== PORTFOLIO VALUE COMPARISON (sampled) ===")
    keys = sorted(set(list(vals_b.keys()) + list(vals_f.keys())))
    step = max(1, len(keys) // 20)
    for k in keys[::step]:
        vb = vals_b.get(k, '?')
        vf = vals_f.get(k, '?')
        diff = ""
        if isinstance(vb, (int,float)) and isinstance(vf, (int,float)):
            diff = f"  delta={vf-vb:+.2f}"
        print(f"  {k}: baseline={vb}  filtered={vf}{diff}")

# Also print all attributes of result
print(f"\n=== RESULT ATTRIBUTES ===")
print([a for a in dir(result_b) if not a.startswith('_')])
