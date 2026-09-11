"""
Single source of truth for "is the market open right now" - agents ask the
Controller via /heartbeat rather than each computing NY time independently.
Doesn't account for US market holidays (Thanksgiving, etc.) - good enough for
cutting off-peak token spend, not a substitute for a real trading calendar.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

NY_TZ = ZoneInfo("America/New_York")


def get_market_session():
    now = datetime.now(NY_TZ)
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return "closed"
    minutes = now.hour * 60 + now.minute
    if 9 * 60 + 30 <= minutes < 16 * 60:
        return "open"
    elif 4 * 60 <= minutes < 9 * 60 + 30:
        return "pre-market"
    elif 16 * 60 <= minutes < 20 * 60:
        return "after-hours"
    else:
        return "closed"
