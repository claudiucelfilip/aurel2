"""Context-pack builders for the AI overlay (v1/v2/v3), point-in-time clean.

docs/plans/2026-07-10-ai-overlay-design.md, "Context pack (single ablation step)":
  v1 (done): prices, momentum table, vol, position, core signal.
  v2: + per-asset RSI(14)/z-scores (16 assets), momentum-spread alarm (1-3m vs 12m
      divergence per asset).
  v3: + breadth (% above 50/200dma), vol rate-of-change, GLD/TLT vs equity relative
      strength, bond-ETF spreads, self-awareness block (drawdown from peak, days
      held, core's projected next action).

Every function here takes `hist`, a price frame ALREADY CLIPPED to <= the prior
close before the pack's as_of date (see clip_to_as_of below) -- callers must clip
before calling, and no function reaches past that boundary, so no pack can leak
future data (point-in-time-clean, the ablation's hard requirement).

Production and the ablation harness both import this module -- it is the single
source of truth for what the AI overlay sees; there is no separate prod copy.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from aurel2.core.assets import ASSET_REGISTRY
from aurel2.core.models import AssetClass


def _to_native(obj):
    """Recursively convert numpy scalars (bool_/int64/float64) to plain Python
    types so packs are `json.dumps`-safe -- pandas/numpy comparisons and
    reductions leak numpy scalar types even when the inputs are plain floats.
    """
    if isinstance(obj, dict):
        return {k: _to_native(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_native(v) for v in obj]
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    return obj

# Bond ETFs used for the v3 "bond-ETF spreads" sense (duration-spread proxies).
BOND_SYMBOLS = ["SHY", "IEF", "TLT", "AGG", "TIP"]
DEFENSIVE_SYMBOLS = ["GLD", "AGG", "SHY", "IEF", "TIP"]


def clip_to_as_of(prices: pd.DataFrame, as_of: date) -> pd.DataFrame:
    """Return only rows strictly before `as_of` (prior-close data only)."""
    prior_cutoff = pd.Timestamp(as_of) - pd.Timedelta(days=1)
    out = prices[pd.to_datetime(prices["date"]) <= prior_cutoff]
    return out


def _series(hist: pd.DataFrame, symbol: str) -> pd.Series:
    sp = hist[hist["symbol"] == symbol].sort_values("date")
    return sp.set_index(pd.to_datetime(sp["date"]))["close"]


def _pct_return(hist: pd.DataFrame, symbol: str, as_of: date, months: int) -> float | None:
    sp = hist[hist["symbol"] == symbol].sort_values("date")
    if sp.empty:
        return None
    cur = sp.iloc[-1]["close"]
    cutoff = pd.Timestamp(as_of) - pd.DateOffset(months=months)
    past = sp[pd.to_datetime(sp["date"]) <= cutoff]
    if past.empty:
        return None
    past_price = past.iloc[-1]["close"]
    if past_price == 0:
        return None
    return round((cur / past_price - 1) * 100, 3)


def _rsi14(closes: pd.Series) -> float | None:
    """Classic Wilder RSI(14). Needs >=15 closes; returns None otherwise."""
    if len(closes) < 15:
        return None
    delta = closes.diff().dropna()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    avg_gain = gains.tail(14).mean()
    avg_loss = losses.tail(14).mean()
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def _zscore(closes: pd.Series, window: int = 60) -> float | None:
    """Z-score of the latest close vs its trailing `window`-day mean/std."""
    tail = closes.tail(window)
    if len(tail) < 20:
        return None
    std = tail.std()
    if not std or std == 0:
        return None
    return round(float((tail.iloc[-1] - tail.mean()) / std), 3)


def _sma(closes: pd.Series, window: int) -> float | None:
    tail = closes.tail(window)
    if len(tail) < window:
        return None
    return float(tail.mean())


def _universe_symbols() -> list[str]:
    return sorted({a.yahoo_symbol for a in ASSET_REGISTRY.values() if a.yahoo_symbol})


# ---------------------------------------------------------------------------
# v1: prices, momentum table, vol, position, core signal (ported from the
# original blind harness's build_context_pack, unchanged in substance).
# ---------------------------------------------------------------------------

def build_context_pack_v1(
    prices: pd.DataFrame,
    calc_date: date,
    dm_assets: dict[AssetClass, object],
    current_holding_symbol: str | None,
    days_held: int,
    deterministic_signal: dict,
    extra_symbols: list[str] | None = None,
) -> dict:
    hist = clip_to_as_of(prices, calc_date)
    extra_symbols = extra_symbols or ["SPY", "QQQ", "GLD"]
    universe_symbols = sorted({a.yahoo_symbol for a in dm_assets.values() if a.yahoo_symbol} | set(extra_symbols))

    last20_pct_change = {}
    for sym in universe_symbols:
        sp = hist[hist["symbol"] == sym].sort_values("date")
        closes = sp["close"].tail(21).tolist()
        if len(closes) < 2:
            continue
        pct = [round((closes[i] / closes[i - 1] - 1) * 100, 3) for i in range(1, len(closes))]
        last20_pct_change[sym] = pct[-20:]

    momentum_snapshot = {}
    for ac, asset in dm_assets.items():
        if ac == AssetClass.CASH or not asset.yahoo_symbol:
            continue
        momentum_snapshot[asset.yahoo_symbol] = {
            "1m": _pct_return(hist, asset.yahoo_symbol, calc_date, 1),
            "3m": _pct_return(hist, asset.yahoo_symbol, calc_date, 3),
            "12m": _pct_return(hist, asset.yahoo_symbol, calc_date, 12),
        }

    dm_ranking = sorted(
        ((sym, v["12m"]) for sym, v in momentum_snapshot.items() if v["12m"] is not None),
        key=lambda x: x[1],
        reverse=True,
    )[:5]

    spy_closes = _series(hist, "SPY").tail(21)
    spy_20d_vol = None
    if len(spy_closes) > 5:
        rets = spy_closes.pct_change().dropna()
        spy_20d_vol = round(float(rets.std() * (252 ** 0.5) * 100), 2)

    held_drawdown = None
    if current_holding_symbol and current_holding_symbol not in ("CASH", None):
        hp = hist[hist["symbol"] == current_holding_symbol].sort_values("date")
        closes_12m = hp[pd.to_datetime(hp["date"]) >= pd.Timestamp(calc_date) - pd.DateOffset(months=12)]["close"]
        if not closes_12m.empty:
            peak = closes_12m.max()
            cur = closes_12m.iloc[-1]
            held_drawdown = round((cur / peak - 1) * 100, 3)

    return _to_native({
        "as_of": calc_date.isoformat(),
        "last_20d_pct_change": last20_pct_change,
        "momentum_snapshot_pct": momentum_snapshot,
        "dm_top5_ranking_12m": dm_ranking,
        "spy_20d_annualized_vol_pct": spy_20d_vol,
        "current_position": current_holding_symbol or "CASH",
        "days_held": days_held,
        "held_drawdown_from_12m_high_pct": held_drawdown,
        "deterministic_a2_signal_today": deterministic_signal,
    })


# ---------------------------------------------------------------------------
# v2: + per-asset RSI(14) + z-scores (all universe assets) + momentum-spread
# alarm (1-3m vs 12m divergence per asset).
# ---------------------------------------------------------------------------

def _momentum_spread_alarm(m1: float | None, m3: float | None, m12: float | None) -> dict | None:
    """Flags when short-term momentum (1-3m) diverges sharply from the 12m trend
    that DM's ranking is based on -- an early warning that the 12m winner's recent
    behavior no longer matches its trailing-year story (blow-off top or dead-cat
    reversal), which single-timeframe momentum tables can't show.
    """
    if m1 is None or m3 is None or m12 is None:
        return None
    short_term = (m1 + m3) / 2
    spread = round(float(short_term - m12 / 4), 3)  # m12/4 approximates a comparable ~3m-scaled trend rate
    alarm = bool(abs(spread) > 8.0)  # short-term running >8pp hotter/colder than the 12m-implied pace
    return {"short_vs_12m_spread_pp": spread, "alarm": alarm}


def build_context_pack_v2(
    prices: pd.DataFrame,
    calc_date: date,
    dm_assets: dict[AssetClass, object],
    current_holding_symbol: str | None,
    days_held: int,
    deterministic_signal: dict,
    extra_symbols: list[str] | None = None,
) -> dict:
    pack = build_context_pack_v1(
        prices, calc_date, dm_assets, current_holding_symbol, days_held,
        deterministic_signal, extra_symbols,
    )
    hist = clip_to_as_of(prices, calc_date)
    universe_symbols = sorted(set(_universe_symbols()) | set(pack["momentum_snapshot_pct"].keys()))

    technicals = {}
    spread_alarms = {}
    for sym in universe_symbols:
        closes = _series(hist, sym)
        if closes.empty:
            continue
        technicals[sym] = {
            "rsi_14": _rsi14(closes),
            "zscore_60d": _zscore(closes, window=60),
        }
        mom = pack["momentum_snapshot_pct"].get(sym)
        if mom:
            spread_alarms[sym] = _momentum_spread_alarm(mom.get("1m"), mom.get("3m"), mom.get("12m"))

    pack["per_asset_technicals"] = technicals
    pack["momentum_spread_alarm"] = spread_alarms
    return _to_native(pack)


# ---------------------------------------------------------------------------
# v3: + breadth, vol rate-of-change, GLD/TLT-vs-equity relative strength,
# bond-ETF spreads, self-awareness block.
# ---------------------------------------------------------------------------

def _breadth(hist: pd.DataFrame, universe_symbols: list[str]) -> dict:
    above_50, above_200, n = 0, 0, 0
    for sym in universe_symbols:
        closes = _series(hist, sym)
        if closes.empty:
            continue
        cur = closes.iloc[-1]
        sma50 = _sma(closes, 50)
        sma200 = _sma(closes, 200)
        if sma50 is None and sma200 is None:
            continue
        n += 1
        if sma50 is not None and cur > sma50:
            above_50 += 1
        if sma200 is not None and cur > sma200:
            above_200 += 1
    if n == 0:
        return {"pct_above_50dma": None, "pct_above_200dma": None, "n_assets": 0}
    return {
        "pct_above_50dma": round(above_50 / n * 100, 1),
        "pct_above_200dma": round(above_200 / n * 100, 1),
        "n_assets": n,
    }


def _vol_rate_of_change(hist: pd.DataFrame, symbol: str = "SPY") -> dict | None:
    """20d annualized vol now vs. 20d annualized vol as of 20 trading days ago --
    is volatility itself accelerating or decaying, not just its current level.
    """
    closes = _series(hist, symbol)
    if len(closes) < 45:
        return None
    rets = closes.pct_change().dropna()
    vol_now = round(float(rets.tail(20).std() * (252 ** 0.5) * 100), 2)
    vol_prior = round(float(rets.tail(40).head(20).std() * (252 ** 0.5) * 100), 2)
    return {"vol_20d_now_pct": vol_now, "vol_20d_20d_ago_pct": vol_prior, "vol_roc_pct": round(vol_now - vol_prior, 2)}


def _relative_strength(hist: pd.DataFrame, defensive: str, equity: str = "SPY", months: int = 3) -> float | None:
    """defensive's `months`-return minus equity's -- positive means the defensive
    asset (safe haven / duration) is outperforming stocks, a risk-off tell distinct
    from any single asset's own momentum number.
    """
    d = _pct_return(hist, defensive, hist["date"].max().date() if not hist.empty else date.today(), months)
    e = _pct_return(hist, equity, hist["date"].max().date() if not hist.empty else date.today(), months)
    if d is None or e is None:
        return None
    return round(d - e, 3)


def _bond_spreads(hist: pd.DataFrame, as_of: date) -> dict:
    """Pairwise 3m-return spreads across the duration curve (SHY short, IEF mid,
    TLT long) plus credit-adjacent AGG/TIP -- a steepening/flattening tell that a
    single bond ETF's own momentum can't show.
    """
    rets_3m = {sym: _pct_return(hist, sym, as_of, 3) for sym in BOND_SYMBOLS}
    spreads = {}
    if rets_3m.get("TLT") is not None and rets_3m.get("SHY") is not None:
        spreads["TLT_minus_SHY_3m_pp"] = round(rets_3m["TLT"] - rets_3m["SHY"], 3)
    if rets_3m.get("IEF") is not None and rets_3m.get("SHY") is not None:
        spreads["IEF_minus_SHY_3m_pp"] = round(rets_3m["IEF"] - rets_3m["SHY"], 3)
    if rets_3m.get("TIP") is not None and rets_3m.get("AGG") is not None:
        spreads["TIP_minus_AGG_3m_pp"] = round(rets_3m["TIP"] - rets_3m["AGG"], 3)
    return {"returns_3m_pct": rets_3m, "spreads_pp": spreads}


def _project_core_next_action(
    momentum_snapshot: dict, current_holding_symbol: str | None, switch_threshold: float = 0.04,
) -> dict:
    """What the deterministic core would do next if today's 12m ranking held --
    NOT a new decision rule, just exposing the same threshold logic
    DualMomentumStrategy already applies (docs/plans §self-awareness block), so
    the AI knows what its own host algorithm is about to do rather than only what
    it did today.
    """
    ranked = sorted(
        ((sym, v["12m"]) for sym, v in momentum_snapshot.items() if v.get("12m") is not None),
        key=lambda x: x[1], reverse=True,
    )
    if not ranked:
        return {"projected_winner": None, "would_switch": False}
    winner_sym, winner_mom = ranked[0]
    current_mom = momentum_snapshot.get(current_holding_symbol, {}).get("12m") if current_holding_symbol else None
    would_switch = (
        winner_sym != current_holding_symbol
        and (current_mom is None or winner_mom - current_mom > switch_threshold * 100)
    )
    return {"projected_winner": winner_sym, "projected_winner_momentum_12m_pct": winner_mom, "would_switch_if_unchanged": would_switch}


def build_context_pack_v3(
    prices: pd.DataFrame,
    calc_date: date,
    dm_assets: dict[AssetClass, object],
    current_holding_symbol: str | None,
    days_held: int,
    deterministic_signal: dict,
    extra_symbols: list[str] | None = None,
) -> dict:
    pack = build_context_pack_v2(
        prices, calc_date, dm_assets, current_holding_symbol, days_held,
        deterministic_signal, extra_symbols,
    )
    hist = clip_to_as_of(prices, calc_date)
    universe_symbols = sorted(set(_universe_symbols()) | set(pack["momentum_snapshot_pct"].keys()))

    pack["breadth"] = _breadth(hist, universe_symbols)
    pack["vol_rate_of_change"] = _vol_rate_of_change(hist, "SPY")
    pack["relative_strength_vs_equity_3m_pp"] = {
        "GLD": _relative_strength(hist, "GLD", "SPY", 3),
        "TLT": _relative_strength(hist, "TLT", "SPY", 3),
    }
    pack["bond_etf_spreads"] = _bond_spreads(hist, calc_date)
    pack["self_awareness"] = {
        "current_drawdown_from_peak_pct": pack["held_drawdown_from_12m_high_pct"],
        "days_held": days_held,
        "core_projected_next_action": _project_core_next_action(
            pack["momentum_snapshot_pct"], pack["current_position"] if pack["current_position"] != "CASH" else None,
        ),
    }
    return _to_native(pack)


BUILDERS = {
    "v1": build_context_pack_v1,
    "v2": build_context_pack_v2,
    "v3": build_context_pack_v3,
}
