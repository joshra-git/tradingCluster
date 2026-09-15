"""
Single source of truth for "should this agent be working right now".

Stocks follow the US exchange session. Crypto never closes, so there is no
exchange bell to follow - instead the user's own waking hours define when it
is worth spending model calls, which is the whole point of moving to crypto:
trading happens while someone is awake to watch it.

Doesn't account for US market holidays - good enough for pacing model calls,
not a substitute for a real trading calendar.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

NY_TZ = ZoneInfo("America/New_York")


def get_market_session():
    """US stock session: open | pre-market | after-hours | closed | weekend."""
    now = datetime.now(NY_TZ)
    if now.weekday() >= 5:
        return "weekend"
    minutes = now.hour * 60 + now.minute
    if 9 * 60 + 30 <= minutes < 16 * 60:
        return "open"
    elif 4 * 60 <= minutes < 9 * 60 + 30:
        return "pre-market"
    elif 16 * 60 <= minutes < 20 * 60:
        return "after-hours"
    else:
        return "closed"


def get_crypto_session(cfg):
    """Crypto is always tradable, so this reports whether the user is likely
    awake rather than whether a market is open: active | quiet."""
    tz = ZoneInfo(cfg.get("crypto_timezone", "Australia/Brisbane"))
    hour = datetime.now(tz).hour
    start = cfg.get("crypto_active_start_hour", 7)
    end = cfg.get("crypto_active_end_hour", 22)
    if start <= end:
        active = start <= hour < end
    else:
        # Handles a window that wraps past midnight, e.g. 22:00 -> 06:00
        active = hour >= start or hour < end
    return "active" if active else "quiet"


def session_for(asset_class, cfg):
    return get_crypto_session(cfg) if asset_class == "crypto" else get_market_session()


def interval_for(asset_class, session, holding, cfg):
    """How long this agent should wait before thinking again."""
    if asset_class == "crypto":
        interval = (cfg["cycle_crypto_active_seconds"] if session == "active"
                    else cfg["cycle_crypto_quiet_seconds"])
        busy = session == "active"
    else:
        if session == "open":
            interval = cfg["cycle_open_seconds"]
        elif session == "weekend":
            interval = cfg["cycle_weekend_seconds"]
        else:
            interval = cfg["cycle_weekday_seconds"]
        busy = session == "open"

    if holding and busy:
        # Exits are enforced by the Controller every 60s regardless, so an agent
        # sitting on a position is waiting on a price, not hunting a new one.
        interval = int(interval * cfg["holding_slowdown_factor"])
    return interval
