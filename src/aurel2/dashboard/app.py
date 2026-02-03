"""FastAPI dashboard application - displays live IBKR positions."""

import os
import json
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import yfinance as yf

app = FastAPI(title="Aurel2 Dashboard")

# Templates
templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

# IBKR connection settings from environment
IBKR_HOST = os.environ.get("IBKR_HOST", "127.0.0.1")
IBKR_PORT = int(os.environ.get("IBKR_PORT", "4002"))

# Data directory
DATA_DIR = Path(os.environ.get("AUREL2_DATA_DIR", str(Path.home() / ".aurel2")))
SNAPSHOTS_FILE = DATA_DIR / "snapshots.json"

# Thread pool for running blocking IBKR calls
executor = ThreadPoolExecutor(max_workers=2)


def load_snapshots() -> list[dict]:
    """Load historical portfolio snapshots."""
    if SNAPSHOTS_FILE.exists():
        try:
            return json.loads(SNAPSHOTS_FILE.read_text())
        except Exception:
            return []
    return []


def save_snapshot(total_value: float, cash: float, positions_value: float):
    """Save today's snapshot (one per day)."""
    snapshots = load_snapshots()
    today = date.today().isoformat()

    # Update or add today's snapshot
    for snap in snapshots:
        if snap["date"] == today:
            snap["total_value"] = total_value
            snap["cash"] = cash
            snap["positions_value"] = positions_value
            break
    else:
        snapshots.append({
            "date": today,
            "total_value": total_value,
            "cash": cash,
            "positions_value": positions_value,
        })

    # Keep last 365 days
    snapshots = sorted(snapshots, key=lambda x: x["date"])[-365:]

    SNAPSHOTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOTS_FILE.write_text(json.dumps(snapshots, indent=2))


def get_heartbeat() -> dict | None:
    """Get daemon heartbeat status."""
    heartbeat_file = DATA_DIR / "heartbeat.json"
    if heartbeat_file.exists():
        try:
            data = json.loads(heartbeat_file.read_text())
            # Calculate how long ago
            ts = data.get("timestamp", 0)
            ago = int(datetime.now().timestamp() - ts)
            data["seconds_ago"] = ago
            data["status"] = "healthy" if ago < 300 else "stale"
            return data
        except Exception:
            pass
    return None


def _sync_get_ibkr_data() -> dict:
    """Fetch positions and account data directly from IBKR (runs in its own event loop)."""

    async def _fetch():
        from aurel2.broker.ibkr import IBKRBroker
        import random

        # Retry up to 3 times with different client IDs
        last_error = None
        for attempt in range(3):
            try:
                client_id = 990 + random.randint(0, 9)
                broker = IBKRBroker(host=IBKR_HOST, port=IBKR_PORT, client_id=client_id)
                connected = await broker.connect()

                if connected:
                    break
                last_error = "Could not connect to IBKR"
            except Exception as e:
                last_error = str(e)

            # Wait before retry
            if attempt < 2:
                await asyncio.sleep(2)
        else:
            return {"connected": False, "error": last_error or "Connection failed after 3 attempts"}

        try:
            positions = await broker.get_positions()
            account = await broker.get_account_summary()

            # Get live prices from Yahoo for accurate P&L
            positions_data = []
            for p in positions:
                # Get current price from Yahoo Finance for accurate P&L
                try:
                    ticker = yf.Ticker(p.symbol)
                    current_price = ticker.info.get("regularMarketPrice") or ticker.info.get("previousClose") or p.market_price
                except Exception:
                    current_price = p.market_price

                market_value = p.shares * current_price
                cost_basis = p.shares * p.avg_cost
                unrealized_pnl = market_value - cost_basis
                pnl_pct = ((current_price / p.avg_cost) - 1) * 100 if p.avg_cost else 0

                positions_data.append({
                    "symbol": p.symbol,
                    "shares": p.shares,
                    "avg_cost": p.avg_cost,
                    "market_price": current_price,
                    "market_value": market_value,
                    "unrealized_pnl": unrealized_pnl,
                    "pnl_pct": pnl_pct,
                })

            account_data = {
                "total_value": account.total_value if account else 0,
                "cash_balance": account.cash_balance if account else 0,
                "buying_power": account.buying_power if account else 0,
            } if account else None

            # Save daily snapshot
            if account_data:
                positions_value = sum(p["market_value"] for p in positions_data)
                save_snapshot(
                    account_data["total_value"],
                    account_data["cash_balance"],
                    positions_value,
                )

            return {
                "connected": True,
                "positions": positions_data,
                "account": account_data,
            }
        finally:
            await broker.disconnect()

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(asyncio.wait_for(_fetch(), timeout=30))
        finally:
            loop.close()
    except asyncio.TimeoutError:
        return {"connected": False, "error": "Connection timed out"}
    except Exception as e:
        return {"connected": False, "error": str(e)}


async def get_ibkr_data() -> dict:
    """Async wrapper that runs IBKR fetch in a separate thread."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, _sync_get_ibkr_data)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard view - shows live IBKR positions."""
    data = await get_ibkr_data()
    heartbeat = get_heartbeat()
    snapshots = load_snapshots()

    # Get comparison chart data
    positions = data.get("positions", [])
    account = data.get("account")
    chart_data = get_comparison_chart_data(positions, account) if account else None

    # Add first trade date info for display
    first_trade_date = get_first_trade_date()

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "today": date.today(),
        "connected": data.get("connected", False),
        "error": data.get("error"),
        "positions": positions,
        "account": data.get("account"),
        "heartbeat": heartbeat,
        "snapshots": snapshots,
        "chart_data": chart_data,
        "first_trade_date": first_trade_date,
    })


@app.get("/api/positions")
async def api_positions():
    """API endpoint for live IBKR positions."""
    return await get_ibkr_data()


@app.get("/api/snapshots")
async def api_snapshots():
    """API endpoint for historical snapshots."""
    return load_snapshots()


def get_first_trade_date() -> date | None:
    """Get the date of the first executed trade from the journal."""
    journal_file = DATA_DIR / "trade_journal.json"
    if not journal_file.exists():
        # Fall back to data directory in project
        journal_file = Path("data/trade_journal.json")

    if journal_file.exists():
        try:
            entries = json.loads(journal_file.read_text())
            for entry in entries:
                if entry.get("executed") and entry.get("action") == "buy":
                    ts = entry.get("timestamp", "")
                    if ts:
                        return datetime.fromisoformat(ts).date()
        except Exception:
            pass
    return None


def get_comparison_chart_data(positions: list[dict], account: dict) -> dict:
    """Get chart data showing actual portfolio value vs benchmarks.

    Shows:
    - Flat cash period before first trade
    - Actual portfolio performance after trade
    - SPY benchmark (what if we'd bought SPY instead)
    - Position benchmark (e.g., GLD - buy and hold from start)
    """
    if not account:
        return {"dates": [], "portfolio": [], "spy": [], "position": []}

    current_total = account.get("total_value", 0)
    current_cash = account.get("cash_balance", 0)

    # Determine chart start date - from first trade or 30 days back
    first_trade = get_first_trade_date()
    if first_trade:
        # Start a few days before first trade to show cash period
        start_date = first_trade - timedelta(days=3)
    else:
        start_date = date.today() - timedelta(days=30)

    end_date = date.today()

    # Calculate starting capital (cost basis of positions + current cash)
    starting_capital = current_cash
    for p in positions:
        starting_capital += p["shares"] * p["avg_cost"]

    # Get SPY data for benchmark
    try:
        spy = yf.Ticker("SPY")
        spy_hist = spy.history(start=start_date.isoformat(), end=end_date.isoformat())
        if spy_hist.empty:
            return {"dates": [], "portfolio": [], "spy": [], "position": []}
    except Exception:
        return {"dates": [], "portfolio": [], "spy": [], "position": []}

    # Get historical prices for current positions
    position_hist = {}
    main_position_symbol = None
    for p in positions:
        symbol = p["symbol"]
        if main_position_symbol is None:
            main_position_symbol = symbol  # Use first/largest position for benchmark
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(start=start_date.isoformat(), end=end_date.isoformat())
            if not hist.empty:
                position_hist[symbol] = {
                    "prices": hist["Close"],
                    "shares": p["shares"],
                    "avg_cost": p["avg_cost"],
                }
        except Exception:
            pass

    # Calculate values for each day
    dates = []
    portfolio_values = []
    spy_values = []
    position_values = []  # Buy-and-hold the main position from day 1

    spy_start = float(spy_hist["Close"].iloc[0])

    # Get position start price for buy-and-hold benchmark
    position_start = None
    if main_position_symbol and main_position_symbol in position_hist:
        pos_prices = position_hist[main_position_symbol]["prices"]
        if len(pos_prices) > 0:
            position_start = float(pos_prices.iloc[0])

    for idx, row in spy_hist.iterrows():
        current_date = idx.date() if hasattr(idx, 'date') else idx
        date_str = idx.strftime("%Y-%m-%d")
        dates.append(date_str)

        # Before first trade: portfolio is all cash
        if first_trade and current_date < first_trade:
            portfolio_val = starting_capital
        else:
            # After first trade: calculate actual portfolio value
            portfolio_val = current_cash
            for symbol, data in position_hist.items():
                prices = data["prices"]
                shares = data["shares"]

                if idx in prices.index:
                    price = float(prices[idx])
                else:
                    earlier = prices[prices.index <= idx]
                    price = float(earlier.iloc[-1]) if len(earlier) > 0 else data["avg_cost"]

                portfolio_val += shares * price

        portfolio_values.append(round(portfolio_val, 0))

        # SPY benchmark: what if we had invested starting_capital in SPY from day 1
        spy_price = float(row["Close"])
        spy_val = (spy_price / spy_start) * starting_capital
        spy_values.append(round(spy_val, 0))

        # Position benchmark: what if we had bought the main position from day 1
        if position_start and main_position_symbol in position_hist:
            pos_prices = position_hist[main_position_symbol]["prices"]
            if idx in pos_prices.index:
                pos_price = float(pos_prices[idx])
            else:
                earlier = pos_prices[pos_prices.index <= idx]
                pos_price = float(earlier.iloc[-1]) if len(earlier) > 0 else position_start
            pos_val = (pos_price / position_start) * starting_capital
            position_values.append(round(pos_val, 0))
        else:
            position_values.append(round(starting_capital, 0))

    # Anchor the last portfolio point to the real IBKR account value
    if portfolio_values and current_total > 0:
        portfolio_values[-1] = round(current_total, 0)

    return {
        "dates": dates,
        "portfolio": portfolio_values,
        "spy": spy_values,
        "position": position_values,
        "position_symbol": main_position_symbol,
        "starting_value": round(starting_capital, 0),
        "current_value": round(current_total, 0),
        "first_trade_date": first_trade.isoformat() if first_trade else None,
    }


@app.get("/api/chart")
async def api_chart():
    """API endpoint for chart data."""
    data = await get_ibkr_data()
    positions = data.get("positions", [])
    account = data.get("account")
    return get_comparison_chart_data(positions, account) if account else {}


@app.get("/api/status")
async def api_status():
    """API endpoint for daemon status."""
    return {
        "heartbeat": get_heartbeat(),
        "snapshots_count": len(load_snapshots()),
    }


def run_dashboard(host: str = "127.0.0.1", port: int = 8000):
    """Run the dashboard server."""
    import uvicorn
    uvicorn.run(app, host=host, port=port)
