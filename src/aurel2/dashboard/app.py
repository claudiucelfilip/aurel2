"""FastAPI dashboard application - displays live broker positions."""

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

app = FastAPI(title="Aurel2 Dashboard")

# Templates
templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

# Alpaca connection settings from environment
APCA_API_KEY = os.environ.get("APCA_API_KEY_ID", "")
APCA_API_SECRET = os.environ.get("APCA_API_SECRET_KEY", "")

# Data directory
DATA_DIR = Path(os.environ.get("AUREL2_DATA_DIR", str(Path.home() / ".aurel2")))

# Trading mode for data partitioning (paper/live)
TRADING_MODE = os.environ.get("TRADING_MODE", "paper")
MODE_DATA_DIR = Path(f"data/{TRADING_MODE}")

# Snapshots stored on persistent volume so they survive container rebuilds
SNAPSHOTS_FILE = DATA_DIR / TRADING_MODE / "snapshots.json"

# Thread pool for running blocking broker calls
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
    pending_file = MODE_DATA_DIR / "pending_decisions.json"

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
    journal_file = MODE_DATA_DIR / "trade_journal.json"

    # Load pending decisions for status cross-reference
    pending_statuses = {}
    pending_file = MODE_DATA_DIR / "pending_decisions.json"
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
        "page": page,
        "per_page": per_page,
        "total_pages": 1,
    }

    if journal_file.exists():
        try:
            entries = json.loads(journal_file.read_text())
            result["total_decisions"] = len(entries)
            result["executed_trades"] = sum(1 for e in entries if e.get("executed"))

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


def _sync_get_broker_data() -> dict:
    """Fetch positions and account data from Alpaca (runs in its own event loop)."""

    async def _fetch():
        from aurel2.broker.alpaca import AlpacaBroker

        if not APCA_API_KEY or not APCA_API_SECRET:
            return {"connected": False, "error": "Alpaca API credentials not configured"}

        broker = AlpacaBroker(
            api_key=APCA_API_KEY,
            api_secret=APCA_API_SECRET,
            paper=(TRADING_MODE == "paper"),
        )

        try:
            connected = await broker.connect()
            if not connected:
                return {"connected": False, "error": "Could not connect to Alpaca"}

            positions = await broker.get_positions()
            account = await broker.get_account_summary()

            positions_data = []
            for p in positions:
                if p.shares == 0:
                    continue
                positions_data.append({
                    "symbol": p.symbol,
                    "shares": p.shares,
                    "avg_cost": p.avg_cost,
                    "market_price": p.market_price,
                    "market_value": p.market_value,
                    "unrealized_pnl": p.unrealized_pnl,
                    "pnl_pct": p.unrealized_pnl_pct,
                })

            total_pnl = sum(p["unrealized_pnl"] for p in positions_data)
            total_cost = sum(p["market_value"] - p["unrealized_pnl"] for p in positions_data)
            total_pnl_pct = (total_pnl / total_cost * 100) if total_cost else 0

            account_data = {
                "total_value": account.total_value if account else 0,
                "cash_balance": account.cash_balance if account else 0,
                "buying_power": account.buying_power if account else 0,
                "unrealized_pnl": total_pnl,
                "unrealized_pnl_pct": total_pnl_pct,
                "gross_position_value": account.gross_position_value if account else 0,
            } if account else None

            # Save daily snapshot
            if account_data:
                save_snapshot(
                    account_data["total_value"],
                    account_data["cash_balance"],
                    account_data["gross_position_value"],
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


async def get_broker_data() -> dict:
    """Async wrapper that runs broker fetch in a separate thread."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, _sync_get_broker_data)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, period: str = "1m", page: int = 1):
    """Main dashboard view - shows live broker positions."""
    if period not in PERIOD_DAYS:
        period = "1m"
    if page < 1:
        page = 1

    data = await get_broker_data()
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
        "period": period,
        "page": page,
    })


@app.get("/api/positions")
async def api_positions():
    """API endpoint for live broker positions."""
    return await get_broker_data()


@app.get("/api/snapshots")
async def api_snapshots():
    """API endpoint for historical snapshots."""
    return load_snapshots()


def get_first_trade_date() -> date | None:
    """Get the date of the first executed trade from the journal."""
    journal_file = MODE_DATA_DIR / "trade_journal.json"

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
    """Get chart data using local snapshots + current account data.

    Deliberately avoids external market-data calls so dashboard remains responsive
    and deterministic even when network access is unavailable.
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

    snapshots = load_snapshots()
    filtered = []
    for snap in snapshots:
        try:
            snap_date = date.fromisoformat(snap["date"])
        except Exception:
            continue
        if start_date <= snap_date <= end_date:
            filtered.append((snap_date, float(snap.get("total_value", 0))))

    filtered.sort(key=lambda x: x[0])

    # Calculate values from local snapshots only
    dates = []
    portfolio_values = []
    for snap_date, total_value in filtered:
        dates.append(snap_date.isoformat())
        portfolio_values.append(round(total_value, 0))

    # Ensure there is always at least one point
    if not dates:
        dates = [end_date.isoformat()]
        portfolio_values = [round(current_total, 0)]

    # Anchor latest point to live account value
    if dates[-1] == end_date.isoformat():
        portfolio_values[-1] = round(current_total, 0)
    else:
        dates.append(end_date.isoformat())
        portfolio_values.append(round(current_total, 0))

    return {
        "dates": dates,
        "portfolio": portfolio_values,  # snapshot-based equity curve
        "spy": [],  # Disabled: dashboard no longer fetches Yahoo
        "position": [],  # Disabled: dashboard no longer fetches Yahoo
        "position_symbol": None,
        "starting_value": round(starting_capital, 0),
        "current_value": round(current_total, 0),
        "first_trade_date": first_trade.isoformat() if first_trade else None,
    }


@app.get("/api/chart")
async def api_chart(period: str = "1m"):
    """API endpoint for chart data."""
    if period not in PERIOD_DAYS:
        period = "1m"
    data = await get_broker_data()
    positions = data.get("positions", [])
    account = data.get("account")
    return get_comparison_chart_data(positions, account, period=period) if account else {}


BACKTEST_COMPARISON_FILE = Path("/app/host-data/backtest_comparison.json")
BACKTEST_COMPARISON_FILE_FALLBACK = Path("data/backtest_comparison.json")


@app.get("/api/backtest-comparison")
async def api_backtest_comparison():
    """Return pre-computed backtest results.

    In Docker: reads from bind-mounted /app/host-data/ (single source of truth).
    Locally: falls back to data/backtest_comparison.json.
    Generated by: python -m aurel2.engine.backtest
    """
    for path in [BACKTEST_COMPARISON_FILE, BACKTEST_COMPARISON_FILE_FALLBACK]:
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
