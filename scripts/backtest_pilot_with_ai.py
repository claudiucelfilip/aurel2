#!/usr/bin/env python3
"""Backtest pilot entry with AI Expert (Sonnet, 3-year lookback).

Full comparison:
1. SPY Buy-and-Hold
2. Original Dual Momentum (no pilot)
3. Dual Momentum with Pilot Entry
4. AI Expert + Pilot Entry (Sonnet 3.5, 3-year lookback)
"""

import os
import sys
from datetime import date, timedelta
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pandas as pd
import structlog
from rich.console import Console
from rich.table import Table

# Suppress excessive logging
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(30),  # WARNING and above
)

from aurel2.agent.ai_evaluator import ClaudeCodeExpertEvaluator
from aurel2.agent.failure_analyzer import FailureAnalysis
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


def build_market_context(prices: pd.DataFrame, target_date: date) -> str:
    """Build market context string for the AI."""
    lines = []

    # SPY info
    spy_prices = prices[prices["symbol"] == "SPY"].copy()
    spy_prices["date"] = pd.to_datetime(spy_prices["date"])
    spy_prices = spy_prices[spy_prices["date"] <= pd.Timestamp(target_date)]

    if not spy_prices.empty:
        current_price = spy_prices.iloc[-1]["close"]
        year_ago = target_date - timedelta(days=365)
        year_prices = spy_prices[spy_prices["date"] >= pd.Timestamp(year_ago)]
        if not year_prices.empty:
            year_high = year_prices["close"].max()
            drawdown = (current_price / year_high - 1) * 100
            lines.append(f"SPY: ${current_price:.2f} (drawdown from 52w high: {drawdown:.1f}%)")

    lines.append(f"Decision Date: {target_date}")
    return "\n".join(lines)


def main():
    console = Console()

    # Parameters
    start_date = date(2021, 1, 1)
    end_date = date.today()
    initial_capital = 10000.0
    failure_file = "data/failure_learnings.json"
    lookback_years = 3
    model = "sonnet"

    console.print("\n[bold cyan]PILOT ENTRY + AI EXPERT BACKTEST[/bold cyan]")
    console.print("=" * 70)
    console.print(f"Period: {start_date} to {end_date}")
    console.print(f"Initial Capital: ${initial_capital:,.2f}")
    console.print(f"AI Model: {model}")
    console.print(f"Failure Lookback: {lookback_years} years (rolling)")
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
    original_strategy = DualMomentumStrategy(assets=ASSET_REGISTRY, pilot_entry_enabled=False)

    original_value = initial_capital
    original_holding = None
    for i, decision_date in enumerate(decision_dates[:-1]):
        next_date = decision_dates[i + 1]
        current_ac = None
        if original_holding:
            for ac, asset in ASSET_REGISTRY.items():
                if asset.symbol == original_holding:
                    current_ac = ac
                    break

        signal = original_strategy.generate_signal(prices_df, decision_date, current_ac)
        if signal.action.value == "buy" and signal.asset:
            original_holding = signal.asset.symbol
        elif signal.action.value == "sell":
            original_holding = None

        if original_holding:
            period_return = get_period_return(prices_df, original_holding, decision_date, next_date)
        else:
            period_return = 0.0
        original_value *= (1 + period_return / 100)

    original_return = (original_value / initial_capital - 1) * 100
    console.print(f"   Final: ${original_value:,.2f} ({original_return:+.2f}%)")

    # 3. Dual Momentum with Pilot Entry
    console.print("\n[bold]3. Dual Momentum with PILOT ENTRY[/bold]")
    pilot_strategy = DualMomentumStrategy(
        assets=ASSET_REGISTRY,
        pilot_entry_enabled=True,
        pilot_lookback_months=3,
        pilot_position_size=0.30,
    )

    pilot_value = initial_capital
    pilot_holding = None
    pilot_entries = 0
    scale_ups = 0
    is_pilot = False

    for i, decision_date in enumerate(decision_dates[:-1]):
        next_date = decision_dates[i + 1]
        current_ac = None
        if pilot_holding:
            for ac, asset in ASSET_REGISTRY.items():
                if asset.symbol == pilot_holding:
                    current_ac = ac
                    break

        signal = pilot_strategy.generate_signal(prices_df, decision_date, current_ac)

        this_is_pilot = "PILOT ENTRY" in signal.reason
        this_is_scale = "SCALE UP" in signal.reason
        this_is_exit = "EXIT PILOT" in signal.reason

        if this_is_pilot:
            pilot_entries += 1
            is_pilot = True
        if this_is_scale:
            scale_ups += 1
            is_pilot = False

        if signal.action.value == "buy" and signal.asset:
            pilot_holding = signal.asset.symbol
        elif signal.action.value == "sell" or this_is_exit:
            pilot_holding = None
            is_pilot = False

        if pilot_holding:
            period_return = get_period_return(prices_df, pilot_holding, decision_date, next_date)
            # Adjust for pilot position size
            if is_pilot:
                period_return *= 0.30
        else:
            period_return = 0.0
        pilot_value *= (1 + period_return / 100)

    pilot_return = (pilot_value / initial_capital - 1) * 100
    console.print(f"   Final: ${pilot_value:,.2f} ({pilot_return:+.2f}%)")
    console.print(f"   Pilot entries: {pilot_entries}, Scale-ups: {scale_ups}")

    # 4. AI Expert with Pilot Entry Strategy
    console.print("\n[bold]4. AI Expert + Pilot Entry (Sonnet, 3yr lookback)[/bold]")

    # Load failure analysis
    full_failure_analysis = None
    if os.path.exists(failure_file):
        full_failure_analysis = FailureAnalysis.load(failure_file)
        console.print(f"   Loaded {len(full_failure_analysis.failure_events)} failure learnings")

    # Initialize AI evaluator
    ai_evaluator = ClaudeCodeExpertEvaluator(model=model)

    # Reset pilot strategy for AI run
    ai_pilot_strategy = DualMomentumStrategy(
        assets=ASSET_REGISTRY,
        pilot_entry_enabled=True,
        pilot_lookback_months=3,
        pilot_position_size=0.30,
    )

    ai_value = initial_capital
    ai_holding = None
    ai_is_pilot = False
    agreements = 0
    disagreements = 0
    ai_decisions = []

    for i, decision_date in enumerate(decision_dates[:-1]):
        next_date = decision_dates[i + 1]
        console.print(f"   Processing {decision_date} ({i+1}/{len(decision_dates)-1})...", end="\r")

        current_ac = None
        if ai_holding:
            for ac, asset in ASSET_REGISTRY.items():
                if asset.symbol == ai_holding:
                    current_ac = ac
                    break

        # Get deterministic signal with pilot entry
        det_signal = ai_pilot_strategy.generate_signal(prices_df, decision_date, current_ac)
        det_action = det_signal.action.value
        det_asset = det_signal.asset.symbol if det_signal.asset else None
        det_is_pilot = "PILOT ENTRY" in det_signal.reason
        det_is_scale = "SCALE UP" in det_signal.reason

        # Build context for AI
        failure_context = ""
        if full_failure_analysis:
            failure_context = full_failure_analysis.to_prompt_text(
                as_of_date=decision_date,
                lookback_years=lookback_years,
            )

        market_context = build_market_context(prices_df, decision_date)
        full_context = f"{failure_context}\n\n---\n\nMARKET CONTEXT:\n{market_context}"

        # Include pilot info in signals
        signals = {
            "dual_momentum": {
                "action": det_action,
                "asset_symbol": det_asset,
                "confidence": 0.7,
                "reasoning": det_signal.reason,
                "is_pilot_entry": det_is_pilot,
                "is_scale_up": det_is_scale,
            }
        }

        deterministic_decision = {"action": det_action, "asset": det_asset}

        # Get AI decision
        try:
            ai_decision = ai_evaluator.evaluate(
                signals=signals,
                context_text=full_context,
                current_holding=ai_holding,
                deterministic_decision=deterministic_decision,
            )
            ai_action = ai_decision.action
            ai_asset = ai_decision.asset
            ai_reasoning = ai_decision.reasoning[:100] if ai_decision.reasoning else ""
        except Exception as e:
            ai_action = det_action
            ai_asset = det_asset
            ai_reasoning = f"AI failed: {e}"

        # Check agreement
        agreed = (det_action == ai_action) and (det_action != "buy" or det_asset == ai_asset)
        if agreed:
            agreements += 1
        else:
            disagreements += 1

        # Execute AI decision (use deterministic pilot detection since AI follows strategy)
        if ai_action == "buy" and ai_asset:
            ai_holding = ai_asset
            if det_is_pilot:
                ai_is_pilot = True
            elif det_is_scale:
                ai_is_pilot = False
        elif ai_action == "sell":
            ai_holding = None
            ai_is_pilot = False

        # Calculate return
        if ai_holding:
            period_return = get_period_return(prices_df, ai_holding, decision_date, next_date)
            if ai_is_pilot:
                period_return *= 0.30
        else:
            period_return = 0.0

        ai_value *= (1 + period_return / 100)
        ai_decisions.append({
            "date": decision_date,
            "det_action": det_action,
            "det_asset": det_asset,
            "ai_action": ai_action,
            "ai_asset": ai_holding,
            "agreed": agreed,
            "reasoning": ai_reasoning,
        })

    console.print("   " + " " * 60)
    ai_return = (ai_value / initial_capital - 1) * 100
    console.print(f"   Final: ${ai_value:,.2f} ({ai_return:+.2f}%)")
    console.print(f"   Agreements: {agreements}, Disagreements: {disagreements}")

    # Results table
    years = (end_date - start_date).days / 365.25

    spy_cagr = ((spy_final / initial_capital) ** (1 / years) - 1) * 100
    original_cagr = ((original_value / initial_capital) ** (1 / years) - 1) * 100
    pilot_cagr = ((pilot_value / initial_capital) ** (1 / years) - 1) * 100
    ai_cagr = ((ai_value / initial_capital) ** (1 / years) - 1) * 100

    console.print("\n")
    table = Table(title=f"Full Performance Comparison ({years:.1f} years)")
    table.add_column("Strategy", style="cyan")
    table.add_column("Final Value", justify="right")
    table.add_column("Total Return", justify="right")
    table.add_column("CAGR", justify="right")
    table.add_column("Alpha vs SPY", justify="right")
    table.add_column("Alpha vs Original", justify="right")

    table.add_row(
        "SPY (Buy & Hold)",
        f"${spy_final:,.2f}",
        f"{spy_return:+.2f}%",
        f"{spy_cagr:.2f}%",
        "-",
        "-",
    )
    table.add_row(
        "Original (no pilot)",
        f"${original_value:,.2f}",
        f"{original_return:+.2f}%",
        f"{original_cagr:.2f}%",
        f"{original_return - spy_return:+.2f}%",
        "-",
    )
    table.add_row(
        "[green]With Pilot Entry[/green]",
        f"[green]${pilot_value:,.2f}[/green]",
        f"[green]{pilot_return:+.2f}%[/green]",
        f"[green]{pilot_cagr:.2f}%[/green]",
        f"[green]{pilot_return - spy_return:+.2f}%[/green]",
        f"[green]{pilot_return - original_return:+.2f}%[/green]",
    )
    table.add_row(
        "[bold magenta]AI Expert + Pilot[/bold magenta]",
        f"[bold magenta]${ai_value:,.2f}[/bold magenta]",
        f"[bold magenta]{ai_return:+.2f}%[/bold magenta]",
        f"[bold magenta]{ai_cagr:.2f}%[/bold magenta]",
        f"[bold magenta]{ai_return - spy_return:+.2f}%[/bold magenta]",
        f"[bold magenta]{ai_return - original_return:+.2f}%[/bold magenta]",
    )

    console.print(table)

    # Summary
    console.print("\n[bold]Summary:[/bold]")
    console.print(f"  Pilot Entry improvement over Original: [green]{pilot_return - original_return:+.2f}%[/green]")
    console.print(f"  AI Expert improvement over Original:   [magenta]{ai_return - original_return:+.2f}%[/magenta]")
    console.print(f"  AI Expert improvement over Pilot:      [magenta]{ai_return - pilot_return:+.2f}%[/magenta]")

    # Show disagreements
    disagree_list = [d for d in ai_decisions if not d["agreed"]]
    if disagree_list:
        console.print(f"\n[bold]AI Disagreements ({len(disagree_list)}):[/bold]")
        for d in disagree_list[:10]:
            console.print(f"  {d['date']}: Det {d['det_action'].upper()} {d['det_asset']} vs AI {d['ai_action'].upper()} {d['ai_asset']}")
            if d['reasoning']:
                console.print(f"    Reason: {d['reasoning'][:80]}...")

    # Save results
    import json
    from datetime import datetime

    results = {
        "type": "pilot_entry_ai_comparison",
        "start_date": str(start_date),
        "end_date": str(end_date),
        "initial_capital": initial_capital,
        "model": model,
        "lookback_years": lookback_years,
        "years": round(years, 2),
        "spy": {
            "final_value": round(spy_final, 2),
            "total_return_pct": round(spy_return, 2),
            "cagr_pct": round(spy_cagr, 2),
        },
        "original": {
            "final_value": round(original_value, 2),
            "total_return_pct": round(original_return, 2),
            "cagr_pct": round(original_cagr, 2),
            "alpha_vs_spy": round(original_return - spy_return, 2),
        },
        "pilot_entry": {
            "final_value": round(pilot_value, 2),
            "total_return_pct": round(pilot_return, 2),
            "cagr_pct": round(pilot_cagr, 2),
            "alpha_vs_spy": round(pilot_return - spy_return, 2),
            "alpha_vs_original": round(pilot_return - original_return, 2),
            "pilot_entries": pilot_entries,
            "scale_ups": scale_ups,
        },
        "ai_expert_pilot": {
            "final_value": round(ai_value, 2),
            "total_return_pct": round(ai_return, 2),
            "cagr_pct": round(ai_cagr, 2),
            "alpha_vs_spy": round(ai_return - spy_return, 2),
            "alpha_vs_original": round(ai_return - original_return, 2),
            "alpha_vs_pilot": round(ai_return - pilot_return, 2),
            "agreements": agreements,
            "disagreements": disagreements,
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

    # Recommendation
    best = max([
        ("Original", original_return),
        ("Pilot Entry", pilot_return),
        ("AI Expert + Pilot", ai_return),
    ], key=lambda x: x[1])

    console.print(f"\n[bold green]RECOMMENDATION: {best[0]} is the best performer with {best[1]:+.2f}% return[/bold green]")

    return pilot_return > original_return or ai_return > original_return


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
