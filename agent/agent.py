"""
An agent pod is deliberately dumb: it has no memory of its own. Every cycle it
asks the Controller what its situation is, looks at the market, asks the model
for a decision, and reports back. If this process dies mid-cycle, Kubernetes
restarts it and the next cycle just starts fresh - nothing was lost because
nothing important was ever held here.

Trades either stocks or crypto - the Controller tells it which on every
heartbeat, so the same image serves both and the split is a config change
rather than a different build.
"""
import os
import time
import json
import math
import logging
from zoneinfo import ZoneInfo
from datetime import datetime
import requests
from anthropic import Anthropic

# Log in Brisbane time, not UTC. Container logs default to UTC, which makes it
# genuinely hard to line up "when did it buy that" against your own day.
class _BrisbaneFormatter(logging.Formatter):
    _TZ = ZoneInfo("Australia/Brisbane")

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=self._TZ)
        return dt.strftime(datefmt or "%H:%M:%S")


_handler = logging.StreamHandler()
_handler.setFormatter(_BrisbaneFormatter("%(asctime)s AEST  %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_handler], force=True)
log = logging.getLogger("agent")

AGENT_NAME = os.environ["AGENT_NAME"]
CONTROLLER_URL = os.environ["CONTROLLER_URL"]

LLM_MODEL = os.environ.get("LLM_MODEL", "claude-sonnet-4-6")

client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

DECISION_TOOL = {
    "name": "trade_decision",
    "description": "Report a trading decision for this cycle.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["buy", "sell", "hold"]},
            "symbol": {"type": "string"},
            "qty": {"type": "number"},
            "stop_loss_pct": {"type": "number", "description": "required for buy actions"},
            "reasoning": {"type": "string"},
        },
        "required": ["action", "reasoning"],
    },
}

PLAYBOOK = """You are one of several autonomous trading agents in a swing-trading system, each
managing an independent slice of capital under identical house rules. Depending on
the agent you may be trading company shares or cryptocurrency - the message will
say which. The judgement is the same either way; only the wording changes (shares
vs coins/units).

YOUR ROLE
Each cycle, you are shown a short, pre-screened list of momentum candidates and asked to
decide: buy, sell, or hold. You are not responsible for discovering candidates yourself -
a separate screening layer has already scanned a broad universe of the real market and
narrowed it down to only the names showing genuine, sustained momentum. Treat every
candidate you are shown as having already cleared that bar.

WHAT A GOOD SETUP LOOKS LIKE
Favor "stair-step" price action: a sequence of recent closes that climbs in stages, where
each new high holds above the prior close, suggesting buying pressure accumulating over
several sessions rather than a single chaotic event.

RED FLAGS THAT SHOULD WEIGH AGAINST A BUY
- A parabolic spike already fading back from its peak (blow-off top risk).
- A single violent one-day gap with no preceding build-up (gap-and-trap risk).
- Sharp, erratic swings rather than an orderly climb - instability, not strength.
- An extremely low absolute price - liquidity and fill quality tend to be weaker.
- With crypto especially, remember it moves much harder than shares do, so a big
  percentage swing is more ordinary there and means less on its own.

POSITION SIZING AND RISK
Size positions conservatively relative to the capital you are told you have available.
A stop-loss is mandatory on every buy, no exceptions. Set it just below a genuine recent
support level, such as the most recent meaningful higher low, not an arbitrary round
number. A deterministic risk layer downstream will independently check your position size
and stop-loss before anything reaches the broker.

HOLDING IS A REAL DECISION, NOT A DEFAULT FAILURE
You do not need to act every cycle. Holding is often correct when no candidate presents a
genuinely clean setup. Do not force a trade just to appear productive.

SWING TRADING, NOT DAY TRADING
This system holds positions for days, not minutes. A candidate that looks attractive this
cycle should still look attractive tomorrow if the thesis is sound.

WRITE FOR A COMPLETE BEGINNER
The person reading your reasoning is new to trading and does not know industry jargon.
Write the way you would explain it to a friend who has never bought a share before.

- Do NOT use terms like: parabolic, blow-off top, consolidation, resistance, support,
  breakout, pivot, RSI, overbought, relative strength, base, retrace, gap-and-trap.
- If a concept genuinely needs one of those ideas, describe it in ordinary words instead.
  Say "the price has been climbing steadily for several days" rather than "sustained
  momentum above the breakout pivot". Say "if it drops this far, something has gone
  wrong and we get out" rather than "invalidation of the thesis below support".
- Always explain what the numbers MEAN, not just what they are. "Up 12% over five days"
  is data; "it has gained 12 cents on the dollar over the last week, which is a fast
  climb" is an explanation.
- When you reject a candidate, say plainly why in one short clause, e.g. "skipped GTBP
  because it jumped all at once in a single day, which often falls back just as fast".
- Aim for three or four short sentences a beginner could read out loud and follow.

OUTPUT FORMAT
Always report your decision using the trade_decision tool. Every buy decision must
include a stop_loss_pct."""


def get_my_status():
    resp = requests.post(f"{CONTROLLER_URL}/heartbeat", json={"agent_name": AGENT_NAME}, timeout=10)
    resp.raise_for_status()
    return resp.json()


def ask_model(status, market_snapshot):
    market = "cryptocurrency" if status.get("asset_class") == "crypto" else "stock"
    holdings = status.get("holdings") or []
    if holdings:
        lines = []
        for h in holdings:
            direction = "up" if h["change_pct"] >= 0 else "down"
            lines.append(
                f"  - {h['symbol']}: you own {h['qty']} bought at ${h['bought_at']}, "
                f"now ${h['now']} ({direction} {abs(h['change_pct']):.1f}%), worth ${h['value']}"
            )
        holdings_text = (
            "\n\nWHAT YOU ALREADY OWN:\n" + "\n".join(lines) +
            "\n\nYou can add to one of these, sell one, or buy something new with the cash you "
            "have left. Adding to a position you already hold concentrates your risk in that one "
            "name, so only do it if it still looks clearly better than the alternatives."
        )
    else:
        holdings_text = "\n\nYou currently own nothing - all your money is sitting as cash."

    prompt = f"""You are managing a {market} swing-trading position with ${status['current_balance']:.2f} in spare cash.{holdings_text}
These are the top {len(market_snapshot)} candidates right now, screened by momentum from a much larger
universe of symbols - each one already stood out enough to reach you, so treat them as pre-filtered.
Current market snapshot: {json.dumps(market_snapshot)}"""

    asset_word = "cryptocurrency" if status.get("asset_class") == "crypto" else "stock"
    response = client.messages.create(
        model=LLM_MODEL,
        max_tokens=1024,
        system=[{"type": "text", "text": PLAYBOOK, "cache_control": {"type": "ephemeral"}}],
        tools=[DECISION_TOOL],
        tool_choice={"type": "tool", "name": "trade_decision"},
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        u = response.usage
        requests.post(f"{CONTROLLER_URL}/usage", json={
            "agent_name": AGENT_NAME,
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cache_creation_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
            "cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        }, timeout=10)
    except Exception as e:
        log.warning(f"failed to report token usage: {e}")

    for block in response.content:
        if block.type == "tool_use":
            return block.input
    return {"action": "hold", "reasoning": "no usable tool call returned"}


def fetch_market_snapshot():
    resp = requests.get(f"{CONTROLLER_URL}/screen", params={"agent_name": AGENT_NAME}, timeout=20)
    resp.raise_for_status()
    return resp.json()


def explain_decision(decision, status, snapshot):
    """Turns the raw decision into something a beginner can actually read in the logs."""
    action = decision.get("action", "hold")
    symbol = decision.get("symbol")
    reasoning = decision.get("reasoning", "")
    lines = ["", "=" * 68]

    if action == "hold":
        lines.append("DECISION: Do nothing this time (hold)")
        lines.append("  Meaning: not buying anything right now, money stays as cash.")
    elif action == "buy":
        qty = decision.get("qty", 0)
        price = (snapshot.get(symbol) or {}).get("price")
        stop = decision.get("stop_loss_pct")
        lines.append(f"DECISION: BUY {symbol}")
        if price:
            cost = qty * price
            pct_of_pot = (cost / status["current_balance"] * 100) if status.get("current_balance") else 0
            lines.append(f"  How many:  {qty} shares at about ${price:,.2f} each")
            lines.append(f"  Total cost: about ${cost:,.2f}  ({pct_of_pot:.0f}% of this agent's ${status['current_balance']:,.2f})")
        else:
            lines.append(f"  How many:  {qty} shares")
        if stop:
            lines.append(f"  Safety net: if it falls {stop:.1f}%, we sell automatically to stop the loss growing")
    elif action == "sell":
        lines.append(f"DECISION: SELL {symbol}")
        lines.append(f"  How many: {decision.get('qty', 0)} shares - turning this back into cash.")

    if reasoning:
        lines.append("")
        lines.append("  WHY:")
        words, line = reasoning.split(), ""
        for w in words:
            if len(line) + len(w) + 1 > 62:
                lines.append(f"    {line}")
                line = w
            else:
                line = f"{line} {w}".strip()
        if line:
            lines.append(f"    {line}")

    if snapshot:
        lines.append("")
        lines.append("  WHAT IT WAS CHOOSING FROM:")
        for sym, data in snapshot.items():
            pct_key = next((k for k in data if k.endswith("_change_pct")), None)
            pct = data.get(pct_key) if pct_key else None
            mark = "  <-- picked" if sym == symbol else ""
            if pct is None:
                lines.append(f"    {sym:<6} ${data.get('price', 0):>9,.2f}{mark}")
            else:
                direction = "up" if pct >= 0 else "down"
                lines.append(f"    {sym:<6} ${data.get('price', 0):>9,.2f}   {direction} {abs(pct):.1f}% over the last week{mark}")

    lines.append("=" * 68)
    return "\n".join(lines)


# Remembers enough about the last cycle to tell whether anything has actually
# changed. 96% of calls were returning "hold" on a shortlist the model had
# already rejected minutes earlier - paying repeatedly for the same answer.
_last_context = {"fingerprint": None, "action": "hold", "escalated": set()}

SKIP_THRESHOLD_PCT = 0.02      # a candidate must move 2% to count as news
ESCALATE_PROFIT_PCT = 8.0      # approaching the +10% target - worth a judgement call
ESCALATE_LOSS_PCT = -15.0      # approaching the -20% stop - worth asking before it trips


def _fingerprint(snapshot):
    """Identity of a shortlist, insensitive to trivial price drift. Two cycles
    with the same coins at roughly the same prices produce the same string."""
    parts = []
    for sym in sorted(snapshot):
        price = snapshot[sym].get("price") or 0
        # Bucket by a percentage step of the price itself, so a 2% move in BTC
        # (~$1,600) and a 2% move in DOGE (~$0.002) both register as one step.
        bucket = round(math.log(price) / math.log(1 + SKIP_THRESHOLD_PCT)) if price > 0 else 0
        parts.append(f"{sym}:{bucket}")
    return "|".join(parts)


def _position_events(status):
    """Positions that have just crossed a threshold worth thinking about.
    Crossings are remembered so the same one does not re-trigger every cycle."""
    events = []
    for h in (status.get("holdings") or []):
        pct = h.get("change_pct")
        if pct is None:
            continue
        key = None
        if pct >= ESCALATE_PROFIT_PCT:
            key = f"{h['symbol']}:profit"
            label = f"{h['symbol']} is up {pct:.1f}% - near the target"
        elif pct <= ESCALATE_LOSS_PCT:
            key = f"{h['symbol']}:loss"
            label = f"{h['symbol']} is down {abs(pct):.1f}% - near the stop"
        if key and key not in _last_context["escalated"]:
            _last_context["escalated"].add(key)
            events.append(label)
        elif not key:
            # recovered back inside the band - allow it to trigger again later
            _last_context["escalated"].discard(f"{h['symbol']}:profit")
            _last_context["escalated"].discard(f"{h['symbol']}:loss")
    return events


def _worth_asking(snapshot, status):
    """Decide in plain Python whether this cycle justifies a model call.
    Returns (ask: bool, why: str)."""
    events = _position_events(status)
    if events:
        return True, "; ".join(events)

    fp = _fingerprint(snapshot)
    if fp != _last_context["fingerprint"]:
        return True, "the shortlist has changed"

    if _last_context["action"] != "hold":
        return True, "last cycle ended in a trade, re-checking"

    return False, "same candidates, same prices, nothing held near a threshold"


def run_cycle(status):
    snapshot = fetch_market_snapshot()
    top_symbol, top_price = None, None
    if snapshot:
        top_symbol = next(iter(snapshot))
        top_price = snapshot[top_symbol]["price"]

    ask, why = _worth_asking(snapshot, status)
    if not ask:
        log.info(f"No AI call needed - {why}.")
        return
    log.info(f"Asking the AI because {why}.")

    decision = ask_model(status, snapshot)
    log.info(explain_decision(decision, status, snapshot))

    try:
        requests.post(f"{CONTROLLER_URL}/decision", json={
            "agent_name": AGENT_NAME,
            "action": decision["action"],
            "symbol": decision.get("symbol"),
            "reasoning": decision.get("reasoning"),
        }, timeout=10)
    except Exception as e:
        log.warning(f"failed to report decision: {e}")

    _last_context["fingerprint"] = _fingerprint(snapshot)
    _last_context["action"] = decision["action"]

    if decision["action"] == "hold":
        return

    proposal = {
        "agent_name": AGENT_NAME,
        "symbol": decision["symbol"],
        "side": decision["action"],
        "qty": decision["qty"],
        "stop_loss_pct": decision.get("stop_loss_pct"),
        "reasoning": decision["reasoning"],
    }
    resp = requests.post(f"{CONTROLLER_URL}/propose", json=proposal, timeout=15)
    try:
        body = resp.json()
    except Exception:
        log.info(f"  RESULT: unexpected reply from the controller ({resp.status_code})")
        return

    if body.get("executed"):
        order = body.get("order", {})
        filled = order.get("filled_price")
        if filled:
            log.info(f"  RESULT: Done - bought at ${filled:,.2f} per share.")
        else:
            if status.get("asset_class") == "crypto":
                log.info("  RESULT: Order placed - waiting for it to fill.")
            else:
                log.info("  RESULT: Order placed. It will go through when the market next opens.")
        note = body.get("sizing_note")
        if note:
            log.info(f"  NOTE:   The safety checks adjusted this - {note}")
    elif body.get("approved") is False:
        log.info(f"  RESULT: Blocked by the safety rules - {body.get('reason')}")
    else:
        err = str(body.get("error", ""))
        if "not fractionable" in err:
            log.info("  RESULT: Rejected - this stock only sells in whole shares, and the amount worked out to a part-share.")
        elif "insufficient buying power" in err:
            log.info("  RESULT: Rejected - not enough spare cash in the account to cover this.")
        else:
            log.info(f"  RESULT: Order failed - {err}")


SESSION_PLAIN_ENGLISH = {
    "open": "US market is OPEN - checking often",
    "pre-market": "US market opens later today - looking in occasionally",
    "after-hours": "US market has closed for the day - looking in occasionally",
    "closed": "US market is shut overnight - looking in occasionally",
    "weekend": "Weekend, nothing is trading - just a slow pulse check",
}


def humanise(seconds):
    if seconds < 120:
        return f"{seconds} seconds"
    if seconds < 3600:
        mins = seconds / 60
        if mins == int(mins):
            return f"{int(mins)} minutes"
        return f"{mins:.1f} minutes"
    hours = seconds / 3600
    return f"{hours:.0f} hours" if hours != 1 else "1 hour"


if __name__ == "__main__":
    log.info(f"agent {AGENT_NAME} starting - model={LLM_MODEL}")
    RETRY_SECONDS = 30
    while True:
        interval = None
        session = None
        status = {}
        try:
            status = get_my_status()
            session = status.get("market_session", "closed")
            # The Controller decides the pace - it knows the session, whether this
            # agent is holding anything, and how much daily budget is left.
            interval = status.get("next_cycle_seconds")

            if status["status"] != "active":
                log.info(f"{AGENT_NAME} is {status['status']}, sleeping")
            elif not status.get("should_think", True):
                held = ", ".join(h["symbol"] for h in (status.get("holdings") or [])) or "nothing"
                log.info(
                    f"Not thinking this cycle - {status.get('idle_reason')}. "
                    f"Holding {held}. The controller still watches every position every "
                    f"minute and sells automatically if the trailing stop is hit, so "
                    f"nothing is unprotected. No AI call made."
                )
            elif status.get("budget_remaining", 1) <= 0:
                log.warning(
                    f"Daily AI budget used up ({status.get('calls_today')} calls today). "
                    f"Pausing until tomorrow - no trades will be made in the meantime."
                )
            else:
                run_cycle(status)
        except Exception as e:
            log.error(f"cycle failed: {e}")

        if interval is None:
            sleep_for = RETRY_SECONDS
            log.info(f"Couldn't reach the controller - trying again in {humanise(sleep_for)}.")
        else:
            sleep_for = interval
            note = SESSION_PLAIN_ENGLISH.get(session, session)
            holding = " (holding a position, so checking less often)" if status.get("holding_position") and session == "open" else ""
            used, remaining = status.get("calls_today", 0), status.get("budget_remaining", 0)
            log.info(f"{note}{holding}. Next check in {humanise(sleep_for)}. "
                     f"AI calls used today: {used} ({remaining} left).")
        time.sleep(sleep_for)
