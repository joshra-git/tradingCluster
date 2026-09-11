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
    "screen_top_n": 5,
    "min_price_floor": 5.0,
    "persistence_min_appearances": 2,
    "persistence_lookback": 3,
    "discovery_interval_seconds": 900,
    "reconcile_interval_seconds": 60,
}


def load():
    try:
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        cfg = {}
    return {**DEFAULTS, **cfg}
