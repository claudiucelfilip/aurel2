#!/usr/bin/env python3
"""
Test live execution: connect → get account → buy SPY → verify → sell SPY → verify.
Designed to run inside the Docker container against paper trading.
"""
import asyncio
import sys
import os
import warnings

warnings.filterwarnings("ignore")

from ib_insync import IB, MarketOrder, Stock, util

# Allow nested event loops (ib_insync sync methods inside async)
import nest_asyncio
nest_asyncio.apply()


GATEWAY_HOST = os.environ.get("IBKR_HOST", "ib-gateway")
GATEWAY_PORT = int(os.environ.get("IBKR_PORT", "4004"))
CLIENT_ID = 98
TEST_SYMBOL = "SPY"
TEST_SHARES = 5


async def main():
    ib = IB()

    print("=" * 60)
    print("LIVE EXECUTION TEST (Paper Trading)")
    print("=" * 60)

    # --- 1. Connect ---
    print(f"\n[1] Connecting to {GATEWAY_HOST}:{GATEWAY_PORT} ...")
    try:
        await ib.connectAsync(GATEWAY_HOST, GATEWAY_PORT, clientId=CLIENT_ID)
        ib.reqMarketDataType(3)
        print(f"    OK Connected. Account: {ib.managedAccounts()}")
    except Exception as e:
        print(f"    FAIL Connection failed: {e}")
        return 1

    # --- 2. Account summary ---
    print("\n[2] Getting account summary ...")
    summary = {item.tag: item.value for item in ib.accountSummary()}
    nlv = float(summary.get("NetLiquidation", 0))
    cash = float(summary.get("TotalCashValue", 0))
    bp = float(summary.get("BuyingPower", 0))
    print(f"    Net Liquidation: ${nlv:,.2f}")
    print(f"    Cash:            ${cash:,.2f}")
    print(f"    Buying Power:    ${bp:,.2f}")

    # --- 3. Get current positions ---
    print("\n[3] Current positions ...")
    positions = ib.positions()
    if positions:
        for pos in positions:
            print(f"    {pos.contract.symbol}: {pos.position} shares @ ${pos.avgCost:.2f}")
    else:
        print("    (no positions)")

    # --- 4. Get market price ---
    print(f"\n[4] Getting market price for {TEST_SYMBOL} ...")
    contract = Stock(TEST_SYMBOL, "SMART", "USD")
    ib.qualifyContracts(contract)
    ticker = ib.reqMktData(contract, "", False, False)
    await asyncio.sleep(3)

    price = ticker.marketPrice()
    if price != price:  # NaN check
        price = ticker.close
    if price != price:
        # Try last price
        price = ticker.last
    if price != price:
        print(f"    FAIL Could not get price for {TEST_SYMBOL}")
        print(f"    Ticker details: bid={ticker.bid}, ask={ticker.ask}, last={ticker.last}, close={ticker.close}")
        ib.disconnect()
        return 1
    print(f"    {TEST_SYMBOL} price: ${price:.2f}")
    ib.cancelMktData(contract)

    # --- 5. BUY test ---
    print(f"\n[5] Placing BUY order: {TEST_SHARES} shares of {TEST_SYMBOL} ...")
    buy_order = MarketOrder("BUY", TEST_SHARES)
    buy_trade = ib.placeOrder(contract, buy_order)

    # Wait for fill
    for i in range(30):
        await asyncio.sleep(1)
        if buy_trade.isDone():
            break
        if i % 5 == 4:
            print(f"    ... waiting ({i+1}s), status: {buy_trade.orderStatus.status}")

    buy_status = buy_trade.orderStatus.status
    buy_filled = buy_trade.orderStatus.filled
    buy_avg = buy_trade.orderStatus.avgFillPrice

    if buy_status == "Filled":
        print(f"    OK BUY FILLED: {buy_filled} shares @ ${buy_avg:.2f}")
    elif "Partial" in str(buy_status):
        print(f"    PARTIAL BUY: {buy_filled}/{TEST_SHARES} shares @ ${buy_avg:.2f}")
    else:
        print(f"    FAIL BUY status: {buy_status} — filled: {buy_filled}")
        ib.disconnect()
        return 1

    # --- 6. Verify position ---
    print("\n[6] Verifying position after BUY ...")
    await asyncio.sleep(2)
    positions = ib.positions()
    spy_pos = None
    for pos in positions:
        if pos.contract.symbol == TEST_SYMBOL:
            spy_pos = pos
            break

    if spy_pos:
        print(f"    OK Position: {spy_pos.position} shares of {TEST_SYMBOL}")
    else:
        print(f"    WARN No {TEST_SYMBOL} position found after buy (may be flat from pre-existing short)")

    # --- 7. SELL test ---
    sell_qty = int(buy_filled) if buy_filled else TEST_SHARES
    print(f"\n[7] Placing SELL order: {sell_qty} shares of {TEST_SYMBOL} ...")
    sell_order = MarketOrder("SELL", sell_qty)
    sell_trade = ib.placeOrder(contract, sell_order)

    for i in range(30):
        await asyncio.sleep(1)
        if sell_trade.isDone():
            break
        if i % 5 == 4:
            print(f"    ... waiting ({i+1}s), status: {sell_trade.orderStatus.status}")

    sell_status = sell_trade.orderStatus.status
    sell_filled = sell_trade.orderStatus.filled
    sell_avg = sell_trade.orderStatus.avgFillPrice

    if sell_status == "Filled":
        print(f"    OK SELL FILLED: {sell_filled} shares @ ${sell_avg:.2f}")
    elif "Partial" in str(sell_status):
        print(f"    PARTIAL SELL: {sell_filled}/{sell_qty} shares @ ${sell_avg:.2f}")
    else:
        print(f"    FAIL SELL status: {sell_status} — filled: {sell_filled}")

    # --- 8. Final positions ---
    print("\n[8] Final positions ...")
    await asyncio.sleep(2)
    positions = ib.positions()
    if positions:
        for pos in positions:
            print(f"    {pos.contract.symbol}: {pos.position} shares @ ${pos.avgCost:.2f}")
    else:
        print("    (no positions — all flat)")

    # --- 9. P&L from the round trip ---
    if buy_avg and sell_avg:
        pnl = (sell_avg - buy_avg) * sell_qty
        print(f"\n[9] Round-trip P&L: ${pnl:+.2f} ({((sell_avg/buy_avg)-1)*100:+.2f}%)")

    # --- Summary ---
    print("\n" + "=" * 60)
    buy_ok = buy_status in ("Filled", "PartiallyFilled")
    sell_ok = sell_status in ("Filled", "PartiallyFilled")
    print(f"BUY:  {'PASS' if buy_ok else 'FAIL'} ({buy_status})")
    print(f"SELL: {'PASS' if sell_ok else 'FAIL'} ({sell_status})")
    print(f"OVERALL: {'ALL PASS' if buy_ok and sell_ok else 'FAIL'}")
    print("=" * 60)

    ib.disconnect()
    return 0 if (buy_ok and sell_ok) else 1


if __name__ == "__main__":
    rc = asyncio.run(main())
    sys.exit(rc)
