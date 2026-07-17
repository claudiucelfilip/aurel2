"""Before/after: switch_threshold 0.02 -> 0.05 over the last 5 years.

BEFORE overrides the strategy back to the old 0.02. AFTER runs the
canonical default with no override, proving the config change took effect.
Full live path (dual momentum + orchestrator), monthly, costs on.
"""

import json
from datetime import date

from aurel2.config.settings import load_settings
from aurel2.core.assets import ASSET_REGISTRY
from aurel2.data.providers.yahoo import YahooFinanceProvider
from aurel2.engine.backtest import BacktestEngine

START = date(2021, 7, 15)
END = date(2026, 7, 15)
DATA_START = date(2019, 6, 1)
CAPITAL = 10_000.0


def run(prices, cost, override):
    engine = BacktestEngine(
        initial_capital=CAPITAL, transaction_cost_pct=cost,
        use_ai=False, dca_amount=0.0,
        correlation_guard=True, sideways_hold=True,
    )
    if override is not None:
        engine.dual_momentum.switch_threshold = override
    threshold_used = engine.dual_momentum.switch_threshold
    r = engine.run(prices=prices, start_date=START, end_date=END,
                   frequency="monthly", benchmark_symbol="SPY")
    switches = [(str(t.date), t.asset.symbol, round(float(t.price), 2))
                for t in r.trades]
    return {
        "threshold": threshold_used,
        "final_value": round(r.final_value, 2),
        "total_return_pct": round(r.total_return * 100, 2),
        "cagr_pct": round(r.cagr * 100, 2),
        "sharpe": round(r.sharpe_ratio, 3),
        "sortino": round(r.sortino_ratio, 3),
        "max_dd_pct": round(r.max_drawdown * 100, 2),
        "num_trades": r.num_trades,
        "benchmark_final": round(r.benchmark_final, 2) if r.benchmark_final else None,
        "trades": switches,
    }


def main():
    settings = load_settings(None)
    cost = settings.risk.transaction_cost_pct
    symbols = [a.yahoo_symbol for a in ASSET_REGISTRY.values() if a.yahoo_symbol]
    prices = YahooFinanceProvider().get_multi_prices(symbols, DATA_START, END)

    before = run(prices, cost, override=0.02)
    after = run(prices, cost, override=None)
    assert after["threshold"] == 0.05, "canonical default did not take effect"

    out = {"period": [str(START), str(END)], "before": before, "after": after}
    with open("experiments/threshold_before_after_5y.json", "w") as f:
        json.dump(out, f, indent=1)

    fmt = "%-18s %14s %14s"
    print(fmt % ("metric", "before 0.02", "after 0.05"))
    for k in ("final_value", "total_return_pct", "cagr_pct", "sharpe",
              "sortino", "max_dd_pct", "num_trades", "benchmark_final"):
        print(fmt % (k, before[k], after[k]))
    print("\nBEFORE trades:")
    for t in before["trades"]:
        print("  ", t)
    print("AFTER trades:")
    for t in after["trades"]:
        print("  ", t)
    print("\nsaved -> experiments/threshold_before_after_5y.json")


if __name__ == "__main__":
    main()
