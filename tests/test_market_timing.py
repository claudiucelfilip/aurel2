import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import pytz

from aurel2.broker.alpaca import AlpacaBroker
from aurel2.broker.base import BrokerOrder, MarketClock, MarketPriceSnapshot
from aurel2.config.canonical import live_asset_registry
from aurel2.live.checker import Checker
from aurel2.live.daemon import LiveDaemon
from aurel2.live.pending import PendingDecision, PendingManager
from aurel2.monitor.health_checker import HealthChecker, HealthStatus, LogErrors, ProcessInfo


def live_symbols() -> list[str]:
    return [asset.yahoo_symbol for asset in live_asset_registry().values() if asset.yahoo_symbol]


def snapshot_price(
    symbol: str, observed_at: datetime, price: float = 100.0
) -> MarketPriceSnapshot:
    return MarketPriceSnapshot(
        symbol=symbol,
        price=price,
        observed_at=observed_at,
    )


def checker_with_snapshot(snapshot: dict[str, MarketPriceSnapshot]):
    symbols = live_symbols()
    history = pd.DataFrame(
        {
            "date": [datetime.now().date() - timedelta(days=10)] * len(symbols),
            "close": [90.0] * len(symbols),
            "symbol": symbols,
        }
    )
    checker = Checker.__new__(Checker)
    checker.provider = MagicMock()
    checker.provider.get_multi_prices.return_value = history
    broker = SimpleNamespace(get_market_snapshot=AsyncMock(return_value=snapshot))
    checker.connection = SimpleNamespace(is_connected=True, broker=broker)
    return checker


def test_fetch_prices_appends_one_fresh_full_universe_snapshot():
    now = datetime.now(timezone.utc)
    symbols = live_symbols()
    snapshot = {
        symbol: snapshot_price(symbol, now, 100.0 + i)
        for i, symbol in enumerate(symbols)
    }
    checker = checker_with_snapshot(snapshot)

    prices = asyncio.run(checker._fetch_prices())
    current_date = datetime.now(ZoneInfo("America/New_York")).date()
    current = prices[prices["date"] == current_date]

    assert set(current["symbol"]) == set(symbols)
    assert len(current) == len(symbols)
    assert current.set_index("symbol").loc[symbols[0], "close"] == snapshot[symbols[0]].price


def test_fetch_prices_rejects_missing_quote():
    now = datetime.now(timezone.utc)
    symbols = live_symbols()
    snapshot = {symbol: snapshot_price(symbol, now) for symbol in symbols[:-1]}
    checker = checker_with_snapshot(snapshot)

    with pytest.raises(RuntimeError, match="Missing current prices"):
        asyncio.run(checker._fetch_prices())


def test_fetch_prices_rejects_stale_quote():
    now = datetime.now(timezone.utc)
    symbols = live_symbols()
    snapshot = {symbol: snapshot_price(symbol, now) for symbol in symbols}
    snapshot[symbols[0]] = snapshot_price(symbols[0], now - timedelta(minutes=3))
    checker = checker_with_snapshot(snapshot)

    with pytest.raises(RuntimeError, match="Stale current prices"):
        asyncio.run(checker._fetch_prices())


def test_alpaca_market_snapshot_uses_validated_quote_midpoint():
    now = datetime.now(timezone.utc)
    broker = AlpacaBroker.__new__(AlpacaBroker)
    broker._data_client = MagicMock()
    broker._data_client.get_stock_latest_quote.return_value = {
        "SPY": SimpleNamespace(bid_price=100.0, ask_price=100.2, timestamp=now),
        "EFA": SimpleNamespace(bid_price=102.0, ask_price=101.0, timestamp=now),
    }

    snapshot = asyncio.run(broker.get_market_snapshot(["spy", "efa"]))

    assert snapshot["SPY"] == MarketPriceSnapshot("SPY", 100.1, now)
    assert "EFA" not in snapshot
    broker._data_client.get_stock_latest_quote.assert_called_once()


def test_alpaca_rejects_order_when_regular_session_is_closed():
    now = datetime.now(timezone.utc)
    broker = AlpacaBroker.__new__(AlpacaBroker)
    broker._connected = True
    broker._client = MagicMock()
    broker.get_market_clock = AsyncMock(
        return_value=MarketClock(False, now, now + timedelta(hours=12), now + timedelta(hours=18))
    )

    result = asyncio.run(broker.place_order(BrokerOrder("SPY", "BUY", 1.0)))

    assert result.status == "REJECTED"
    assert "closed" in result.message
    broker._client.submit_order.assert_not_called()


def pending_decision(created_at: datetime, original_price: float = 100.0) -> PendingDecision:
    return PendingDecision(
        id="pending-1",
        created_at=created_at.isoformat(),
        urgency="non_routine",
        action="buy",
        symbol="SPY",
        reasoning="test",
        confidence=0.9,
        original_price=original_price,
    )


def test_pending_decisions_expire_after_ten_minutes(tmp_path):
    manager = PendingManager(str(tmp_path / "pending.json"))
    decision = pending_decision(datetime.now() - timedelta(minutes=11))

    assert decision.timeout_seconds() == 600
    assert manager.validate_decision_still_valid(decision, 100.0)[0] is False


def test_pending_decision_requires_current_price(tmp_path):
    manager = PendingManager(str(tmp_path / "pending.json"))
    decision = pending_decision(datetime.now())

    assert manager.validate_decision_still_valid(decision, 0.0) == (
        False,
        "Current price unavailable",
    )


def test_daemon_defaults_to_new_york_1030(monkeypatch, tmp_path):
    heartbeat = tmp_path / "heartbeat.json"
    monkeypatch.setattr("aurel2.live.daemon.HEARTBEAT_FILE", heartbeat)

    daemon = LiveDaemon()

    assert str(daemon.timezone) == "America/New_York"
    assert daemon.check_time.hour == 10
    assert daemon.check_time.minute == 30


def test_daemon_restores_last_check_across_restart(monkeypatch, tmp_path):
    checked_at = pytz.timezone("America/New_York").localize(datetime(2026, 7, 13, 10, 30))
    heartbeat = tmp_path / "heartbeat.json"
    heartbeat.write_text(json.dumps({"last_check": checked_at.isoformat()}))
    monkeypatch.setattr("aurel2.live.daemon.HEARTBEAT_FILE", heartbeat)

    daemon = LiveDaemon()

    assert daemon._last_check == checked_at
    assert daemon._should_run_check(checked_at + timedelta(hours=1)) is False


def test_health_checker_degrades_on_failed_scheduled_check(tmp_path, monkeypatch):
    heartbeat = tmp_path / "heartbeat.json"
    heartbeat.write_text(json.dumps({
        "timestamp": datetime.now().timestamp(),
        "connected": True,
        "circuit_breaker": {"state": "closed"},
        "last_check": datetime.now().isoformat(),
        "last_check_success": False,
        "last_check_message": "Stale current prices for: SHY",
    }))
    checker = HealthChecker(heartbeat_file=heartbeat, log_file=tmp_path / "daemon.log")
    monkeypatch.setattr(checker, "_check_process", lambda: ProcessInfo(running=True))
    monkeypatch.setattr(checker, "_check_logs", lambda: LogErrors())

    report = checker.check()

    assert report.status is HealthStatus.DEGRADED
    assert report.issues == ["Last scheduled check failed: Stale current prices for: SHY"]
