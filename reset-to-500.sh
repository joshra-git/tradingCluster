#!/usr/bin/env bash
set -euo pipefail

# ONE-OFF: run this AFTER you have reset/recreated your Alpaca paper account
# in their web dashboard with a $500 starting balance.
#
# Alpaca has no API for resetting a paper account - it is dashboard-only, and
# the reset invalidates your old API keys. So this script does the three things
# that have to happen on THIS side afterwards:
#   1. point the cluster at the new Alpaca keys
#   2. wipe our ledger (it still believes in the old account's money)
#   3. re-scale the capital tiers to $500
#
# Usage:  ./reset-to-500.sh <NEW_ALPACA_KEY> <NEW_ALPACA_SECRET>

NAMESPACE="trading"

if [ $# -ne 2 ]; then
  echo "Usage: $0 <NEW_ALPACA_KEY> <NEW_ALPACA_SECRET>"
  echo
  echo "Get these from the Alpaca dashboard AFTER resetting the paper account."
  exit 1
fi

NEW_KEY="$1"
NEW_SECRET="$2"

echo "==> 1/5 Updating Alpaca credentials (other secret values left untouched)"
kubectl -n "$NAMESPACE" patch secret trading-secrets \
  -p "{\"stringData\":{\"ALPACA_API_KEY\":\"$NEW_KEY\",\"ALPACA_SECRET_KEY\":\"$NEW_SECRET\"}}"

echo "==> 2/5 Scaling capital tiers down to \$500"
kubectl -n "$NAMESPACE" patch configmap trading-tier-config --type merge -p "$(cat <<'JSON'
{"data":{"config.yaml":"min_capital: 500\nmax_capital: 1000\ninitial_pod_count: 1\ncull_floor_fraction: 0.2\nmax_position_pct: 0.25\nmax_daily_loss_pct: 0.10\npdt_day_trade_limit: 3\nexit_check_interval_seconds: 60\ntake_profit_pct: 15.0\ndefault_stop_loss_pct: 8.0\ndiscovery_interval_seconds: 900\nmin_price_floor: 5.0\npersistence_min_appearances: 2\npersistence_lookback: 3\nscreen_top_n: 5\ntrend_window_days: 5\nreconcile_interval_seconds: 60\n"}}
JSON
)"

echo "==> 3/5 Removing existing agent deployments"
kubectl -n "$NAMESPACE" delete deployment -l app=trading-agent --ignore-not-found

echo "==> 4/5 Restarting controller so it picks up the new credentials"
kubectl -n "$NAMESPACE" rollout restart deployment/controller
kubectl -n "$NAMESPACE" rollout status deployment/controller --timeout=120s

echo "==> 5/5 Clearing the ledger so it reseeds against the new account"
kubectl -n "$NAMESPACE" exec deploy/controller -- python3 -c "
import db
with db.get_conn() as conn:
    cur = conn.cursor()
    cur.execute('TRUNCATE agents, trades, capital_events, positions, decisions, market_scans, api_usage RESTART IDENTITY CASCADE')
    cur.execute('UPDATE unallocated_pool SET balance = 0')
print('ledger cleared')
"

echo "==> Restarting controller again to trigger a clean bootstrap"
kubectl -n "$NAMESPACE" rollout restart deployment/controller
kubectl -n "$NAMESPACE" rollout status deployment/controller --timeout=120s

sleep 8
echo
echo "==> Result:"
kubectl -n "$NAMESPACE" exec deploy/controller -- python3 -c "
import alpaca_client, db
acct = alpaca_client.get_account()
print(f\"Alpaca cash:      \${acct['cash']:.2f}\")
print(f\"Alpaca buying pwr: \${float(alpaca_client.trading_client.get_account().buying_power):.2f}\")
print(f\"Open positions:    {len(alpaca_client.trading_client.get_all_positions())}\")
print()
for a in db.list_active_agents():
    print(f\"  {a['name']}: \${a['current_balance']}  (tier \${a['min_capital']} -> \${a['max_capital']})\")
"
echo
kubectl -n "$NAMESPACE" get pods
