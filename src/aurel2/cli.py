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
    """Show current momentum scores for all assets."""
    from datetime import date as date_type

    check_date = date_type.fromisoformat(date_str) if date_str else date_type.today()

    typer.echo(f"Calculating momentum scores for {check_date}")

    # Load settings
    settings = load_settings(config)

    # Build assets (use US ETFs for demo)
    assets = {
        AssetClass.US_STOCKS: Asset(
            symbol="SPY", name="S&P 500", asset_class=AssetClass.US_STOCKS, yahoo_symbol="SPY"
        ),
        AssetClass.GLOBAL_STOCKS: Asset(
            symbol="EFA", name="International", asset_class=AssetClass.GLOBAL_STOCKS, yahoo_symbol="EFA"
        ),
        AssetClass.BONDS: Asset(
            symbol="AGG", name="Bonds", asset_class=AssetClass.BONDS, yahoo_symbol="AGG"
        ),
        AssetClass.CASH: Asset(
            symbol="CASH", name="Cash", asset_class=AssetClass.CASH
        ),
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

    typer.echo("\n" + "=" * 50)
    typer.echo("MOMENTUM SCORES (12-month)")
    typer.echo("=" * 50)

    for asset_class, score in sorted(scores.items(), key=lambda x: x[1].momentum_12m, reverse=True):
        indicator = "👑" if score == max(scores.values(), key=lambda s: s.momentum_12m) else "  "
        positive = "✓" if score.is_positive else "✗"
        typer.echo(
            f"{indicator} {asset_class.value:15} | {score.momentum_12m:7.2%} | "
            f"${score.price:8.2f} | Positive: {positive}"
        )

    typer.echo("=" * 50)


@app.command()
def version():
    """Show version information."""
    from aurel2 import __version__
    typer.echo(f"Aurel2 v{__version__}")


if __name__ == "__main__":
    app()
