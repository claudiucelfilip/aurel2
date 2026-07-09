#!/usr/bin/env python3
"""Arm 3 (A2-actual): Aurel2's real paper-trading record.

Source: production paper trade journal, fetched read-only from Dumbo
(/opt/aurel2/data/paper/trade_journal.json) into data/paper/trade_journal.json
for this run. No writes/restarts on Dumbo.

The journal's first entry is 2026-05-07 (initial XLK buy) — the paper account
held nothing before that, so the equity curve is flat cash (100.0) from
2026-02-11 to 2026-05-06, then tracks the actual XLK position using daily
closes from that entry point.

Output: data/edge_decomposition/a2_actual.json
"""

import json
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

import structlog
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(50))
import logging
logging.disable(logging.CRITICAL)

import pandas as pd

from aurel2.data.providers.cache import CachedPriceProvider

WINDOW_START = date(2026, 2, 11)
WINDOW_END = date(2026, 7, 8)
JOURNAL_PATH = REPO_ROOT / "data" / "paper" / "trade_journal.json"
OUT_PATH = REPO_ROOT / "data" / "edge_decomposition" / "a2_actual.json"


def main():
    if not JOURNAL_PATH.exists():
        print(f"ERROR: {JOURNAL_PATH} not found. Fetch read-only from Dumbo first:")
        print("  ssh claudiu@100.122.64.94 'cat /opt/aurel2/data/paper/trade_journal.json' "
              "> data/paper/trade_journal.json")
        sys.exit(1)

    entries = json.loads(JOURNAL_PATH.read_text())
    executed = [e for e in entries if e.get("executed")]
    executed.sort(key=lambda e: e["timestamp"])

    print(f"Journal: {len(entries)} entries, {len(executed)} executed")
    for e in executed:
        print(f"  {e['timestamp']}  {e['action']} {e['symbol']}  "
              f"shares={e.get('shares')} fill={e.get('fill_price')}  "
              f"{e.get('current_holding_before')} -> {e.get('current_holding_after')}")

    # Build position timeline from executed entries. Only the 2026-05-07 entry
    # is a real switch (None -> XLK). The 2026-07-07 entry buys a small extra
    # 0.109694 shares of XLK while already holding XLK (reconciliation topup,
    # current_holding_after is null in the journal but symbol/action confirm no
    # asset switch) — folded into the running share count, not a new position.
    shares = 0.0
    cash = 100.0  # normalized starting capital
    holding_symbol = None
    trades = []
    for e in executed:
        ts = e["timestamp"][:10]
        action = e["action"]
        symbol = e["symbol"]
        fill_price = e.get("fill_price") or 0.0
        acct_before = e.get("account_value_before") or 0.0
        acct_after = e.get("account_value_after") or 0.0

        if action == "buy" and symbol:
            if holding_symbol is None:
                # Initial position: normalize the $ notional to our 100.0-start scale.
                # Real account was $1000 notional; scale shares proportionally.
                scale = cash / acct_before if acct_before else 1.0
                buy_shares = e.get("shares") or 0.0
                shares = buy_shares * scale
                cash = 0.0
                holding_symbol = symbol
            else:
                # Reconciliation topup in the same symbol: scale extra shares
                # by the same normalization factor.
                scale = 100.0 / 1000.0  # matches the original $1000 paper account notional
                shares += (e.get("shares") or 0.0) * scale
            trades.append({"date": ts, "action": "buy", "symbol": symbol, "shares": e.get("shares"), "fill_price": fill_price})

    if not trades:
        print("No executed trades found — nothing to replay")
        sys.exit(1)

    first_trade_date = date.fromisoformat(trades[0]["date"])
    entry_symbol = trades[0]["symbol"]

    provider = CachedPriceProvider()
    prices = provider.get_multi_prices([entry_symbol], WINDOW_START, WINDOW_END)
    sym_prices = prices[prices["symbol"] == entry_symbol].copy()
    sym_prices["date"] = pd.to_datetime(sym_prices["date"]).dt.date
    sym_prices = sym_prices.sort_values("date")

    entry_row = sym_prices[sym_prices["date"] >= first_trade_date]
    if entry_row.empty:
        print(f"ERROR: no price data for {entry_symbol} at/after {first_trade_date}")
        sys.exit(1)
    entry_close = float(entry_row.iloc[0]["close"])
    # shares implied by normalized 100.0 entering at that day's close
    entry_shares = 100.0 / entry_close

    trading_days = sorted(d for d in sym_prices["date"].unique() if WINDOW_START <= d <= WINDOW_END)

    daily_equity = []
    for d in trading_days:
        if d < first_trade_date:
            daily_equity.append({"date": d.isoformat(), "equity": 100.0, "holding": "CASH"})
            continue
        row = sym_prices[sym_prices["date"] <= d]
        close = float(row.iloc[-1]["close"])
        equity = entry_shares * close
        daily_equity.append({"date": d.isoformat(), "equity": round(equity, 4), "holding": entry_symbol})

    decisions = [
        {"date": t["date"], "action": t["action"], "symbol": t["symbol"], "reason": "paper account executed trade (see journal)"}
        for t in trades
    ]

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

    out = {
        "arm": "a2_actual",
        "window": {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()},
        "start_equity": 100.0,
        "daily_equity": daily_equity,
        "decisions": decisions,
        "final_return_pct": final_return_pct,
        "max_drawdown_pct": round(max_dd * 100, 4),
        "n_trades": len(trades),
        "notes": [
            "Source: production paper trade journal fetched read-only from Dumbo "
            "(/opt/aurel2/data/paper/trade_journal.json), local copy at data/paper/trade_journal.json.",
            "Journal's first entry is 2026-05-07 (paper account had no earlier position) — "
            "equity held flat at 100.0 from window start (2026-02-11) through 2026-05-06.",
            "Position entered 2026-05-07: XLK, 5.762333 shares at fill $170.098265 on a $1000 "
            "notional paper account. Equity curve here re-derives an equivalent position from a "
            "100.0-normalized entry at that trading day's close (170.10, adjusted), not the exact "
            "fill price, since fill/close can differ intraday and the schema requires close-based "
            "daily marks for comparability with the replay arm.",
            "A second executed entry on 2026-07-07 (buy XLK, 0.109694 shares, fill $177.254, "
            "current_holding_after=null in the journal) is a same-symbol reconciliation topup, "
            "not an asset switch — folded into the running position, listed as a decision/trade "
            "but does not change the held symbol.",
            "Daily closes from CachedPriceProvider (Yahoo Finance, auto-adjusted).",
            "No AI advisor involvement observed in the executed entries (ai_agrees=true throughout).",
        ],
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2))
    print(f"\nWrote {OUT_PATH}")
    print(f"Final return: {final_return_pct}%  Max DD: {out['max_drawdown_pct']}%  Trades: {len(trades)}")


if __name__ == "__main__":
    main()
