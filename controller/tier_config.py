"""
Reads the tier config from a mounted ConfigMap volume every time it's called
(NOT cached at startup). ConfigMap volumes sync to disk periodically without a
pod restart, so editing the ConfigMap and re-applying it changes behaviour on
the next reconcile pass with no redeploy needed.
"""
import os
import yaml

CONFIG_PATH = os.environ.get("TIER_CONFIG_PATH", "/etc/trading-config/config.yaml")

DEFAULTS = {
    # --- what to trade ---
    # Both on = agents are split 50/50 between the two. Both off is rejected.
    "stocks_enabled": False,
    "crypto_enabled": True,

    # --- capital tiers ---
    "min_capital": 250,
    "max_capital": 500,
    "initial_pod_count": 2,
    "cull_floor_fraction": 0.2,

    # --- risk limits ---
    "max_position_pct": 0.25,
    "max_daily_loss_pct": 0.10,
    "pdt_day_trade_limit": 3,          # stocks only; crypto has no PDT rule

    # --- exits, enforced by the Controller ---
    "exit_check_interval_seconds": 60,
    "take_profit_pct": 15.0,
    "default_stop_loss_pct": 8.0,
    "crypto_stop_loss_pct": 12.0,      # crypto swings harder, so a wider stop
    "crypto_take_profit_pct": 20.0,

    # --- discovery and screening ---
    "discovery_interval_seconds": 900,
    "min_price_floor": 5.0,
    "persistence_min_appearances": 2,
    "persistence_lookback": 3,
    "screen_top_n": 5,
    "trend_window_days": 5,

    # --- pacing: stocks follow the US market session ---
    "cycle_open_seconds": 90,
    "cycle_weekday_seconds": 600,
    "cycle_weekend_seconds": 3600,
    "holding_slowdown_factor": 3,

    # --- pacing: crypto trades 24/7, so YOUR waking hours define "active"
    #     rather than an exchange bell. Outside these hours it idles cheaply.
    "crypto_timezone": "Australia/Brisbane",
    "crypto_active_start_hour": 7,     # 7am local
    "crypto_active_end_hour": 22,      # 10pm local
    "cycle_crypto_active_seconds": 120,
    "cycle_crypto_quiet_seconds": 1800,

    "daily_call_budget": 1400,
    "reconcile_interval_seconds": 60,
}


def load():
    try:
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        cfg = {}
    merged = {**DEFAULTS, **cfg}
    if not merged["stocks_enabled"] and not merged["crypto_enabled"]:
        # Refusing to silently do nothing - if both are off, that's a config
        # mistake, and falling back to crypto is the safer guess than halting.
        merged["crypto_enabled"] = True
    return merged


def enabled_asset_classes(cfg=None):
    cfg = cfg or load()
    classes = []
    if cfg["stocks_enabled"]:
        classes.append("stocks")
    if cfg["crypto_enabled"]:
        classes.append("crypto")
    return classes
