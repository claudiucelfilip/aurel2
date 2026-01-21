"""Portfolio persistence - tracks your holdings."""

import json
from dataclasses import dataclass, asdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Optional


@dataclass
class Holding:
    """A position in the portfolio."""
    symbol: str  # e.g., "VWRA" or "CSPX"
    name: str
    shares: float
    entry_price: float
    entry_date: date
    broker: str  # "tradeville", "ibkr_eu", "ibkr_us"
    isin: Optional[str] = None

    @property
    def cost_basis(self) -> float:
        """Total cost of position."""
        return self.shares * self.entry_price

    def days_held(self, as_of: date = None) -> int:
        """Number of days position has been held."""
        as_of = as_of or date.today()
        return (as_of - self.entry_date).days

    def is_long_term(self, as_of: date = None) -> bool:
        """True if held >365 days (qualifies for 1% Romanian tax)."""
        return self.days_held(as_of) > 365

    def days_until_long_term(self, as_of: date = None) -> int:
        """Days remaining until 365-day threshold."""
        days = 365 - self.days_held(as_of)
        return max(0, days)

    def tax_rate(self, broker: str = None) -> float:
        """Estimated tax rate based on broker and holding period."""
        broker = broker or self.broker
        if broker in ("tradeville", "bt_capital", "xstation"):
            # Romanian broker
            if self.is_long_term():
                return 0.01  # 1%
            else:
                return 0.03  # 3%
        else:
            # Foreign broker (IBKR, etc.)
            return 0.16  # 16%

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON storage."""
        return {
            "symbol": self.symbol,
            "name": self.name,
            "shares": self.shares,
            "entry_price": self.entry_price,
            "entry_date": self.entry_date.isoformat(),
            "broker": self.broker,
            "isin": self.isin,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Holding":
        """Create from dictionary."""
        return cls(
            symbol=data["symbol"],
            name=data["name"],
            shares=data["shares"],
            entry_price=data["entry_price"],
            entry_date=date.fromisoformat(data["entry_date"]),
            broker=data["broker"],
            isin=data.get("isin"),
        )


@dataclass
class Portfolio:
    """User's portfolio state."""
    cash: float  # Available cash in EUR
    holdings: list[Holding]
    last_updated: date

    def get_holding(self, symbol: str) -> Optional[Holding]:
        """Get holding by symbol."""
        for h in self.holdings:
            if h.symbol.upper() == symbol.upper():
                return h
        return None

    @property
    def total_invested(self) -> float:
        """Total value of all holdings at cost."""
        return sum(h.cost_basis for h in self.holdings)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON storage."""
        return {
            "cash": self.cash,
            "holdings": [h.to_dict() for h in self.holdings],
            "last_updated": self.last_updated.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Portfolio":
        """Create from dictionary."""
        return cls(
            cash=data["cash"],
            holdings=[Holding.from_dict(h) for h in data["holdings"]],
            last_updated=date.fromisoformat(data["last_updated"]),
        )

    @classmethod
    def empty(cls) -> "Portfolio":
        """Create empty portfolio."""
        return cls(cash=0.0, holdings=[], last_updated=date.today())


class PortfolioStore:
    """Persists portfolio to local JSON file."""

    def __init__(self, path: Path = None):
        self.path = path or Path.home() / ".aurel2" / "portfolio.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> Portfolio:
        """Load portfolio from disk."""
        if not self.path.exists():
            return Portfolio.empty()

        with open(self.path) as f:
            data = json.load(f)
        return Portfolio.from_dict(data)

    def save(self, portfolio: Portfolio) -> None:
        """Save portfolio to disk."""
        portfolio.last_updated = date.today()
        with open(self.path, "w") as f:
            json.dump(portfolio.to_dict(), f, indent=2)

    def add_holding(
        self,
        symbol: str,
        name: str,
        shares: float,
        price: float,
        entry_date: date,
        broker: str,
        isin: str = None,
    ) -> Holding:
        """Add a new holding to portfolio."""
        portfolio = self.load()

        holding = Holding(
            symbol=symbol.upper(),
            name=name,
            shares=shares,
            entry_price=price,
            entry_date=entry_date,
            broker=broker.lower(),
            isin=isin,
        )
        portfolio.holdings.append(holding)
        self.save(portfolio)
        return holding

    def remove_holding(self, symbol: str) -> Optional[Holding]:
        """Remove a holding (when sold)."""
        portfolio = self.load()
        holding = portfolio.get_holding(symbol)

        if holding:
            portfolio.holdings.remove(holding)
            self.save(portfolio)

        return holding

    def set_cash(self, amount: float) -> None:
        """Update cash balance."""
        portfolio = self.load()
        portfolio.cash = amount
        self.save(portfolio)

    def clear(self) -> None:
        """Clear all holdings."""
        self.save(Portfolio.empty())
