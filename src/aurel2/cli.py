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
    dca: float = typer.Option(0.0, "--dca", help="Monthly DCA contribution amount"),
    frequency: str = typer.Option("monthly", help="Rebalance frequency: monthly or quarterly"),
    ai: bool = typer.Option(False, "--ai", help="Enable AI advisor (disabled by default — see ARCHITECTURE.md for findings)"),
    ai_model: str = typer.Option("haiku", "--ai-model", help="AI model: sonnet, opus, haiku"),
    amnesia: bool = typer.Option(False, "--amnesia", help="Tell AI to ignore training data financial knowledge and redact dates"),
    correlation_guard: bool = typer.Option(True, "--correlation-guard/--no-correlation-guard", help="Redirect bond rotations when SPY-AGG correlation is high"),
    sideways_hold: bool = typer.Option(True, "--sideways-hold/--no-sideways-hold", help="Suppress switches in sideways markets unless momentum advantage is large"),
    config: Path = typer.Option(None, help="Config file path"),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Verbose output"),
):
    """Run a backtest mirroring the full live trading path (3 strategies + orchestrator + AI)."""
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
    typer.echo(f"Calm-market hold: enabled")
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
    ai: bool = typer.Option(False, "--ai", help="Enable AI advisor (disabled by default)"),
    notify: bool = typer.Option(True, "--notify/--no-notify", help="Send notifications"),
    ntfy_topic: str = typer.Option("aurel2", "--ntfy-topic", help="Ntfy topic"),
    approval_url: str = typer.Option(
        "https://approval-endpoint.vercel.app/api/decision",
        "--approval-url",
        help="Approval endpoint URL",
    ),
    failure_file: str = typer.Option(
        "data/failure_learnings.json",
        "--failures",
        help="Path to failure learnings",
    ),
):
    """Run the AI agent with failure-learning advisor.
    
    The agent:
    1. Gets strategy signals from all 3 strategies
    2. Orchestrator produces deterministic decision
    3. AI advisor reviews decision against historical failures
    4. If AI disagrees, upgrades to NON_ROUTINE and suggests override
    5. Sends notification and creates approval request if needed
    """
    import uuid
    import httpx
    from rich.panel import Panel

    from aurel2.agent.orchestrator import AgentOrchestrator, DecisionType
    from aurel2.agent.advisor import AIAdvisor
    from aurel2.mcp.server import Aurel2MCPServer
    from aurel2.notifications.ntfy import NtfyNotifier

    console = Console()

    console.print("\n[bold cyan]Running Aurel2 AI Agent with Failure Learning...[/bold cyan]\n")

    # Initialize components
    server = Aurel2MCPServer()
    orchestrator = AgentOrchestrator()

    # Get strategy signals
    with console.status("[bold green]Fetching strategy signals..."):
        signals_response = server._get_strategy_signals()
        signals = signals_response.get("signals", {})

    # Get market context
    with console.status("[bold green]Fetching market context..."):
        market_context = server._get_market_context()
        prices = server._get_prices()

    # Get current holding
    portfolio = server._get_portfolio()
    current_holding = None
    for h in portfolio.get("holdings", []):
        if h["symbol"] not in ["CASH", "EUR"]:
            current_holding = h["symbol"]
            break

    # Transform signals into format expected by orchestrator
    orchestrator_signals = {}
    for strategy_name, signal_data in signals.items():
        if "error" not in signal_data:
            orchestrator_signals[strategy_name] = {
                "action": signal_data.get("action", "hold"),
                "confidence": signal_data.get("confidence", 0.5),
                "asset_symbol": signal_data.get("asset") or signal_data.get("asset_class"),
            }

    # Orchestrator analysis (deterministic)
    decision = orchestrator.analyze(
        signals=orchestrator_signals,
        market_context={
            "drawdown": market_context.get("drawdown", 0.0),
            "volatility": "normal",
            "regime": market_context.get("regime", "neutral"),
        },
        current_holding=current_holding,
    )

    deterministic_action = decision.action.value
    deterministic_asset = decision.asset_symbol

    # AI Advisor review (unless disabled)
    ai_advice = None
    ai_override = False
    if ai:
        with console.status("[bold green]AI advisor reviewing decision..."):
            try:
                advisor = AIAdvisor(failure_file=failure_file)
                ai_advice = advisor.review(
                    deterministic_action=deterministic_action,
                    deterministic_asset=deterministic_asset,
                    strategy_signals=signals,
                    market_context=market_context,
                    prices=prices,
                    current_holding=current_holding,
                )
                ai_override = not ai_advice.agrees_with_deterministic
            except Exception as e:
                console.print(f"[yellow]AI advisor failed: {e}[/yellow]")

    # Display results
    console.print("\n" + "=" * 70)
    console.print("[bold]AGENT DECISION[/bold]")
    console.print("=" * 70)

    # Decision type with color
    type_color = {
        DecisionType.ROUTINE: "green",
        DecisionType.NON_ROUTINE: "yellow",
        DecisionType.URGENT: "red",
    }
    action_color = {
        "buy": "green",
        "sell": "red",
        "hold": "yellow",
    }

    color = type_color.get(decision.decision_type, "white")
    console.print(f"Decision Type: [{color}]{decision.decision_type.value.upper()}[/{color}]")

    action_val = decision.action.value
    acolor = action_color.get(action_val, "white")
    console.print(f"Deterministic: [{acolor}]{action_val.upper()}[/{acolor}] {deterministic_asset or ''}")
    console.print(f"Confidence: {decision.confidence:.1%}")

    # AI Advisor section
    if ai_advice:
        console.print()
        if ai_advice.agrees_with_deterministic:
            console.print(Panel.fit(
                "[bold green]✓ AI AGREES[/bold green]\n\n"
                f"The AI advisor reviewed the decision against {len(advisor.failure_analysis.failure_events) if advisor.failure_analysis else 0} "
                "historical failures and agrees with the deterministic signal.",
                border_style="green"
            ))
        else:
            # AI disagrees - upgrade to NON_ROUTINE
            decision.decision_type = DecisionType.NON_ROUTINE
            decision.requires_approval = True
            
            console.print(Panel.fit(
                "[bold red]⚠ AI OVERRIDE SUGGESTED[/bold red]\n\n"
                f"Deterministic: {ai_advice.deterministic_action.upper()} {ai_advice.deterministic_asset or ''}\n"
                f"AI suggests:   {ai_advice.recommended_action.upper()} {ai_advice.recommended_asset or ''}\n"
                f"Confidence:    {ai_advice.confidence:.0%}\n\n"
                f"Reasoning: {ai_advice.reasoning[:200]}...",
                border_style="red"
            ))

            if ai_advice.failure_patterns_detected:
                console.print("\n[bold]Detected Failure Patterns:[/bold]")
                for pattern in ai_advice.failure_patterns_detected:
                    console.print(f"  • {pattern}")

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
    console.print(f"  Current Holding: {current_holding or 'Cash'}")

    console.print("=" * 70)

    # Determine final action and asset
    final_action = ai_advice.recommended_action if ai_override and ai_advice else deterministic_action
    final_asset = ai_advice.recommended_asset if ai_override and ai_advice else deterministic_asset
    final_reasoning = ai_advice.reasoning if ai_override and ai_advice else decision.reasoning
    final_confidence = ai_advice.confidence if ai_override and ai_advice else decision.confidence

    # Execute or request approval
    if dry_run:
        console.print("\n[yellow]Dry run - no actions taken.[/yellow]")
    elif not decision.requires_approval and not ai_override:
        # Routine decision with AI agreement - auto-execute
        console.print("\n[green]Auto-executing routine decision (AI agrees)...[/green]")
        result = orchestrator.execute(decision)
        console.print(f"Execution status: {result['status']}")
        
        if notify:
            notifier = NtfyNotifier(topic=ntfy_topic)
            notifier.send(
                message=f"Auto-executed: {final_action.upper()} {final_asset or ''}\n\n{final_reasoning[:150]}",
                title=f"Aurel2: {final_action.upper()} {final_asset or ''}",
                tags=["white_check_mark", "chart_with_upwards_trend"],
            )
    else:
        # Needs approval - create approval request
        decision_id = str(uuid.uuid4())[:8]
        approval_link = f"{approval_url}/{decision_id}"

        console.print(f"\n[yellow]Approval required. Creating approval request...[/yellow]")

        # Build strategy context for approval page
        strategy_context = []
        for name, sig in signals.items():
            if "error" not in sig:
                strategy_context.append({
                    "name": name.replace("_", " ").title(),
                    "action": sig.get("action", "hold").upper(),
                    "confidence": sig.get("confidence", 0.5),
                })
        
        # Check strategy agreement
        strategy_actions = [s["action"] for s in strategy_context]
        strategies_agree = len(set(strategy_actions)) == 1
        
        # Create decision in Vercel KV with rich context
        try:
            with httpx.Client() as client:
                response = client.post(
                    approval_link,
                    json={
                        "action": final_action,
                        "symbol": final_asset or "HOLD",
                        "reasoning": final_reasoning[:500],
                        "confidence": final_confidence,
                        # Rich context
                        "deterministic_action": deterministic_action,
                        "deterministic_asset": deterministic_asset,
                        "ai_agrees": not ai_override,
                        "ai_action": ai_advice.recommended_action.upper() if ai_advice else None,
                        "ai_asset": ai_advice.recommended_asset if ai_advice else None,
                        "ai_reasoning": ai_advice.reasoning[:300] if ai_advice else None,
                        "strategies_agree": strategies_agree,
                        "strategies": strategy_context,
                        "market_regime": market_context.get("regime", "unknown").upper(),
                        "spy_price": market_context.get("spy_price"),
                        "drawdown": market_context.get("drawdown"),
                        "current_holding": current_holding,
                    },
                    timeout=10.0,
                )
                if response.status_code == 201:
                    console.print(f"[green]Approval request created: {approval_link}[/green]")
                else:
                    console.print(f"[yellow]Could not create approval request: {response.text}[/yellow]")
        except Exception as e:
            console.print(f"[yellow]Could not reach approval endpoint: {e}[/yellow]")

        # Send notification
        if notify:
            notifier = NtfyNotifier(topic=ntfy_topic)
            
            if ai_override:
                notifier.send(
                    message=(
                        f"🤖 AI Override Alert!\n\n"
                        f"Deterministic: {deterministic_action.upper()} {deterministic_asset or ''}\n"
                        f"AI suggests: {final_action.upper()} {final_asset or ''}\n\n"
                        f"Confidence: {final_confidence:.0%}\n"
                        f"Reasoning: {final_reasoning[:150]}...\n\n"
                        f"Approve/Reject: {approval_link}"
                    ),
                    title="Aurel2: AI Override Suggested",
                    priority="high",
                    tags=["warning", "robot"],
                    click_url=approval_link,
                )
            else:
                notifier.send(
                    message=(
                        f"Approval Required\n\n"
                        f"Action: {final_action.upper()} {final_asset or ''}\n"
                        f"Confidence: {final_confidence:.0%}\n"
                        f"Reasoning: {final_reasoning[:150]}...\n\n"
                        f"Approve/Reject: {approval_link}"
                    ),
                    title=f"Aurel2: {final_action.upper()} {final_asset or ''}",
                    priority="default",
                    tags=["question", "chart_with_upwards_trend"],
                    click_url=approval_link,
                )
            console.print("[dim]Notification sent.[/dim]")

        console.print(f"\n[yellow]Decision will timeout in {decision.timeout_hours} hours.[/yellow]")

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

    # Save results
    years = (end_date - start_date).days / 365.25
    _save_backtest_results({
        "type": "backtest_agent",
        "start_date": str(start_date),
        "end_date": str(end_date),
        "initial_capital": capital,
        "frequency": frequency,
        "years": round(years, 2),
        "final_value": round(result.final_value, 2),
        "total_return_pct": round(result.total_return * 100, 2),
        "cagr_pct": round(result.cagr * 100, 2),
        "max_drawdown_pct": round(result.max_drawdown * 100, 2),
        "sharpe_ratio": round(result.sharpe_ratio, 2),
        "benchmark_return_pct": round(result.benchmark_return * 100, 2) if result.benchmark_return else None,
        "alpha_vs_spy_pct": round((result.total_return - result.benchmark_return) * 100, 2) if result.benchmark_return else None,
        "num_trades": result.num_trades,
        "total_decisions": total_decisions,
        "routine_count": result.routine_count,
        "non_routine_count": result.non_routine_count,
        "urgent_count": result.urgent_count,
        "strategy_agreement_rate_pct": round(result.strategy_agreement_rate * 100, 2),
    })


@app.command("eval-agent")
def eval_agent(
    weeks: int = typer.Option(4, "--weeks", "-w", help="Number of weeks to evaluate"),
    end_date: str = typer.Option(None, "--end", "-e", help="End date (YYYY-MM-DD), defaults to today"),
    mock: bool = typer.Option(False, "--mock", "-m", help="Use mock AI (no API calls)"),
    claude_code: bool = typer.Option(False, "--claude-code", help="Use Claude Code CLI instead of Anthropic API (uses your Claude plan)"),
    claude_model: str = typer.Option("haiku", "--claude-model", help="Model for Claude Code: sonnet, opus, haiku"),
    expert: bool = typer.Option(False, "--expert", "-x", help="Use Expert AI with extended thinking (Opus 4.5)"),
    no_cache: bool = typer.Option(False, "--no-cache", help="Don't use cached data"),
    compare: bool = typer.Option(False, "--compare", "-c", help="Show comparison summary from all cached data"),
    outcomes: bool = typer.Option(False, "--outcomes", "-o", help="Calculate historical outcomes to measure performance"),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Verbose output"),
):
    """Evaluate AI vs deterministic decision-making over recent weeks.

    This command compares what the AI agent would decide vs the deterministic
    weighted voting approach. It helps determine if AI reasoning adds value.

    The evaluation:
    1. Fetches market context (VIX, SPY levels, Fed meetings, etc.)
    2. Gets signals from all 3 strategies
    3. Runs deterministic weighted voting
    4. Runs AI evaluation with Claude
    5. Compares and caches results

    Results are cached to avoid repeated API calls. After 1 week, you can
    update outcomes to measure which approach was better.
    """
    from rich.panel import Panel

    from aurel2.agent.eval_runner import EvalRunner
    from aurel2.agent.eval_cache import EvalCache

    console = Console()

    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Header
    console.print()
    console.print(Panel.fit(
        "[bold cyan]AI vs Deterministic Evaluation[/bold cyan]",
        border_style="cyan",
    ))
    console.print()

    # Just show comparison if requested
    if compare:
        cache = EvalCache()
        summary = cache.compute_comparison_summary()

        if summary.total_decisions == 0:
            console.print("[yellow]No cached decisions found. Run an evaluation first.[/yellow]")
            raise typer.Exit(0)

        console.print("[bold]COMPARISON SUMMARY[/bold]")
        console.print("-" * 50)

        comp_table = Table(show_header=False, box=None, padding=(0, 2))
        comp_table.add_column("Metric", style="bold")
        comp_table.add_column("Value", justify="right")

        comp_table.add_row("Total Decisions:", f"{summary.total_decisions}")

        agree_pct = summary.agreements / summary.total_decisions if summary.total_decisions > 0 else 0
        comp_table.add_row("Agreements:", f"{summary.agreements} ({agree_pct:.0%})")
        comp_table.add_row("Disagreements:", f"{summary.disagreements}")

        if summary.disagreements > 0:
            ai_win_rate = summary.ai_better_when_disagreed / summary.disagreements
            det_win_rate = summary.deterministic_better_when_disagreed / summary.disagreements
            ai_color = "green" if ai_win_rate > 0.5 else "red"
            det_color = "green" if det_win_rate > 0.5 else "red"
            comp_table.add_row("AI Better (on disagreements):", f"[{ai_color}]{summary.ai_better_when_disagreed} ({ai_win_rate:.0%})[/{ai_color}]")
            comp_table.add_row("Det. Better (on disagreements):", f"[{det_color}]{summary.deterministic_better_when_disagreed} ({det_win_rate:.0%})[/{det_color}]")

        if summary.ai_cumulative_return != 0 or summary.deterministic_cumulative_return != 0:
            ai_ret_color = "green" if summary.ai_cumulative_return > 0 else "red"
            det_ret_color = "green" if summary.deterministic_cumulative_return > 0 else "red"
            comp_table.add_row("AI Cumulative Return:", f"[{ai_ret_color}]{summary.ai_cumulative_return:+.2f}%[/{ai_ret_color}]")
            comp_table.add_row("Det. Cumulative Return:", f"[{det_ret_color}]{summary.deterministic_cumulative_return:+.2f}%[/{det_ret_color}]")

        console.print(comp_table)
        console.print()

        # Show recent decisions
        if summary.decisions:
            console.print("[bold]RECENT DECISIONS[/bold]")
            console.print("-" * 50)

            dec_table = Table(show_header=True, box=None, padding=(0, 1))
            dec_table.add_column("Date", style="bold")
            dec_table.add_column("Det.", justify="center")
            dec_table.add_column("AI", justify="center")
            dec_table.add_column("Agree", justify="center")
            dec_table.add_column("Det. 1w", justify="right")
            dec_table.add_column("AI 1w", justify="right")

            for dec in summary.decisions[-10:]:  # Last 10
                det_dec = dec.get("deterministic_decision", {})
                ai_dec = dec.get("ai_decision", {})

                det_action = det_dec.get("action", "?").upper()
                det_asset = det_dec.get("asset", "")
                det_str = f"{det_action}" + (f" {det_asset}" if det_asset else "")

                ai_action = ai_dec.get("action", "?").upper() if ai_dec else "?"
                ai_asset = ai_dec.get("asset", "") if ai_dec else ""
                ai_str = f"{ai_action}" + (f" {ai_asset}" if ai_asset else "")

                agreed = dec.get("agreed", False)
                agree_str = "[green]Yes[/green]" if agreed else "[red]No[/red]"

                det_1w = dec.get("outcome_deterministic_1w")
                ai_1w = dec.get("outcome_ai_1w")
                det_1w_str = f"{det_1w:+.1f}%" if det_1w is not None else "-"
                ai_1w_str = f"{ai_1w:+.1f}%" if ai_1w is not None else "-"

                dec_table.add_row(
                    dec.get("date", "?"),
                    det_str,
                    ai_str,
                    agree_str,
                    det_1w_str,
                    ai_1w_str,
                )

            console.print(dec_table)

        console.print()
        raise typer.Exit(0)

    # Calculate historical outcomes if requested
    if outcomes:
        from rich.panel import Panel

        console.print("[bold]Calculating historical outcomes...[/bold]")
        console.print()

        runner = EvalRunner(use_mock_ai=True)  # Don't need AI for outcome calculation

        try:
            results = runner.calculate_all_outcomes()
        except Exception as e:
            console.print(f"[red]Error calculating outcomes: {e}[/red]")
            if verbose:
                import traceback
                console.print(traceback.format_exc())
            raise typer.Exit(1)

        if "error" in results:
            console.print(f"[yellow]{results['error']}[/yellow]")
            raise typer.Exit(0)

        # Display results
        console.print(Panel.fit(
            "[bold cyan]AI vs Deterministic: Historical Performance[/bold cyan]",
            border_style="cyan",
        ))
        console.print()

        # Summary table
        console.print("[bold]OVERALL RESULTS[/bold]")
        console.print("=" * 60)

        summary_table = Table(show_header=False, box=None, padding=(0, 2))
        summary_table.add_column("Metric", style="bold")
        summary_table.add_column("Value", justify="right")

        summary_table.add_row("Total Decisions:", f"{results['total_decisions']}")
        summary_table.add_row("Decisions with Outcomes:", f"{results['decisions_with_outcomes']}")
        summary_table.add_row("Agreements:", f"{results['agreements']}")
        summary_table.add_row("Disagreements:", f"{results['disagreements']}")

        console.print(summary_table)
        console.print()

        # Performance comparison
        console.print("[bold]PERFORMANCE COMPARISON[/bold]")
        console.print("-" * 60)

        perf_table = Table(show_header=True, box=None, padding=(0, 2))
        perf_table.add_column("Metric", style="bold")
        perf_table.add_column("AI", justify="right")
        perf_table.add_column("Deterministic", justify="right")
        perf_table.add_column("Winner", justify="center")

        # Total return
        ai_ret = results['ai_total_return']
        det_ret = results['det_total_return']
        ai_color = "green" if ai_ret > det_ret else "red" if ai_ret < det_ret else "yellow"
        det_color = "green" if det_ret > ai_ret else "red" if det_ret < ai_ret else "yellow"
        winner = "[green]AI[/green]" if ai_ret > det_ret + 0.5 else "[green]DET[/green]" if det_ret > ai_ret + 0.5 else "[yellow]TIE[/yellow]"
        perf_table.add_row(
            "Total Return:",
            f"[{ai_color}]{ai_ret:+.2f}%[/{ai_color}]",
            f"[{det_color}]{det_ret:+.2f}%[/{det_color}]",
            winner,
        )

        # Average return per decision
        ai_avg = results['ai_avg_return']
        det_avg = results['det_avg_return']
        ai_color = "green" if ai_avg > det_avg else "red" if ai_avg < det_avg else "yellow"
        det_color = "green" if det_avg > ai_avg else "red" if det_avg < ai_avg else "yellow"
        winner = "[green]AI[/green]" if ai_avg > det_avg + 0.05 else "[green]DET[/green]" if det_avg > ai_avg + 0.05 else "[yellow]TIE[/yellow]"
        perf_table.add_row(
            "Avg Return/Decision:",
            f"[{ai_color}]{ai_avg:+.2f}%[/{ai_color}]",
            f"[{det_color}]{det_avg:+.2f}%[/{det_color}]",
            winner,
        )

        console.print(perf_table)
        console.print()

        # Disagreement analysis
        if results['disagreements'] > 0:
            console.print("[bold]WHEN THEY DISAGREED[/bold]")
            console.print("-" * 60)

            disagree_table = Table(show_header=False, box=None, padding=(0, 2))
            disagree_table.add_column("Metric", style="bold")
            disagree_table.add_column("Value", justify="right")

            ai_wins = results['ai_wins']
            det_wins = results['det_wins']
            ties = results['ties']
            total_disagree = results['disagreements']

            ai_win_pct = ai_wins / total_disagree * 100
            det_win_pct = det_wins / total_disagree * 100

            ai_color = "green" if ai_wins > det_wins else "red"
            det_color = "green" if det_wins > ai_wins else "red"

            disagree_table.add_row("AI Wins:", f"[{ai_color}]{ai_wins} ({ai_win_pct:.0f}%)[/{ai_color}]")
            disagree_table.add_row("Deterministic Wins:", f"[{det_color}]{det_wins} ({det_win_pct:.0f}%)[/{det_color}]")
            disagree_table.add_row("Ties:", f"{ties}")

            console.print(disagree_table)
            console.print()

            # Show disagreement details
            if results['disagreement_details'] and verbose:
                console.print("[bold]DISAGREEMENT DETAILS[/bold]")
                console.print("-" * 60)

                detail_table = Table(show_header=True, box=None, padding=(0, 1))
                detail_table.add_column("Date", style="bold")
                detail_table.add_column("Det.", justify="center")
                detail_table.add_column("AI", justify="center")
                detail_table.add_column("Det. Ret", justify="right")
                detail_table.add_column("AI Ret", justify="right")
                detail_table.add_column("Winner", justify="center")

                for d in results['disagreement_details']:
                    winner_style = "green" if d['winner'] == "AI" else "cyan" if d['winner'] == "DET" else "yellow"
                    det_ret_color = "green" if d['det_return'] > 0 else "red"
                    ai_ret_color = "green" if d['ai_return'] > 0 else "red"

                    detail_table.add_row(
                        d['date'],
                        d['det_action'],
                        d['ai_action'],
                        f"[{det_ret_color}]{d['det_return']:+.1f}%[/{det_ret_color}]",
                        f"[{ai_ret_color}]{d['ai_return']:+.1f}%[/{ai_ret_color}]",
                        f"[{winner_style}]{d['winner']}[/{winner_style}]",
                    )

                console.print(detail_table)
                console.print()

        # Final verdict
        console.print("=" * 60)
        if ai_ret > det_ret + 1.0:
            console.print("[bold green]VERDICT: AI outperformed deterministic approach[/bold green]")
        elif det_ret > ai_ret + 1.0:
            console.print("[bold cyan]VERDICT: Deterministic outperformed AI approach[/bold cyan]")
        else:
            console.print("[bold yellow]VERDICT: Performance roughly equal[/bold yellow]")
        console.print("=" * 60)
        console.print()

        raise typer.Exit(0)

    # Run evaluation
    end = date.fromisoformat(end_date) if end_date else date.today()

    console.print(f"Evaluating {weeks} weeks ending {end}")
    if mock:
        console.print("AI Mode: Mock (no API calls)")
    elif expert:
        console.print(f"AI Mode: Expert AI via Claude Code CLI ({claude_model})")
    elif claude_code:
        console.print(f"AI Mode: Claude Code CLI ({claude_model})")
    else:
        console.print("AI Mode: Anthropic API")
    console.print(f"Cache: {'Disabled' if no_cache else 'Enabled'}")
    console.print()

    runner = EvalRunner(use_mock_ai=mock, use_claude_code=claude_code, use_expert_mode=expert, claude_model=claude_model)

    try:
        results = runner.run_evaluation(
            weeks=weeks,
            end_date=end,
            use_cache=not no_cache,
        )
    except Exception as e:
        console.print(f"[red]Error running evaluation: {e}[/red]")
        if verbose:
            import traceback
            console.print(traceback.format_exc())
        raise typer.Exit(1)

    if not results:
        console.print("[yellow]No results to display.[/yellow]")
        raise typer.Exit(0)

    # Display results
    console.print("[bold]EVALUATION RESULTS[/bold]")
    console.print("=" * 60)

    agreements = sum(1 for r in results if r.agreed)
    disagreements = len(results) - agreements

    summary_table = Table(show_header=False, box=None, padding=(0, 2))
    summary_table.add_column("Metric", style="bold")
    summary_table.add_column("Value", justify="right")

    summary_table.add_row("Period:", f"{results[0].date} to {results[-1].date}")
    summary_table.add_row("Total Decisions:", f"{len(results)}")

    agree_color = "green" if agreements / len(results) > 0.6 else "yellow"
    summary_table.add_row("Agreements:", f"[{agree_color}]{agreements} ({agreements/len(results)*100:.0f}%)[/{agree_color}]")
    summary_table.add_row("Disagreements:", f"{disagreements}")

    console.print(summary_table)
    console.print()

    # Decision details
    console.print("[bold]DECISION DETAILS[/bold]")
    console.print("-" * 60)

    for r in results:
        status_style = "green" if r.agreed else "red"
        status = "AGREE" if r.agreed else "DIFFER"

        det = f"{r.deterministic_action.upper()}"
        if r.deterministic_asset:
            det += f" {r.deterministic_asset}"

        ai = f"{r.ai_action.upper()}"
        if r.ai_asset:
            ai += f" {r.ai_asset}"

        console.print(f"\n[bold]{r.date}[/bold] [[{status_style}]{status}[/{status_style}]]")
        console.print(f"  Deterministic: {det}")
        console.print(f"  AI:            {ai}")

        if not r.agreed and r.ai_reasoning:
            # Truncate reasoning for display
            reasoning = r.ai_reasoning[:150] + "..." if len(r.ai_reasoning) > 150 else r.ai_reasoning
            console.print(f"  [dim]AI Reasoning: {reasoning}[/dim]")

        # Show context summary
        if verbose:
            ctx = r.context
            if ctx.vix:
                console.print(f"  [dim]VIX: {ctx.vix:.1f}[/dim]", end="")
            if ctx.spy_drawdown:
                console.print(f"  [dim]Drawdown: {ctx.spy_drawdown:.1f}%[/dim]", end="")
            if ctx.spy_1w_return:
                console.print(f"  [dim]1w: {ctx.spy_1w_return:+.1f}%[/dim]", end="")
            console.print()

    console.print()
    console.print("=" * 60)

    # Hint about outcomes
    console.print()
    console.print("[dim]Tip: After 1 week, run with --compare to see outcome statistics.[/dim]")
    console.print()


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
    check_time: str = typer.Option("16:00", "--check-time", "-t", help="Daily check time (HH:MM in Romania timezone)", envvar="CHECK_TIME"),
    poll_interval: int = typer.Option(5, "--poll-interval", "-p", help="Approval poll interval in minutes", envvar="POLL_INTERVAL"),
    ntfy_topic: str = typer.Option("aurel2", "--ntfy-topic", help="Ntfy notification topic", envvar="NTFY_TOPIC"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Don't execute trades, just simulate", envvar="DRY_RUN"),
    ai: bool = typer.Option(False, "--ai", help="Enable AI advisor (disabled by default)", envvar="USE_AI"),
    ai_model: str = typer.Option("haiku", "--ai-model", "-m", help="AI model: sonnet, opus, haiku", envvar="AI_MODEL"),
    ai_lookback: int = typer.Option(3, "--ai-lookback", "-l", help="AI failure pattern lookback years (default: 3)", envvar="AI_LOOKBACK_YEARS"),
    ibkr_host: str = typer.Option("127.0.0.1", "--ibkr-host", help="IBKR Gateway host", envvar="IBKR_HOST"),
    ibkr_port: int = typer.Option(None, "--ibkr-port", help="IBKR Gateway port (default: 4002 paper, 4001 live)", envvar="IBKR_PORT"),
):
    """Run the live trading daemon.

    The daemon:
    1. Connects to IBKR (launches IB Gateway if needed)
    2. Runs daily check at the specified time (default 4 PM Romania)
    3. Auto-executes ROUTINE decisions (all strategies agree)
    4. Creates approval requests for NON_ROUTINE/URGENT decisions
    5. Polls for approvals every 5 minutes
    6. Auto-executes timed-out decisions after 1 hour

    AI Configuration:
    - Default: Sonnet with 3-year lookback (best in backtests: +42.86% alpha)
    - Lookback window filters failure patterns to recent years only

    Environment Variables:
        IBKR_HOST, IBKR_PORT, CHECK_TIME, POLL_INTERVAL, NTFY_TOPIC,
        DRY_RUN, AI_MODEL, AI_LOOKBACK_YEARS

    Examples:
        aurel2 live --paper          # Paper trading (default)
        aurel2 live --real           # LIVE trading (caution!)
        aurel2 live --dry-run        # Simulate without executing
        aurel2 live --check-time 09:30  # Check at 9:30 AM
        aurel2 live --ai-lookback 5  # Use 5-year lookback window
        aurel2 live --ibkr-host ib-gateway  # Docker: connect to ib-gateway container
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
        poll_interval_minutes=poll_interval,
        ntfy_topic=ntfy_topic,
        dry_run=dry_run,
        use_ai=ai,
        ai_model=ai_model,
        ai_lookback_years=ai_lookback,
        ibkr_host=ibkr_host,
        ibkr_port=ibkr_port,
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
    1. Connects to IBKR
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
    from aurel2.strategies.mean_reversion import MeanReversionStrategy
    from aurel2.strategies.multi_timeframe import MultiTimeframeTrendStrategy

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

    # Initialize additional strategies for richer signals
    mean_reversion = MeanReversionStrategy()
    multi_timeframe = MultiTimeframeTrendStrategy()

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

        # Build all 3 strategy signals for AI context
        signals = {
            "dual_momentum": {
                "action": det_action,
                "asset_symbol": det_asset,
                "confidence": 0.7,
                "reasoning": det_signal.reason,
            }
        }
        for name, strat in [("mean_reversion", mean_reversion), ("multi_timeframe", multi_timeframe)]:
            try:
                sig = strat.generate_signal(prices=prices_df, calc_date=decision_date, current_holding=current_asset_class)
                action = sig.action.value if hasattr(sig.action, 'value') else str(sig.action)
                asset_sym = None
                if hasattr(sig, 'asset_class') and sig.asset_class and sig.asset_class in ASSET_REGISTRY:
                    asset_sym = ASSET_REGISTRY[sig.asset_class].symbol
                signals[name] = {
                    "action": action,
                    "asset_symbol": asset_sym,
                    "confidence": getattr(sig, 'confidence', 0.8),
                    "reasoning": getattr(sig, 'reasoning', ''),
                }
            except Exception:
                signals[name] = {"action": "hold", "confidence": 0.0, "error": "failed"}

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
        "snapshots.json",
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
