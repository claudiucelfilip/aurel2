"""Command-line interface for Aurel2."""

from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import typer
import structlog
from rich.console import Console
from rich.table import Table

from aurel2.config.settings import load_settings
from aurel2.core.models import Asset, AssetClass
from aurel2.data.providers.yahoo import YahooFinanceProvider
from aurel2.strategies.dual_momentum import DualMomentumStrategy
from aurel2.engine.backtest import BacktestEngine
from aurel2.persistence.portfolio import PortfolioStore, Holding

# Configure structlog
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

import logging
logging.basicConfig(level=logging.INFO)

app = typer.Typer(help="Aurel2: Tax-optimized momentum trading")


@app.command()
def backtest(
    start: str = typer.Option("2015-01-01", help="Start date (YYYY-MM-DD)"),
    end: str = typer.Option(None, help="End date (YYYY-MM-DD), defaults to today"),
    capital: float = typer.Option(10000.0, help="Initial capital"),
    dca: float = typer.Option(0.0, "--dca", help="Monthly DCA contribution amount"),
    frequency: str = typer.Option("monthly", help="Rebalance frequency: daily, monthly, or quarterly"),
    ai: bool = typer.Option(False, "--ai", help="Enable AI advisor (disabled by default — see ARCHITECTURE.md for findings)"),
    ai_model: str = typer.Option("haiku", "--ai-model", help="AI model: sonnet, opus, haiku"),
    amnesia: bool = typer.Option(False, "--amnesia", help="Tell AI to ignore training data financial knowledge and redact dates"),
    ai_enrich: bool = typer.Option(False, "--ai-enrich", help="Add per-asset confirmation stats (trend/momentum/RSI/RS) to the AI advisor prompt"),
    correlation_guard: bool = typer.Option(True, "--correlation-guard/--no-correlation-guard", help="Redirect bond rotations when SPY-AGG correlation is high"),
    sideways_hold: bool = typer.Option(True, "--sideways-hold/--no-sideways-hold", help="Suppress switches in sideways markets unless momentum advantage is large"),
    config: Path = typer.Option(None, help="Config file path"),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Verbose output"),
):
    """Run a backtest mirroring the full live trading path (dual momentum + orchestrator + AI)."""
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Parse dates
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end) if end else date.today()

    typer.echo(f"Running backtest from {start_date} to {end_date}")
    typer.echo(f"Initial capital: ${capital:,.2f}")
    if dca > 0:
        typer.echo(f"Monthly DCA: ${dca:,.2f}")
    typer.echo(f"Rebalance frequency: {frequency}")
    typer.echo(f"AI advisor: {'enabled' if ai else 'disabled'} (model: {ai_model})")
    if amnesia:
        typer.echo(f"AI amnesia mode: enabled (dates redacted, no financial knowledge)")
    if ai_enrich:
        typer.echo("AI asset-context enrichment: enabled (per-asset trend/momentum/RSI/RS)")
    typer.echo(f"Correlation guard: {'enabled' if correlation_guard else 'disabled'}")
    typer.echo(f"Sideways hold: {'enabled' if sideways_hold else 'disabled'}")

    # Load settings
    settings = load_settings(config)

    # Use full asset registry (11 assets) for consistency with compare command
    from aurel2.core.assets import ASSET_REGISTRY, get_all_yahoo_symbols
    assets = ASSET_REGISTRY

    # Fetch price data directly from Yahoo Finance
    typer.echo("\nFetching historical data...")
    provider = YahooFinanceProvider()
    symbols = [a.yahoo_symbol for a in assets.values() if a.yahoo_symbol]

    prices = provider.get_multi_prices(symbols, start_date, end_date)
    typer.echo(f"Fetched {len(prices)} price records")

    # Run backtest with full live path
    typer.echo("\nRunning backtest...")
    engine = BacktestEngine(
        initial_capital=capital,
        transaction_cost_pct=settings.risk.transaction_cost_pct,
        use_ai=ai,
        ai_model=ai_model,
        amnesia=amnesia,
        ai_enrich=ai_enrich,
        dca_amount=dca,
        correlation_guard=correlation_guard,
        sideways_hold=sideways_hold,
    )

    # Use SPY as benchmark
    benchmark = "SPY"

    result = engine.run(
        prices=prices,
        start_date=start_date,
        end_date=end_date,
        frequency=frequency,
        benchmark_symbol=benchmark,
    )

    # Print results
    result.print_summary()

    # Print trade history
    if verbose and result.trades:
        typer.echo("\nTRADE HISTORY:")
        typer.echo("-" * 60)
        for trade in result.trades:
            typer.echo(
                f"{trade.date} | {trade.action.value:4} | {trade.asset.symbol:10} | "
                f"{float(trade.shares):8.2f} @ ${trade.price:8.2f} | "
                f"Value: ${trade.value:10.2f}"
            )

    # Save results
    years = (end_date - start_date).days / 365.25
    benchmark_return = (result.benchmark_final / capital - 1) if result.benchmark_final else None
    _save_backtest_results({
        "type": "backtest",
        "start_date": str(start_date),
        "end_date": str(end_date),
        "initial_capital": capital,
        "dca_amount": dca,
        "total_invested": round(result.total_invested, 2) if dca > 0 else capital,
        "frequency": frequency,
        "years": round(years, 2),
        "final_value": round(result.final_value, 2),
        "total_return_pct": round(result.total_return * 100, 2),
        "cagr_pct": round(result.cagr * 100, 2),
        "max_drawdown_pct": round(result.max_drawdown * 100, 2),
        "sharpe_ratio": round(result.sharpe_ratio, 2) if result.sharpe_ratio else None,
        "benchmark_return_pct": round(benchmark_return * 100, 2) if benchmark_return else None,
        "alpha_pct": round((result.total_return - benchmark_return) * 100, 2) if benchmark_return else None,
        "num_trades": len(result.trades),
    })


@app.command()
def momentum(
    date_str: str = typer.Option(None, "--date", "-d", help="Date to check (YYYY-MM-DD), defaults to today"),
    config: Path = typer.Option(None, help="Config file path"),
):
    """Show current momentum scores and personalized trading recommendation."""
    from datetime import date as date_type

    check_date = date_type.fromisoformat(date_str) if date_str else date_type.today()

    typer.echo(f"\nCalculating momentum scores for {check_date}...")

    # Load portfolio
    store = PortfolioStore()
    portfolio = store.load()

    # Load settings
    settings = load_settings(config)

    # Define assets with full details
    assets = {
        AssetClass.US_STOCKS: Asset(
            symbol="SPY",
            name="S&P 500 US Stocks",
            asset_class=AssetClass.US_STOCKS,
            yahoo_symbol="SPY"
        ),
        AssetClass.GLOBAL_STOCKS: Asset(
            symbol="EFA",
            name="International Developed Markets (ex-US)",
            asset_class=AssetClass.GLOBAL_STOCKS,
            yahoo_symbol="EFA"
        ),
        AssetClass.BONDS: Asset(
            symbol="AGG",
            name="US Aggregate Bonds",
            asset_class=AssetClass.BONDS,
            yahoo_symbol="AGG"
        ),
        AssetClass.CASH: Asset(
            symbol="CASH",
            name="Cash / Money Market",
            asset_class=AssetClass.CASH
        ),
    }

    # US ETFs used for trading
    etf_details = {
        AssetClass.US_STOCKS: {
            "symbol": "SPY",
            "name": "SPDR S&P 500 ETF Trust",
        },
        AssetClass.GLOBAL_STOCKS: {
            "symbol": "EFA",
            "name": "iShares MSCI EAFE ETF",
        },
        AssetClass.BONDS: {
            "symbol": "AGG",
            "name": "iShares Core US Aggregate Bond ETF",
        },
        AssetClass.CASH: {
            "symbol": "CASH",
            "name": "Hold in broker cash account",
        },
    }

    # Map symbols to asset classes
    symbol_to_class = {
        "SPY": AssetClass.US_STOCKS,
        "EFA": AssetClass.GLOBAL_STOCKS,
        "AGG": AssetClass.BONDS,
    }

    # Fetch data
    provider = YahooFinanceProvider()
    symbols = [a.yahoo_symbol for a in assets.values() if a.yahoo_symbol]

    from dateutil.relativedelta import relativedelta
    start = check_date - relativedelta(months=14)

    prices = provider.get_multi_prices(symbols, start, check_date)

    # Calculate momentum
    from aurel2.data.momentum import calculate_momentum_scores

    scores = calculate_momentum_scores(
        prices=prices,
        assets=assets,
        calc_date=check_date,
        lookback_months=12,
        cash_rate=0.04,
    )

    # Find winner
    sorted_scores = sorted(scores.items(), key=lambda x: x[1].momentum_12m, reverse=True)
    winner_class, winner_score = sorted_scores[0]
    winner_etf = etf_details[winner_class]

    # Determine current holding
    current_holding = None
    current_asset_class = None
    for h in portfolio.holdings:
        if h.symbol.upper() in symbol_to_class:
            current_holding = h
            current_asset_class = symbol_to_class[h.symbol.upper()]
            break

    # Print current portfolio status
    typer.echo("\n" + "=" * 70)
    typer.echo("YOUR CURRENT POSITION")
    typer.echo("=" * 70)

    if current_holding:
        typer.echo(f"  Holding: {current_holding.shares:.4f} shares of {current_holding.symbol}")
        typer.echo(f"  Entry:   {current_holding.entry_price:.2f} EUR on {current_holding.entry_date}")
        typer.echo(f"  Broker:  {current_holding.broker}")
        typer.echo(f"  Days held: {current_holding.days_held()}")
        if current_holding.is_long_term():
            typer.echo(f"  Tax rate: 1% (held >365 days)")
        else:
            typer.echo(f"  Tax rate: 3% (held <365 days)")
            typer.echo(f"  Days until 1% tax: {current_holding.days_until_long_term()}")
    else:
        typer.echo("  No position recorded. Holding cash.")
        typer.echo("  Use 'aurel2 buy <symbol> <shares> <price>' to record a purchase.")

    if portfolio.cash > 0:
        typer.echo(f"  Cash available: {portfolio.cash:.2f} EUR")

    # Print momentum table
    typer.echo("\n" + "=" * 70)
    typer.echo("MOMENTUM SCORES (12-month lookback)")
    typer.echo("=" * 70)
    typer.echo(f"{'':2} {'Asset':<35} {'Momentum':>10} {'Price':>10}")
    typer.echo("-" * 70)

    for asset_class, score in sorted_scores:
        indicator = ">>" if asset_class == winner_class else "  "
        held = " (YOU)" if asset_class == current_asset_class else ""
        typer.echo(
            f"{indicator} {score.asset.name:<35} {score.momentum_12m:>9.2%} "
            f"${score.price:>9.2f}{held}"
        )

    typer.echo("=" * 70)

    # Calculate switch threshold
    switch_threshold = 0.10  # 10%

    # Determine action
    typer.echo("\n" + "=" * 70)
    typer.echo("RECOMMENDATION")
    typer.echo("=" * 70)

    if current_holding is None:
        # No position - recommend buying the winner
        if winner_score.momentum_12m <= 0.04:
            typer.echo("\nACTION: STAY IN CASH")
            typer.echo("\nAll assets are underperforming cash. Wait for better conditions.")
        else:
            typer.echo(f"\nACTION: BUY {winner_etf['symbol']}")
            typer.echo(f"\n  Symbol: {winner_etf['symbol']}")
            typer.echo(f"  Name:   {winner_etf['name']}")
            typer.echo(f"\n  {winner_class.value.replace('_', ' ').title()} has {winner_score.momentum_12m:.2%} momentum")

            if portfolio.cash > 0:
                # Estimate shares to buy (rough, as we don't have live EUR prices)
                typer.echo(f"\n  With {portfolio.cash:.2f} EUR, buy as many shares as possible.")
    else:
        # Have a position - check if we should switch
        current_score = scores.get(current_asset_class)

        if current_score is None:
            typer.echo(f"\nWARNING: Cannot find momentum data for your holding ({current_holding.symbol})")
        elif winner_class == current_asset_class:
            # Already holding the winner
            typer.echo(f"\nACTION: HOLD {current_holding.symbol}")
            typer.echo(f"\nYou already own the momentum winner. No action needed.")
            typer.echo(f"  Your holding: {current_score.momentum_12m:.2%} momentum")
        else:
            # Different asset is winning - check threshold
            momentum_diff = winner_score.momentum_12m - current_score.momentum_12m

            if momentum_diff > switch_threshold:
                # Should switch
                typer.echo(f"\nACTION: SELL {current_holding.symbol} -> BUY {winner_etf['symbol']}")
                typer.echo(f"\n  Momentum difference: {momentum_diff:.2%} (threshold: {switch_threshold:.0%})")
                typer.echo(f"  Your holding: {current_score.momentum_12m:.2%}")
                typer.echo(f"  Winner:       {winner_score.momentum_12m:.2%}")

                # Tax warning
                typer.echo("\n" + "-" * 70)
                typer.echo("TAX IMPACT:")
                typer.echo("-" * 70)

                if current_holding.is_long_term():
                    typer.echo(f"  Tax rate: 1% (you've held >365 days)")
                else:
                    days_left = current_holding.days_until_long_term()
                    typer.echo(f"  Tax rate if sold now: 3%")
                    typer.echo(f"  Tax rate if you wait {days_left} more days: 1%")

                    # Calculate if waiting is worth it
                    # This is a rough heuristic
                    if days_left <= 30:
                        typer.echo(f"\n  SUGGESTION: Consider waiting {days_left} days for 1% tax rate.")
                        typer.echo(f"              The 2% tax savings may outweigh momentum difference.")
                    elif days_left <= 90 and momentum_diff < 0.20:
                        typer.echo(f"\n  SUGGESTION: Consider waiting. Momentum diff ({momentum_diff:.2%}) is moderate.")
                    else:
                        typer.echo(f"\n  SUGGESTION: Switch now. Momentum diff ({momentum_diff:.2%}) is significant.")

                typer.echo("\n" + "-" * 70)
                typer.echo("TO EXECUTE:")
                typer.echo("-" * 70)
                typer.echo(f"  1. Sell all {current_holding.shares:.4f} shares of {current_holding.symbol}")
                typer.echo(f"  2. Buy {winner_etf['symbol']} ({winner_etf['name']})")
                typer.echo(f"  3. Record the sale: aurel2 sell {current_holding.symbol}")
                typer.echo(f"  4. Record the buy:  aurel2 buy {winner_etf['symbol']} <shares> <price>")
            else:
                # Difference not big enough
                typer.echo(f"\nACTION: HOLD {current_holding.symbol}")
                typer.echo(f"\n  {winner_etf['symbol']} is winning, but difference is only {momentum_diff:.2%}")
                typer.echo(f"  Threshold to switch: {switch_threshold:.0%}")
                typer.echo(f"  No action needed - keep holding {current_holding.symbol}.")

    # Check absolute momentum (below cash)
    if current_holding and current_asset_class:
        current_score = scores.get(current_asset_class)
        cash_score = scores.get(AssetClass.CASH)
        if current_score and cash_score and current_score.momentum_12m < cash_score.momentum_12m:
            typer.echo("\n" + "-" * 70)
            typer.echo("WARNING: NEGATIVE ABSOLUTE MOMENTUM")
            typer.echo("-" * 70)
            typer.echo(f"  Your holding ({current_score.momentum_12m:.2%}) is below cash ({cash_score.momentum_12m:.2%})")
            typer.echo(f"  Consider moving to cash to protect capital.")

    typer.echo("\n" + "=" * 70)

    # Next rebalance
    year = check_date.year
    month = check_date.month

    quarter_ends = [(3, 31), (6, 30), (9, 30), (12, 31)]
    next_rebalance = None
    for q_month, q_day in quarter_ends:
        if month < q_month or (month == q_month and check_date.day < q_day):
            next_rebalance = date_type(year, q_month, q_day)
            break
    if next_rebalance is None:
        next_rebalance = date_type(year + 1, 3, 31)

    typer.echo(f"Next rebalance check: {next_rebalance}")


@app.command()
def buy(
    symbol: str = typer.Argument(..., help="ETF symbol (e.g., VWRA, CSPX, AGGH)"),
    shares: float = typer.Argument(..., help="Number of shares purchased"),
    price: float = typer.Argument(..., help="Price per share in EUR"),
    entry_date: str = typer.Option(None, "--date", "-d", help="Purchase date (YYYY-MM-DD), defaults to today"),
    broker: str = typer.Option("alpaca", "--broker", "-b", help="Broker name"),
    name: str = typer.Option(None, "--name", "-n", help="ETF name (optional)"),
    isin: str = typer.Option(None, "--isin", "-i", help="ISIN code (optional)"),
):
    """Record a purchase in your portfolio."""
    from datetime import date as date_type

    purchase_date = date_type.fromisoformat(entry_date) if entry_date else date_type.today()

    # Default names for known symbols
    known_etfs = {
        "VWRA": ("Vanguard FTSE All-World UCITS ETF (Acc)", "IE00BK5BQT80"),
        "CSPX": ("iShares Core S&P 500 UCITS ETF (Acc)", "IE00B5BMR087"),
        "AGGH": ("iShares Core Global Aggregate Bond UCITS ETF (Acc)", "IE00BDBRDM35"),
        "SPY": ("SPDR S&P 500 ETF Trust", None),
        "EFA": ("iShares MSCI EAFE ETF", None),
        "AGG": ("iShares Core US Aggregate Bond ETF", None),
    }

    symbol = symbol.upper()
    if not name and symbol in known_etfs:
        name = known_etfs[symbol][0]
    if not isin and symbol in known_etfs:
        isin = known_etfs[symbol][1]

    name = name or symbol

    store = PortfolioStore()
    holding = store.add_holding(
        symbol=symbol,
        name=name,
        shares=shares,
        price=price,
        entry_date=purchase_date,
        broker=broker,
        isin=isin,
    )

    total_cost = shares * price
    typer.echo(f"\nRecorded purchase:")
    typer.echo(f"  {shares:.4f} shares of {symbol} @ {price:.2f} EUR")
    typer.echo(f"  Total cost: {total_cost:.2f} EUR")
    typer.echo(f"  Date: {purchase_date}")
    typer.echo(f"  Broker: {broker}")
    typer.echo(f"\nHolding will qualify for 1% tax after: {holding.entry_date.replace(year=holding.entry_date.year + 1)}")


@app.command()
def sell(
    symbol: str = typer.Argument(..., help="ETF symbol to sell"),
):
    """Record a sale and remove from portfolio."""
    store = PortfolioStore()
    holding = store.remove_holding(symbol)

    if holding:
        typer.echo(f"\nRemoved {symbol} from portfolio.")
        typer.echo(f"  Was: {holding.shares:.4f} shares @ {holding.entry_price:.2f} EUR")
        typer.echo(f"  Held for: {holding.days_held()} days")
        if holding.is_long_term():
            typer.echo(f"  Tax rate: 1% (held >365 days)")
        else:
            typer.echo(f"  Tax rate: 3% (held <365 days)")
    else:
        typer.echo(f"\nNo holding found for {symbol}")


@app.command()
def portfolio():
    """Show current portfolio holdings."""
    store = PortfolioStore()
    p = store.load()

    if not p.holdings:
        typer.echo("\nNo holdings recorded.")
        typer.echo("Use 'aurel2 buy <symbol> <shares> <price>' to record a purchase.")
        return

    typer.echo("\n" + "=" * 80)
    typer.echo("YOUR PORTFOLIO")
    typer.echo("=" * 80)
    typer.echo(f"{'Symbol':<8} {'Name':<35} {'Shares':>10} {'Entry':>10} {'Days':>6} {'Tax':>5}")
    typer.echo("-" * 80)

    for h in p.holdings:
        tax_rate = "1%" if h.is_long_term() else "3%"
        days_info = str(h.days_held())
        if not h.is_long_term():
            days_info += f" ({h.days_until_long_term()} to 1%)"

        typer.echo(
            f"{h.symbol:<8} {h.name[:35]:<35} {h.shares:>10.4f} "
            f"{h.entry_price:>9.2f}E {h.days_held():>5}d {tax_rate:>5}"
        )

    typer.echo("-" * 80)
    typer.echo(f"Cash available: {p.cash:.2f} EUR")
    typer.echo(f"Total invested: {p.total_invested:.2f} EUR")
    typer.echo(f"Last updated: {p.last_updated}")
    typer.echo("=" * 80)


@app.command()
def cash(
    amount: float = typer.Argument(..., help="Cash amount available in EUR"),
):
    """Set available cash balance."""
    store = PortfolioStore()
    store.set_cash(amount)
    typer.echo(f"\nCash balance set to: {amount:.2f} EUR")


@app.command()
def dashboard(
    host: str = typer.Option("127.0.0.1", "--host", "-h", help="Host to bind to"),
    port: int = typer.Option(8000, "--port", "-p", help="Port to bind to"),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes"),
):
    """Launch the web dashboard."""
    typer.echo(f"\nStarting Aurel2 Dashboard at http://{host}:{port}")
    typer.echo("Press Ctrl+C to stop\n")

    try:
        import uvicorn
        if reload:
            uvicorn.run(
                "aurel2.dashboard.app:app",
                host=host, port=port, log_level="warning",
                reload=True, reload_dirs=["/app/src"],
            )
        else:
            from aurel2.dashboard.app import app as dashboard_app
            uvicorn.run(dashboard_app, host=host, port=port, log_level="warning")
    except ImportError:
        typer.echo("Dashboard requires: pip install uvicorn fastapi jinja2")
        raise typer.Exit(1)


@app.command()
def advise(
    date_str: str = typer.Option(None, "--date", "-d", help="Date to check (YYYY-MM-DD), defaults to today"),
    notify: bool = typer.Option(False, "--notify", "-n", help="Send notification if AI disagrees"),
    ntfy_topic: str = typer.Option("aurel2", "--ntfy-topic", help="Ntfy topic for notifications"),
    failure_file: str = typer.Option("data/failure_learnings.json", "--failures", "-f", help="Path to failure learnings"),
):
    """Get AI-enhanced trading recommendation.
    
    Compares deterministic momentum signal with AI advisor that has learned
    from historical failures. Alerts you when they disagree (rare but valuable).
    """
    import os
    from datetime import date as date_type, timedelta

    from rich.panel import Panel

    from aurel2.agent.advisor import AIAdvisor
    from aurel2.core.assets import ASSET_REGISTRY
    from aurel2.notifications.ntfy import NtfyNotifier

    console = Console()
    check_date = date_type.fromisoformat(date_str) if date_str else date_type.today()

    console.print()
    console.print(Panel.fit(
        f"[bold cyan]AI-Enhanced Trading Advisor[/bold cyan]\n"
        f"Date: {check_date}",
        border_style="cyan"
    ))
    console.print()

    # Load portfolio to get current holding
    store = PortfolioStore()
    portfolio = store.load()
    
    current_holding = None
    current_symbol = None
    for h in portfolio.holdings:
        if h.symbol not in ["CASH", "EUR"]:
            current_symbol = h.symbol
            for ac, asset in ASSET_REGISTRY.items():
                if asset.symbol == h.symbol or (hasattr(asset, 'yahoo_symbol') and asset.yahoo_symbol == h.symbol):
                    current_holding = ac
                    break

    console.print(f"[dim]Current holding: {current_symbol or 'Cash'}[/dim]")

    # Fetch price data
    provider = YahooFinanceProvider()
    symbols = ["SPY", "EFA", "EEM", "XLK", "XLF", "XLE", "XLV", "AGG", "TLT", "GLD", "DBC"]
    extended_start = check_date - timedelta(days=400)

    with console.status("[bold green]Fetching market data..."):
        all_prices = []
        for symbol in symbols:
            try:
                prices = provider.get_prices(symbol, extended_start, check_date)
                all_prices.append(prices)
            except Exception:
                pass
        prices_df = pd.concat(all_prices, ignore_index=True)

    # Get deterministic signal
    strategy = DualMomentumStrategy(assets=ASSET_REGISTRY)
    det_signal = strategy.generate_signal(
        prices=prices_df,
        calc_date=check_date,
        current_holding=current_holding,
    )

    det_action = det_signal.action.value.upper()
    det_asset = det_signal.asset.symbol if det_signal.asset else None

    console.print()
    console.print("[bold]DETERMINISTIC SIGNAL:[/bold]")
    console.print(f"  Action: [yellow]{det_action}[/yellow]")
    if det_asset:
        console.print(f"  Asset:  [yellow]{det_asset}[/yellow]")
    console.print(f"  Reason: {det_signal.reason}")

    # Get AI opinion via advisor.review()
    console.print()
    with console.status("[bold green]Consulting AI advisor..."):
        advisor = AIAdvisor(failure_file=failure_file)
        if advisor.failure_analysis:
            num_past_failures = len([f for f in advisor.failure_analysis.failure_events if f.date < check_date])
            console.print(f"[dim]Loaded {num_past_failures} historical failures for AI context[/dim]")
        else:
            console.print("[yellow]Warning: No failure learnings found. Run failure analysis first.[/yellow]")

        # Build signals dict
        signals = {
            "dual_momentum": {
                "action": det_signal.action.value,
                "asset_symbol": det_signal.asset.symbol if det_signal.asset else None,
                "confidence": 0.7,
                "reasoning": det_signal.reason,
            }
        }

        # Get AI decision
        try:
            ai_advice = advisor.review(
                deterministic_action=det_action.lower(),
                deterministic_asset=det_asset,
                strategy_signals=signals,
                prices=prices_df,
                current_holding=current_symbol,
                target_date=check_date,
            )
            ai_action = ai_advice.recommended_action.upper()
            ai_asset = ai_advice.recommended_asset
            ai_reasoning = ai_advice.reasoning
            ai_confidence = ai_advice.confidence
        except Exception as e:
            console.print(f"[red]AI evaluation failed: {e}[/red]")
            ai_action = det_action
            ai_asset = det_asset
            ai_reasoning = f"AI failed, using deterministic: {e}"
            ai_confidence = 0.5

    # Compare decisions
    agreed = (det_action.lower() == ai_action.lower()) and (det_action.lower() != "buy" or det_asset == ai_asset)

    console.print()
    console.print("[bold]AI ADVISOR OPINION:[/bold]")
    console.print(f"  Action:     [cyan]{ai_action}[/cyan]")
    if ai_asset:
        console.print(f"  Asset:      [cyan]{ai_asset}[/cyan]")
    console.print(f"  Confidence: {ai_confidence:.0%}")
    console.print(f"  Reasoning:  {ai_reasoning[:200]}...")

    # Show comparison
    console.print()
    if agreed:
        console.print(Panel.fit(
            "[bold green]✓ AGREEMENT[/bold green]\n\n"
            "Both deterministic and AI agree.\n"
            "Safe to proceed with the signal.",
            border_style="green"
        ))
    else:
        console.print(Panel.fit(
            "[bold red]⚠ DISAGREEMENT - AI OVERRIDE SUGGESTED[/bold red]\n\n"
            f"Deterministic: {det_action} {det_asset or ''}\n"
            f"AI suggests:   {ai_action} {ai_asset or ''}\n\n"
            "The AI has identified a potential failure pattern.\n"
            "Consider the AI's recommendation carefully.",
            border_style="red"
        ))

        # Send notification if requested
        if notify:
            notifier = NtfyNotifier(topic=ntfy_topic)
            notifier.send(
                message=(
                    f"AI Override Alert!\n\n"
                    f"Deterministic: {det_action} {det_asset or ''}\n"
                    f"AI suggests: {ai_action} {ai_asset or ''}\n\n"
                    f"Confidence: {ai_confidence:.0%}\n"
                    f"Reasoning: {ai_reasoning[:150]}..."
                ),
                title="Aurel2: AI Disagrees with Signal",
                priority="high",
                tags=["warning", "robot"],
            )
            console.print("[dim]Notification sent to ntfy topic.[/dim]")

    console.print()


@app.command()
def learn(
    start: str = typer.Option("2018-01-01", "--start", "-s", help="Start date (YYYY-MM-DD)"),
    end: str = typer.Option(None, "--end", "-e", help="End date (YYYY-MM-DD), defaults to today"),
    output: str = typer.Option("data/failure_learnings.json", "--output", "-o", help="Output file path"),
):
    """Run failure analysis and save learnings.
    
    Analyzes historical backtests to identify when the deterministic
    momentum system made suboptimal decisions. These learnings are
    then used by the AI advisor to avoid repeating past mistakes.
    
    Example:
        aurel2 learn --start 2018-01-01 --output data/failure_learnings.json
    """
    from datetime import date as date_type
    from rich.panel import Panel

    from aurel2.agent.failure_analyzer import run_failure_analysis

    console = Console()

    end_date = end if end else date_type.today().isoformat()

    console.print()
    console.print(Panel.fit(
        "[bold cyan]Failure Learning Analysis[/bold cyan]\n"
        f"Period: {start} to {end_date}",
        border_style="cyan"
    ))
    console.print()

    with console.status("[bold green]Running backtest and analyzing failures..."):
        analysis = run_failure_analysis(start_date=start, end_date=end_date)

    # Display results
    console.print(f"[bold]Analysis Complete[/bold]")
    console.print(f"  Total periods: {analysis.total_periods}")
    console.print(f"  Failures identified: {len(analysis.failure_events)}")
    console.print(f"  Failure rate: {analysis.failure_rate:.1%}")
    console.print(f"  Total opportunity cost: {analysis.total_opportunity_cost:.1%}")

    console.print("\n[bold]Failure Types:[/bold]")
    for failure_type, count in analysis.common_failure_types.items():
        console.print(f"  {failure_type}: {count}")

    # Top 5 worst failures
    console.print("\n[bold]Top 5 Worst Failures:[/bold]")
    for f in analysis.worst_failures[:5]:
        console.print(
            f"  {f.date}: {f.failure_type} - held {f.asset_held}, "
            f"optimal was {f.optimal_asset} ({f.opportunity_cost:.1%} cost)"
        )

    # Save to file
    with console.status(f"[bold green]Saving to {output}..."):
        analysis.save(output)

    console.print(f"\n[green]✓ Saved {len(analysis.failure_events)} failure learnings to {output}[/green]")
    console.print()
    console.print("[dim]These learnings will be used by the AI advisor to avoid repeating past mistakes.[/dim]")
    console.print("[dim]Run `aurel2 agent` to see the AI advisor in action.[/dim]")
    console.print()


@app.command()
def live(
    paper: bool = typer.Option(True, "--paper/--real", help="Use paper trading (default) or real trading"),
    check_time: str = typer.Option("10:30", "--check-time", "-t", help="Daily check time (HH:MM in market timezone)", envvar="CHECK_TIME"),
    market_timezone: str = typer.Option("America/New_York", "--market-timezone", help="IANA timezone for the daily check", envvar="MARKET_TIMEZONE"),
    poll_interval: int = typer.Option(5, "--poll-interval", "-p", help="Approval poll interval in minutes", envvar="POLL_INTERVAL"),
    ntfy_topic: str = typer.Option("aurel2", "--ntfy-topic", help="Ntfy notification topic", envvar="NTFY_TOPIC"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Don't execute trades, just simulate", envvar="DRY_RUN"),
    ai: bool = typer.Option(False, "--ai", help="Enable AI advisor (disabled by default)", envvar="USE_AI"),
    ai_model: str = typer.Option("haiku", "--ai-model", "-m", help="AI model: sonnet, opus, haiku", envvar="AI_MODEL"),
    ai_lookback: int = typer.Option(3, "--ai-lookback", "-l", help="AI failure pattern lookback years (default: 3)", envvar="AI_LOOKBACK_YEARS"),
):
    """Run the live trading daemon.

    The daemon:
    1. Connects to Alpaca Markets
    2. Runs daily check at the specified time (default 10:30 AM New York)
    3. Auto-executes ROUTINE decisions (all strategies agree)
    4. Creates approval requests for NON_ROUTINE/URGENT decisions
    5. Polls for approvals every 5 minutes
    6. Expires unapproved decisions after 10 minutes

    AI Configuration:
    - Default: Sonnet with 3-year lookback (best in backtests: +42.86% alpha)
    - Lookback window filters failure patterns to recent years only

    Environment Variables:
        APCA_API_KEY_ID, APCA_API_SECRET_KEY, CHECK_TIME, MARKET_TIMEZONE, POLL_INTERVAL,
        NTFY_TOPIC, DRY_RUN, AI_MODEL, AI_LOOKBACK_YEARS

    Examples:
        aurel2 live --paper          # Paper trading (default)
        aurel2 live --real           # LIVE trading (caution!)
        aurel2 live --dry-run        # Simulate without executing
        aurel2 live --check-time 09:30  # Check at 9:30 AM
        aurel2 live --ai-lookback 5  # Use 5-year lookback window
    """
    import asyncio
    import os
    from datetime import time as dt_time

    from aurel2.live.daemon import LiveDaemon

    console = Console()

    # Parse check time
    try:
        hour, minute = map(int, check_time.split(":"))
        check_time_obj = dt_time(hour, minute)
    except ValueError:
        console.print(f"[red]Invalid check time format: {check_time}. Use HH:MM.[/red]")
        raise typer.Exit(1)

    # Warn about real trading (skip in non-interactive mode)
    if not paper and os.isatty(0):
        console.print("\n[bold red]WARNING: LIVE TRADING MODE[/bold red]")
        console.print("You are about to run with REAL money!")
        console.print("Make sure you understand the risks.\n")

        confirm = typer.confirm("Are you sure you want to continue?")
        if not confirm:
            console.print("Aborted.")
            raise typer.Exit(0)

    daemon = LiveDaemon(
        paper=paper,
        check_time=check_time_obj,
        timezone=market_timezone,
        poll_interval_minutes=poll_interval,
        ntfy_topic=ntfy_topic,
        dry_run=dry_run,
        use_ai=ai,
        ai_model=ai_model,
        ai_lookback_years=ai_lookback,
    )

    try:
        asyncio.run(daemon.start())
    except KeyboardInterrupt:
        console.print("\nStopped by user.")


@app.command()
def check(
    paper: bool = typer.Option(True, "--paper/--real", help="Use paper trading (default) or real trading"),
    ntfy_topic: str = typer.Option("aurel2", "--ntfy-topic", help="Ntfy notification topic"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Don't execute trades, just simulate"),
):
    """Run a single market check (for testing).

    This runs the same logic as the daemon but only once:
    1. Connects to broker
    2. Syncs positions
    3. Fetches market data
    4. Runs all strategies
    5. Produces a decision
    6. Executes or creates approval request

    Use this to test the system before running the daemon.

    Examples:
        aurel2 check --paper          # Test with paper trading
        aurel2 check --dry-run        # Simulate without executing
        aurel2 check --real           # Single check with LIVE trading
    """
    import asyncio

    from aurel2.live.daemon import run_single_check

    console = Console()

    # Warn about real trading
    if not paper:
        console.print("\n[bold red]WARNING: LIVE TRADING MODE[/bold red]")
        console.print("This check may execute REAL trades!")

        confirm = typer.confirm("Are you sure you want to continue?")
        if not confirm:
            console.print("Aborted.")
            raise typer.Exit(0)

    try:
        asyncio.run(run_single_check(
            paper=paper,
            ntfy_topic=ntfy_topic,
            dry_run=dry_run,
        ))
    except KeyboardInterrupt:
        console.print("\nStopped by user.")


@app.command()
def compare(
    start: str = typer.Option("2020-01-01", help="Start date (YYYY-MM-DD)"),
    end: str = typer.Option(None, help="End date (YYYY-MM-DD), defaults to today"),
    capital: float = typer.Option(10000.0, help="Initial capital"),
    failure_file: str = typer.Option("data/failure_learnings.json", help="Path to failure learnings"),
    lookback_years: int = typer.Option(5, "--lookback", "-l", help="Years of failure history to use (3, 5, 7, or 0 for all)"),
    model: str = typer.Option("haiku", "--model", "-m", help="AI model: sonnet, opus, haiku"),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Show detailed decision history"),
):
    """Compare SPY vs Deterministic vs AI Expert over a time period.

    Runs a 3-way backtest comparison:
    - SPY: Buy and hold S&P 500
    - Deterministic: Dual momentum strategy (rule-based)
    - AI Expert: AI with failure learnings that reviews deterministic decisions
    """
    from datetime import timedelta
    from rich.table import Table
    from rich.console import Console
    import os

    from aurel2.agent.advisor import AIAdvisor
    from aurel2.core.assets import ASSET_REGISTRY
    from aurel2.core.models import AssetClass

    console = Console()

    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end) if end else date.today()

    console.print(f"\n[bold]3-Way Backtest Comparison[/bold]")
    console.print(f"Period: {start_date} to {end_date}")
    console.print(f"Initial Capital: ${capital:,.2f}")
    console.print(f"Failure Lookback: {'All history' if lookback_years == 0 else f'{lookback_years} years (rolling)'}")
    console.print(f"AI Model: {model}")
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

    # 1. SPY Buy-and-Hold
    console.print("\n[bold]Calculating SPY Buy-and-Hold...[/bold]")
    spy_prices = prices_df[prices_df["symbol"] == "SPY"].copy()
    spy_prices["date"] = pd.to_datetime(spy_prices["date"])
    spy_start_price = spy_prices[spy_prices["date"] >= pd.Timestamp(start_date)].iloc[0]["close"]
    spy_end_price = spy_prices[spy_prices["date"] <= pd.Timestamp(end_date)].iloc[-1]["close"]
    spy_return = (spy_end_price / spy_start_price - 1) * 100
    spy_final_value = capital * (1 + spy_return / 100)

    # 2. Deterministic (Dual Momentum)
    console.print("[bold]Running Deterministic backtest...[/bold]")
    strategy = DualMomentumStrategy(assets=ASSET_REGISTRY)

    # Monthly decision dates
    decision_dates = pd.date_range(start=start_date, end=end_date, freq="ME")
    decision_dates = [d.date() for d in decision_dates]

    det_portfolio_value = capital
    det_holding: str | None = None
    det_decisions = []

    for i, decision_date in enumerate(decision_dates[:-1]):
        next_date = decision_dates[i + 1]

        # Get signal
        current_asset_class = None
        if det_holding:
            for ac, asset in ASSET_REGISTRY.items():
                if asset.symbol == det_holding:
                    current_asset_class = ac
                    break

        signal = strategy.generate_signal(
            prices=prices_df,
            calc_date=decision_date,
            current_holding=current_asset_class,
        )

        # Execute
        if signal.action.value == "buy" and signal.asset:
            new_holding = signal.asset.symbol
            if new_holding != det_holding:
                det_holding = new_holding
        elif signal.action.value == "sell":
            det_holding = None

        # Calculate return for period
        if det_holding:
            period_return = _get_period_return(prices_df, det_holding, decision_date, next_date)
        else:
            period_return = 0.0  # Cash

        det_portfolio_value *= (1 + period_return / 100)
        det_decisions.append({
            "date": decision_date,
            "action": signal.action.value,
            "asset": det_holding,
            "period_return": period_return,
            "portfolio_value": det_portfolio_value,
        })

    det_return = (det_portfolio_value / capital - 1) * 100

    # 3. AI Expert with Failure Learnings
    console.print("[bold]Running AI Expert backtest (this may take a while)...[/bold]")

    # Initialize AI advisor (handles failure learnings, context, evaluator)
    advisor = AIAdvisor(
        failure_file=failure_file,
        model=model,
        lookback_years=lookback_years if lookback_years > 0 else None,
    )
    if advisor.failure_analysis:
        console.print(f"Loaded {len(advisor.failure_analysis.failure_events)} failure learnings")
    else:
        console.print("[yellow]No failure learnings found. AI will operate without historical patterns.[/yellow]")

    ai_portfolio_value = capital
    ai_holding: str | None = None
    ai_decisions = []
    agreements = 0
    disagreements = 0

    for i, decision_date in enumerate(decision_dates[:-1]):
        next_date = decision_dates[i + 1]
        console.print(f"  Processing {decision_date} ({i+1}/{len(decision_dates)-1})...", end="\r")

        # Get deterministic signal first
        current_asset_class = None
        if ai_holding:
            for ac, asset in ASSET_REGISTRY.items():
                if asset.symbol == ai_holding:
                    current_asset_class = ac
                    break

        det_signal = strategy.generate_signal(
            prices=prices_df,
            calc_date=decision_date,
            current_holding=current_asset_class,
        )

        det_action = det_signal.action.value
        det_asset = det_signal.asset.symbol if det_signal.asset else None

        # Build strategy signals for AI context
        signals = {
            "dual_momentum": {
                "action": det_action,
                "asset_symbol": det_asset,
                "confidence": 0.7,
                "reasoning": det_signal.reason,
            }
        }

        # Get AI decision via advisor.review()
        try:
            ai_advice = advisor.review(
                deterministic_action=det_action,
                deterministic_asset=det_asset,
                strategy_signals=signals,
                prices=prices_df,
                current_holding=ai_holding,
                target_date=decision_date,
            )
            ai_action = ai_advice.recommended_action
            ai_asset = ai_advice.recommended_asset
            ai_reasoning = ai_advice.reasoning[:100] if ai_advice.reasoning else ""
        except Exception as e:
            # Fallback to deterministic on failure
            ai_action = det_action
            ai_asset = det_asset
            ai_reasoning = f"AI failed: {e}"

        # Check agreement
        agreed = (det_action == ai_action) and (det_action != "buy" or det_asset == ai_asset)
        if agreed:
            agreements += 1
        else:
            disagreements += 1

        # Execute AI decision
        if ai_action == "buy" and ai_asset:
            ai_holding = ai_asset
        elif ai_action == "sell":
            ai_holding = None

        # Calculate return for period
        if ai_holding:
            period_return = _get_period_return(prices_df, ai_holding, decision_date, next_date)
        else:
            period_return = 0.0  # Cash

        ai_portfolio_value *= (1 + period_return / 100)
        ai_decisions.append({
            "date": decision_date,
            "det_action": det_action,
            "det_asset": det_asset,
            "ai_action": ai_action,
            "ai_asset": ai_holding,
            "agreed": agreed,
            "period_return": period_return,
            "portfolio_value": ai_portfolio_value,
            "reasoning": ai_reasoning,
        })

    console.print("  " + " " * 50)  # Clear progress line
    ai_return = (ai_portfolio_value / capital - 1) * 100

    # Results table
    console.print("\n")
    table = Table(title="5-Year Performance Comparison")
    table.add_column("Strategy", style="cyan")
    table.add_column("Final Value", justify="right")
    table.add_column("Total Return", justify="right")
    table.add_column("Alpha vs SPY", justify="right")

    table.add_row(
        "SPY (Buy & Hold)",
        f"${spy_final_value:,.2f}",
        f"{spy_return:+.2f}%",
        "-",
    )
    table.add_row(
        "Deterministic (Dual Momentum)",
        f"${det_portfolio_value:,.2f}",
        f"{det_return:+.2f}%",
        f"{det_return - spy_return:+.2f}%",
    )
    table.add_row(
        "AI Expert (with learnings)",
        f"${ai_portfolio_value:,.2f}",
        f"{ai_return:+.2f}%",
        f"{ai_return - spy_return:+.2f}%",
    )

    console.print(table)

    # AI vs Deterministic comparison
    console.print(f"\n[bold]AI vs Deterministic:[/bold]")
    console.print(f"  Agreements: {agreements} ({agreements/(agreements+disagreements)*100:.1f}%)")
    console.print(f"  Disagreements: {disagreements}")
    console.print(f"  AI Alpha vs Deterministic: {ai_return - det_return:+.2f}%")

    # Show disagreements if verbose
    if verbose:
        console.print(f"\n[bold]Decision History (Disagreements):[/bold]")
        for d in ai_decisions:
            if not d["agreed"]:
                console.print(f"\n{d['date']}:")
                console.print(f"  Det: {d['det_action'].upper()} {d['det_asset'] or 'CASH'}")
                console.print(f"  AI:  {d['ai_action'].upper()} {d['ai_asset'] or 'CASH'}")
                console.print(f"  Return: {d['period_return']:+.2f}%")
                if d["reasoning"]:
                    console.print(f"  Reasoning: {d['reasoning']}")

    # Calculate CAGR
    years = (end_date - start_date).days / 365.25
    spy_cagr = ((spy_final_value / capital) ** (1 / years) - 1) * 100
    det_cagr = ((det_portfolio_value / capital) ** (1 / years) - 1) * 100
    ai_cagr = ((ai_portfolio_value / capital) ** (1 / years) - 1) * 100

    # Save results
    _save_backtest_results({
        "type": "compare",
        "start_date": str(start_date),
        "end_date": str(end_date),
        "initial_capital": capital,
        "lookback_years": lookback_years if lookback_years > 0 else "all",
        "model": model,
        "years": round(years, 2),
        "spy": {
            "final_value": round(spy_final_value, 2),
            "total_return_pct": round(spy_return, 2),
            "cagr_pct": round(spy_cagr, 2),
        },
        "deterministic": {
            "final_value": round(det_portfolio_value, 2),
            "total_return_pct": round(det_return, 2),
            "cagr_pct": round(det_cagr, 2),
            "alpha_vs_spy": round(det_return - spy_return, 2),
        },
        "ai_expert": {
            "final_value": round(ai_portfolio_value, 2),
            "total_return_pct": round(ai_return, 2),
            "cagr_pct": round(ai_cagr, 2),
            "alpha_vs_spy": round(ai_return - spy_return, 2),
            "alpha_vs_det": round(ai_return - det_return, 2),
            "agreements": agreements,
            "disagreements": disagreements,
        },
    })


def _save_backtest_results(
    results: dict,
    filename: str = "data/backtest_results.json",
) -> None:
    """Save backtest results to JSON file."""
    import json
    from datetime import datetime
    from pathlib import Path

    filepath = Path(filename)
    filepath.parent.mkdir(parents=True, exist_ok=True)

    # Load existing results
    existing = []
    if filepath.exists():
        try:
            with open(filepath) as f:
                data = json.load(f)
                existing = data.get("results", [])
        except Exception:
            existing = []

    # Add new result with timestamp
    results["saved_at"] = datetime.now().isoformat()
    existing.append(results)

    # Keep last 50 results
    existing = existing[-50:]

    with open(filepath, "w") as f:
        json.dump({"results": existing}, f, indent=2, default=str)

    print(f"\n✓ Results saved to {filename}")


def _get_period_return(prices: pd.DataFrame, symbol: str, start_date: date, end_date: date) -> float:
    """Calculate return for a symbol over a period."""
    if not symbol:
        return 0.0

    sym_prices = prices[prices["symbol"] == symbol].copy()
    if sym_prices.empty:
        return 0.0

    sym_prices["date"] = pd.to_datetime(sym_prices["date"])

    start_prices = sym_prices[sym_prices["date"] <= pd.Timestamp(start_date)]
    end_prices = sym_prices[sym_prices["date"] <= pd.Timestamp(end_date)]

    if start_prices.empty or end_prices.empty:
        return 0.0

    start_price = start_prices.iloc[-1]["close"]
    end_price = end_prices.iloc[-1]["close"]

    return (end_price / start_price - 1) * 100


@app.command()
def monitor(
    paper: bool = typer.Option(True, "--paper/--real", help="Daemon uses paper trading (default) or real trading"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Daemon uses dry run mode"),
    ntfy_topic: str = typer.Option("aurel2", "--ntfy-topic", help="Ntfy notification topic"),
    ai: bool = typer.Option(False, "--ai", help="Enable AI-powered analysis using Claude Code CLI"),
    ai_model: str = typer.Option("haiku", "--ai-model", help="Claude model for AI analysis (sonnet, opus, haiku)"),
):
    """Run the daemon monitor agent.

    The monitor:
    1. Watches daemon health every 60 seconds
    2. Detects errors (connection failures, crashes, circuit breaker)
    3. Auto-fixes fixable issues (restarts daemon)
    4. Sends ntfy notifications for warnings and errors
    5. Tracks session progress for evaluation

    With --ai flag:
    - Uses Claude Code CLI for intelligent root cause analysis
    - Learns from historical incident patterns
    - Makes autonomous decisions on fix actions
    - Falls back to deterministic rules if AI fails

    Examples:
        aurel2 monitor                # Monitor paper trading daemon
        aurel2 monitor --real         # Monitor live trading daemon
        aurel2 monitor --dry-run      # Monitor dry-run daemon
        aurel2 monitor --ai           # Monitor with AI-powered analysis
        aurel2 monitor --ai --ai-model opus  # Use opus model for AI
    """
    import asyncio

    from aurel2.monitor import DaemonMonitor

    console = Console()

    daemon_monitor = DaemonMonitor(
        paper=paper,
        dry_run=dry_run,
        ntfy_topic=ntfy_topic,
        ai_enabled=ai,
        ai_model=ai_model,
    )

    try:
        asyncio.run(daemon_monitor.start())
    except KeyboardInterrupt:
        console.print("\nStopped by user.")


@app.command()
def progress():
    """Show paper trading progress summary.

    Displays:
    - Overall return and max drawdown
    - Total trades and win rate
    - AI agreement rate
    - Recent session history

    Data is kept for 90 days (3 months).
    """
    from aurel2.monitor import SessionTracker

    console = Console()

    tracker = SessionTracker()
    summary = tracker.get_summary()

    console.print("\n" + "=" * 60)
    console.print("[bold]Paper Trading Progress[/bold]")
    console.print("=" * 60)

    if summary["start_date"] is None:
        console.print("\n[yellow]No session data yet. Start the daemon and monitor to track progress.[/yellow]\n")
        return

    # Overall metrics
    console.print(f"\n[bold]Period:[/bold] {summary['start_date']} to today ({summary['days_active']} days active)")

    if summary["initial_value"] and summary["current_value"]:
        console.print(f"\n[bold]Account:[/bold]")
        console.print(f"  Initial: ${summary['initial_value']:,.2f}")
        console.print(f"  Current: ${summary['current_value']:,.2f}")

        if summary["total_return_pct"] is not None:
            color = "green" if summary["total_return_pct"] >= 0 else "red"
            console.print(f"  Return: [{color}]{summary['total_return_pct']:+.2f}%[/{color}]")

        if summary["max_drawdown_pct"]:
            dd_color = "red" if summary["max_drawdown_pct"] > 10 else "yellow" if summary["max_drawdown_pct"] > 5 else "green"
            console.print(f"  Max Drawdown: [{dd_color}]{-summary['max_drawdown_pct']:.2f}%[/{dd_color}]")

    # Trading stats
    console.print(f"\n[bold]Trading:[/bold]")
    console.print(f"  Total Trades: {summary['total_trades']}")
    if summary["win_rate_pct"] is not None:
        wr_color = "green" if summary["win_rate_pct"] >= 50 else "red"
        console.print(f"  Win Rate: [{wr_color}]{summary['win_rate_pct']:.1f}%[/{wr_color}]")

    if summary["ai_agreement_pct"] is not None:
        console.print(f"  AI Agreement: {summary['ai_agreement_pct']:.1f}%")

    # System health
    console.print(f"\n[bold]System:[/bold]")
    console.print(f"  Total Restarts: {summary['total_restarts']}")
    console.print(f"  Total Errors: {summary['total_errors']}")

    if summary["avg_uptime_hours"]:
        console.print(f"  Avg Daily Uptime: {summary['avg_uptime_hours']:.1f} hours")

    # Recent sessions
    recent = tracker.get_recent_sessions(7)
    if recent:
        console.print(f"\n[bold]Recent Sessions (Last 7 Days):[/bold]")

        table = Table(show_header=True, box=None, padding=(0, 2))
        table.add_column("Date", style="dim")
        table.add_column("Uptime", justify="right")
        table.add_column("Decisions", justify="right")
        table.add_column("Trades", justify="right")
        table.add_column("Return", justify="right")
        table.add_column("Errors", justify="right")

        for session in recent:
            uptime_str = f"{session.uptime_minutes // 60}h {session.uptime_minutes % 60}m"
            return_str = f"{session.daily_return * 100:+.2f}%" if session.daily_return is not None else "-"
            return_color = "green" if session.daily_return and session.daily_return >= 0 else "red" if session.daily_return else "dim"
            error_str = str(session.errors) if session.errors > 0 else "-"
            error_color = "red" if session.errors > 0 else "dim"

            table.add_row(
                session.date,
                uptime_str,
                str(session.decisions) if session.decisions > 0 else "-",
                str(session.executions) if session.executions > 0 else "-",
                f"[{return_color}]{return_str}[/{return_color}]",
                f"[{error_color}]{error_str}[/{error_color}]",
            )

        console.print(table)

    console.print("\n" + "=" * 60)
    console.print()


@app.command("ab-report")
def ab_report(
    days: int = typer.Option(7, "--days", "-d", help="Lookback window in days"),
):
    """Show weekly-style A/B comparison: paper baseline vs live experiment."""
    from aurel2.live.journal import TradeJournal, journal_path_for_mode
    from aurel2.monitor.session_tracker import SessionTracker, session_path_for_mode

    console = Console()

    if days < 1:
        console.print("[red]--days must be >= 1[/red]")
        raise typer.Exit(1)

    cutoff_date = date.today() - timedelta(days=days)
    cutoff_dt = datetime.combine(cutoff_date, datetime.min.time())

    def _mode_metrics(mode: str) -> dict:
        tracker = SessionTracker(session_file=session_path_for_mode(mode))
        progress = tracker.get_progress()

        # Account curve from daily sessions
        recent_sessions = [s for s in progress.sessions if s.date >= cutoff_date.isoformat()]
        curve = [(s.date, s.account_value) for s in recent_sessions if s.account_value is not None]

        period_return_pct = None
        max_dd_pct = None
        if len(curve) >= 2:
            start_val = curve[0][1]
            end_val = curve[-1][1]
            if start_val and start_val > 0:
                period_return_pct = (end_val / start_val - 1) * 100

            peak = curve[0][1]
            max_dd = 0.0
            for _, val in curve:
                if val > peak:
                    peak = val
                if peak > 0:
                    dd = (peak - val) / peak
                    if dd > max_dd:
                        max_dd = dd
            max_dd_pct = max_dd * 100

        journal = TradeJournal(filepath=journal_path_for_mode(mode))
        recent_decisions = []
        for e in journal.entries:
            if e.entry_type != "decision":
                continue
            try:
                ts = datetime.fromisoformat(e.timestamp)
            except Exception:
                continue
            if ts >= cutoff_dt:
                recent_decisions.append(e)

        trade_decisions = [e for e in recent_decisions if e.action in ("buy", "sell")]
        executed_trades = [e for e in trade_decisions if e.executed]
        turnover = len(executed_trades)

        switches = 0
        for e in executed_trades:
            if e.action == "buy" and e.current_holding_before and e.symbol and e.current_holding_before != e.symbol:
                switches += 1

        agreement_rate = None
        override_rate = None
        if recent_decisions:
            agrees = sum(1 for e in recent_decisions if e.ai_agrees)
            agreement_rate = agrees / len(recent_decisions) * 100
            override_rate = 100 - agreement_rate

        execution_rate = None
        if trade_decisions:
            execution_rate = len(executed_trades) / len(trade_decisions) * 100

        return {
            "mode": mode,
            "return_pct": period_return_pct,
            "max_dd_pct": max_dd_pct,
            "turnover": turnover,
            "switches": switches,
            "decisions": len(recent_decisions),
            "agreement_rate": agreement_rate,
            "override_rate": override_rate,
            "execution_rate": execution_rate,
        }

    paper = _mode_metrics("paper")
    live = _mode_metrics("live")

    console.print(f"\n[bold]A/B Report (last {days} days)[/bold]")
    table = Table(show_header=True, box=None, padding=(0, 2))
    table.add_column("Metric", style="bold")
    table.add_column("Paper (Baseline)", justify="right")
    table.add_column("Live (Experiment)", justify="right")

    def _fmt_pct(v):
        return "-" if v is None else f"{v:+.2f}%"

    def _fmt_num(v):
        return "-" if v is None else str(v)

    table.add_row("Return", _fmt_pct(paper["return_pct"]), _fmt_pct(live["return_pct"]))
    table.add_row("Max Drawdown", _fmt_pct(-paper["max_dd_pct"] if paper["max_dd_pct"] is not None else None), _fmt_pct(-live["max_dd_pct"] if live["max_dd_pct"] is not None else None))
    table.add_row("Turnover (executed trades)", _fmt_num(paper["turnover"]), _fmt_num(live["turnover"]))
    table.add_row("Switches", _fmt_num(paper["switches"]), _fmt_num(live["switches"]))
    table.add_row("Decision count", _fmt_num(paper["decisions"]), _fmt_num(live["decisions"]))
    table.add_row("AI agreement", _fmt_pct(paper["agreement_rate"]), _fmt_pct(live["agreement_rate"]))
    table.add_row("AI override", _fmt_pct(paper["override_rate"]), _fmt_pct(live["override_rate"]))
    table.add_row("Trade execution success", _fmt_pct(paper["execution_rate"]), _fmt_pct(live["execution_rate"]))

    console.print(table)
    console.print()


@app.command("model-eval")
def model_eval(
    runs: int = typer.Option(1, "--runs", "-r", help="Number of runs per model (for consistency measurement)"),
    models_str: str = typer.Option("haiku,sonnet,opus", "--models", "-m", help="Comma-separated model list"),
    min_cost: float = typer.Option(0.10, "--min-cost", help="Minimum failure cost to include (decimal, e.g. 0.10 = 10%)"),
    start: str = typer.Option(None, "--start", "-s", help="Only include failures after this date"),
    end: str = typer.Option(None, "--end", "-e", help="Only include failures before this date"),
    save: bool = typer.Option(True, "--save/--no-save", help="Save results to data/model_eval_results.json"),
):
    """Compare Haiku, Sonnet, and Opus on known failure scenarios.

    Extracts scenarios where the deterministic system made mistakes (from
    failure_learnings.json), replays each through all 3 models, and compares
    which model catches more failures with better override decisions.
    """
    from aurel2.engine.model_eval import run_model_comparison, print_comparison_results, save_results

    models = [m.strip() for m in models_str.split(",")]

    typer.echo(f"Model evaluation on failure scenarios")
    typer.echo(f"Models: {', '.join(models)}")
    typer.echo(f"Min failure cost: {min_cost:.0%}")
    typer.echo(f"Runs per model: {runs}")

    result = run_model_comparison(
        models=models,
        runs_per_model=runs,
        min_cost=min_cost,
        start_date=start,
        end_date=end,
    )

    print_comparison_results(result)

    if save:
        save_results(result)


@app.command()
def reset_data(
    mode: str = typer.Argument(..., help="Trading mode to reset: paper or live"),
):
    """Archive and reset data for a trading mode.

    Moves trade_journal, pending_decisions, and session_progress
    to data/archive/{timestamp}/ and starts fresh.

    Examples:
        aurel2 reset-data paper    # Reset paper trading data
        aurel2 reset-data live     # Reset live trading data (with confirmation)
    """
    import shutil
    from datetime import datetime as dt

    console = Console()

    if mode not in ("paper", "live"):
        console.print(f"[red]Invalid mode: {mode}. Use 'paper' or 'live'.[/red]")
        raise typer.Exit(1)

    mode_dir = Path(f"data/{mode}")

    if mode == "live":
        console.print("[bold red]WARNING: This will archive LIVE trading data![/bold red]")
        confirm = typer.confirm("Are you sure?")
        if not confirm:
            console.print("Aborted.")
            raise typer.Exit(0)

    # Archive existing data
    timestamp = dt.now().strftime("%Y%m%d-%H%M%S")
    archive_dir = Path(f"data/archive/{mode}-{timestamp}")

    files_to_archive = [
        "trade_journal.json",
        "pending_decisions.json",
        "session_progress.json",
    ]

    archived = []
    for filename in files_to_archive:
        src = mode_dir / filename
        if src.exists():
            archive_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(archive_dir / filename))
            archived.append(filename)

    # Also check legacy flat paths
    for filename in files_to_archive:
        src = Path(f"data/{filename}")
        if src.exists():
            archive_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(archive_dir / f"legacy-{filename}"))
            archived.append(f"legacy {filename}")

    if archived:
        console.print(f"[green]Archived {len(archived)} files to {archive_dir}/[/green]")
        for f in archived:
            console.print(f"  - {f}")
    else:
        console.print("[yellow]No data files found to archive.[/yellow]")

    # Ensure clean mode directory exists
    mode_dir.mkdir(parents=True, exist_ok=True)
    console.print(f"\n[green]Data reset complete for '{mode}' mode.[/green]")


@app.command()
def version():
    """Show version information."""
    from aurel2 import __version__
    typer.echo(f"Aurel2 v{__version__}")


if __name__ == "__main__":
    app()
