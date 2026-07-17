"""Robustness sweep: switch_threshold x start-year, aurel2 full live path.

The single-path A/B (aurel3_review_ab.py) showed 0.04-0.06 beating the
canonical 0.02 but with a non-monotonic dip at 0.03 — a path-dependence
warning. This sweep re-runs each threshold from seven start years. A real
effect should win across most starts, not just from 2012.
"""

import json
from datetime import date

from aurel2.config.settings import load_settings
from aurel2.core.assets import ASSET_REGISTRY
from aurel2.data.providers.yahoo import YahooFinanceProvider
from aurel2.engine.backtest import BacktestEngine

END = date(2026, 7, 15)
DATA_START = date(2008, 6, 1)
THRESHOLDS = [0.02, 0.03, 0.04, 0.05, 0.06]
START_YEARS = [2010, 2012, 2014, 2016, 2018, 2020, 2022]


def main():
    settings = load_settings(None)
    cost = settings.risk.transaction_cost_pct
    symbols = [a.yahoo_symbol for a in ASSET_REGISTRY.values() if a.yahoo_symbol]
    prices = YahooFinanceProvider().get_multi_prices(symbols, DATA_START, END)
    print(f"records={len(prices)} cost={cost}")

    rows = []
    for year in START_YEARS:
        start = date(year, 1, 1)
        for th in THRESHOLDS:
            engine = BacktestEngine(
                initial_capital=10_000.0, transaction_cost_pct=cost,
                use_ai=False, dca_amount=0.0,
                correlation_guard=True, sideways_hold=True,
            )
            engine.dual_momentum.switch_threshold = th
            r = engine.run(prices=prices, start_date=start, end_date=END,
                           frequency="monthly", benchmark_symbol="SPY")
            rows.append({"start": year, "threshold": th,
                         "cagr": round(r.cagr * 100, 2),
                         "sharpe": round(r.sharpe_ratio, 3),
                         "max_dd": round(r.max_drawdown * 100, 2),
                         "trades": r.num_trades})
            print(f"start={year} th={th} cagr={r.cagr*100:.2f}% "
                  f"sharpe={r.sharpe_ratio:.3f} dd={r.max_drawdown*100:.1f}% "
                  f"trades={r.num_trades}")

    print("\nCAGR%% by start year (rows) x threshold (cols)")
    print("%6s" % "start", "".join("%9.2f" % t for t in THRESHOLDS))
    for year in START_YEARS:
        vals = [r["cagr"] for r in rows if r["start"] == year]
        best = max(vals)
        cells = "".join(
            "%8.2f%s" % (v, "*" if v == best else " ") for v in vals)
        print("%6d" % year, cells)

    print("\nSharpe by start year x threshold")
    print("%6s" % "start", "".join("%9.2f" % t for t in THRESHOLDS))
    for year in START_YEARS:
        vals = [r["sharpe"] for r in rows if r["start"] == year]
        best = max(vals)
        cells = "".join(
            "%8.3f%s" % (v, "*" if v == best else " ") for v in vals)
        print("%6d" % year, cells)

    with open("experiments/threshold_robustness_results.json", "w") as f:
        json.dump(rows, f, indent=1)
    print("\nsaved -> experiments/threshold_robustness_results.json")


if __name__ == "__main__":
    main()
