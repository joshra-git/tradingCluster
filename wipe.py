#!/usr/bin/env python3
"""Sell all crypto positions in chunks.

Usage:
    python wipe.py --dry-run    # preview only, submits nothing
    python wipe.py              # asks for confirmation, then sells

Splits each position into CHUNKS market sells with DELAY_SEC between them.
This avoids eating the spread on thinner coins.

Before running: STOP the trading agent first, or it may fight your sells.
"""
import argparse
import os
import sys
import time
from decimal import Decimal, ROUND_DOWN
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

CHUNKS = 4          # split each position into this many sells
DELAY_SEC = 15      # wait between chunks

parser = argparse.ArgumentParser()
parser.add_argument("--dry-run", action="store_true", help="Preview without submitting orders")
args = parser.parse_args()

client = TradingClient(
    os.environ["APCA_API_KEY_ID"],
    os.environ["APCA_API_SECRET_KEY"],
    paper=False,
)

# Crypto symbols on Alpaca contain "/" (e.g. BTC/USD). Filter to those only.
positions = [p for p in client.get_all_positions() if "/" in p.symbol]

if not positions:
    print("No crypto positions to sell.")
    sys.exit(0)

total_value = sum(float(p.market_value) for p in positions)
print(f"Will sell {len(positions)} crypto positions worth ${total_value:,.2f}:")
for p in positions:
    print(f"  {p.symbol:<12} qty {p.qty:<18} value ${float(p.market_value):>12,.2f}")

if not args.dry_run:
    confirm = input("\nType 'SELL ALL' to proceed: ").strip()
    if confirm != "SELL ALL":
        print("Aborted.")
        sys.exit(1)

for p in positions:
    total_qty = Decimal(p.qty)
    chunk_qty = (total_qty / CHUNKS).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
    print(f"\n{p.symbol}: {total_qty} in {CHUNKS} chunks")

    for i in range(CHUNKS):
        # Last chunk sells the remainder so nothing gets left behind by rounding
        remaining = Decimal(client.get_open_position(p.symbol.replace("/", "")).qty)
        this_qty = remaining if i == CHUNKS - 1 else min(chunk_qty, remaining)
        if this_qty <= 0:
            print(f"  chunk {i+1}/{CHUNKS}: skipped (nothing left)")
            continue

        if args.dry_run:
            print(f"  [dry-run] chunk {i+1}/{CHUNKS}: would sell {this_qty} {p.symbol}")
        else:
            try:
                order = client.submit_order(MarketOrderRequest(
                    symbol=p.symbol,
                    qty=float(this_qty),
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.GTC,
                ))
                print(f"  chunk {i+1}/{CHUNKS}: sold {this_qty} (order {order.id})")
            except Exception as e:
                print(f"  chunk {i+1}/{CHUNKS} FAILED: {e}")

        if i < CHUNKS - 1 and not args.dry_run:
            time.sleep(DELAY_SEC)

if not args.dry_run:
    print("\nWaiting 30s for fills to settle...")
    time.sleep(30)
    remaining = [p for p in client.get_all_positions() if "/" in p.symbol]
    if remaining:
        print(f"WARNING - {len(remaining)} positions still open:")
        for p in remaining:
            print(f"  {p.symbol}: {p.qty}")
        print("Re-run the script to clear the leftovers.")
    else:
        print("All crypto positions closed.")
    account = client.get_account()
    print(f"Cash now: ${float(account.cash):,.2f}")

print("Done.")
