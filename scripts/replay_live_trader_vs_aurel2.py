#!/usr/bin/env python3
"""Replay the recorded live-trader run and compare it with Aurel2.

This script serves two purposes:
1. Reconstruct a deterministic approximation of the live trader on the
   recorded decision dates and compare those decisions with the actual log.
2. Run 1y and 5y backtests for the deterministic OpenClaw-style strategy
   versus Aurel2 on the same price set.

Notes:
- The replay intentionally ignores discretionary/AI tilt inputs because the
  historical tilt series is not fully archived.
- The April 1, 2026 log entries contain Alpaca backfill records that conflict
  with the narrative trade records, so the default replay window starts on
  2026-04-02.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import warnings
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd
import structlog

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(50))

from aurel2.core.assets import get_all_yahoo_symbols
from aurel2.data.providers.cache import CACHE_DIR, CachedPriceProvider
from aurel2.engine.backtest import BacktestEngine


WATCHLIST = ["SPY", "QQQ", "GLD", "TLT", "IWM", "EFA", "EEM", "XLE"]
RISK_ASSETS = {"SPY", "QQQ", "IWM", "EFA", "EEM", "XLE"}
DEFENSIVE = {"GLD", "TLT"}
ROTATION_MIN_DAYS = 2
STRONG_EDGE_THRESHOLD = 4.0

DEFAULT_LOG_PATH = Path("/root/.openclaw/workspace/live-trader/trades.jsonl")
DEFAULT_REPLAY_START = date(2026, 4, 2)


def pct_return(closes: list[float], n: int) -> float | None:
    if len(closes) < n + 1:
        return None
    return ((closes[-1] / closes[-(n + 1)]) - 1.0) * 100.0


def realized_vol(closes: list[float], n: int = 20) -> float | None:
    if len(closes) < n + 1:
        return None
    rets = []
    for i in range(-n, 0):
        prev = closes[i - 1]
        cur = closes[i]
        if prev > 0:
            rets.append((cur / prev) - 1.0)
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return (var ** 0.5) * 100.0


def drawdown_lookback(closes: list[float], n: int = 20) -> float | None:
    if len(closes) < n:
        return None
    window = closes[-n:]
    peak = max(window)
    if peak <= 0:
        return None
    return ((window[-1] / peak) - 1.0) * 100.0


def build_features(prices_df: pd.DataFrame, symbols: list[str], calc_date: date) -> dict[str, dict]:
    features = {}
    for sym in symbols:
        sym_data = prices_df[(prices_df["symbol"] == sym) & (prices_df["date"] <= calc_date)]
        if len(sym_data) < 80:
            continue
        closes = sym_data["close"].values[-80:].tolist()
        r5 = pct_return(closes, 5)
        r20 = pct_return(closes, 20)
        r60 = pct_return(closes, 60)
        vol20 = realized_vol(closes, 20)
        dd20 = drawdown_lookback(closes, 20)
        if None in {r5, r20, r60, vol20, dd20}:
            continue
        features[sym] = {
            "price": closes[-1],
            "ret_5d": round(r5, 4),
            "ret_20d": round(r20, 4),
            "ret_60d": round(r60, 4),
            "vol_20d": round(vol20, 4),
            "dd_20d": round(dd20, 4),
        }
    return features


def infer_regime(features: dict[str, dict]) -> dict[str, float | str | int]:
    risk_positive = sum(1 for s in RISK_ASSETS if s in features and features[s]["ret_20d"] > 0)
    breadth = risk_positive / max(1, len([s for s in RISK_ASSETS if s in features]))

    spy20 = features.get("SPY", {}).get("ret_20d", 0.0)
    qqq20 = features.get("QQQ", {}).get("ret_20d", 0.0)
    gld20 = features.get("GLD", {}).get("ret_20d", 0.0)

    risk_on = breadth >= 0.55 and (spy20 > 0 or qqq20 > 0)
    risk_off = breadth <= 0.30 and gld20 > 0

    if risk_off:
        name = "risk_off"
    elif risk_on:
        name = "risk_on"
    else:
        name = "mixed"

    return {
        "name": name,
        "breadth_risk_assets": round(breadth, 4),
        "risk_positive_count": risk_positive,
    }


def conviction_score(sym: str, f: dict, regime: dict) -> tuple[float, dict]:
    trend = 0.45 * f["ret_20d"] + 0.20 * f["ret_60d"] + 0.15 * f["ret_5d"]
    risk_penalty = 0.25 * max(0.0, f["vol_20d"]) + 0.25 * abs(min(0.0, f["dd_20d"]))

    regime_adj = 0.0
    if regime["name"] == "risk_off":
        regime_adj += 1.5 if sym in DEFENSIVE else -2.0
    elif regime["name"] == "risk_on":
        regime_adj += 1.0 if sym in RISK_ASSETS else -0.5

    total = trend - risk_penalty + regime_adj
    components = {
        "trend_component": round(trend, 4),
        "risk_penalty": round(risk_penalty, 4),
        "regime_adjustment": round(regime_adj, 4),
    }
    return round(total, 4), components


def choose_target(features: dict[str, dict], regime: dict) -> tuple[str, dict, list[dict]]:
    ranked = []
    for sym, f in features.items():
        score, components = conviction_score(sym, f, regime)
        ranked.append({"symbol": sym, "score": score, "components": components, **f})

    ranked.sort(key=lambda x: x["score"], reverse=True)

    if regime["name"] == "risk_off":
        defensive = [r for r in ranked if r["symbol"] in DEFENSIVE]
        if defensive:
            return defensive[0]["symbol"], defensive[0], ranked

    if not ranked:
        raise ValueError("No ranked features available")
    return ranked[0]["symbol"], ranked[0], ranked


def should_rotate_decision(
    current_symbol: str,
    target_symbol: str,
    current_row: dict,
    target_row: dict,
    regime: dict,
    last_rotation_date: date | None,
    check_date: date,
    mode: str = "baseline",
) -> tuple[bool, float, bool, str]:
    score_gap = target_row["score"] - current_row["score"]
    cooldown_block = False
    if last_rotation_date:
        cooldown_block = (check_date - last_rotation_date).days < ROTATION_MIN_DAYS

    if target_symbol == current_symbol:
        return False, score_gap, cooldown_block, "same_symbol"
    if cooldown_block:
        return False, score_gap, cooldown_block, "cooldown"

    if score_gap >= STRONG_EDGE_THRESHOLD:
        return True, score_gap, cooldown_block, "strong_edge"

    if mode == "early_rotation":
        fresh_momentum_release = (
            regime["name"] == "risk_on"
            and score_gap >= 3.0
            and current_row.get("ret_20d", 0.0) <= 0.0
            and current_row.get("ret_5d", 0.0) < 0.0
            and target_row.get("ret_20d", 0.0) >= 4.0
            and target_row.get("ret_5d", 0.0) > 0.0
            and (target_row.get("ret_20d", 0.0) - current_row.get("ret_20d", 0.0)) >= 5.0
        )
        if fresh_momentum_release:
            return True, score_gap, cooldown_block, "fresh_momentum_release"

    return False, score_gap, cooldown_block, "below_threshold"


def backtest_openclaw(
    prices_df: pd.DataFrame,
    start_date: date,
    end_date: date,
    initial_capital: float = 10000.0,
    rebalance: str = "monthly",
    mode: str = "baseline",
) -> dict[str, float]:
    prices_df = prices_df.copy()
    prices_df["date"] = pd.to_datetime(prices_df["date"]).dt.date

    spy_dates = prices_df[
        (prices_df["symbol"] == "SPY")
        & (prices_df["date"] >= start_date)
        & (prices_df["date"] <= end_date)
    ]["date"].sort_values().unique()

    if rebalance == "monthly":
        date_series = pd.Series(pd.to_datetime(spy_dates))
        check_dates = date_series.groupby(date_series.dt.to_period("M")).last().values
        check_dates = [pd.Timestamp(d).date() for d in check_dates]
    else:
        check_dates = [pd.Timestamp(d).date() for d in spy_dates]

    capital = initial_capital
    current_symbol = None
    current_shares = 0.0
    last_rotation_date = None
    num_trades = 0
    snapshots = []
    transaction_cost = 0.001

    for check_date in check_dates:
        features = build_features(prices_df, WATCHLIST, check_date)
        if not features:
            continue

        if current_symbol and current_symbol in features:
            portfolio_value = current_shares * features[current_symbol]["price"]
        elif current_symbol:
            portfolio_value = capital
        else:
            portfolio_value = capital

        snapshots.append({"date": check_date, "value": portfolio_value})

        regime = infer_regime(features)
        target_symbol, target_row, ranked = choose_target(features, regime)

        if not current_symbol:
            price = target_row["price"]
            cost = portfolio_value * transaction_cost
            current_shares = (portfolio_value - cost) / price
            current_symbol = target_symbol
            capital = 0.0
            num_trades += 1
            last_rotation_date = check_date
            continue

        current_row = next(
            (r for r in ranked if r["symbol"] == current_symbol),
            {"symbol": current_symbol, "score": -999.0, "price": 0.0},
        )
        should_rotate, _, _, _ = should_rotate_decision(
            current_symbol=current_symbol,
            target_symbol=target_symbol,
            current_row=current_row,
            target_row=target_row,
            regime=regime,
            last_rotation_date=last_rotation_date,
            check_date=check_date,
            mode=mode,
        )

        if should_rotate:
            sell_price = features[current_symbol]["price"] if current_symbol in features else current_row["price"]
            proceeds = current_shares * sell_price
            proceeds -= proceeds * transaction_cost
            buy_price = target_row["price"]
            current_shares = proceeds / buy_price
            current_symbol = target_symbol
            capital = 0.0
            num_trades += 1
            last_rotation_date = check_date

    if current_symbol:
        final_data = prices_df[
            (prices_df["symbol"] == current_symbol) & (prices_df["date"] <= end_date)
        ]
        if not final_data.empty:
            final_value = current_shares * final_data.iloc[-1]["close"]
        else:
            final_value = snapshots[-1]["value"] if snapshots else initial_capital
    else:
        final_value = capital or initial_capital

    years = (end_date - start_date).days / 365.25
    cagr = (final_value / initial_capital) ** (1 / years) - 1 if years > 0 else 0.0

    peak = 0.0
    max_dd = 0.0
    for snap in snapshots:
        value = snap["value"]
        if value > peak:
            peak = value
        dd = (peak - value) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    sharpe = 0.0
    if len(snapshots) > 1:
        values = [s["value"] for s in snapshots]
        returns = pd.Series(values).pct_change().dropna()
        if len(returns) > 0 and returns.std() > 0:
            periods = 12 if rebalance == "monthly" else 252
            sharpe = (returns.mean() * periods) / (returns.std() * np.sqrt(periods))

    turnover = num_trades / years if years > 0 else 0.0
    return {
        "cagr": cagr,
        "max_dd": max_dd,
        "sharpe": sharpe,
        "turnover": turnover,
        "trades": num_trades,
        "final_value": final_value,
    }


@dataclass
class ReplayDecision:
    decision_date: date
    actual_action: str
    actual_from_symbol: str | None
    actual_to_symbol: str


def _safe_json_lines(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def load_live_decisions(log_path: Path, replay_start: date) -> list[ReplayDecision]:
    rows = _safe_json_lines(log_path)
    grouped: dict[date, list[dict]] = {}
    for row in rows:
        if row.get("source") == "alpaca_backfill":
            continue
        ts = row.get("timestamp")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            continue
        d = dt.date()
        if d < replay_start:
            continue
        grouped.setdefault(d, []).append(row)

    decisions: list[ReplayDecision] = []
    for d in sorted(grouped):
        day_rows = grouped[d]
        holds = [r for r in day_rows if r.get("action") == "HOLD"]
        rotations = [r for r in day_rows if r.get("type") == "ROTATION"]
        buys = [r for r in day_rows if r.get("action") == "BUY"]
        sells = [r for r in day_rows if r.get("action") == "SELL"]

        if holds:
            symbol = holds[-1].get("symbol")
            if symbol:
                decisions.append(
                    ReplayDecision(
                        decision_date=d,
                        actual_action="hold",
                        actual_from_symbol=symbol,
                        actual_to_symbol=symbol,
                    )
                )
            continue

        if rotations:
            rot = rotations[-1]
            to_symbol = rot.get("to_asset")
            from_symbol = rot.get("from_asset")
            if to_symbol:
                decisions.append(
                    ReplayDecision(
                        decision_date=d,
                        actual_action="rotate",
                        actual_from_symbol=from_symbol,
                        actual_to_symbol=to_symbol,
                    )
                )
            continue

        if buys and sells:
            decisions.append(
                ReplayDecision(
                    decision_date=d,
                    actual_action="rotate",
                    actual_from_symbol=sells[-1].get("symbol"),
                    actual_to_symbol=buys[-1].get("symbol"),
                )
            )

    return decisions


def replay_live_run(prices_df: pd.DataFrame, decisions: list[ReplayDecision], mode: str = "baseline") -> list[dict]:
    if not decisions:
        return []

    results = []
    current_symbol = decisions[0].actual_from_symbol
    last_rotation_date = None

    for decision in decisions:
        features = build_features(prices_df, WATCHLIST, decision.decision_date)
        if not features or not current_symbol:
            continue
        if current_symbol not in features:
            continue

        regime = infer_regime(features)
        target_symbol, target_row, ranked = choose_target(features, regime)
        current_row = next(
            (r for r in ranked if r["symbol"] == current_symbol),
            {"symbol": current_symbol, "score": -999.0},
        )

        predicted_rotate, score_gap, cooldown_block, trigger = should_rotate_decision(
            current_symbol=current_symbol,
            target_symbol=target_symbol,
            current_row=current_row,
            target_row=target_row,
            regime=regime,
            last_rotation_date=last_rotation_date,
            check_date=decision.decision_date,
            mode=mode,
        )
        predicted_action = "rotate" if predicted_rotate else "hold"
        predicted_to_symbol = target_symbol if predicted_rotate else current_symbol

        results.append(
            {
                "date": decision.decision_date.isoformat(),
                "actual_action": decision.actual_action,
                "actual_from_symbol": current_symbol,
                "actual_to_symbol": decision.actual_to_symbol,
                "predicted_action": predicted_action,
                "predicted_to_symbol": predicted_to_symbol,
                "match": (
                    predicted_action == decision.actual_action
                    and predicted_to_symbol == decision.actual_to_symbol
                ),
                "regime": regime["name"],
                "score_gap": round(score_gap, 4),
                "cooldown_block": cooldown_block,
                "trigger": trigger,
                "target_symbol": target_symbol,
                "current_score": current_row["score"],
                "target_score": target_row["score"],
            }
        )

        if decision.actual_action == "rotate":
            current_symbol = decision.actual_to_symbol
            last_rotation_date = decision.decision_date
        else:
            current_symbol = decision.actual_to_symbol

    return results


def load_prices(start_date: date, end_date: date, refresh_missing: bool) -> pd.DataFrame:
    symbols = sorted(set(get_all_yahoo_symbols() + WATCHLIST))

    if refresh_missing:
        provider = CachedPriceProvider()
        return provider.get_multi_prices(symbols, start_date, end_date)

    frames = []
    for symbol in symbols:
        cache_path = CACHE_DIR / f"{symbol}.parquet"
        if not cache_path.exists():
            continue
        df = pd.read_parquet(cache_path)
        if df.empty:
            continue
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df = df[(df["date"] >= start_date - timedelta(days=400)) & (df["date"] <= end_date)]
        if not df.empty:
            frames.append(df[["date", "close", "symbol"]])

    if not frames:
        return pd.DataFrame(columns=["date", "close", "symbol"])

    return pd.concat(frames, ignore_index=True)


def _fmt_pct(v: float) -> str:
    return f"{v:+.1%}"


def _spy_cagr(prices: pd.DataFrame, start: date, end: date, initial_capital: float = 10000.0) -> float | None:
    spy = prices[(prices["symbol"] == "SPY") & (prices["date"] >= start) & (prices["date"] <= end)].copy()
    if spy.empty:
        return None
    start_px = float(spy.iloc[0]["close"])
    end_px = float(spy.iloc[-1]["close"])
    if start_px <= 0:
        return None
    final = initial_capital * (end_px / start_px)
    years = (end - start).days / 365.25
    if years <= 0:
        return None
    return (final / initial_capital) ** (1 / years) - 1


def run_backtest_comparison(prices: pd.DataFrame, end_date: date) -> list[dict]:
    periods = [
        ("1y", date(end_date.year - 1, end_date.month, 1), end_date),
        ("5y", date(end_date.year - 5, end_date.month, 1), end_date),
    ]

    rows = []
    for label, start, end in periods:
        oc_monthly = backtest_openclaw(prices, start, end, rebalance="monthly", mode="baseline")
        oc_daily = backtest_openclaw(prices, start, end, rebalance="daily", mode="baseline")
        early_monthly = backtest_openclaw(prices, start, end, rebalance="monthly", mode="early_rotation")
        early_daily = backtest_openclaw(prices, start, end, rebalance="daily", mode="early_rotation")

        engine = BacktestEngine(initial_capital=10000.0, use_ai=False)
        old_stdout = sys.stdout
        sys.stdout = StringIO()
        try:
            a2 = engine.run(prices=prices, start_date=start, end_date=end, benchmark_symbol="SPY")
        finally:
            sys.stdout = old_stdout
        a2.calculate_metrics()

        rows.append(
            {
                "label": label,
                "start": start,
                "end": end,
                "openclaw_monthly": oc_monthly,
                "openclaw_daily": oc_daily,
                "early_monthly": early_monthly,
                "early_daily": early_daily,
                "aurel2": a2,
                "spy_cagr": _spy_cagr(prices, start, end),
            }
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay the live-trader run and compare it to Aurel2.")
    parser.add_argument("--log-path", default=str(DEFAULT_LOG_PATH), help="Path to the live trader trades.jsonl file.")
    parser.add_argument(
        "--replay-start",
        default=DEFAULT_REPLAY_START.isoformat(),
        help="Replay decision dates starting from YYYY-MM-DD (default: 2026-04-02).",
    )
    parser.add_argument(
        "--refresh-missing",
        action="store_true",
        help="Allow network-backed cache refresh for missing/stale symbols.",
    )
    args = parser.parse_args()

    replay_start = date.fromisoformat(args.replay_start)
    log_path = Path(args.log_path)

    decisions = load_live_decisions(log_path, replay_start)
    if not decisions:
        raise SystemExit("No replayable decisions found in the selected window.")

    end_date = decisions[-1].decision_date
    fetch_start = min(date(end_date.year - 6, 1, 1), replay_start - timedelta(days=500))
    print("=" * 96)
    print("LIVE-TRADER REPLAY + OPENCLAW VS AUREL2 COMPARISON")
    print("=" * 96)
    print(f"Replay window: {replay_start} -> {end_date}")
    print(f"Trade log: {log_path}")
    print("Mode: deterministic replay (no archived AI/discretionary tilt history)")

    print("\nLoading cached price history (and refreshing gaps if needed)...")
    prices = load_prices(fetch_start, end_date, refresh_missing=args.refresh_missing)
    if prices.empty:
        raise SystemExit("No price data available.")
    prices["date"] = pd.to_datetime(prices["date"]).dt.date

    symbol_max = prices.groupby("symbol")["date"].max().to_dict()
    missing_or_stale = [sym for sym in WATCHLIST if symbol_max.get(sym) is None or symbol_max[sym] < end_date]
    if missing_or_stale:
        common_end = min(symbol_max[sym] for sym in WATCHLIST if sym in symbol_max)
        print(f"Price coverage is incomplete through {end_date}. Using common end date {common_end}.")
        print(f"Symbols missing/stale at requested end: {', '.join(missing_or_stale)}")
        end_date = common_end
        decisions = [d for d in decisions if d.decision_date <= end_date]

    baseline_rows = replay_live_run(prices, decisions, mode="baseline")
    early_rows = replay_live_run(prices, decisions, mode="early_rotation")
    baseline_matches = sum(1 for row in baseline_rows if row["match"])
    early_matches = sum(1 for row in early_rows if row["match"])

    print("\nReplay Results")
    print("-" * 124)
    print(
        f"{'Date':10s} {'Actual':18s} {'Baseline':18s} {'Early':18s} "
        f"{'Regime':10s} {'Gap':>7s} {'Trigger':22s}"
    )
    print("-" * 124)
    for base_row, early_row in zip(baseline_rows, early_rows):
        actual = f"{base_row['actual_action']} {base_row['actual_to_symbol']}"
        baseline_pred = f"{base_row['predicted_action']} {base_row['predicted_to_symbol']}"
        early_pred = f"{early_row['predicted_action']} {early_row['predicted_to_symbol']}"
        print(
            f"{base_row['date']:10s} {actual:18s} {baseline_pred:18s} {early_pred:18s} "
            f"{base_row['regime']:10s} {base_row['score_gap']:>+7.2f} {early_row['trigger']:22s}"
        )
    print("-" * 124)
    print(
        f"Replay match rate: baseline {baseline_matches}/{len(baseline_rows)} = {baseline_matches / len(baseline_rows):.1%} | "
        f"early_rotation {early_matches}/{len(early_rows)} = {early_matches / len(early_rows):.1%}"
    )

    comparison_rows = run_backtest_comparison(prices, end_date)
    print("\nPerformance Comparison")
    print("-" * 124)
    print(
        f"{'Period':8s} | {'Base M':>10s} {'DD':>7s} | {'Base D':>10s} {'DD':>7s} "
        f"| {'Early M':>10s} {'DD':>7s} | {'Early D':>10s} {'DD':>7s} "
        f"| {'Aurel2':>10s} {'DD':>7s} | {'SPY':>10s}"
    )
    print("-" * 124)
    for row in comparison_rows:
        ocm = row["openclaw_monthly"]
        ocd = row["openclaw_daily"]
        ecm = row["early_monthly"]
        ecd = row["early_daily"]
        a2 = row["aurel2"]
        spy = row["spy_cagr"]
        spy_str = _fmt_pct(spy) if spy is not None else "n/a"
        print(
            f"{row['label']:8s} | {_fmt_pct(ocm['cagr']):>10s} {_fmt_pct(-ocm['max_dd']):>7s} "
            f"| {_fmt_pct(ocd['cagr']):>10s} {_fmt_pct(-ocd['max_dd']):>7s} "
            f"| {_fmt_pct(ecm['cagr']):>10s} {_fmt_pct(-ecm['max_dd']):>7s} "
            f"| {_fmt_pct(ecd['cagr']):>10s} {_fmt_pct(-ecd['max_dd']):>7s} "
            f"| {_fmt_pct(a2.cagr):>10s} {_fmt_pct(-a2.max_drawdown):>7s} "
            f"| {spy_str:>10s}"
        )


if __name__ == "__main__":
    main()
