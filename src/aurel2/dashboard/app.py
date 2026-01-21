"""FastAPI dashboard application."""

from datetime import date
from pathlib import Path

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from aurel2.persistence.portfolio import PortfolioStore
from aurel2.data.providers.yahoo import YahooFinanceProvider
from aurel2.data.momentum import calculate_momentum_scores
from aurel2.core.models import Asset, AssetClass

app = FastAPI(title="Aurel2 Dashboard")

# Templates
templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

# Asset definitions
ASSETS = {
    AssetClass.US_STOCKS: Asset(
        symbol="SPY", name="S&P 500 US Stocks",
        asset_class=AssetClass.US_STOCKS, yahoo_symbol="SPY"
    ),
    AssetClass.GLOBAL_STOCKS: Asset(
        symbol="EFA", name="International Developed Markets",
        asset_class=AssetClass.GLOBAL_STOCKS, yahoo_symbol="EFA"
    ),
    AssetClass.BONDS: Asset(
        symbol="AGG", name="US Aggregate Bonds",
        asset_class=AssetClass.BONDS, yahoo_symbol="AGG"
    ),
    AssetClass.CASH: Asset(
        symbol="CASH", name="Cash / Money Market",
        asset_class=AssetClass.CASH
    ),
}

UCITS_MAP = {
    AssetClass.US_STOCKS: {"symbol": "CSPX", "name": "iShares Core S&P 500 UCITS ETF (Acc)", "isin": "IE00B5BMR087"},
    AssetClass.GLOBAL_STOCKS: {"symbol": "VWRA", "name": "Vanguard FTSE All-World UCITS ETF (Acc)", "isin": "IE00BK5BQT80"},
    AssetClass.BONDS: {"symbol": "AGGH", "name": "iShares Core Global Aggregate Bond UCITS ETF (Acc)", "isin": "IE00BDBRDM35"},
    AssetClass.CASH: {"symbol": "CASH", "name": "Cash", "isin": "N/A"},
}

SYMBOL_TO_CLASS = {
    "CSPX": AssetClass.US_STOCKS, "SPY": AssetClass.US_STOCKS,
    "VWRA": AssetClass.GLOBAL_STOCKS, "EFA": AssetClass.GLOBAL_STOCKS,
    "AGGH": AssetClass.BONDS, "AGG": AssetClass.BONDS,
}


def get_momentum_data():
    """Calculate current momentum scores."""
    from dateutil.relativedelta import relativedelta

    check_date = date.today()
    provider = YahooFinanceProvider()
    symbols = [a.yahoo_symbol for a in ASSETS.values() if a.yahoo_symbol]
    start = check_date - relativedelta(months=14)

    prices = provider.get_multi_prices(symbols, start, check_date)

    scores = calculate_momentum_scores(
        prices=prices,
        assets=ASSETS,
        calc_date=check_date,
        lookback_months=12,
        cash_rate=0.04,
    )

    return scores


def get_recommendation(scores, portfolio):
    """Generate trading recommendation based on scores and portfolio."""
    sorted_scores = sorted(scores.items(), key=lambda x: x[1].momentum_12m, reverse=True)
    winner_class, winner_score = sorted_scores[0]

    # Find current holding
    current_holding = None
    current_class = None
    for h in portfolio.holdings:
        if h.symbol.upper() in SYMBOL_TO_CLASS:
            current_holding = h
            current_class = SYMBOL_TO_CLASS[h.symbol.upper()]
            break

    recommendation = {
        "winner_class": winner_class,
        "winner_score": winner_score,
        "winner_ucits": UCITS_MAP[winner_class],
        "current_holding": current_holding,
        "current_class": current_class,
        "action": "HOLD",
        "details": "",
        "tax_warning": None,
    }

    if current_holding is None:
        if winner_score.momentum_12m > 0.04:
            recommendation["action"] = "BUY"
            recommendation["details"] = f"Buy {UCITS_MAP[winner_class]['symbol']} ({winner_score.momentum_12m:.1%} momentum)"
        else:
            recommendation["action"] = "STAY_CASH"
            recommendation["details"] = "All assets underperforming cash"
    else:
        current_score = scores.get(current_class)
        if current_class == winner_class:
            recommendation["action"] = "HOLD"
            recommendation["details"] = f"You own the winner ({winner_score.momentum_12m:.1%} momentum)"
        elif current_score:
            diff = winner_score.momentum_12m - current_score.momentum_12m
            if diff > 0.10:
                recommendation["action"] = "SWITCH"
                recommendation["details"] = f"Sell {current_holding.symbol} → Buy {UCITS_MAP[winner_class]['symbol']} (diff: {diff:.1%})"

                if not current_holding.is_long_term():
                    days_left = current_holding.days_until_long_term()
                    recommendation["tax_warning"] = f"Selling now = 3% tax. Wait {days_left} days for 1% tax."
            else:
                recommendation["action"] = "HOLD"
                recommendation["details"] = f"Difference ({diff:.1%}) below 10% threshold"

    return recommendation


def get_next_rebalance():
    """Get next quarterly rebalance date."""
    today = date.today()
    year = today.year
    month = today.month

    quarter_ends = [(3, 31), (6, 30), (9, 30), (12, 31)]
    for q_month, q_day in quarter_ends:
        if month < q_month or (month == q_month and today.day < q_day):
            return date(year, q_month, q_day)
    return date(year + 1, 3, 31)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard view."""
    store = PortfolioStore()
    portfolio = store.load()
    scores = get_momentum_data()
    recommendation = get_recommendation(scores, portfolio)
    next_rebalance = get_next_rebalance()

    # Prepare momentum data for template
    momentum_data = []
    sorted_scores = sorted(scores.items(), key=lambda x: x[1].momentum_12m, reverse=True)
    for asset_class, score in sorted_scores:
        is_winner = asset_class == recommendation["winner_class"]
        is_held = recommendation["current_class"] == asset_class
        momentum_data.append({
            "asset_class": asset_class.value,
            "name": score.asset.name,
            "momentum": score.momentum_12m,
            "price": score.price,
            "is_winner": is_winner,
            "is_held": is_held,
            "ucits": UCITS_MAP[asset_class],
        })

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "portfolio": portfolio,
        "momentum_data": momentum_data,
        "recommendation": recommendation,
        "next_rebalance": next_rebalance,
        "today": date.today(),
    })


@app.get("/api/momentum")
async def api_momentum():
    """API endpoint for momentum data."""
    scores = get_momentum_data()
    result = {}
    for asset_class, score in scores.items():
        result[asset_class.value] = {
            "momentum": score.momentum_12m,
            "price": score.price,
            "is_positive": score.is_positive,
        }
    return result


@app.get("/api/portfolio")
async def api_portfolio():
    """API endpoint for portfolio data."""
    store = PortfolioStore()
    portfolio = store.load()
    return {
        "cash": portfolio.cash,
        "total_invested": portfolio.total_invested,
        "holdings": [h.to_dict() for h in portfolio.holdings],
    }


@app.post("/api/buy")
async def api_buy(
    symbol: str = Form(...),
    shares: float = Form(...),
    price: float = Form(...),
    broker: str = Form("tradeville"),
):
    """Record a buy."""
    store = PortfolioStore()

    known_etfs = {
        "VWRA": ("Vanguard FTSE All-World UCITS ETF (Acc)", "IE00BK5BQT80"),
        "CSPX": ("iShares Core S&P 500 UCITS ETF (Acc)", "IE00B5BMR087"),
        "AGGH": ("iShares Core Global Aggregate Bond UCITS ETF (Acc)", "IE00BDBRDM35"),
    }

    symbol = symbol.upper()
    name, isin = known_etfs.get(symbol, (symbol, None))

    store.add_holding(
        symbol=symbol,
        name=name,
        shares=shares,
        price=price,
        entry_date=date.today(),
        broker=broker,
        isin=isin,
    )

    return {"status": "ok", "message": f"Recorded {shares} shares of {symbol}"}


@app.post("/api/sell")
async def api_sell(symbol: str = Form(...)):
    """Record a sale."""
    store = PortfolioStore()
    holding = store.remove_holding(symbol)

    if holding:
        return {"status": "ok", "message": f"Removed {symbol} from portfolio"}
    else:
        return {"status": "error", "message": f"No holding found for {symbol}"}


def run_dashboard(host: str = "127.0.0.1", port: int = 8000):
    """Run the dashboard server."""
    import uvicorn
    uvicorn.run(app, host=host, port=port)
