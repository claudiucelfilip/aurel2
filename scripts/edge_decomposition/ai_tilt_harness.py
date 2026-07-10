#!/usr/bin/env python3
"""Tier 1 + 2: AI discretionary tilt overlay on A2's replay, blind out-of-sample.

Prototypes the Phase 2 AI overlay (docs/plans/2026-07-09-edge-decomposition-goal.md,
"Phase 2 non-negotiables"): the AI's only write-surface is a capped, versioned tilt
(symbol_bias in [-0.03, 0.03] added to DM's momentum score, regime_view gating the
switch thresholds) — it cannot introduce decision logic, place orders, or touch config.

Every AI call shells out to the `claude` CLI (non-interactive print mode). NEVER the
anthropic SDK — see CLAUDE.md. Claude's knowledge cutoff is January 2026; the window
below (2026-02-11 -> 2026-07-08) is entirely after cutoff, so the model cannot have
memorized outcomes. Blindness is also enforced in the prompt itself.

Tilt mechanism (no strategy reimplementation): DualMomentumStrategy.generate_signal
runs completely unmodified. The bias is injected by scaling that one asset's current
price on that one calc_date so momentum_12m shifts by exactly `bias` — the same
function (calculate_momentum_scores) and the same decision tree run either way.

Tier 1 output: data/edge_decomposition/ai_tilt_replay.json
Tier 2 (Claw calibration) is folded into the same file under "claw_calibration".
Raw CLI responses cached to data/edge_decomposition/ai_tilt_cache.jsonl (reruns
never re-call for a date+prompt already cached).
"""

import hashlib
import json
import subprocess
import sys
import time
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

import structlog
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(50))
import logging
logging.disable(logging.CRITICAL)

import pandas as pd

from aurel2.core.assets import ASSET_REGISTRY, get_all_yahoo_symbols
from aurel2.core.models import AssetClass, SignalAction
from aurel2.data.providers.cache import CachedPriceProvider
from aurel2.engine.backtest import BacktestEngine
from aurel2.strategies.dual_momentum import DualMomentumStrategy

WINDOW_START = date(2026, 2, 11)
WINDOW_END = date(2026, 7, 8)
CACHE_PATH = REPO_ROOT / "data" / "edge_decomposition" / "ai_tilt_cache.jsonl"
OUT_PATH = REPO_ROOT / "data" / "edge_decomposition" / "ai_tilt_replay.json"
LT_ACTUAL_PATH = REPO_ROOT / "data" / "edge_decomposition" / "lt_actual.json"

MAX_TILT = 0.03
CLI_TIMEOUT_S = 150

# Claw's real interventions in the window, for Tier 2 calibration.
# (date, action description, direction: which symbol the move favored)
CLAW_ACTIONS = [
    ("2026-02-20", "GLD -> EEM", "EEM"),
    ("2026-02-27", "EEM -> XLE", "XLE"),
    ("2026-03-02", "XLE -> GLD", "GLD"),
    ("2026-03-11", "GLD -> XLE", "XLE"),
    ("2026-04-10", "XLE -> EEM", "EEM"),
    ("2026-04-23", "EEM -> QQQ", "QQQ"),
    ("2026-06-12", "QQQ -> IWM", "IWM"),
    ("2026-06-29", "IWM sell", None),
    ("2026-07-07", "XLK buy", "XLK"),
]


# ---------------------------------------------------------------------------
# Context pack
# ---------------------------------------------------------------------------

def build_context_pack(
    prices: pd.DataFrame,
    calc_date: date,
    dm_assets: dict,
    current_holding_symbol: str | None,
    days_held: int,
    deterministic_signal: dict,
) -> dict:
    """Everything the AI sees, built strictly from data <= calc_date's prior close.

    All price history is clipped to trading days before calc_date so there is no
    lookahead even though the underlying price cache spans the whole window.
    """
    prior_cutoff = pd.Timestamp(calc_date) - pd.Timedelta(days=1)
    hist = prices[pd.to_datetime(prices["date"]) <= prior_cutoff]

    extra_symbols = ["SPY", "QQQ", "GLD"]
    universe_symbols = sorted({a.yahoo_symbol for a in dm_assets.values() if a.yahoo_symbol} | set(extra_symbols))

    last20_pct_change = {}
    for sym in universe_symbols:
        sp = hist[hist["symbol"] == sym].sort_values("date")
        closes = sp["close"].tail(21).tolist()
        if len(closes) < 2:
            continue
        pct = [round((closes[i] / closes[i - 1] - 1) * 100, 3) for i in range(1, len(closes))]
        last20_pct_change[sym] = pct[-20:]

    def _ret(sym: str, months: int) -> float | None:
        sp = hist[hist["symbol"] == sym].sort_values("date")
        if sp.empty:
            return None
        cur = sp.iloc[-1]["close"]
        cutoff = pd.Timestamp(calc_date) - pd.DateOffset(months=months)
        past = sp[pd.to_datetime(sp["date"]) <= cutoff]
        if past.empty:
            return None
        past_price = past.iloc[-1]["close"]
        if past_price == 0:
            return None
        return round((cur / past_price - 1) * 100, 3)

    momentum_snapshot = {}
    for ac, asset in dm_assets.items():
        if ac == AssetClass.CASH or not asset.yahoo_symbol:
            continue
        momentum_snapshot[asset.yahoo_symbol] = {
            "1m": _ret(asset.yahoo_symbol, 1),
            "3m": _ret(asset.yahoo_symbol, 3),
            "12m": _ret(asset.yahoo_symbol, 12),
        }

    dm_ranking = sorted(
        ((sym, v["12m"]) for sym, v in momentum_snapshot.items() if v["12m"] is not None),
        key=lambda x: x[1],
        reverse=True,
    )[:5]

    spy_hist = hist[hist["symbol"] == "SPY"].sort_values("date")
    spy_closes = spy_hist["close"].tail(21).tolist()
    spy_20d_vol = None
    if len(spy_closes) > 5:
        rets = pd.Series(spy_closes).pct_change().dropna()
        spy_20d_vol = round(float(rets.std() * (252 ** 0.5) * 100), 2)

    held_drawdown = None
    if current_holding_symbol and current_holding_symbol not in ("CASH", None):
        hp = hist[hist["symbol"] == current_holding_symbol].sort_values("date")
        closes_12m = hp[pd.to_datetime(hp["date"]) >= pd.Timestamp(calc_date) - pd.DateOffset(months=12)]["close"]
        if not closes_12m.empty:
            peak = closes_12m.max()
            cur = closes_12m.iloc[-1]
            held_drawdown = round((cur / peak - 1) * 100, 3)

    return {
        "as_of": calc_date.isoformat(),
        "last_20d_pct_change": last20_pct_change,
        "momentum_snapshot_pct": momentum_snapshot,
        "dm_top5_ranking_12m": dm_ranking,
        "spy_20d_annualized_vol_pct": spy_20d_vol,
        "current_position": current_holding_symbol or "CASH",
        "days_held": days_held,
        "held_drawdown_from_12m_high_pct": held_drawdown,
        "deterministic_a2_signal_today": deterministic_signal,
    }


PROMPT_TEMPLATE = """You are a discretionary overlay on a mechanical momentum strategy. You may tilt but not override. Assume you know NOTHING about markets after this date -- do not use any memorized knowledge of what happened after {as_of}, reason only from the data below.

Context (all data strictly as of the close before {as_of}):
{context_json}

Given ONLY this data, return STRICT JSON with no prose, no markdown fences, matching exactly this schema:
{{"regime_view": "risk_on|mixed|risk_off", "symbol_bias": {{"<TICKER>": float in [-0.03,0.03]}}, "confidence": 0..1, "reasoning": "<2 sentences>"}}

Empty symbol_bias ({{}}) is a valid answer -- only bias on real conviction from the data shown."""


def _extract_json(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def call_claude_cli(prompt: str, model: str = "sonnet") -> tuple[dict | None, float, str]:
    """Shell out to `claude -p ... --output-format json`. Returns (parsed_result_or_None, latency_s, raw_text)."""
    start = time.monotonic()
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt, "--model", model, "--output-format", "json"],
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return None, time.monotonic() - start, "TIMEOUT"

    latency = time.monotonic() - start
    if proc.returncode != 0:
        return None, latency, f"CLI_ERROR: {proc.stderr[:500]}"

    try:
        envelope = json.loads(proc.stdout)
        result_text = envelope.get("result", "")
    except json.JSONDecodeError:
        return None, latency, f"ENVELOPE_PARSE_FAIL: {proc.stdout[:500]}"

    parsed = _extract_json(result_text)
    return parsed, latency, result_text


def call_codex_cli(prompt: str, model: str = "gpt-5.5") -> tuple[dict | None, float, str]:
    """Shell out to `codex exec --json ...`. Returns (parsed_result_or_None, latency_s, raw_text).

    stdout is newline-delimited JSON events; the final answer is the
    "agent_message" item's "text" field. stdin is redirected from /dev/null --
    codex exec otherwise blocks reading additional input.
    """
    start = time.monotonic()
    try:
        proc = subprocess.run(
            ["codex", "exec", "--model", model, "--json", prompt],
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return None, time.monotonic() - start, "TIMEOUT"

    latency = time.monotonic() - start
    if proc.returncode != 0:
        return None, latency, f"CLI_ERROR: {proc.stderr[:500]}"

    result_text = ""
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "item.completed" and event.get("item", {}).get("type") == "agent_message":
            result_text = event["item"].get("text", "")

    if not result_text:
        return None, latency, f"NO_AGENT_MESSAGE: {proc.stdout[:500]}"

    parsed = _extract_json(result_text)
    return parsed, latency, result_text


CLI_BACKENDS = {
    "sonnet": lambda prompt: call_claude_cli(prompt, model="sonnet"),
    "fable5": lambda prompt: call_claude_cli(prompt, model="claude-fable-5"),
    "gpt55": lambda prompt: call_codex_cli(prompt, model="gpt-5.5"),
    "gpt56sol": lambda prompt: call_codex_cli(prompt, model="gpt-5.6-sol"),
}


def validate_tilt(parsed: dict) -> dict | None:
    if not isinstance(parsed, dict):
        return None
    regime = parsed.get("regime_view")
    if regime not in ("risk_on", "mixed", "risk_off"):
        return None
    bias = parsed.get("symbol_bias")
    if not isinstance(bias, dict):
        return None
    clean_bias = {}
    for sym, v in bias.items():
        try:
            fv = float(v)
        except (TypeError, ValueError):
            return None
        clean_bias[sym] = max(-MAX_TILT, min(MAX_TILT, fv))
    confidence = parsed.get("confidence", 0.0)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "regime_view": regime,
        "symbol_bias": clean_bias,
        "confidence": confidence,
        "reasoning": str(parsed.get("reasoning", ""))[:1000],
    }


def load_cache(cache_path: Path = CACHE_PATH) -> dict:
    cache = {}
    if cache_path.exists():
        for line in cache_path.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            cache[rec["cache_key"]] = rec
    return cache


def append_cache(rec: dict, cache_path: Path = CACHE_PATH):
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with cache_path.open("a") as f:
        f.write(json.dumps(rec) + "\n")


def get_ai_tilt(
    calc_date: date,
    context: dict,
    cache: dict,
    backend: str = "sonnet",
    cache_path: Path = CACHE_PATH,
) -> tuple[dict, float]:
    prompt = PROMPT_TEMPLATE.format(as_of=calc_date.isoformat(), context_json=json.dumps(context, indent=2))
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:16]
    cache_key = f"{calc_date.isoformat()}:{prompt_hash}"

    if cache_key in cache:
        rec = cache[cache_key]
        return rec["tilt"], 0.0

    call_fn = CLI_BACKENDS[backend]
    for attempt in range(2):
        parsed, latency, raw = call_fn(prompt)
        tilt = validate_tilt(parsed) if parsed is not None else None
        if tilt is not None:
            rec = {
                "cache_key": cache_key,
                "date": calc_date.isoformat(),
                "prompt_hash": prompt_hash,
                "attempt": attempt + 1,
                "raw_response": raw,
                "tilt": tilt,
                "latency_s": round(latency, 1),
            }
            append_cache(rec, cache_path)
            cache[cache_key] = rec
            return tilt, latency
        print(f"    [retry {attempt + 1}] malformed response: {raw[:200]}")

    # Both attempts failed -> record no-tilt for this week.
    fallback = {"regime_view": "mixed", "symbol_bias": {}, "confidence": 0.0, "reasoning": "PARSE_FAILURE: recorded as no-tilt"}
    rec = {
        "cache_key": cache_key,
        "date": calc_date.isoformat(),
        "prompt_hash": prompt_hash,
        "attempt": "fallback",
        "raw_response": "BOTH_ATTEMPTS_FAILED",
        "tilt": fallback,
        "latency_s": 0.0,
    }
    append_cache(rec, cache_path)
    cache[cache_key] = rec
    return fallback, 0.0


# ---------------------------------------------------------------------------
# Tilted momentum: inject bias via price scaling, run the REAL DM code path.
# ---------------------------------------------------------------------------

def apply_price_tilt(prices: pd.DataFrame, calc_date: date, symbol_bias: dict[str, float]) -> pd.DataFrame:
    """Scale each biased symbol's price on calc_date so momentum_12m shifts by
    exactly `bias`, for exactly that one calc_date's ranking. calculate_momentum_scores
    reads the closest price <= calc_date, so scaling that single row is sufficient and
    does not leak into any other date's calculation.
    """
    if not symbol_bias:
        return prices
    out = prices.copy()
    out["date"] = pd.to_datetime(out["date"])
    cutoff = pd.Timestamp(calc_date)
    for sym, bias in symbol_bias.items():
        mask = (out["symbol"] == sym) & (out["date"] <= cutoff)
        if not mask.any():
            continue
        last_idx = out[mask].index[-1]
        # momentum = current/past - 1  =>  scaling current by (1+m+bias)/(1+m) shifts
        # momentum by exactly `bias` for this one row, leaving `past` untouched.
        row_date = out.loc[last_idx, "date"]
        if row_date < cutoff:
            continue  # bias only applies to the calc-date row itself (avoid leaking into later reads of an older row)
        current_price = out.loc[last_idx, "close"]
        lookback_date = cutoff - pd.DateOffset(months=12)
        past_mask = (out["symbol"] == sym) & (out["date"] <= lookback_date)
        if not past_mask.any():
            continue
        past_price = out.loc[out[past_mask].index[-1], "close"]
        if past_price == 0:
            continue
        momentum = current_price / past_price - 1
        scale = (1 + momentum + bias) / (1 + momentum) if (1 + momentum) != 0 else 1.0
        out.loc[last_idx, "close"] = current_price * scale
    out["date"] = out["date"].dt.date.astype(str)
    return out


GATE_THRESHOLDS = {
    "risk_off": {"equity_to_defensive_threshold": 0.02, "defensive_to_equity_threshold": 0.15},
    "risk_on": {"equity_to_defensive_threshold": 0.15, "defensive_to_equity_threshold": 0.05},
    # mixed: keep previous thresholds (handled by caller, not overwritten here)
}


def build_engine() -> BacktestEngine:
    no_tlt_assets = {ac: a for ac, a in ASSET_REGISTRY.items() if ac != AssetClass.BONDS_TREASURY}
    engine = BacktestEngine(initial_capital=100.0, use_ai=False, correlation_guard=False, sideways_hold=False)
    engine.dual_momentum = DualMomentumStrategy(
        assets=no_tlt_assets,
        lookback_months=12,
        switch_threshold=0.02,
        cash_rate=0.0,
    )
    return engine


def run_replay(prices: pd.DataFrame, engine: BacktestEngine, tilts_by_week: dict[date, dict] | None):
    """Same bookkeeping shape as a2_replay.py. tilts_by_week=None -> baseline (no AI)."""
    rebalance_dates = engine.dual_momentum.get_rebalance_dates(WINDOW_START, WINDOW_END, "daily")
    decisions = []
    daily_equity = []
    cash = Decimal("100.0")
    current_holding: AssetClass | None = None
    current_holding_symbol: str | None = None
    current_shares = Decimal("0")
    last_switch_date = None
    n_trades = 0

    def get_price(symbol: str, as_of: date) -> float | None:
        return engine._get_price(prices, symbol, as_of)

    # Track the current active tilt (applied until next decision day, per spec).
    active_regime = "mixed"
    active_bias: dict[str, float] = {}

    for rebal_date in rebalance_dates:
        if tilts_by_week is not None and rebal_date in tilts_by_week:
            tilt = tilts_by_week[rebal_date]
            active_bias = tilt["symbol_bias"]
            if tilt["regime_view"] != "mixed":
                active_regime = tilt["regime_view"]
            # 'mixed' keeps previous regime/thresholds per spec.

        day_prices = apply_price_tilt(prices, rebal_date, active_bias) if active_bias else prices

        signals = {}
        for name, strategy in [
            ("dual_momentum", engine.dual_momentum),
            ("mean_reversion", engine.mean_reversion),
            ("multi_timeframe", engine.multi_timeframe),
        ]:
            try:
                src_prices = day_prices if name == "dual_momentum" else prices
                sig = strategy.generate_signal(prices=src_prices, calc_date=rebal_date, current_holding=current_holding)
                signals[name] = engine._normalize_signal(sig)
            except Exception as e:
                signals[name] = {"action": "hold", "confidence": 0.0, "error": str(e)}

        market_context = engine._build_market_context(prices, rebal_date)
        if last_switch_date is not None:
            market_context["days_since_last_switch"] = (rebal_date - last_switch_date).days

        # Regime gate: override the DM instance's switch thresholds for this decision.
        if tilts_by_week is not None and active_regime in GATE_THRESHOLDS:
            engine.dual_momentum.equity_to_defensive_threshold = GATE_THRESHOLDS[active_regime]["equity_to_defensive_threshold"]
            engine.dual_momentum.defensive_to_equity_threshold = GATE_THRESHOLDS[active_regime]["defensive_to_equity_threshold"]
        elif tilts_by_week is not None:
            engine.dual_momentum.equity_to_defensive_threshold = 0.15
            engine.dual_momentum.defensive_to_equity_threshold = 0.05

        decision = engine.orchestrator.analyze(
            signals=signals, market_context=market_context, current_holding=current_holding_symbol,
        )

        action = decision.action
        target_symbol = decision.asset_symbol
        position_size_pct = decision.position_size_pct

        trade_happened = False
        if action == SignalAction.BUY and target_symbol:
            target_asset_class = engine._symbol_to_asset_class(target_symbol)
            target_asset = engine._asset_for_class(target_asset_class) if target_asset_class else None

            if current_holding_symbol == target_symbol and current_shares > 0:
                pass
            elif target_asset_class == AssetClass.CASH:
                if current_holding and current_shares > 0:
                    held_asset = engine._asset_for_class(current_holding)
                    held_symbol = (held_asset.yahoo_symbol or held_asset.symbol) if held_asset else None
                    sell_price = get_price(held_symbol, rebal_date) if held_symbol else None
                    if sell_price:
                        sell_value = float(current_shares) * sell_price
                        commission = sell_value * engine.transaction_cost_pct
                        cash += Decimal(str(sell_value - commission))
                        current_shares = Decimal("0")
                current_holding = AssetClass.CASH
                current_holding_symbol = "CASH"
                last_switch_date = rebal_date
                trade_happened = True
            elif target_asset and target_asset.yahoo_symbol:
                buy_price = get_price(target_asset.yahoo_symbol, rebal_date)
                if buy_price:
                    if current_holding and current_holding != AssetClass.CASH and current_shares > 0:
                        old_asset = engine._asset_for_class(current_holding) or ASSET_REGISTRY[current_holding]
                        sell_price = get_price(old_asset.yahoo_symbol or old_asset.symbol, rebal_date)
                        if sell_price:
                            sell_value = float(current_shares) * sell_price
                            commission = sell_value * engine.transaction_cost_pct
                            cash += Decimal(str(sell_value - commission))
                            current_shares = Decimal("0")
                    buy_value = float(cash) * position_size_pct
                    remaining_cash = float(cash) - buy_value
                    commission = buy_value * engine.transaction_cost_pct
                    net_value = buy_value - commission
                    shares_to_buy = Decimal(str(net_value / buy_price))
                    cash = Decimal(str(remaining_cash))
                    current_shares = shares_to_buy
                    current_holding = target_asset_class
                    current_holding_symbol = target_symbol
                    last_switch_date = rebal_date
                    trade_happened = True
        elif action == SignalAction.SELL:
            if current_holding and current_holding != AssetClass.CASH and current_shares > 0:
                old_asset = engine._asset_for_class(current_holding) or ASSET_REGISTRY[current_holding]
                sell_price = get_price(old_asset.yahoo_symbol or old_asset.symbol, rebal_date)
                if sell_price:
                    sell_value = float(current_shares) * sell_price
                    commission = sell_value * engine.transaction_cost_pct
                    cash += Decimal(str(sell_value - commission))
                    current_shares = Decimal("0")
                    current_holding = AssetClass.CASH
                    current_holding_symbol = "CASH"
                    trade_happened = True

        if trade_happened:
            n_trades += 1

        current_price = None
        if current_holding and current_holding != AssetClass.CASH and current_shares > 0:
            held_asset = engine._asset_for_class(current_holding)
            if held_asset and held_asset.yahoo_symbol:
                current_price = get_price(held_asset.yahoo_symbol, rebal_date)

        total_value = float(current_shares) * current_price + float(cash) if current_price and current_shares > 0 else float(cash)

        decisions.append({
            "date": rebal_date.isoformat(),
            "action": action.value,
            "symbol": target_symbol,
            "reason": decision.reasoning,
            "active_regime": active_regime if tilts_by_week is not None else None,
            "active_bias": dict(active_bias) if (tilts_by_week is not None and active_bias) else {},
        })
        daily_equity.append({
            "date": rebal_date.isoformat(),
            "equity": round(total_value, 4),
            "holding": current_holding_symbol or "CASH",
        })

    return decisions, daily_equity, n_trades


def compute_metrics(daily_equity: list[dict], n_trades: int) -> dict:
    final_equity = daily_equity[-1]["equity"] if daily_equity else 100.0
    final_return_pct = round((final_equity / 100.0 - 1) * 100, 4)

    peak = 100.0
    max_dd = 0.0
    for d in daily_equity:
        v = d["equity"]
        if v > peak:
            peak = v
        dd = (peak - v) / peak
        if dd > max_dd:
            max_dd = dd

    rets = []
    for i in range(1, len(daily_equity)):
        prev = daily_equity[i - 1]["equity"]
        cur = daily_equity[i]["equity"]
        if prev:
            rets.append(cur / prev - 1)
    sharpe = None
    if len(rets) > 1:
        s = pd.Series(rets)
        std = s.std()
        if std and std > 0:
            sharpe = round(float(s.mean() / std * (252 ** 0.5)), 4)

    return {
        "final_return_pct": final_return_pct,
        "max_drawdown_pct": round(max_dd * 100, 4),
        "sharpe": sharpe,
        "n_trades": n_trades,
    }


# ---------------------------------------------------------------------------
# Tier 2: Claw calibration
# ---------------------------------------------------------------------------

def build_claw_calibration(weekly_tilts: dict[date, dict]) -> list[dict]:
    sorted_weeks = sorted(weekly_tilts.keys())
    table = []
    for date_str, claw_action, favored_symbol in CLAW_ACTIONS:
        claw_date = date.fromisoformat(date_str)
        # nearest AI tilt week <= claw_date (most recent decision in effect that day)
        candidates = [d for d in sorted_weeks if d <= claw_date]
        nearest = max(candidates) if candidates else (sorted_weeks[0] if sorted_weeks else None)
        if nearest is None:
            table.append({"date": date_str, "claw_action": claw_action, "ai_tilt_week": None, "agreement": "no_ai_data"})
            continue
        tilt = weekly_tilts[nearest]
        bias = tilt["symbol_bias"]
        agreement = "neutral"
        if favored_symbol is None:
            agreement = "neutral (sell/exit, no single favored symbol)"
        elif favored_symbol in bias:
            b = bias[favored_symbol]
            agreement = "agree" if b > 0 else ("disagree" if b < 0 else "neutral")
        elif tilt["regime_view"] == "risk_off" and favored_symbol in ("GLD",):
            agreement = "agree (regime_view risk_off favors defensive GLD)"
        elif tilt["regime_view"] == "risk_on" and favored_symbol in ("QQQ", "IWM", "XLK", "XLE", "EEM"):
            agreement = "agree (regime_view risk_on favors equity rotation)"
        table.append({
            "date": date_str,
            "claw_action": claw_action,
            "ai_tilt_week": nearest.isoformat(),
            "ai_regime_view": tilt["regime_view"],
            "ai_symbol_bias": bias,
            "ai_reasoning": tilt["reasoning"],
            "agreement": agreement,
        })
    return table


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    provider = CachedPriceProvider()
    symbols = get_all_yahoo_symbols()
    for extra in ("SPY", "QQQ"):
        if extra not in symbols:
            symbols.append(extra)

    fetch_start = date(WINDOW_START.year - 2, WINDOW_START.month, WINDOW_START.day)
    print(f"Fetching {len(symbols)} symbols {fetch_start} -> {WINDOW_END}...")
    prices = provider.get_multi_prices(symbols, fetch_start, WINDOW_END)
    if prices.empty:
        print("ERROR: no price data")
        sys.exit(1)
    print(f"{len(prices)} price rows fetched")

    baseline_engine = build_engine()
    print("\n=== Baseline replay (A2, no AI) ===")
    _, baseline_equity, baseline_trades = run_replay(prices, baseline_engine, tilts_by_week=None)
    baseline_metrics = compute_metrics(baseline_equity, baseline_trades)
    print(f"Baseline: {baseline_metrics}")

    # Weekly decision-day cadence: Monday, or first trading day of the week.
    all_days = pd.bdate_range(WINDOW_START, WINDOW_END).date.tolist()
    week_starts = []
    seen_weeks = set()
    for d in all_days:
        wk = d.isocalendar()[:2]
        if wk not in seen_weeks:
            seen_weeks.add(wk)
            week_starts.append(d)

    print(f"\n=== Weekly AI tilt calls: {len(week_starts)} decision days ===")
    tilt_engine = build_engine()
    cache = load_cache()

    weekly_tilts: dict[date, dict] = {}
    total_latency = 0.0
    latencies = []

    # Use the baseline (no-AI) run's own daily decision/holding log as the
    # ground truth for "current position / days held / deterministic signal"
    # shown to the AI each week -- this is what a live overlay would see: the
    # mechanical strategy's own state, independent of the AI's own past tilts.
    baseline_decisions, _, _ = run_replay(prices, build_engine(), tilts_by_week=None)
    decisions_by_date = {d["date"]: d for d in baseline_decisions}
    equity_by_date = {d["date"]: d for d in baseline_equity}

    days_held = 0
    prev_holding = None
    holding_since = {}
    for d in baseline_equity:
        h = d["holding"]
        if h != prev_holding:
            holding_since[d["date"]] = d["date"]
            cur_since = d["date"]
        else:
            holding_since[d["date"]] = cur_since
        prev_holding = h

    for i, wk_date in enumerate(week_starts, 1):
        ds = wk_date.isoformat()
        eq_row = equity_by_date.get(ds)
        dec_row = decisions_by_date.get(ds)
        cur_holding_symbol = eq_row["holding"] if eq_row else "CASH"
        since = holding_since.get(ds, ds)
        held_days = (date.fromisoformat(ds) - date.fromisoformat(since)).days

        no_tlt_assets = tilt_engine.dual_momentum.assets
        context = build_context_pack(
            prices=prices,
            calc_date=wk_date,
            dm_assets=no_tlt_assets,
            current_holding_symbol=None if cur_holding_symbol == "CASH" else cur_holding_symbol,
            days_held=held_days,
            deterministic_signal={
                "action": dec_row["action"] if dec_row else None,
                "symbol": dec_row["symbol"] if dec_row else None,
                "reason": dec_row["reason"] if dec_row else None,
            },
        )

        tilt, latency = get_ai_tilt(wk_date, context, cache)
        latencies.append(latency)
        total_latency += latency
        weekly_tilts[wk_date] = tilt

        top_bias = max(tilt["symbol_bias"].items(), key=lambda kv: abs(kv[1]), default=None)
        print(f"[{i}/{len(week_starts)}] {ds}  regime={tilt['regime_view']:9s}  "
              f"top_bias={top_bias}  conf={tilt['confidence']:.2f}  latency={latency:.1f}s")

    print(f"\nTotal AI call latency: {total_latency:.1f}s ({sum(1 for l in latencies if l > 0)} live calls, "
          f"{sum(1 for l in latencies if l == 0)} cache hits)")

    print("\n=== AI-tilted replay (A2 + AI) ===")
    ai_engine = build_engine()
    ai_decisions, ai_equity, ai_trades = run_replay(prices, ai_engine, tilts_by_week=weekly_tilts)
    ai_metrics = compute_metrics(ai_equity, ai_trades)
    print(f"A2+AI: {ai_metrics}")

    print("\n=== Claw calibration ===")
    claw_calibration = build_claw_calibration(weekly_tilts)
    for row in claw_calibration:
        print(f"  {row['date']}  {row['claw_action']:20s}  ai_week={row.get('ai_tilt_week')}  "
              f"regime={row.get('ai_regime_view')}  {row['agreement']}")

    weekly_timeline = []
    for wk_date in week_starts:
        tilt = weekly_tilts[wk_date]
        weekly_timeline.append({
            "date": wk_date.isoformat(),
            "regime_view": tilt["regime_view"],
            "symbol_bias": tilt["symbol_bias"],
            "confidence": tilt["confidence"],
            "reasoning": tilt["reasoning"],
        })

    out = {
        "window": {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()},
        "baseline_a2": baseline_metrics,
        "a2_plus_ai": ai_metrics,
        "delta": {
            "final_return_pct": round(ai_metrics["final_return_pct"] - baseline_metrics["final_return_pct"], 4),
            "max_drawdown_pct": round(ai_metrics["max_drawdown_pct"] - baseline_metrics["max_drawdown_pct"], 4),
            "n_trades": ai_metrics["n_trades"] - baseline_metrics["n_trades"],
        },
        "weekly_ai_tilts": weekly_timeline,
        "ai_tilted_decisions": ai_decisions,
        "ai_tilted_daily_equity": ai_equity,
        "baseline_daily_equity": baseline_equity,
        "claw_calibration": claw_calibration,
        "cli_stats": {
            "n_calls_planned": len(week_starts),
            "n_live_calls": sum(1 for l in latencies if l > 0),
            "n_cache_hits": sum(1 for l in latencies if l == 0),
            "total_latency_s": round(total_latency, 1),
            "avg_live_latency_s": round(total_latency / max(1, sum(1 for l in latencies if l > 0)), 1),
            "max_latency_s": round(max(latencies), 1) if latencies else 0,
        },
        "notes": [
            "Tilt mechanism: symbol_bias is injected by scaling the biased symbol's "
            "price on the decision date so momentum_12m shifts by exactly the bias "
            "amount, clamped to +/-0.03; DualMomentumStrategy.generate_signal and the "
            "orchestrator run completely unmodified on the adjusted price series -- "
            "no strategy logic was reimplemented.",
            "regime_view risk_off sets DM thresholds to (equity_to_defensive=0.02, "
            "defensive_to_equity=0.15); risk_on sets (0.15, 0.05, the live default); "
            "mixed leaves the previously active thresholds in place.",
            "Tilts persist from one weekly decision day until the next (applied to "
            "every daily rebalance date in between), per the task spec.",
            "Context pack is built from the BASELINE (no-AI) replay's own daily "
            "holding/decision log, not from the AI-tilted run -- the AI always sees "
            "the mechanical strategy's own state, never its own prior tilts feeding "
            "back into its next context (avoids a self-reinforcing loop in this replay).",
            "Price-only context is a real limitation: it omits news, macro releases, "
            "and cross-asset flows an informed discretionary overlay would use live -- "
            "expect this to understate a genuinely informed overlay's value.",
            "Single regime cycle (Feb-Jul 2026, broadly risk-on) -- directional "
            "evidence only, not a generalization claim.",
            "Every AI call is the `claude` CLI in non-interactive print mode "
            "(claude -p ... --model sonnet --output-format json), never the "
            "anthropic SDK; cutoff (Jan 2026) is strictly before the window start.",
        ],
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2))
    print(f"\nWrote {OUT_PATH}")
    print(f"Baseline: {baseline_metrics}")
    print(f"A2+AI:    {ai_metrics}")
    print(f"Delta:    {out['delta']}")


if __name__ == "__main__":
    main()
