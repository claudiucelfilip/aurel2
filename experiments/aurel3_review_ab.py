"""A/B test: does an aurel3-style decision-review layer improve aurel2?

Baseline arms run aurel2's full live-path backtest (dual momentum +
orchestrator, AI off) at fixed switch thresholds. Adaptive arms add a
walk-forward review layer that grades each matured strategy decision
against its counterfactual (the runner-up asset) and nudges the switch
threshold — the concrete feedback loop the aurel3 analytical layer would
provide in live.

Pre-registered adaptation rule (chosen before running, not tuned):
- Each rebalance, grade every prior ungraded decision over the window
  from its date to today:
    hold  decision: late_hold   if runner-up beat the holding by > 1%
    switch decision: churn_switch if the old holding beat the new asset
                     by > 1% + 2x transaction cost
- Over the last 12 graded decisions: if late_holds exceed churn_switches
  by >= 3, threshold -= 0.005; if churn_switches exceed late_holds by
  >= 3, threshold += 0.005. Clamp to [0.005, 0.06].

Only equity<->equity tension decisions are graded; cash/defensive moves
are governed by separate thresholds and are out of scope.
"""

import json
import sys
from collections import deque
from datetime import date

from aurel2.config.settings import load_settings
from aurel2.core.assets import ASSET_REGISTRY
from aurel2.core.models import AssetClass, SignalAction
from aurel2.data.providers.yahoo import YahooFinanceProvider
from aurel2.engine.backtest import BacktestEngine

START = date(2012, 1, 1)
END = date(2026, 7, 15)
DATA_START = date(2010, 6, 1)  # 12m lookback + buffer
CAPITAL = 10_000.0
MARGIN = 0.01
ADAPT_STEP = 0.005
ADAPT_MIN, ADAPT_MAX = 0.005, 0.06
WINDOW = 12
TRIGGER = 3

FIXED_ARMS = [0.005, 0.01, 0.02, 0.03, 0.04, 0.06]
ADAPTIVE_ARMS = [0.01, 0.02, 0.04]


def yahoo_sym(asset_class):
    a = ASSET_REGISTRY.get(asset_class)
    return a.yahoo_symbol if a else None


def make_engine(cost_pct):
    return BacktestEngine(
        initial_capital=CAPITAL,
        transaction_cost_pct=cost_pct,
        use_ai=False,
        dca_amount=0.0,
        correlation_guard=True,
        sideways_hold=True,
    )


def attach_review_layer(engine, cost_pct):
    """Wrap the strategy's generate_signal with grading + adaptation."""
    strategy = engine.dual_momentum
    original = strategy.generate_signal
    state = {
        "pending": [],            # ungraded decisions
        "grades": deque(maxlen=WINDOW),
        "threshold_path": [],
        "grade_counts": {"late_hold": 0, "good_hold": 0,
                         "churn_switch": 0, "good_switch": 0},
    }

    def graded_return(prices, sym, d0, d1):
        p0 = engine._get_price(prices, sym, d0)
        p1 = engine._get_price(prices, sym, d1)
        if not p0 or not p1:
            return None
        return p1 / p0 - 1

    def wrapper(prices, calc_date, current_holding=None):
        # 1. Grade matured decisions (walk-forward: only past data used)
        still_pending = []
        for dec in state["pending"]:
            if dec["date"] >= calc_date:
                still_pending.append(dec)
                continue
            r_chosen = graded_return(prices, dec["chosen"], dec["date"], calc_date)
            r_alt = graded_return(prices, dec["alt"], dec["date"], calc_date)
            if r_chosen is None or r_alt is None:
                continue
            if dec["kind"] == "hold":
                grade = "late_hold" if r_alt - r_chosen > MARGIN else "good_hold"
            else:
                grade = ("churn_switch"
                         if r_alt - r_chosen > MARGIN + 2 * cost_pct
                         else "good_switch")
            state["grades"].append(grade)
            state["grade_counts"][grade] += 1
        state["pending"] = still_pending

        # 2. Adapt threshold from the rolling window
        late = sum(1 for g in state["grades"] if g == "late_hold")
        churn = sum(1 for g in state["grades"] if g == "churn_switch")
        if late - churn >= TRIGGER:
            strategy.switch_threshold = max(ADAPT_MIN, strategy.switch_threshold - ADAPT_STEP)
        elif churn - late >= TRIGGER:
            strategy.switch_threshold = min(ADAPT_MAX, strategy.switch_threshold + ADAPT_STEP)
        state["threshold_path"].append(
            {"date": str(calc_date), "threshold": round(strategy.switch_threshold, 4)})

        # 3. Run the real strategy
        signal = original(prices, calc_date, current_holding=current_holding)

        # 4. Log this decision's tension pair (equity vs equity only)
        try:
            scores = signal.momentum_scores or {}
            risky = {ac: ms for ac, ms in scores.items() if ac != AssetClass.CASH}
            winner_ac = max(risky, key=lambda ac: risky[ac].momentum_12m) if risky else None
            holding_sym = yahoo_sym(current_holding) if current_holding else None
            winner_sym = yahoo_sym(winner_ac) if winner_ac else None
            chosen_sym = signal.asset.yahoo_symbol if signal.asset else None
            if signal.action == SignalAction.HOLD and holding_sym and winner_sym \
                    and holding_sym != winner_sym and current_holding != AssetClass.CASH:
                state["pending"].append({"date": calc_date, "kind": "hold",
                                         "chosen": holding_sym, "alt": winner_sym})
            elif signal.action == SignalAction.BUY and holding_sym and chosen_sym \
                    and chosen_sym != holding_sym \
                    and current_holding != AssetClass.CASH \
                    and signal.asset.symbol != "CASH":
                state["pending"].append({"date": calc_date, "kind": "switch",
                                         "chosen": chosen_sym, "alt": holding_sym})
        except Exception as e:
            print(f"  decision-log error {calc_date}: {e}", file=sys.stderr)

        return signal

    strategy.generate_signal = wrapper
    return state


def run_arm(name, prices, cost_pct, threshold, adaptive):
    engine = make_engine(cost_pct)
    engine.dual_momentum.switch_threshold = threshold
    state = attach_review_layer(engine, cost_pct) if adaptive else None
    result = engine.run(prices=prices, start_date=START, end_date=END,
                        frequency="monthly", benchmark_symbol="SPY")
    row = {
        "arm": name,
        "final_value": round(result.final_value, 2),
        "cagr_pct": round(result.cagr * 100, 2),
        "sharpe": round(result.sharpe_ratio, 3),
        "max_dd_pct": round(result.max_drawdown * 100, 2),
        "num_trades": result.num_trades,
    }
    if state:
        row["grade_counts"] = state["grade_counts"]
        row["final_threshold"] = state["threshold_path"][-1]["threshold"]
        row["threshold_path"] = state["threshold_path"]
    return row


def main():
    settings = load_settings(None)
    cost_pct = settings.risk.transaction_cost_pct
    print(f"transaction cost: {cost_pct}")

    symbols = [a.yahoo_symbol for a in ASSET_REGISTRY.values() if a.yahoo_symbol]
    provider = YahooFinanceProvider()
    prices = provider.get_multi_prices(symbols, DATA_START, END)
    print(f"price records: {len(prices)}, symbols: {prices['symbol'].nunique()}")

    rows = []
    for th in FIXED_ARMS:
        print(f"\n== fixed threshold {th}")
        rows.append(run_arm(f"fixed_{th}", prices, cost_pct, th, adaptive=False))
        print()
    for th in ADAPTIVE_ARMS:
        print(f"\n== adaptive from {th}")
        rows.append(run_arm(f"adaptive_from_{th}", prices, cost_pct, th, adaptive=True))
        print()

    print("\n%-20s %12s %8s %8s %8s %7s" % ("arm", "final", "cagr%", "sharpe", "maxDD%", "trades"))
    for r in rows:
        print("%-20s %12.2f %8.2f %8.3f %8.2f %7d" % (
            r["arm"], r["final_value"], r["cagr_pct"], r["sharpe"],
            r["max_dd_pct"], r["num_trades"]))
        if "final_threshold" in r:
            print("    final_threshold=%s grades=%s" % (r["final_threshold"], r["grade_counts"]))

    with open("experiments/aurel3_review_ab_results.json", "w") as f:
        json.dump(rows, f, indent=1)
    print("\nsaved -> experiments/aurel3_review_ab_results.json")


if __name__ == "__main__":
    main()
