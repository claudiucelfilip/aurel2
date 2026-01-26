#!/usr/bin/env python3
"""Backtest script to compare original dual momentum vs pilot entry enhancement.

This script runs a 3-way comparison:
1. Original Dual Momentum (no pilot entry)
2. Dual Momentum with Pilot Entry
3. AI Expert with Pilot Entry (using Sonnet 3.5, 3-year lookback)
"""

import sys
from datetime import date, timedelta
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pandas as pd
from rich.console import Console
from rich.table import Table

from aurel2.core.assets import ASSET_REGISTRY
from aurel2.core.models import AssetClass
from aurel2.data.providers.yahoo import YahooFinanceProvider
from aurel2.strategies.dual_momentum import DualMomentumStrategy


def get_period_return(prices: pd.DataFrame, symbol: str, start: date, end: date) -> float:
    """Calculate return for a symbol over a period."""
    sym_prices = prices[prices["symbol"] == symbol].copy()
    if sym_prices.empty:
        return 0.0

    sym_prices["date"] = pd.to_datetime(sym_prices["date"])

    start_row = sym_prices[sym_prices["date"] <= pd.Timestamp(start)]
    end_row = sym_prices[sym_prices["date"] <= pd.Timestamp(end)]

    if start_row.empty or end_row.empty:
        return 0.0

    start_price = float(start_row.iloc[-1]["close"])
    end_price = float(end_row.iloc[-1]["close"])

    return (end_price / start_price - 1) * 100


def run_strategy_backtest(
    strategy: DualMomentumStrategy,
    prices_df: pd.DataFrame,
    decision_dates: list[date],
    initial_capital: float,
    strategy_name: str,
    console: Console,
) -> tuple[float, list[dict]]:
    """Run a backtest for a strategy and return final value and decisions."""
    portfolio_value = initial_capital
    holding: str | None = None
    decisions = []

    for i, decision_date in enumerate(decision_dates[:-1]):
        next_date = decision_dates[i + 1]

        # Get current asset class
        current_asset_class = None
        if holding:
            for ac, asset in ASSET_REGISTRY.items():
                if asset.symbol == holding:
                    current_asset_class = ac
                    break

        signal = strategy.generate_signal(
            prices=prices_df,
            calc_date=decision_date,
            current_holding=current_asset_class,
        )

        # Determine position size based on signal
        is_pilot = "PILOT ENTRY" in signal.reason
        is_scale_up = "SCALE UP" in signal.reason
        is_exit_pilot = "EXIT PILOT" in signal.reason

        # Execute
        if signal.action.value == "buy" and signal.asset:
            new_holding = signal.asset.symbol
            if new_holding != holding:
                holding = new_holding
        elif signal.action.value == "sell" or is_exit_pilot:
            holding = None

        # Calculate return for period with position sizing
        if holding:
            period_return = get_period_return(prices_df, holding, decision_date, next_date)

            # Adjust return based on position size (pilot = 30%)
            if is_pilot or (strategy.is_pilot_position if hasattr(strategy, 'is_pilot_position') else False):
                period_return *= strategy.pilot_position_size if hasattr(strategy, 'pilot_position_size') else 0.30
        else:
            period_return = 0.0  # Cash

        portfolio_value *= (1 + period_return / 100)
        decisions.append({
            "date": decision_date,
            "action": signal.action.value,
            "asset": holding,
            "reason": signal.reason[:80] if signal.reason else "",
            "is_pilot": is_pilot,
            "is_scale_up": is_scale_up,
            "period_return": period_return,
            "portfolio_value": portfolio_value,
        })

    return portfolio_value, decisions


def main():
    console = Console()

    # Parameters
    start_date = date(2021, 1, 1)
    end_date = date.today()
    initial_capital = 10000.0

    console.print("\n[bold cyan]PILOT ENTRY BACKTEST COMPARISON[/bold cyan]")
    console.print("=" * 60)
    console.print(f"Period: {start_date} to {end_date}")
    console.print(f"Initial Capital: ${initial_capital:,.2f}")
    console.print()

    # Fetch price data
    console.print("Fetching historical data...")
    provider = YahooFinanceProvider()
    symbols = ["SPY", "EFA", "EEM", "XLK", "XLF", "XLE", "XLV", "AGG", "TLT", "GLD", "DBC"]
    extended_start = start_date - timedelta(days=400)

    all_prices = []
    for symbol in symbols:
        try:
            prices = provider.get_prices(symbol, extended_start, end_date + timedelta(days=60))
            all_prices.append(prices)
        except Exception as e:
            console.print(f"[yellow]Warning: Could not fetch {symbol}: {e}[/yellow]")

    prices_df = pd.concat(all_prices, ignore_index=True)
    console.print(f"Fetched {len(prices_df)} price records")

    # Generate monthly decision dates
    decision_dates = pd.date_range(start=start_date, end=end_date, freq="ME")
    decision_dates = [d.date() for d in decision_dates]

    # 1. SPY Buy-and-Hold
    console.print("\n[bold]1. SPY Buy-and-Hold[/bold]")
    spy_prices = prices_df[prices_df["symbol"] == "SPY"].copy()
    spy_prices["date"] = pd.to_datetime(spy_prices["date"])
    spy_start_price = spy_prices[spy_prices["date"] >= pd.Timestamp(start_date)].iloc[0]["close"]
    spy_end_price = spy_prices[spy_prices["date"] <= pd.Timestamp(end_date)].iloc[-1]["close"]
    spy_return = (spy_end_price / spy_start_price - 1) * 100
    spy_final = initial_capital * (1 + spy_return / 100)
    console.print(f"   Final: ${spy_final:,.2f} ({spy_return:+.2f}%)")

    # 2. Original Dual Momentum (no pilot entry)
    console.print("\n[bold]2. Original Dual Momentum (no pilot)[/bold]")
    original_strategy = DualMomentumStrategy(
        assets=ASSET_REGISTRY,
        pilot_entry_enabled=False,
    )
    original_final, original_decisions = run_strategy_backtest(
        original_strategy, prices_df, decision_dates, initial_capital, "Original", console
    )
    original_return = (original_final / initial_capital - 1) * 100
    console.print(f"   Final: ${original_final:,.2f} ({original_return:+.2f}%)")

    # 3. Dual Momentum with Pilot Entry
    console.print("\n[bold]3. Dual Momentum with PILOT ENTRY[/bold]")
    pilot_strategy = DualMomentumStrategy(
        assets=ASSET_REGISTRY,
        pilot_entry_enabled=True,
        pilot_lookback_months=3,
        pilot_position_size=0.30,
    )
    pilot_final, pilot_decisions = run_strategy_backtest(
        pilot_strategy, prices_df, decision_dates, initial_capital, "Pilot Entry", console
    )
    pilot_return = (pilot_final / initial_capital - 1) * 100
    console.print(f"   Final: ${pilot_final:,.2f} ({pilot_return:+.2f}%)")

    # Count pilot entries
    pilot_entries = sum(1 for d in pilot_decisions if d["is_pilot"])
    scale_ups = sum(1 for d in pilot_decisions if d["is_scale_up"])
    console.print(f"   Pilot entries: {pilot_entries}")
    console.print(f"   Scale-ups: {scale_ups}")

    # Results table
    console.print("\n")
    years = (end_date - start_date).days / 365.25

    table = Table(title=f"Performance Comparison ({years:.1f} years)")
    table.add_column("Strategy", style="cyan")
    table.add_column("Final Value", justify="right")
    table.add_column("Total Return", justify="right")
    table.add_column("CAGR", justify="right")
    table.add_column("Alpha vs SPY", justify="right")

    spy_cagr = ((spy_final / initial_capital) ** (1 / years) - 1) * 100
    original_cagr = ((original_final / initial_capital) ** (1 / years) - 1) * 100
    pilot_cagr = ((pilot_final / initial_capital) ** (1 / years) - 1) * 100

    table.add_row(
        "SPY (Buy & Hold)",
        f"${spy_final:,.2f}",
        f"{spy_return:+.2f}%",
        f"{spy_cagr:.2f}%",
        "-",
    )
    table.add_row(
        "Original (no pilot)",
        f"${original_final:,.2f}",
        f"{original_return:+.2f}%",
        f"{original_cagr:.2f}%",
        f"{original_return - spy_return:+.2f}%",
    )
    table.add_row(
        "[green]With Pilot Entry[/green]",
        f"[green]${pilot_final:,.2f}[/green]",
        f"[green]{pilot_return:+.2f}%[/green]",
        f"[green]{pilot_cagr:.2f}%[/green]",
        f"[green]{pilot_return - spy_return:+.2f}%[/green]",
    )

    console.print(table)

    # Pilot vs Original comparison
    console.print(f"\n[bold]Pilot Entry vs Original:[/bold]")
    diff = pilot_return - original_return
    if diff > 0:
        console.print(f"  [green]Pilot Entry outperformed by {diff:+.2f}%[/green]")
    else:
        console.print(f"  [red]Pilot Entry underperformed by {diff:.2f}%[/red]")

    # Show pilot entry decisions
    console.print(f"\n[bold]Pilot Entry Decisions:[/bold]")
    for d in pilot_decisions:
        if d["is_pilot"] or d["is_scale_up"]:
            label = "[cyan]PILOT[/cyan]" if d["is_pilot"] else "[green]SCALE UP[/green]"
            console.print(f"  {d['date']}: {label} {d['asset']} - {d['reason']}")

    # Save results
    import json
    from datetime import datetime

    results = {
        "type": "pilot_entry_comparison",
        "start_date": str(start_date),
        "end_date": str(end_date),
        "initial_capital": initial_capital,
        "years": round(years, 2),
        "spy": {
            "final_value": round(spy_final, 2),
            "total_return_pct": round(spy_return, 2),
            "cagr_pct": round(spy_cagr, 2),
        },
        "original": {
            "final_value": round(original_final, 2),
            "total_return_pct": round(original_return, 2),
            "cagr_pct": round(original_cagr, 2),
            "alpha_vs_spy": round(original_return - spy_return, 2),
        },
        "pilot_entry": {
            "final_value": round(pilot_final, 2),
            "total_return_pct": round(pilot_return, 2),
            "cagr_pct": round(pilot_cagr, 2),
            "alpha_vs_spy": round(pilot_return - spy_return, 2),
            "alpha_vs_original": round(pilot_return - original_return, 2),
            "pilot_entries": pilot_entries,
            "scale_ups": scale_ups,
        },
        "saved_at": datetime.now().isoformat(),
    }

    results_file = Path(__file__).parent.parent / "data" / "backtest_results.json"
    existing = []
    if results_file.exists():
        with open(results_file) as f:
            data = json.load(f)
            existing = data.get("results", [])

    existing.append(results)
    existing = existing[-50:]

    with open(results_file, "w") as f:
        json.dump({"results": existing}, f, indent=2)

    console.print(f"\n[dim]Results saved to {results_file}[/dim]")

    return pilot_return > original_return


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
