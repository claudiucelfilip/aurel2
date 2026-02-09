"""FastAPI dashboard application - displays live IBKR positions."""

import os
import json
import asyncio
from collections import Counter
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


def load_pending_decisions() -> list[dict]:
    """Load pending decisions awaiting approval."""
    pending_file = DATA_DIR / "pending_decisions.json"
    if not pending_file.exists():
        # Fall back to project data dir
        pending_file = Path("data/pending_decisions.json")

    if pending_file.exists():
        try:
            data = json.loads(pending_file.read_text())
            decisions = data.get("decisions", {})
            pending = []
            for d in decisions.values():
                if d.get("status") == "pending":
                    # Calculate time remaining
                    created = datetime.fromisoformat(d["created_at"])
                    timeout = timedelta(hours=1)
                    deadline = created + timeout
                    remaining = deadline - datetime.now()
                    if remaining.total_seconds() > 0:
                        mins = int(remaining.total_seconds() // 60)
                        d["time_remaining_mins"] = mins
                    else:
                        d["time_remaining_mins"] = 0
                    pending.append(d)
            return pending
        except Exception:
            return []
    return []


def load_trade_history(page: int = 1, per_page: int = 10) -> dict:
    """Load trade journal and compute summary stats with pagination."""
    journal_file = DATA_DIR / "trade_journal.json"
    if not journal_file.exists():
        journal_file = Path("data/trade_journal.json")

    # Load pending decisions for status cross-reference
    pending_statuses = {}
    pending_file = DATA_DIR / "pending_decisions.json"
    if not pending_file.exists():
        pending_file = Path("data/pending_decisions.json")
    if pending_file.exists():
        try:
            pdata = json.loads(pending_file.read_text())
            for d in pdata.get("decisions", {}).values():
                jid = d.get("journal_decision_id")
                if jid:
                    pending_statuses[jid] = d.get("status", "pending")
                # Also map by decision id itself
                pending_statuses[d["id"]] = d.get("status", "pending")
        except Exception:
            pass

    result = {
        "total_decisions": 0,
        "executed_trades": 0,
        "all_decisions": [],
        "first_account_value": None,
        "latest_account_value": None,
        "page": page,
        "per_page": per_page,
        "total_pages": 1,
    }

    if journal_file.exists():
        try:
            entries = json.loads(journal_file.read_text())
            result["total_decisions"] = len(entries)
            result["executed_trades"] = sum(1 for e in entries if e.get("executed"))

            # Get first and latest account values for overall P&L
            for entry in entries:
                val = entry.get("account_value_before")
                if val and val > 0:
                    if result["first_account_value"] is None:
                        result["first_account_value"] = val
                    result["latest_account_value"] = val

            # All non-hold decisions, newest first
            all_sorted = sorted(
                [e for e in entries if e.get("action") != "hold"],
                key=lambda e: e.get("timestamp", ""),
                reverse=True,
            )

            # Enrich with status and formatted time
            for entry in all_sorted:
                ts = entry.get("timestamp", "")
                if ts:
                    try:
                        dt = datetime.fromisoformat(ts)
                        entry["formatted_time"] = dt.strftime("%b %d, %H:%M")
                    except Exception:
                        entry["formatted_time"] = ts[:16]

                # Compute original deterministic action for AI override display
                if not entry.get("ai_agrees", True):
                    # Derive deterministic action from strategy signals majority
                    sigs = entry.get("strategy_signals", {})
                    actions = [s.get("action", "hold") for s in sigs.values() if isinstance(s, dict)]
                    if actions:
                        det_action = Counter(actions).most_common(1)[0][0]
                    else:
                        det_action = "hold"
                    # Deterministic asset from the majority action's signals
                    det_assets = [s.get("asset_symbol") for s in sigs.values()
                                  if isinstance(s, dict) and s.get("action") == det_action and s.get("asset_symbol")]
                    det_asset = det_assets[0] if det_assets else None
                    entry["deterministic_action"] = det_action
                    entry["deterministic_asset"] = det_asset

                # Determine status
                eid = entry.get("id", "")
                if entry.get("execution_error"):
                    entry["status"] = "failed"
                elif entry.get("executed") and entry.get("shares", 0) > 0:
                    entry["status"] = "executed"
                elif entry.get("executed") or pending_statuses.get(eid) == "executed":
                    entry["status"] = "finalized"
                elif eid in pending_statuses:
                    entry["status"] = pending_statuses[eid]
                elif entry.get("action") == "hold":
                    entry["status"] = "hold"
                else:
                    entry["status"] = "no_action"

            # Paginate
            total = len(all_sorted)
            result["total_pages"] = max(1, (total + per_page - 1) // per_page)
            start = (page - 1) * per_page
            result["all_decisions"] = all_sorted[start:start + per_page]
        except Exception:
            pass

    return result


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

            # Filter out 0-share positions (closed positions still reported by IBKR)
            positions_data = [p for p in positions_data if p["shares"] != 0]

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
async def dashboard(request: Request, period: str = "1m", page: int = 1):
    """Main dashboard view - shows live IBKR positions."""
    if period not in PERIOD_DAYS:
        period = "1m"
    if page < 1:
        page = 1

    data = await get_ibkr_data()
    heartbeat = get_heartbeat()
    snapshots = load_snapshots()
    pending_decisions = load_pending_decisions()
    trade_history = load_trade_history(page=page)

    # Get comparison chart data
    positions = data.get("positions", [])
    account = data.get("account")
    chart_data = get_comparison_chart_data(positions, account, period=period) if account else None

    # Add first trade date info for display
    first_trade_date = get_first_trade_date()

    # Calculate overall gain/loss
    overall_gain = None
    overall_gain_pct = None
    if account and trade_history["first_account_value"]:
        starting = trade_history["first_account_value"]
        current = account["total_value"]
        overall_gain = current - starting
        overall_gain_pct = ((current / starting) - 1) * 100 if starting else 0

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
        "pending_decisions": pending_decisions,
        "trade_history": trade_history,
        "overall_gain": overall_gain,
        "overall_gain_pct": overall_gain_pct,
        "period": period,
        "page": page,
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


PERIOD_DAYS = {
    "1w": 7,
    "1m": 30,
    "6m": 180,
    "1y": 365,
    "5y": 1825,
}


def get_comparison_chart_data(positions: list[dict], account: dict, period: str = "1m") -> dict:
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

    # Determine chart start date from period
    days = PERIOD_DAYS.get(period, 30)
    start_date = date.today() - timedelta(days=days)

    # For shorter periods, clamp to first trade date if we have one
    first_trade = get_first_trade_date()
    if first_trade and start_date < first_trade - timedelta(days=3):
        # For long periods, start a few days before first trade
        pass  # keep the requested start_date
    elif first_trade:
        # For short periods, allow going back before first trade to show cash period
        start_date = min(start_date, first_trade - timedelta(days=3))

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
async def api_chart(period: str = "1m"):
    """API endpoint for chart data."""
    if period not in PERIOD_DAYS:
        period = "1m"
    data = await get_ibkr_data()
    positions = data.get("positions", [])
    account = data.get("account")
    return get_comparison_chart_data(positions, account, period=period) if account else {}


BACKTEST_COMPARISON_FILE = DATA_DIR / "backtest_comparison.json"
BACKTEST_COMPARISON_FILE_ALT = Path("data/backtest_comparison.json")


@app.get("/api/backtest-comparison")
async def api_backtest_comparison():
    """Return pre-computed 5y and 10y backtest results.

    Reads from data/backtest_comparison.json, generated by:
        python -m aurel2.engine.backtest
    """
    for path in [BACKTEST_COMPARISON_FILE, BACKTEST_COMPARISON_FILE_ALT]:
        if path.exists():
            try:
                return json.loads(path.read_text())
            except Exception:
                continue
    return {"error": "No backtest data. Run: python -m aurel2.engine.backtest"}


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
