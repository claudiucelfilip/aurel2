"""Command-line interface for Aurel2."""

from datetime import date
from pathlib import Path

import pandas as pd
import typer
import structlog

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
def version():
    """Show version information."""
    from aurel2 import __version__
    typer.echo(f"Aurel2 v{__version__}")


if __name__ == "__main__":
    app()
