from datetime import date

import pandas as pd
from dateutil.relativedelta import relativedelta

from aurel2.agent.orchestrator import AgentDecision, DecisionType, Urgency
from aurel2.core.assets import ASSET_REGISTRY
from aurel2.core.models import AssetClass, SignalAction
from aurel2.live.checker import Checker, _decision_display_asset
from aurel2.live.journal import TradeJournal
from aurel2.strategies.dual_momentum import DualMomentumStrategy


def test_decision_display_asset_for_hold_uses_current_holding():
    decision = AgentDecision(
        decision_type=DecisionType.NON_ROUTINE,
        action=SignalAction.HOLD,
        asset_symbol="XLE",
        reasoning="hold current position",
        confidence=0.7,
        strategy_signals={},
        requires_approval=True,
        timeout_hours=8.0,
        urgency=Urgency.MEDIUM,
    )

    assert _decision_display_asset(decision, "GLD") == "GLD"


def test_decision_display_asset_for_hold_uses_cash_when_flat():
    decision = AgentDecision(
        decision_type=DecisionType.NON_ROUTINE,
        action=SignalAction.HOLD,
        asset_symbol="XLE",
        reasoning="hold current position",
        confidence=0.7,
        strategy_signals={},
        requires_approval=True,
        timeout_hours=8.0,
        urgency=Urgency.MEDIUM,
    )

    assert _decision_display_asset(decision, None) == "cash"


def test_decision_display_asset_for_buy_keeps_decision_symbol():
    decision = AgentDecision(
        decision_type=DecisionType.NON_ROUTINE,
        action=SignalAction.BUY,
        asset_symbol="EFA",
        reasoning="rotate to EFA",
        confidence=0.8,
        strategy_signals={},
        requires_approval=True,
        timeout_hours=8.0,
        urgency=Urgency.MEDIUM,
    )

    assert _decision_display_asset(decision, "GLD") == "EFA"


def test_journal_record_decision_preserves_display_and_decision_symbols(tmp_path):
    journal = TradeJournal(filepath=str(tmp_path / "journal.json"))

    journal.record_decision(
        decision_id="hold-001",
        action="hold",
        symbol="GLD",
        decision_symbol="XLE",
        current_holding_symbol="GLD",
        confidence=0.6,
        decision_type="non_routine",
        strategy_signals={},
        account_value=100000.0,
        current_holding="GLD",
    )

    entry = journal.entries[-1]
    assert entry.symbol == "GLD"
    assert entry.decision_symbol == "XLE"
    assert entry.current_holding_symbol == "GLD"


def test_unmapped_broker_holding_does_not_reuse_stale_strategy_state():
    calc_date = date.today()
    lookback_date = calc_date - relativedelta(months=12)
    prices = pd.DataFrame(
        [
            {"date": lookback_date, "symbol": "XLK", "close": 100.0},
            {"date": calc_date, "symbol": "XLK", "close": 160.0},
            {"date": lookback_date, "symbol": "EEM", "close": 100.0},
            {"date": calc_date, "symbol": "EEM", "close": 120.0},
        ]
    )

    strategy = DualMomentumStrategy(
        assets={
            AssetClass.TECH_SECTOR: ASSET_REGISTRY[AssetClass.TECH_SECTOR],
            AssetClass.EMERGING_MARKETS: ASSET_REGISTRY[AssetClass.EMERGING_MARKETS],
            AssetClass.CASH: ASSET_REGISTRY[AssetClass.CASH],
        },
        switch_threshold=0.02,
        pilot_entry_enabled=False,
    )
    strategy.current_holding = AssetClass.TECH_SECTOR

    checker = Checker.__new__(Checker)
    checker.strategies = {"dual_momentum": strategy}

    signals = checker._run_strategies(prices, current_holding="IWM")

    assert signals["dual_momentum"]["action"] == "buy"
    assert signals["dual_momentum"]["asset_symbol"] == "XLK"
