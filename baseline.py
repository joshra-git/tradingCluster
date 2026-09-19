#!/usr/bin/env python3
"""Snapshot account state before wiping. Run this FIRST.

Saves a JSON file you can compare against later to see if the new logic
actually beats where you started.
"""
import json
import os
from datetime import datetime, timezone
from alpaca.trading.client import TradingClient

client = TradingClient(
    os.environ["APCA_API_KEY_ID"],
    os.environ["APCA_API_SECRET_KEY"],
    paper=False,
)

account = client.get_account()
positions = client.get_all_positions()

snapshot = {
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "account_value_usd": float(account.portfolio_value),
    "cash_usd": float(account.cash),
    "positions": [
        {
            "symbol": p.symbol,
            "qty": float(p.qty),
            "market_value_usd": float(p.market_value),
            "unrealized_pl_usd": float(p.unrealized_pl),
            "cost_basis_usd": float(p.cost_basis),
        }
        for p in positions
    ],
}

filename = f"baseline_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
with open(filename, "w") as f:
    json.dump(snapshot, f, indent=2)

print(f"Saved: {filename}")
print(f"Account value: ${snapshot['account_value_usd']:,.2f}")
print(f"Cash:          ${snapshot['cash_usd']:,.2f}")
print(f"Positions:     {len(snapshot['positions'])}")
for p in snapshot["positions"]:
    print(f"  {p['symbol']:<12} ${p['market_value_usd']:>12,.2f}   P/L ${p['unrealized_pl_usd']:>+10,.2f}")
