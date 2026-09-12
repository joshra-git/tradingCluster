"""
Reads the tier config from a mounted ConfigMap volume every time it's called
(NOT cached at startup). ConfigMap volumes sync to disk periodically without a
pod restart, so editing the ConfigMap and re-applying it changes behavior on
the next reconcile pass with no redeploy needed.
"""
import os
import yaml

CONFIG_PATH = os.environ.get("TIER_CONFIG_PATH", "/etc/trading-config/config.yaml")

DEFAULTS = {
    "min_capital": 50,
    "max_capital": 100,
    "trend_window_days": 5,
    "max_position_pct": 0.25,
    "max_daily_loss_pct": 0.10,
    "cull_floor_fraction": 0.2,
    "pdt_day_trade_limit": 3,
    "initial_pod_count": 2,
    "screen_top_n": 5,                    # how many final candidates reach Claude each cycle
    "min_price_floor": 5.0,               # excludes penny-stock traps from discovery
    "persistence_min_appearances": 2,     # how many scans a symbol must appear in...
    "persistence_lookback": 3,            # ...out of the last N scans, to count as real
    "discovery_interval_seconds": 900,    # how often the real market gets scanned (15 min)
    "exit_check_interval_seconds": 60,    # how often stop-loss/take-profit are enforced
    "take_profit_pct": 15.0,              # auto-sell once a position is up this much
    "default_stop_loss_pct": 8.0,         # fallback if a position has no stop recorded
    "reconcile_interval_seconds": 60,
}


def load():
    try:
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        cfg = {}
    return {**DEFAULTS, **cfg}
