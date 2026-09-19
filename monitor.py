#!/usr/bin/env python3
"""Watch orders and account state as the agent trades. Ctrl+C to stop.

Run this after wipe.py once you restart the agent, so you can see the
first few decisions it makes.
"""
import os
import time
from datetime import datetime
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus

client = TradingClient(
    os.environ["APCA_API_KEY_ID"],
    os.environ["APCA_API_SECRET_KEY"],
    paper=False,
)

seen = set()
print("Monitoring... Ctrl+C to stop.\n")

while True:
    try:
        orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, limit=25))
        for o in reversed(orders):  # oldest first so log reads top-to-bottom
            if o.id not in seen:
                seen.add(o.id)
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"[{ts}] {o.side.value.upper():<4} {o.qty} {o.symbol:<10} status={o.status.value}")

        account = client.get_account()
        positions = client.get_all_positions()
        print(
            f"  value=${float(account.portfolio_value):>10,.2f}  "
            f"cash=${float(account.cash):>10,.2f}  "
            f"positions={len(positions)}",
            end="\r",
        )
        time.sleep(10)
    except KeyboardInterrupt:
        print("\nStopped.")
        break
    except Exception as e:
        print(f"\nError: {e} — retrying in 30s")
        time.sleep(30)
