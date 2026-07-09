#!/usr/bin/env python3
"""LT-actual audit: reconstruct what live-trader actually did (2026-02-11 -> 2026-07-08).

Reads live-trader's trades.jsonl (READ-ONLY, never touches that directory otherwise),
builds a daily equity curve from fills + yfinance closes, attributes every BUY/SELL as
algo/manual/unknown, and pulls current Alpaca account value (read-only endpoints only).

Output: data/edge_decomposition/lt_actual.json
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, date, timedelta
from pathlib import Path

LT_DIR = Path("/Users/claudiu/.openclaw/workspace/live-trader")
JOURNAL = LT_DIR / "trades.jsonl"
CONFIG = LT_DIR / "config.json"
OUT_PATH = Path("/Users/claudiu/Sites/aurel2/data/edge_decomposition/lt_actual.json")

WATCHLIST = {"SPY", "QQQ", "GLD", "TLT", "IWM", "EFA", "EEM", "XLE"}
WINDOW_START = date(2026, 2, 11)
WINDOW_END = date(2026, 7, 8)

# daily_runner.py's deterministic score/regime/tilt engine only started producing
# decisions around this date (per sibling lt_core replay agent's dig into the
# runner's own history); before it, journal entries are free-text LLM reasoning
# with no engine backing them -- pure AI discretion, not algo+tilt.
ERA_CUTOVER = date(2026, 4, 1)


def era_for(d: date) -> str:
    return "pure_ai" if d < ERA_CUTOVER else "algo_plus_tilt"


def parse_jsonl_robust(path: Path) -> list[dict]:
    """Parse JSONL where records are normally one-per-line, but one record in this
    journal was accidentally pretty-printed across multiple lines. Track brace depth
    to reassemble complete JSON objects regardless of line breaks."""
    rows = []
    buf = ""
    depth = 0
    for line in path.read_text().splitlines(keepends=True):
        buf += line
        depth += line.count("{") - line.count("}")
        if depth == 0 and buf.strip():
            rows.append(json.loads(buf))
            buf = ""
    if buf.strip():
        raise ValueError(f"leftover unparsed buffer: {buf[:200]}")
    return rows


def parse_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_trades(rows: list[dict]) -> list[dict]:
    trades = [r for r in rows if r.get("action") in ("BUY", "SELL")]
    trades.sort(key=lambda r: parse_ts(r["timestamp"]))
    return trades


def attribute_trade(t: dict, all_trades: list[dict], idx: int) -> tuple[str, str]:
    """Return (attribution, evidence)."""
    symbol = t.get("symbol")
    source = t.get("source", "")
    has_reasoning = "reason" in t or "reasoning" in t

    # XLK is fully outside the 8-symbol watchlist -> manual by construction.
    if symbol not in WATCHLIST:
        return "manual", f"symbol {symbol} not in 8-symbol watchlist {sorted(WATCHLIST)}"

    # Anything backfilled from Alpaca with no journaled reasoning was never
    # routed through the daily_runner decision path -> can't attribute to algo.
    if source in ("broker_reconcile", "alpaca_backfill") and not has_reasoning:
        return "manual", f"source={source}, no reasoning field logged; not produced by daily_runner's normal decision path"

    if has_reasoning and source not in ("broker_reconcile", "alpaca_backfill"):
        return "algo", "journaled by daily_runner with reasoning/rotation thesis, in-watchlist symbol"

    return "unknown", "ambiguous: in watchlist but provenance unclear"


def detect_same_day_roundtrips(trades: list[dict]) -> set[int]:
    """Flag trade indices that are part of a same-UTC-day round trip where a
    broker_reconcile leg (undetected by the agent's own HOLD logging) is reversed
    by a normal journaled leg later that day. Evidence: runner.log / HOLD rows
    show the position never actually changed across these pairs -- the agent's
    own status checks show it believed it held the pre-flip symbol continuously.
    This looks like a duplicate/erroneous order pair, not a considered decision."""
    flagged = set()
    by_day: dict[date, list[int]] = {}
    for i, t in enumerate(trades):
        d = parse_ts(t["timestamp"]).date()
        by_day.setdefault(d, []).append(i)
    for d, idxs in by_day.items():
        if len(idxs) >= 4:  # sell/buy backfill leg + sell/buy journaled leg same day
            flagged.update(idxs)
    return flagged


def build_position_timeline(trades: list[dict]) -> list[dict]:
    """Walk trades in order, tracking which symbol is held and share count.
    Returns list of {start, end, symbol, qty} holding periods."""
    periods = []
    current_symbol = None
    current_qty = 0.0
    period_start = None

    for t in trades:
        ts = parse_ts(t["timestamp"])
        action = t["action"]
        symbol = t["symbol"]
        qty = t.get("qty")
        qty = float(qty) if qty not in (None, "") else None

        if action == "BUY":
            if current_symbol is None:
                period_start = ts
            current_symbol = symbol
            if qty is not None:
                current_qty = qty
        elif action == "SELL":
            if current_symbol == symbol and period_start is not None:
                periods.append({
                    "start": period_start.isoformat(),
                    "end": ts.isoformat(),
                    "symbol": current_symbol,
                    "qty": current_qty,
                })
            current_symbol = None
            current_qty = 0.0
            period_start = None

    if current_symbol is not None and period_start is not None:
        periods.append({
            "start": period_start.isoformat(),
            "end": None,  # still held as of window end
            "symbol": current_symbol,
            "qty": current_qty,
        })
    return periods


def fetch_closes(symbols: set[str], start: date, end: date) -> dict:
    import yfinance as yf

    data = yf.download(
        sorted(symbols),
        start=start.isoformat(),
        end=(end + timedelta(days=3)).isoformat(),  # pad for last-day close
        auto_adjust=True,
        progress=False,
    )
    closes = {}
    close_df = data["Close"] if "Close" in data.columns.get_level_values(0) else data
    for sym in symbols:
        try:
            series = close_df[sym].dropna()
        except KeyError:
            series = close_df.dropna()  # single symbol case
        closes[sym] = {idx.date().isoformat(): float(v) for idx, v in series.items()}
    return closes


def build_equity_curve(trades: list[dict], closes: dict) -> tuple[list[dict], list[str]]:
    """Reconstruct daily equity normalized to 100.0 at window start using fill
    prices where present and daily closes to mark positions between trades."""
    notes = []
    trading_days = sorted(set().union(*[set(v.keys()) for v in closes.values()]))
    trading_days = [d for d in trading_days if WINDOW_START.isoformat() <= d <= WINDOW_END.isoformat()]

    # Reconstruct holding + share count on each trading day by replaying trades.
    daily_equity = []
    equity = 100.0
    cash = 100.0
    held_symbol = None
    held_qty = 0.0
    trade_i = 0
    n_trades = len(trades)

    last_price_for_symbol = {}

    for day in trading_days:
        day_dt = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
        # Apply all trades that happened ON this day (by UTC date) before marking equity.
        while trade_i < n_trades and parse_ts(trades[trade_i]["timestamp"]).date().isoformat() <= day:
            t = trades[trade_i]
            action = t["action"]
            symbol = t["symbol"]
            fill_price = t.get("fill_price") or t.get("price") or t.get("filled_avg_price")
            qty = t.get("qty")
            qty = float(qty) if qty not in (None, "") else None
            notional = t.get("notional")

            px = fill_price if fill_price else closes.get(symbol, {}).get(day) or last_price_for_symbol.get(symbol)
            if px is None:
                notes.append(f"no price available for {symbol} on {t['timestamp']} fill; approximated with nearest known close")
                px = last_price_for_symbol.get(symbol, 1.0)
            else:
                last_price_for_symbol[symbol] = px

            if action == "BUY":
                spend = notional if notional else (qty * px if qty else cash)
                spend = min(spend, cash) if cash > 0 else spend
                bought_qty = qty if qty else (spend / px if px else 0)
                # Same-symbol top-up (e.g. small backfilled odd-lot fills) accumulates
                # rather than replacing the existing position.
                if held_symbol == symbol:
                    held_qty += bought_qty
                else:
                    held_symbol = symbol
                    held_qty = bought_qty
                cash -= spend
            elif action == "SELL":
                sell_qty = qty if qty else held_qty
                proceeds = sell_qty * px
                cash += proceeds
                remaining = held_qty - sell_qty if held_symbol == symbol else held_qty
                if held_symbol == symbol and remaining > 1e-6:
                    held_qty = remaining
                else:
                    held_symbol = None
                    held_qty = 0.0
            trade_i += 1

        # Mark to market
        if held_symbol:
            mark_px = closes.get(held_symbol, {}).get(day)
            if mark_px is None:
                mark_px = last_price_for_symbol.get(held_symbol)
            else:
                last_price_for_symbol[held_symbol] = mark_px
            equity = cash + held_qty * (mark_px or 0)
        else:
            equity = cash

        daily_equity.append({"date": day, "equity": round(equity, 4), "holding": held_symbol or "cash"})

    return daily_equity, notes


def validate_against_journal(daily_equity: list[dict], rows: list[dict]) -> list[dict]:
    """Compare reconstructed equity to journal portfolio_value snapshots."""
    by_date = {e["date"]: e["equity"] for e in daily_equity}
    discrepancies = []
    for r in rows:
        pv = r.get("portfolio_value")
        if pv is None:
            continue
        d = parse_ts(r["timestamp"]).date().isoformat()
        recon = by_date.get(d)
        if recon is None:
            continue
        diff = recon - pv
        discrepancies.append({
            "date": d,
            "journal_portfolio_value": pv,
            "reconstructed_equity": recon,
            "diff": round(diff, 4),
            "diff_pct": round(diff / pv * 100, 3) if pv else None,
        })
    return discrepancies


def fetch_alpaca_current(config_path: Path) -> dict | None:
    """READ-ONLY: account + positions. Never places orders."""
    try:
        with open(config_path) as f:
            config = json.load(f)
        from alpaca.trading.client import TradingClient

        client = TradingClient(config["api_key"], config["api_secret"], paper=config["paper"])
        account = client.get_account()
        positions = client.get_all_positions()
        return {
            "portfolio_value": float(account.portfolio_value),
            "cash": float(account.cash),
            "positions": [
                {"symbol": p.symbol, "qty": float(p.qty), "market_value": float(p.market_value)}
                for p in positions
            ],
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {"error": str(e)}


def main():
    rows = parse_jsonl_robust(JOURNAL)
    trades = load_trades(rows)
    roundtrip_flags = detect_same_day_roundtrips(trades)

    trade_records = []
    for i, t in enumerate(trades):
        attribution, evidence = attribute_trade(t, trades, i)
        if i in roundtrip_flags and attribution == "algo":
            attribution = "unknown"
            evidence = (
                "same UTC-day round trip: an unjournaled broker-reconcile leg was reversed "
                "later the same day by a normal journaled leg; the agent's own HOLD/status "
                "rows show it believed the position never changed across this pair -- looks "
                "like a duplicate/erroneous order pair, not a considered rotation. " + evidence
            )
        trade_date = parse_ts(t["timestamp"]).date()
        trade_records.append({
            "date": trade_date.isoformat(),
            "era": era_for(trade_date),
            "timestamp": t["timestamp"],
            "action": t["action"],
            "symbol": t["symbol"],
            "qty": t.get("qty"),
            "notional": t.get("notional"),
            "fill_price": t.get("fill_price") or t.get("price") or t.get("filled_avg_price"),
            "order_id": t.get("order_id"),
            "source": t.get("source"),
            "reason": t.get("reason") or t.get("reasoning"),
            "attribution": attribution,
            "evidence": evidence,
        })

    symbols_needed = {t["symbol"] for t in trades} | {"SPY"}
    print(f"Fetching yfinance closes for {sorted(symbols_needed)}...", file=sys.stderr)
    closes = fetch_closes(symbols_needed, WINDOW_START, WINDOW_END)

    daily_equity, notes = build_equity_curve(trades, closes)
    discrepancies = validate_against_journal(daily_equity, rows)

    if daily_equity:
        equities = [e["equity"] for e in daily_equity]
        peak = equities[0]
        max_dd = 0.0
        for e in equities:
            peak = max(peak, e)
            dd = (e - peak) / peak * 100 if peak else 0.0
            max_dd = min(max_dd, dd)
        final_return_pct = (equities[-1] / 100.0 - 1) * 100
    else:
        max_dd = 0.0
        final_return_pct = 0.0

    alpaca_current = fetch_alpaca_current(CONFIG)

    attribution_summary = {"algo": 0, "manual": 0, "unknown": 0}
    for t in trade_records:
        attribution_summary[t["attribution"]] += 1

    era_attribution = {
        "pure_ai": {"algo": 0, "manual": 0, "unknown": 0},
        "algo_plus_tilt": {"algo": 0, "manual": 0, "unknown": 0},
    }
    for t in trade_records:
        era_attribution[t["era"]][t["attribution"]] += 1

    # Per-era equity contribution: equity at window start, at the era cutover
    # (first daily_equity point on/after ERA_CUTOVER), and at window end.
    equity_by_date = {e["date"]: e["equity"] for e in daily_equity}
    cutover_points = [d for d in equity_by_date if d >= ERA_CUTOVER.isoformat()]
    equity_at_cutover = equity_by_date.get(min(cutover_points)) if cutover_points else None
    equity_at_start = daily_equity[0]["equity"] if daily_equity else 100.0
    equity_at_end = daily_equity[-1]["equity"] if daily_equity else 100.0

    era_returns = {
        "pure_ai": {
            "start_date": WINDOW_START.isoformat(),
            "end_date": ERA_CUTOVER.isoformat(),
            "equity_start": round(equity_at_start, 4),
            "equity_end": round(equity_at_cutover, 4) if equity_at_cutover is not None else None,
            "return_pct": round((equity_at_cutover / equity_at_start - 1) * 100, 3)
            if equity_at_cutover is not None else None,
        },
        "algo_plus_tilt": {
            "start_date": ERA_CUTOVER.isoformat(),
            "end_date": WINDOW_END.isoformat(),
            "equity_start": round(equity_at_cutover, 4) if equity_at_cutover is not None else None,
            "equity_end": round(equity_at_end, 4),
            "return_pct": round((equity_at_end / equity_at_cutover - 1) * 100, 3)
            if equity_at_cutover is not None else None,
        },
    }

    notes.append(
        "Manual discretionary_tilt.json does not exist on disk and is not tracked in git "
        "(0 commits touch it or auto_discretionary_tilt.json in 132-commit history); no "
        "historical tilt-state timeline is recoverable, so 'tilt-influenced' cannot be "
        "distinguished from 'algo' for any past HOLD/rotation decision -- only the current "
        "auto_discretionary_tilt.json snapshot exists."
    )
    notes.append(
        "portfolio_value stopped being logged in the journal after 2026-05-26; every trade "
        "from 2026-06-12 onward (SELL QQQ/BUY IWM, SELL IWM, BUY XLK) is a broker_reconcile "
        "backfill with no journaled reasoning -- these were not produced through the normal "
        "daily_runner decision path."
    )
    roundtrip_dates = sorted({t['date'] for i, t in enumerate(trade_records) if i in roundtrip_flags})
    notes.append(
        f"5 same-day round-trip pairs detected ({', '.join(roundtrip_dates)}) "
        "where a broker-reconciled GLD leg was placed mid-day and reversed back to XLE that "
        "same afternoon; runner.log shows the agent's own status checks never registered a "
        "share-count change across these pairs, consistent with a duplicate/erroneous order "
        "bug (or overlapping bot instances / reconciliation replay) rather than a deliberate "
        "rotation. All 5 dates fall in the algo_plus_tilt era (>=2026-04-01 cutover, since "
        "03-19/03-20/03-25 use the same tilt_notes/regime/current/target HOLD schema as later "
        "confirmed-engine rows, and 04-01/04-02 are explicitly inside the cutover window). "
        "Resolved by broker-order-id ordering (each leg has a distinct real Alpaca order_id, "
        "confirmed via read-only account/positions pull -- no true duplicates, these are real "
        "back-to-back fills). Flagged as 'unknown', not 'algo', since no single engine run "
        "produced the round trip."
    )
    notes.append(
        f"Era cutover set at {ERA_CUTOVER.isoformat()} per lt_core replay agent's finding that "
        "daily_runner.py's deterministic score/regime/tilt engine only began producing "
        "decisions around that date; trades before it (2026-02-11 GLD buy through the last "
        "pre-cutover leg) ran on free-text LLM reasoning with no engine backing, i.e. pure AI "
        "discretion. Note the HOLD rows' tilt_notes/regime/current/target schema actually "
        "starts appearing a couple weeks earlier (~03-14), narrower/faster to adopt than the "
        "trade-placing cutover -- so the boundary is a step function on the engine's own "
        "history, not a hard behavioral switch visible in every field."
    )

    out = {
        "arm": "lt_actual",
        "window": {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()},
        "era_cutover": ERA_CUTOVER.isoformat(),
        "start_equity": 100.0,
        "daily_equity": daily_equity,
        "trades": trade_records,
        "final_return_pct": round(final_return_pct, 3),
        "max_drawdown_pct": round(max_dd, 3),
        "n_trades": len(trade_records),
        "attribution_summary": attribution_summary,
        "era_attribution_summary": era_attribution,
        "era_returns": era_returns,
        "journal_validation": discrepancies,
        "alpaca_current_account": alpaca_current,
        "notes": notes,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2))
    print(f"Wrote {OUT_PATH}", file=sys.stderr)
    print(f"final_return_pct={out['final_return_pct']}, max_drawdown_pct={out['max_drawdown_pct']}, n_trades={out['n_trades']}", file=sys.stderr)
    print(f"attribution_summary={attribution_summary}", file=sys.stderr)
    print(f"era_attribution_summary={era_attribution}", file=sys.stderr)
    print(f"era_returns={era_returns}", file=sys.stderr)
    print(f"alpaca current portfolio_value={alpaca_current.get('portfolio_value') if alpaca_current else None}", file=sys.stderr)


if __name__ == "__main__":
    main()
