from aurel2.agent.orchestrator import AgentDecision, DecisionType, Urgency
from aurel2.core.models import SignalAction
from aurel2.live.checker import _build_hold_notification_message


def test_hold_notification_explicitly_separates_current_and_candidate_assets():
    decision = AgentDecision(
        decision_type=DecisionType.NON_ROUTINE,
        action=SignalAction.HOLD,
        asset_symbol="XLE",
        reasoning="[BULL] Calm-market hold: keeping GLD",
        confidence=0.72,
        strategy_signals={},
        requires_approval=True,
        timeout_hours=8.0,
        urgency=Urgency.MEDIUM,
    )

    message = _build_hold_notification_message(
        decision=decision,
        current_holding="GLD",
        account_value=101234.0,
        market_context={"regime": "bull"},
        signals={
            "dual_momentum": {"action": "buy"},
            "mean_reversion": {"action": "hold"},
            "multi_timeframe": {"action": "buy"},
        },
    )

    assert "Current holding: GLD" in message
    assert "Candidate asset: XLE" in message
    assert "Action: HOLD current position" in message
    assert "Account: $101,234" in message
    assert "Regime: BULL" in message


def test_hold_notification_uses_cash_when_no_current_holding():
    decision = AgentDecision(
        decision_type=DecisionType.NON_ROUTINE,
        action=SignalAction.HOLD,
        asset_symbol=None,
        reasoning="[SIDEWAYS] Stay in cash",
        confidence=0.6,
        strategy_signals={},
        requires_approval=True,
        timeout_hours=8.0,
        urgency=Urgency.MEDIUM,
    )

    message = _build_hold_notification_message(
        decision=decision,
        current_holding=None,
        account_value=None,
        market_context={"regime": "sideways"},
        signals={"dual_momentum": {"action": "hold"}},
    )

    assert "Current holding: cash" in message
    assert "Candidate asset: —" in message
    assert "Action: HOLD current position" in message
    assert "Regime: SIDEWAYS" in message
