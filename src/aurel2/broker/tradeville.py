"""
TradeVille API integration for portfolio sync.

NOTE: TradeVille API requires signing an addendum to your contract.
Contact: [email protected] or use platform chat.

The API is read-only for live accounts (no order placement).
This integration syncs your portfolio to Aurel2 for tracking.

API Documentation: Request from TradeVille after signing addendum.
"""

import json
import hashlib
from datetime import date, datetime
from typing import Optional
from pathlib import Path

import structlog

logger = structlog.get_logger()

# Try to import httpx for async HTTP
try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False


class TradeVilleAPI:
    """
    TradeVille API client for portfolio sync.

    Usage:
        api = TradeVilleAPI(username="your_username", password="your_password")
        await api.login()
        positions = await api.get_positions()
        await api.logout()

    Note: You need to request API access from TradeVille first.
    """

    BASE_URL = "https://api.tradeville.ro"

    def __init__(
        self,
        username: str = None,
        password: str = None,
        config_path: Path = None,
    ):
        if not HAS_HTTPX:
            raise ImportError("httpx is required. Install with: pip install httpx")

        # Load credentials from config or parameters
        self.username = username
        self.password = password

        if config_path and config_path.exists():
            with open(config_path) as f:
                config = json.load(f)
                self.username = self.username or config.get("username")
                self.password = self.password or config.get("password")

        self.session_token: Optional[str] = None
        self.client = httpx.AsyncClient(timeout=30.0)

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()

    async def login(self) -> bool:
        """
        Authenticate with TradeVille API.

        Returns True if successful.
        """
        if not self.username or not self.password:
            raise ValueError("Username and password required")

        try:
            # Note: Actual endpoint may differ - update after getting API docs
            response = await self.client.post(
                f"{self.BASE_URL}/auth/login",
                json={
                    "username": self.username,
                    "password": self.password,
                },
            )

            if response.status_code == 200:
                data = response.json()
                self.session_token = data.get("token") or data.get("sessionId")
                logger.info("tradeville_login_success")
                return True
            else:
                logger.error("tradeville_login_failed", status=response.status_code)
                return False

        except Exception as e:
            logger.error("tradeville_login_error", error=str(e))
            return False

    async def logout(self) -> None:
        """Logout and clear session."""
        if self.session_token:
            try:
                await self.client.post(
                    f"{self.BASE_URL}/auth/logout",
                    headers=self._auth_headers(),
                )
            except Exception:
                pass
            self.session_token = None

    def _auth_headers(self) -> dict:
        """Get authentication headers."""
        if self.session_token:
            return {"Authorization": f"Bearer {self.session_token}"}
        return {}

    async def get_positions(self) -> list[dict]:
        """
        Get current portfolio positions.

        Returns list of positions with:
        - symbol: str
        - name: str
        - quantity: float
        - avg_price: float
        - current_price: float
        - market_value: float
        - pnl: float
        - pnl_percent: float
        """
        if not self.session_token:
            raise ConnectionError("Not logged in. Call login() first.")

        try:
            # Note: Actual endpoint may differ
            response = await self.client.get(
                f"{self.BASE_URL}/portfolio/positions",
                headers=self._auth_headers(),
            )

            if response.status_code == 200:
                data = response.json()
                # Transform to standard format
                positions = []
                for item in data.get("positions", data):
                    positions.append({
                        "symbol": item.get("symbol") or item.get("ticker"),
                        "name": item.get("name") or item.get("description"),
                        "quantity": float(item.get("quantity") or item.get("qty") or 0),
                        "avg_price": float(item.get("avgPrice") or item.get("avg_price") or 0),
                        "current_price": float(item.get("currentPrice") or item.get("price") or 0),
                        "market_value": float(item.get("marketValue") or item.get("value") or 0),
                        "pnl": float(item.get("pnl") or item.get("profit") or 0),
                        "pnl_percent": float(item.get("pnlPercent") or item.get("profit_pct") or 0),
                        "currency": item.get("currency", "EUR"),
                    })
                return positions
            else:
                logger.error("get_positions_failed", status=response.status_code)
                return []

        except Exception as e:
            logger.error("get_positions_error", error=str(e))
            return []

    async def get_account_summary(self) -> dict:
        """
        Get account summary.

        Returns:
        - total_value: float
        - cash_balance: float
        - invested_value: float
        - pnl: float
        - currency: str
        """
        if not self.session_token:
            raise ConnectionError("Not logged in")

        try:
            response = await self.client.get(
                f"{self.BASE_URL}/account/summary",
                headers=self._auth_headers(),
            )

            if response.status_code == 200:
                data = response.json()
                return {
                    "total_value": float(data.get("totalValue") or data.get("total") or 0),
                    "cash_balance": float(data.get("cashBalance") or data.get("cash") or 0),
                    "invested_value": float(data.get("investedValue") or data.get("invested") or 0),
                    "pnl": float(data.get("pnl") or data.get("profit") or 0),
                    "currency": data.get("currency", "EUR"),
                }
            else:
                return {}

        except Exception as e:
            logger.error("get_account_summary_error", error=str(e))
            return {}

    async def get_transactions(
        self,
        start_date: date = None,
        end_date: date = None,
    ) -> list[dict]:
        """
        Get transaction history.

        Returns list of transactions with:
        - date: str
        - symbol: str
        - action: str (BUY/SELL)
        - quantity: float
        - price: float
        - value: float
        - commission: float
        """
        if not self.session_token:
            raise ConnectionError("Not logged in")

        params = {}
        if start_date:
            params["startDate"] = start_date.isoformat()
        if end_date:
            params["endDate"] = end_date.isoformat()

        try:
            response = await self.client.get(
                f"{self.BASE_URL}/transactions",
                headers=self._auth_headers(),
                params=params,
            )

            if response.status_code == 200:
                data = response.json()
                transactions = []
                for item in data.get("transactions", data):
                    transactions.append({
                        "date": item.get("date") or item.get("tradeDate"),
                        "symbol": item.get("symbol") or item.get("ticker"),
                        "action": item.get("action") or item.get("side"),
                        "quantity": float(item.get("quantity") or item.get("qty") or 0),
                        "price": float(item.get("price") or 0),
                        "value": float(item.get("value") or item.get("amount") or 0),
                        "commission": float(item.get("commission") or item.get("fee") or 0),
                    })
                return transactions
            else:
                return []

        except Exception as e:
            logger.error("get_transactions_error", error=str(e))
            return []

    async def get_quote(self, symbol: str) -> Optional[dict]:
        """
        Get current quote for a symbol.

        Returns:
        - symbol: str
        - price: float
        - change: float
        - change_percent: float
        - volume: int
        - timestamp: str
        """
        try:
            response = await self.client.get(
                f"{self.BASE_URL}/market/quote/{symbol}",
                headers=self._auth_headers(),
            )

            if response.status_code == 200:
                data = response.json()
                return {
                    "symbol": symbol,
                    "price": float(data.get("price") or data.get("last") or 0),
                    "change": float(data.get("change") or 0),
                    "change_percent": float(data.get("changePercent") or data.get("pct") or 0),
                    "volume": int(data.get("volume") or 0),
                    "timestamp": data.get("timestamp") or data.get("time"),
                }
            else:
                return None

        except Exception as e:
            logger.error("get_quote_error", symbol=symbol, error=str(e))
            return None


class TradeVilleSync:
    """
    Sync TradeVille portfolio to Aurel2.

    This pulls your positions from TradeVille and updates
    the local portfolio tracking.
    """

    def __init__(self, api: TradeVilleAPI):
        self.api = api

    async def sync_to_portfolio(self) -> dict:
        """
        Sync TradeVille positions to local portfolio.

        Returns summary of what was synced.
        """
        from aurel2.persistence.portfolio import PortfolioStore, Holding

        positions = await self.api.get_positions()
        account = await self.api.get_account_summary()

        store = PortfolioStore()
        portfolio = store.load()

        # Clear existing holdings and replace with TradeVille data
        synced = []
        for pos in positions:
            # Try to find entry date from transactions
            # For now, use today as placeholder
            holding = Holding(
                symbol=pos["symbol"],
                name=pos["name"],
                shares=pos["quantity"],
                entry_price=pos["avg_price"],
                entry_date=date.today(),  # Would need transaction history for actual date
                broker="tradeville",
            )
            synced.append(holding)

        portfolio.holdings = synced
        portfolio.cash = account.get("cash_balance", 0)
        store.save(portfolio)

        logger.info(
            "tradeville_sync_complete",
            positions=len(synced),
            cash=portfolio.cash,
        )

        return {
            "positions_synced": len(synced),
            "cash_balance": portfolio.cash,
            "total_value": account.get("total_value", 0),
        }
