"""Command-line interface for Aurel2."""

from datetime import date
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
    frequency: str = typer.Option("quarterly", help="Rebalance frequency: monthly or quarterly"),
    config: Path = typer.Option(None, help="Config file path"),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Verbose output"),
):
    """Run a backtest of the dual momentum strategy."""
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Parse dates
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end) if end else date.today()

    typer.echo(f"Running backtest from {start_date} to {end_date}")
    typer.echo(f"Initial capital: ${capital:,.2f}")
    typer.echo(f"Rebalance frequency: {frequency}")

    # Load settings
    settings = load_settings(config)

    # Build assets dictionary
    assets = {}

    if settings.assets:
        assets[AssetClass.US_STOCKS] = Asset(
            symbol=settings.assets.us_stocks.symbol,
            name=settings.assets.us_stocks.name,
            asset_class=AssetClass.US_STOCKS,
            isin=settings.assets.us_stocks.isin,
            yahoo_symbol=settings.assets.us_stocks.yahoo_symbol,
        )
        assets[AssetClass.GLOBAL_STOCKS] = Asset(
            symbol=settings.assets.global_stocks.symbol,
            name=settings.assets.global_stocks.name,
            asset_class=AssetClass.GLOBAL_STOCKS,
            isin=settings.assets.global_stocks.isin,
            yahoo_symbol=settings.assets.global_stocks.yahoo_symbol,
        )
        assets[AssetClass.BONDS] = Asset(
            symbol=settings.assets.bonds.symbol,
            name=settings.assets.bonds.name,
            asset_class=AssetClass.BONDS,
            isin=settings.assets.bonds.isin,
            yahoo_symbol=settings.assets.bonds.yahoo_symbol,
        )
        cash_rate = settings.assets.cash_rate
    else:
        # Default US-focused assets for longer backtest history
        assets[AssetClass.US_STOCKS] = Asset(
            symbol="SPY",
            name="SPDR S&P 500 ETF",
            asset_class=AssetClass.US_STOCKS,
            yahoo_symbol="SPY",
        )
        assets[AssetClass.GLOBAL_STOCKS] = Asset(
            symbol="EFA",
            name="iShares MSCI EAFE ETF",
            asset_class=AssetClass.GLOBAL_STOCKS,
            yahoo_symbol="EFA",
        )
        assets[AssetClass.BONDS] = Asset(
            symbol="AGG",
            name="iShares Core US Aggregate Bond ETF",
            asset_class=AssetClass.BONDS,
            yahoo_symbol="AGG",
        )
        cash_rate = 0.04

    assets[AssetClass.CASH] = Asset(
        symbol="CASH",
        name="Cash",
        asset_class=AssetClass.CASH,
    )

    # Fetch price data
    typer.echo("\nFetching historical data...")
    provider = YahooFinanceProvider()
    symbols = [a.yahoo_symbol for a in assets.values() if a.yahoo_symbol]

    prices = provider.get_multi_prices(symbols, start_date, end_date)
    typer.echo(f"Fetched {len(prices)} price records")

    # Create strategy
    strategy = DualMomentumStrategy(
        assets=assets,
        lookback_months=settings.strategy.lookback_months,
        switch_threshold=settings.strategy.switch_threshold,
        cash_rate=cash_rate,
    )

    # Run backtest
    typer.echo("\nRunning backtest...")
    engine = BacktestEngine(
        strategy=strategy,
        initial_capital=capital,
        transaction_cost_pct=settings.risk.transaction_cost_pct,
    )

    # Use SPY as benchmark
    benchmark = "SPY"
    if benchmark not in [a.yahoo_symbol for a in assets.values()]:
        # Fetch benchmark data
        bench_prices = provider.get_prices(benchmark, start_date, end_date)
        prices = pd.concat([prices, bench_prices], ignore_index=True)

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

    # UCITS equivalents for actual trading
    ucits_equivalents = {
        AssetClass.US_STOCKS: {
            "symbol": "CSPX",
            "name": "iShares Core S&P 500 UCITS ETF (Acc)",
            "isin": "IE00B5BMR087",
            "exchange": "Xetra (Germany)",
            "asset_class": AssetClass.US_STOCKS,
        },
        AssetClass.GLOBAL_STOCKS: {
            "symbol": "VWRA",
            "name": "Vanguard FTSE All-World UCITS ETF (Acc)",
            "isin": "IE00BK5BQT80",
            "exchange": "Xetra (Germany)",
            "asset_class": AssetClass.GLOBAL_STOCKS,
        },
        AssetClass.BONDS: {
            "symbol": "AGGH",
            "name": "iShares Core Global Aggregate Bond UCITS ETF (Acc)",
            "isin": "IE00BDBRDM35",
            "exchange": "Xetra (Germany)",
            "asset_class": AssetClass.BONDS,
        },
        AssetClass.CASH: {
            "symbol": "CASH",
            "name": "Hold in broker cash account",
            "isin": "N/A",
            "exchange": "N/A",
            "asset_class": AssetClass.CASH,
        },
    }

    # Map symbols to asset classes
    symbol_to_class = {
        "CSPX": AssetClass.US_STOCKS,
        "SPY": AssetClass.US_STOCKS,
        "VWRA": AssetClass.GLOBAL_STOCKS,
        "EFA": AssetClass.GLOBAL_STOCKS,
        "AGGH": AssetClass.BONDS,
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
    winner_ucits = ucits_equivalents[winner_class]

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
            typer.echo(f"\nACTION: BUY {winner_ucits['symbol']}")
            typer.echo(f"\n  Symbol: {winner_ucits['symbol']}")
            typer.echo(f"  Name:   {winner_ucits['name']}")
            typer.echo(f"  ISIN:   {winner_ucits['isin']}")
            typer.echo(f"  Exchange: {winner_ucits['exchange']}")
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
                typer.echo(f"\nACTION: SELL {current_holding.symbol} -> BUY {winner_ucits['symbol']}")
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
                typer.echo(f"  2. Buy {winner_ucits['symbol']} ({winner_ucits['name']})")
                typer.echo(f"     ISIN: {winner_ucits['isin']}")
                typer.echo(f"     Exchange: {winner_ucits['exchange']}")
                typer.echo(f"  3. Record the sale: aurel2 sell {current_holding.symbol}")
                typer.echo(f"  4. Record the buy:  aurel2 buy {winner_ucits['symbol']} <shares> <price>")
            else:
                # Difference not big enough
                typer.echo(f"\nACTION: HOLD {current_holding.symbol}")
                typer.echo(f"\n  {winner_ucits['symbol']} is winning, but difference is only {momentum_diff:.2%}")
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
    broker: str = typer.Option("tradeville", "--broker", "-b", help="Broker: tradeville, ibkr_eu, ibkr_us"),
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
):
    """Launch the web dashboard."""
    typer.echo(f"\nStarting Aurel2 Dashboard at http://{host}:{port}")
    typer.echo("Press Ctrl+C to stop\n")

    try:
        import uvicorn
        from aurel2.dashboard.app import app as dashboard_app
        uvicorn.run(dashboard_app, host=host, port=port, log_level="warning")
    except ImportError:
        typer.echo("Dashboard requires: pip install uvicorn fastapi jinja2")
        raise typer.Exit(1)


@app.command()
def agent(
    once: bool = typer.Option(False, "--once", help="Run once and exit"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Don't execute trades"),
):
    """Run the AI agent."""
    from aurel2.agent.orchestrator import AgentOrchestrator, DecisionType
    from aurel2.mcp.server import Aurel2MCPServer

    console = Console()

    console.print("\n[bold]Running Aurel2 AI Agent...[/bold]\n")

    # Initialize server and orchestrator
    server = Aurel2MCPServer()
    orchestrator = AgentOrchestrator()

    # Get strategy signals
    console.print("Fetching strategy signals...")
    signals_response = server._get_strategy_signals()
    signals = signals_response.get("signals", {})

    # Get market context
    console.print("Fetching market context...")
    market_context = server._get_market_context()

    # Transform signals into format expected by orchestrator
    orchestrator_signals = {}
    for strategy_name, signal_data in signals.items():
        if "error" not in signal_data:
            orchestrator_signals[strategy_name] = {
                "action": signal_data.get("action", "hold"),
                "confidence": signal_data.get("confidence", 0.5),
                "asset_symbol": signal_data.get("asset") or signal_data.get("asset_class"),
            }

    # Analyze and get decision
    decision = orchestrator.analyze(
        signals=orchestrator_signals,
        market_context={
            "drawdown": market_context.get("drawdown", 0.0),
            "volatility": "normal",
        },
    )

    # Display decision details
    console.print("\n" + "=" * 60)
    console.print("[bold]AGENT DECISION[/bold]")
    console.print("=" * 60)

    # Decision type with color
    type_color = {
        DecisionType.ROUTINE: "green",
        DecisionType.NON_ROUTINE: "yellow",
        DecisionType.URGENT: "red",
    }
    color = type_color.get(decision.decision_type, "white")
    console.print(f"Decision Type: [{color}]{decision.decision_type.value.upper()}[/{color}]")

    # Action with color
    action_color = {
        "buy": "green",
        "sell": "red",
        "hold": "yellow",
    }
    action_val = decision.action.value
    acolor = action_color.get(action_val, "white")
    console.print(f"Action: [{acolor}]{action_val.upper()}[/{acolor}]")

    if decision.asset_symbol:
        console.print(f"Asset: {decision.asset_symbol}")

    console.print(f"Confidence: {decision.confidence:.1%}")
    console.print(f"Urgency: {decision.urgency.value.upper()}")
    console.print(f"Requires Approval: {'Yes' if decision.requires_approval else 'No'}")
    console.print(f"Timeout: {decision.timeout_hours} hours")

    console.print("\n[bold]Reasoning:[/bold]")
    console.print(f"  {decision.reasoning}")

    # Show strategy signals
    console.print("\n[bold]Strategy Signals:[/bold]")
    for strategy_name, signal_data in signals.items():
        if "error" in signal_data:
            console.print(f"  {strategy_name}: [red]Error - {signal_data['error']}[/red]")
        else:
            action = signal_data.get("action", "unknown")
            acolor = action_color.get(action, "white")
            conf = signal_data.get("confidence")
            conf_str = f" ({conf:.0%})" if conf else ""
            console.print(f"  {strategy_name}: [{acolor}]{action.upper()}[/{acolor}]{conf_str}")

    # Market context
    console.print("\n[bold]Market Context:[/bold]")
    console.print(f"  Regime: {market_context.get('regime', 'unknown').upper()}")
    console.print(f"  SPY: ${market_context.get('spy_price', 'N/A')} (200-day MA: ${market_context.get('ma_200', 'N/A')})")
    dd = market_context.get("drawdown")
    console.print(f"  Drawdown: {dd:.2%}" if dd is not None else "  Drawdown: N/A")
    console.print(f"  RSI: {market_context.get('rsi', 'N/A')} ({market_context.get('rsi_interpretation', 'N/A')})")

    console.print("=" * 60)

    # Execute if appropriate
    if not dry_run and not decision.requires_approval:
        console.print("\n[green]Auto-executing routine decision...[/green]")
        result = orchestrator.execute(decision)
        console.print(f"Execution status: {result['status']}")
    elif dry_run:
        console.print("\n[yellow]Dry run - no trades executed.[/yellow]")
    else:
        console.print(f"\n[yellow]Approval required. Decision will timeout in {decision.timeout_hours} hours.[/yellow]")

    console.print()


@app.command()
def strategies():
    """Show current signals from all strategies."""
    from aurel2.mcp.server import Aurel2MCPServer

    console = Console()

    console.print("\n[bold]Fetching strategy signals...[/bold]\n")

    server = Aurel2MCPServer()
    signals_response = server._get_strategy_signals()
    signals = signals_response.get("signals", {})
    signal_date = signals_response.get("date", "unknown")

    console.print(f"Date: {signal_date}\n")

    # Create a table for strategy signals
    table = Table(title="Strategy Signals")
    table.add_column("Strategy", style="bold")
    table.add_column("Action", justify="center")
    table.add_column("Confidence", justify="center")
    table.add_column("Details")

    action_styles = {
        "buy": "bold green",
        "sell": "bold red",
        "hold": "bold yellow",
    }

    for strategy_name, signal_data in signals.items():
        # Format strategy name
        display_name = strategy_name.replace("_", " ").title()

        if "error" in signal_data:
            table.add_row(
                display_name,
                "[red]ERROR[/red]",
                "-",
                signal_data["error"][:50],
            )
        else:
            action = signal_data.get("action", "unknown")
            style = action_styles.get(action, "white")
            action_display = f"[{style}]{action.upper()}[/{style}]"

            confidence = signal_data.get("confidence")
            conf_display = f"{confidence:.0%}" if confidence else "-"

            # Build details string
            details_parts = []
            if signal_data.get("asset"):
                details_parts.append(f"Asset: {signal_data['asset']}")
            if signal_data.get("asset_class"):
                details_parts.append(f"Class: {signal_data['asset_class']}")
            if signal_data.get("reason"):
                details_parts.append(signal_data["reason"][:40])
            if signal_data.get("reasoning"):
                details_parts.append(signal_data["reasoning"][:40])

            details = "; ".join(details_parts) if details_parts else "-"

            table.add_row(display_name, action_display, conf_display, details)

    console.print(table)

    # Show metadata for each strategy
    console.print("\n[bold]Strategy Details:[/bold]")
    for strategy_name, signal_data in signals.items():
        if "error" not in signal_data:
            display_name = strategy_name.replace("_", " ").title()
            console.print(f"\n[bold]{display_name}:[/bold]")

            if signal_data.get("reason"):
                console.print(f"  Reason: {signal_data['reason']}")
            if signal_data.get("reasoning"):
                console.print(f"  Reasoning: {signal_data['reasoning']}")
            if signal_data.get("metadata"):
                console.print("  Metadata:")
                for key, value in signal_data["metadata"].items():
                    if isinstance(value, float):
                        console.print(f"    {key}: {value:.4f}")
                    else:
                        console.print(f"    {key}: {value}")

    console.print()


@app.command("backtest-agent")
def backtest_agent(
    start: str = typer.Option("2015-01-01", help="Start date (YYYY-MM-DD)"),
    end: str = typer.Option(None, help="End date (YYYY-MM-DD), defaults to today"),
    capital: float = typer.Option(10000.0, help="Initial capital"),
    frequency: str = typer.Option("weekly", help="Check frequency: daily, weekly, monthly"),
    analyze: bool = typer.Option(True, "--analyze/--no-analyze", help="Run decision analysis"),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Verbose output"),
):
    """Backtest the AI agent's multi-strategy decision making."""
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, TextColumn

    from aurel2.core.assets import ASSET_REGISTRY, get_all_yahoo_symbols
    from aurel2.engine import AgentBacktestEngine, DecisionFlowAnalyzer

    console = Console()

    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Parse dates
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end) if end else date.today()

    # Header
    console.print()
    console.print(Panel.fit(
        "[bold cyan]Aurel2 Agent Backtest[/bold cyan]",
        border_style="cyan",
    ))
    console.print()

    # Configuration info
    config_table = Table(show_header=False, box=None, padding=(0, 2))
    config_table.add_column("Key", style="dim")
    config_table.add_column("Value")
    config_table.add_row("Period:", f"{start_date} to {end_date}")
    config_table.add_row("Initial Capital:", f"${capital:,.2f}")
    config_table.add_row("Check Frequency:", frequency)
    console.print(config_table)
    console.print()

    # Fetch price data for all assets
    provider = YahooFinanceProvider()
    symbols = get_all_yahoo_symbols()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Fetching price data...", total=None)

        try:
            prices = provider.get_multi_prices(symbols, start_date, end_date)
        except Exception as e:
            console.print(f"[red]Error fetching price data: {e}[/red]")
            raise typer.Exit(1)

        progress.update(task, completed=True)

    if prices.empty:
        console.print("[red]No price data available for the specified period.[/red]")
        raise typer.Exit(1)

    console.print(f"[green]Fetched price data for {len(symbols)} symbols[/green]")
    console.print()

    # Run the agent backtest
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Running agent backtest...", total=None)

        engine = AgentBacktestEngine(
            initial_capital=capital,
            transaction_cost_pct=0.001,
        )

        try:
            result = engine.run(
                prices=prices,
                start_date=start_date,
                end_date=end_date,
                check_frequency=frequency,
            )
        except Exception as e:
            console.print(f"[red]Error running backtest: {e}[/red]")
            if verbose:
                import traceback
                console.print(traceback.format_exc())
            raise typer.Exit(1)

        progress.update(task, completed=True)

    console.print()

    # Performance results
    console.print("[bold]PERFORMANCE[/bold]")
    console.print("-" * 40)

    perf_table = Table(show_header=False, box=None, padding=(0, 2))
    perf_table.add_column("Metric", style="bold")
    perf_table.add_column("Value", justify="right")

    perf_table.add_row("Final Value:", f"${result.final_value:,.2f}")

    # Color-code returns
    return_color = "green" if result.total_return >= 0 else "red"
    perf_table.add_row("Total Return:", f"[{return_color}]{result.total_return:+.1%}[/{return_color}]")

    cagr_color = "green" if result.cagr >= 0 else "red"
    perf_table.add_row("CAGR:", f"[{cagr_color}]{result.cagr:+.1%}[/{cagr_color}]")

    dd_color = "red" if result.max_drawdown > 0.20 else "yellow" if result.max_drawdown > 0.10 else "green"
    perf_table.add_row("Max Drawdown:", f"[{dd_color}]{-result.max_drawdown:.1%}[/{dd_color}]")

    sharpe_color = "green" if result.sharpe_ratio > 1.0 else "yellow" if result.sharpe_ratio > 0.5 else "red"
    perf_table.add_row("Sharpe Ratio:", f"[{sharpe_color}]{result.sharpe_ratio:.2f}[/{sharpe_color}]")

    perf_table.add_row("Number of Trades:", f"{result.num_trades}")

    console.print(perf_table)
    console.print()

    # Comparison vs benchmarks
    console.print("[bold]COMPARISON[/bold]")
    console.print("-" * 40)

    comp_table = Table(show_header=False, box=None, padding=(0, 2))
    comp_table.add_column("Strategy", style="bold")
    comp_table.add_column("Return", justify="right")
    comp_table.add_column("Alpha", justify="right")

    # Agent return
    agent_color = "green" if result.total_return >= 0 else "red"
    comp_table.add_row(
        "Agent:",
        f"[{agent_color}]{result.total_return:+.1%}[/{agent_color}]",
        "",
    )

    # Benchmark (SPY Buy & Hold)
    if result.benchmark_return is not None:
        bench_color = "green" if result.benchmark_return >= 0 else "red"
        alpha_vs_bench = result.total_return - result.benchmark_return
        alpha_color = "green" if alpha_vs_bench >= 0 else "red"
        comp_table.add_row(
            "SPY Buy & Hold:",
            f"[{bench_color}]{result.benchmark_return:+.1%}[/{bench_color}]",
            f"[{alpha_color}]{alpha_vs_bench:+.1%}[/{alpha_color}]",
        )

    # Individual strategies (if available)
    if result.vs_dual_momentum_only is not None:
        dm_return = result.total_return - result.vs_dual_momentum_only
        dm_color = "green" if dm_return >= 0 else "red"
        alpha_color = "green" if result.vs_dual_momentum_only >= 0 else "red"
        comp_table.add_row(
            "Dual Momentum:",
            f"[{dm_color}]{dm_return:+.1%}[/{dm_color}]",
            f"[{alpha_color}]{result.vs_dual_momentum_only:+.1%}[/{alpha_color}]",
        )

    if result.vs_mean_reversion_only is not None:
        mr_return = result.total_return - result.vs_mean_reversion_only
        mr_color = "green" if mr_return >= 0 else "red"
        alpha_color = "green" if result.vs_mean_reversion_only >= 0 else "red"
        comp_table.add_row(
            "Mean Reversion:",
            f"[{mr_color}]{mr_return:+.1%}[/{mr_color}]",
            f"[{alpha_color}]{result.vs_mean_reversion_only:+.1%}[/{alpha_color}]",
        )

    if result.vs_multi_timeframe_only is not None:
        mtf_return = result.total_return - result.vs_multi_timeframe_only
        mtf_color = "green" if mtf_return >= 0 else "red"
        alpha_color = "green" if result.vs_multi_timeframe_only >= 0 else "red"
        comp_table.add_row(
            "Multi-Timeframe:",
            f"[{mtf_color}]{mtf_return:+.1%}[/{mtf_color}]",
            f"[{alpha_color}]{result.vs_multi_timeframe_only:+.1%}[/{alpha_color}]",
        )

    console.print(comp_table)
    console.print()

    # Decision summary
    console.print("[bold]DECISIONS[/bold]")
    console.print("-" * 40)

    total_decisions = len(result.decisions)
    decision_table = Table(show_header=False, box=None, padding=(0, 2))
    decision_table.add_column("Type", style="bold")
    decision_table.add_column("Count", justify="right")
    decision_table.add_column("Percentage", justify="right")

    routine_pct = result.routine_count / total_decisions if total_decisions > 0 else 0
    non_routine_pct = result.non_routine_count / total_decisions if total_decisions > 0 else 0
    urgent_pct = result.urgent_count / total_decisions if total_decisions > 0 else 0

    decision_table.add_row("Total Decisions:", f"{total_decisions}", "")
    decision_table.add_row("  Routine:", f"{result.routine_count}", f"[green]{routine_pct:.0%}[/green]")
    decision_table.add_row("  Non-Routine:", f"{result.non_routine_count}", f"[yellow]{non_routine_pct:.0%}[/yellow]")
    decision_table.add_row("  Urgent:", f"{result.urgent_count}", f"[red]{urgent_pct:.0%}[/red]")
    decision_table.add_row("Strategy Agreement:", "", f"{result.strategy_agreement_rate:.0%}")

    console.print(decision_table)
    console.print()

    # Run decision analysis if requested
    if analyze:
        console.print("[bold]DECISION ANALYSIS[/bold]")
        console.print("-" * 40)

        analyzer = DecisionFlowAnalyzer(result)
        analysis = analyzer.analyze()

        # Strategy agreement by regime
        if analysis.agreement_by_regime:
            console.print("\n[bold]Strategy Agreement by Regime:[/bold]")
            regime_table = Table(show_header=True, box=None, padding=(0, 2))
            regime_table.add_column("Regime", style="bold")
            regime_table.add_column("Agreement Rate", justify="right")

            for regime, rate in sorted(analysis.agreement_by_regime.items(), key=lambda x: -x[1]):
                rate_color = "green" if rate > 0.6 else "yellow" if rate > 0.4 else "red"
                regime_table.add_row(
                    regime.replace("_", " ").title(),
                    f"[{rate_color}]{rate:.0%}[/{rate_color}]",
                )

            console.print(regime_table)

        # Strategy accuracy
        if analysis.strategy_accuracy:
            console.print("\n[bold]Strategy Accuracy:[/bold]")
            acc_table = Table(show_header=True, box=None, padding=(0, 2))
            acc_table.add_column("Strategy", style="bold")
            acc_table.add_column("Accuracy", justify="right")
            acc_table.add_column("", justify="left")

            for strategy, accuracy in sorted(analysis.strategy_accuracy.items(), key=lambda x: -x[1]):
                acc_color = "green" if accuracy > 0.55 else "yellow" if accuracy > 0.45 else "red"
                best_marker = "[bold cyan]<-- BEST[/bold cyan]" if strategy == analysis.best_strategy else ""
                acc_table.add_row(
                    strategy.replace("_", " ").title(),
                    f"[{acc_color}]{accuracy:.0%}[/{acc_color}]",
                    best_marker,
                )

            console.print(acc_table)

        # Urgent decisions
        if analysis.urgent_decisions:
            console.print(f"\n[bold]Urgent Decisions:[/bold] {len(analysis.urgent_decisions)} total")
            console.print(f"  Positive outcome rate: {analysis.urgent_outcome_positive_pct:.0%}")

            if verbose and analysis.urgent_decisions:
                console.print("\n  Recent urgent decisions:")
                for ud in analysis.urgent_decisions[-5:]:
                    outcome = f"{ud['outcome_return']:+.1%}" if ud['outcome_return'] is not None else "N/A"
                    action_color = "green" if ud['action'] == "buy" else "red" if ud['action'] == "sell" else "yellow"
                    console.print(
                        f"    {ud['date']}: [{action_color}]{ud['action'].upper()}[/{action_color}] "
                        f"(regime: {ud['market_regime']}, outcome: {outcome})"
                    )

        # Regime performance
        if analysis.regime_returns:
            console.print("\n[bold]Returns by Regime:[/bold]")
            regime_perf_table = Table(show_header=True, box=None, padding=(0, 2))
            regime_perf_table.add_column("Regime", style="bold")
            regime_perf_table.add_column("Avg Return", justify="right")

            for regime, ret in sorted(analysis.regime_returns.items(), key=lambda x: -x[1]):
                ret_color = "green" if ret > 0 else "red"
                regime_perf_table.add_row(
                    regime.replace("_", " ").title(),
                    f"[{ret_color}]{ret:+.2%}[/{ret_color}]",
                )

            console.print(regime_perf_table)

        console.print()

    # Final separator
    console.print("=" * 40)
    console.print()


@app.command()
def version():
    """Show version information."""
    from aurel2 import __version__
    typer.echo(f"Aurel2 v{__version__}")


if __name__ == "__main__":
    app()
