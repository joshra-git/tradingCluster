#!/usr/bin/env bash
set -euo pipefail

# ONE-OFF: point the cluster at a freshly reset Alpaca paper account.
#   - swaps in the new Alpaca key/secret (Anthropic key + Postgres password untouched)
#   - sets tiers to $250 per agent, 2 agents, spawn a sibling at $750
#   - wipes the ledger, which still believes in the old account's money
#   - restarts the controller so it bootstraps cleanly against the new account
#
# Usage:  ./reset-cluster.sh <NEW_ALPACA_KEY> <NEW_ALPACA_SECRET>

NAMESPACE="trading"

if [ $# -ne 2 ]; then
  echo "Usage: $0 <NEW_ALPACA_KEY> <NEW_ALPACA_SECRET>"
  exit 1
fi

NEW_KEY="$1"
NEW_SECRET="$2"

echo "==> 1/6 Updating Alpaca credentials"
kubectl -n "$NAMESPACE" patch secret trading-secrets \
  -p "{\"stringData\":{\"ALPACA_API_KEY\":\"$NEW_KEY\",\"ALPACA_SECRET_KEY\":\"$NEW_SECRET\"}}"

echo "==> 2/6 Applying new capital tiers (\$250 per agent, 2 agents, spawn at \$750)"
kubectl apply -f - <<'YAML'
apiVersion: v1
kind: ConfigMap
metadata:
  name: trading-tier-config
  namespace: trading
data:
  config.yaml: |
    # --- capital tiers ---
    min_capital: 250              # each agent starts with $250
    max_capital: 750              # crossing $750 spawns a sibling, funded with $250
    initial_pod_count: 2          # $250 x 2 = the full $500 account
    cull_floor_fraction: 0.2      # cull an agent below $50

    # --- risk limits ---
    max_position_pct: 0.25        # max $62.50 in one position at the $250 tier
    max_daily_loss_pct: 0.10
    pdt_day_trade_limit: 3

    # --- exits, enforced by the Controller ---
    exit_check_interval_seconds: 60
    take_profit_pct: 15.0
    default_stop_loss_pct: 8.0

    # --- discovery and screening ---
    discovery_interval_seconds: 900
    min_price_floor: 5.0
    persistence_min_appearances: 2
    persistence_lookback: 3
    screen_top_n: 5
    trend_window_days: 5

    reconcile_interval_seconds: 60
YAML

echo "==> 3/6 Removing existing agent deployments"
kubectl -n "$NAMESPACE" delete deployment -l app=trading-agent --ignore-not-found

echo "==> 4/6 Restarting controller to pick up the new credentials"
kubectl -n "$NAMESPACE" rollout restart deployment/controller
kubectl -n "$NAMESPACE" rollout status deployment/controller --timeout=120s

echo "==> 5/6 Clearing the ledger"
kubectl -n "$NAMESPACE" exec deploy/controller -- python3 -c "
import db
with db.get_conn() as conn:
    cur = conn.cursor()
    cur.execute('TRUNCATE agents, trades, capital_events, positions, decisions, market_scans, api_usage RESTART IDENTITY CASCADE')
    cur.execute('UPDATE unallocated_pool SET balance = 0')
print('ledger cleared')
"

echo "==> 6/6 Restarting controller again to trigger a clean bootstrap"
kubectl -n "$NAMESPACE" rollout restart deployment/controller
kubectl -n "$NAMESPACE" rollout status deployment/controller --timeout=120s

sleep 10
echo
echo "==> Result:"
kubectl -n "$NAMESPACE" exec deploy/controller -- python3 -c "
import alpaca_client, db
raw = alpaca_client.trading_client.get_account()
print(f'Alpaca cash:       \${float(raw.cash):.2f}')
print(f'Alpaca buying pwr: \${float(raw.buying_power):.2f}')
print(f'Open positions:    {len(alpaca_client.trading_client.get_all_positions())}')
print()
agents = db.list_active_agents()
if not agents:
    print('  NO AGENTS SEEDED - check controller logs for a bootstrap warning')
for a in agents:
    print(f\"  {a['name']}: \${a['current_balance']}  (tier \${a['min_capital']} -> \${a['max_capital']})\")
"
echo
kubectl -n "$NAMESPACE" get pods
