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
    """Show current momentum scores and trading recommendation."""
    from datetime import date as date_type

    check_date = date_type.fromisoformat(date_str) if date_str else date_type.today()

    typer.echo(f"\nCalculating momentum scores for {check_date}...")

    # Load settings
    settings = load_settings(config)

    # Define assets with full details
    # Backtest symbols (US-listed, for data availability)
    # and UCITS equivalents (for actual trading with Romanian tax benefits)
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
        },
        AssetClass.GLOBAL_STOCKS: {
            "symbol": "VWRA",
            "name": "Vanguard FTSE All-World UCITS ETF (Acc)",
            "isin": "IE00BK5BQT80",
            "exchange": "Xetra (Germany)",
        },
        AssetClass.BONDS: {
            "symbol": "AGGH",
            "name": "iShares Core Global Aggregate Bond UCITS ETF (Acc)",
            "isin": "IE00BDBRDM35",
            "exchange": "Xetra (Germany)",
        },
        AssetClass.CASH: {
            "symbol": "CASH",
            "name": "Hold in broker cash account",
            "isin": "N/A",
            "exchange": "N/A",
        },
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

    # Print momentum table
    typer.echo("\n" + "=" * 70)
    typer.echo("MOMENTUM SCORES (12-month lookback)")
    typer.echo("=" * 70)
    typer.echo(f"{'':2} {'Asset':<35} {'Momentum':>10} {'Price':>10}")
    typer.echo("-" * 70)

    for asset_class, score in sorted_scores:
        indicator = ">>" if asset_class == winner_class else "  "
        typer.echo(
            f"{indicator} {score.asset.name:<35} {score.momentum_12m:>9.2%} "
            f"${score.price:>9.2f}"
        )

    typer.echo("=" * 70)

    # Print recommendation
    typer.echo("\n" + "=" * 70)
    typer.echo("RECOMMENDATION")
    typer.echo("=" * 70)

    if winner_score.momentum_12m <= 0.04:  # Below cash rate
        typer.echo("\nACTION: Hold CASH (all assets underperforming)")
        typer.echo("\nKeep funds in your broker's money market or savings account.")
    else:
        typer.echo(f"\nACTION: BUY {winner_class.value.upper().replace('_', ' ')}")
        typer.echo(f"\nWinner has {winner_score.momentum_12m:.2%} momentum (12-month return)")

    typer.echo("\n" + "-" * 70)
    typer.echo("WHAT TO BUY:")
    typer.echo("-" * 70)

    typer.echo(f"\n  For US broker (e.g., Interactive Brokers US):")
    typer.echo(f"    Symbol: {assets[winner_class].symbol}")
    typer.echo(f"    Name:   {assets[winner_class].name}")

    typer.echo(f"\n  For European broker (TradeVille, IBKR EU) - RECOMMENDED for RO tax:")
    typer.echo(f"    Symbol: {winner_ucits['symbol']}")
    typer.echo(f"    Name:   {winner_ucits['name']}")
    typer.echo(f"    ISIN:   {winner_ucits['isin']}")
    typer.echo(f"    Exchange: {winner_ucits['exchange']}")

    typer.echo("\n" + "-" * 70)
    typer.echo("TAX NOTE (Romania):")
    typer.echo("-" * 70)
    typer.echo("  - Hold >365 days via Romanian broker: 1% tax on gains")
    typer.echo("  - Hold <365 days via Romanian broker: 3% tax on gains")
    typer.echo("  - Via IBKR (foreign broker): 16% tax on net annual gains")
    typer.echo("  - Use accumulating ETFs (Acc) to defer dividend tax")
    typer.echo("=" * 70)

    # Next rebalance
    import calendar
    year = check_date.year
    month = check_date.month

    # Find next quarter end
    quarter_ends = [(3, 31), (6, 30), (9, 30), (12, 31)]
    next_rebalance = None
    for q_month, q_day in quarter_ends:
        if month < q_month or (month == q_month and check_date.day < q_day):
            next_rebalance = date_type(year, q_month, q_day)
            break
    if next_rebalance is None:
        next_rebalance = date_type(year + 1, 3, 31)

    typer.echo(f"\nNext rebalance check: {next_rebalance}")


@app.command()
def version():
    """Show version information."""
    from aurel2 import __version__
    typer.echo(f"Aurel2 v{__version__}")


if __name__ == "__main__":
    app()
