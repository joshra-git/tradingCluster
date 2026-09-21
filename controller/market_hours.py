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
    """Crypto never closes, so unlike stocks there's no exchange-hours concept
    here at all - it's always 'active'. This used to gate thinking to the
    owner's own waking hours (so trading only happened while he could watch
    it), but that reasoning stopped holding once Telegram notifications made
    "watching it happen" available regardless of when a trade fires. Kept as
    a function (not just a constant) so session_for()'s interface stays the
    same as the stocks path."""
    return "active"


def session_for(asset_class, cfg):
    return get_crypto_session(cfg) if asset_class == "crypto" else get_market_session()


def should_think(asset_class, session, can_act, cfg):
    """Whether it is worth spending a model call this cycle, and how long to wait
    before the next one.

    The split that matters: an agent with cash is HUNTING - it needs to look hard
    and often, because a good entry is time-sensitive and missing it costs more
    than the call does. An agent already holding is WAITING - the trailing stop
    handles the exit without any model involvement, so it only checks in
    occasionally in case there is a judgement reason to get out early.

    When the stock market is shut, neither applies: nothing can be bought or
    sold, so no call is justified at all. Crypto has no equivalent "shut" state
    any more - session_for() always reports it active, so this branch below is
    stocks-only in practice now.

    Returns (think: bool, interval_seconds: int, why: str).
    """
    tradable = (session == "active") if asset_class == "crypto" else (session == "open")

    if not tradable:
        idle = cfg["cycle_weekend_seconds"] if session == "weekend" else cfg["cycle_weekday_seconds"]
        return False, idle, f"US market is {session}"

    if can_act:
        interval = (cfg["cycle_crypto_active_seconds"] if asset_class == "crypto"
                    else cfg["cycle_hunting_seconds"])
        return True, interval, "hunting for an entry"

    return True, cfg["cycle_holding_seconds"], "holding - periodic check only"
