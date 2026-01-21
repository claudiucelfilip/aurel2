"""Base broker interface."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Optional


@dataclass
class BrokerPosition:
    """A position held at the broker."""
    symbol: str
    shares: float
    avg_cost: float
    market_price: float
    market_value: float
    unrealized_pnl: float
    currency: str = "EUR"


@dataclass
class BrokerOrder:
    """An order to be placed."""
    symbol: str
    action: str  # "BUY" or "SELL"
    quantity: float
    order_type: str = "MKT"  # MKT, LMT
    limit_price: Optional[float] = None


@dataclass
class OrderResult:
    """Result of an order execution."""
    order_id: str
    symbol: str
    action: str
    quantity: float
    filled_quantity: float
    avg_fill_price: float
    status: str  # "FILLED", "PARTIAL", "REJECTED", "PENDING"
    message: str = ""


@dataclass
class AccountSummary:
    """Broker account summary."""
    total_value: float
    cash_balance: float
    buying_power: float
    currency: str = "EUR"


class BaseBroker(ABC):
    """Abstract base class for broker integrations."""

    @abstractmethod
    async def connect(self) -> bool:
        """Connect to the broker. Returns True if successful."""
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from the broker."""
        pass

    @abstractmethod
    async def get_account_summary(self) -> AccountSummary:
        """Get account summary (cash, total value, etc.)."""
        pass

    @abstractmethod
    async def get_positions(self) -> list[BrokerPosition]:
        """Get all current positions."""
        pass

    @abstractmethod
    async def get_position(self, symbol: str) -> Optional[BrokerPosition]:
        """Get a specific position by symbol."""
        pass

    @abstractmethod
    async def place_order(self, order: BrokerOrder) -> OrderResult:
        """Place an order. Returns the result."""
        pass

    @abstractmethod
    async def get_market_price(self, symbol: str) -> Optional[float]:
        """Get current market price for a symbol."""
        pass

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Check if connected to broker."""
        pass
