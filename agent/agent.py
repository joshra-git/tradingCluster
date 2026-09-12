"""
An agent pod is deliberately dumb: it has no memory of its own. Every cycle it
asks the Controller what its situation is, looks at the market, asks Claude
for a decision, and reports back. If this process dies mid-cycle, Kubernetes
restarts it and the next cycle just starts fresh - nothing was lost because
nothing important was ever held here.
"""
import os
import time
import json
import logging
import requests
from anthropic import Anthropic

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("agent")

AGENT_NAME = os.environ["AGENT_NAME"]
CONTROLLER_URL = os.environ["CONTROLLER_URL"]
CYCLE_SECONDS = int(os.environ.get("CYCLE_SECONDS", "300"))            # market-hours cadence: 5 min
OFF_PEAK_CYCLE_SECONDS = int(os.environ.get("OFF_PEAK_CYCLE_SECONDS", "3600"))  # off-peak cadence: 1 hour

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

# This block is identical on every single call, across every agent - which is exactly
# what makes it eligible for Anthropic's prompt caching (a 90% discount on repeated
# context, versus paying full price every time). It needs to be long enough to clear
# the ~1,024 token minimum, so it's written as a genuinely useful playbook rather than
# padding - the same rules Claude would otherwise have to reconstruct from scratch
# in shorter form on every call.
PLAYBOOK = """You are one of several autonomous trading agents in a swing-trading system, each
managing an independent slice of capital under identical house rules. Read this playbook
once; it applies to every cycle you run, for as long as this system is active.

YOUR ROLE
Each cycle, you are shown a short, pre-screened list of momentum candidates and asked to
decide: buy, sell, or hold. You are not responsible for discovering candidates yourself -
a separate screening layer has already scanned a broad universe of the real market and
narrowed it down to only the names showing genuine, sustained momentum. Treat every
candidate you are shown as having already cleared that bar. Your job is judgment on top
of what's already been filtered for you, not further discovery.

WHAT A GOOD SETUP LOOKS LIKE
Favor "stair-step" price action: a sequence of recent closes that climbs in stages, where
each new high holds above the prior close, suggesting buying pressure accumulating over
several sessions rather than a single chaotic event. A real example from this system: a
candidate whose closes moved 6.39 -> 6.40 -> 6.20 -> 7.45 -> 9.75 was judged favorably
specifically because each leg higher held its ground - that is the pattern to look for.

RED FLAGS THAT SHOULD WEIGH AGAINST A BUY, EVEN ON A STRONG-LOOKING CANDIDATE
- A parabolic spike that is already fading back down from its peak (blow-off top risk) -
  for example a move from roughly $1 to $12 that has already retraced to $5 in the same
  window is a warning sign, not a bargain.
- A single violent one-day gap with no preceding build-up (gap-and-trap risk) - a jump
  from $3.67 to $6.76 in one session, with nothing before it, is fragile, not confirmed.
- Sharp, erratic swings within the lookback window rather than an orderly climb - a dip
  to a multi-day low mid-week followed by a sudden recovery suggests instability, not
  strength, even if the net change over the period looks impressive.
- An extremely low absolute share price. Liquidity and fill quality tend to be weaker at
  the very low end of the price range even after a price floor has technically been met.

POSITION SIZING AND RISK
Size positions conservatively relative to the capital you are told you have available for
this cycle - do not treat a strong conviction as license to oversize. A stop-loss is
mandatory on every buy, with no exceptions. Set it just below a genuine recent support
level, such as the most recent meaningful higher low in the closing price sequence, not
an arbitrary round number picked for convenience. Remember that a deterministic risk
layer downstream will independently check your position size and stop-loss before
anything is actually sent to the broker - your job is to propose something sound, not to
assume your exact numbers will be used unmodified.

HOLDING IS A REAL DECISION, NOT A DEFAULT FAILURE
You do not need to act every cycle. Holding is often the correct call when no candidate
presents a genuinely clean setup. The goal is a good decision when one is warranted, not
constant activity - do not force a trade just to appear productive this cycle.

BEING AWARE OF OTHER AGENTS
Other agents in this system may be looking at this exact same candidate list at this
exact same moment. If your reasoning leads you to the same pick another agent would also
reach, that is expected given shared input data, not a flaw in your judgment - but it
does mean the system's real diversification is lower than the number of agents running
might suggest, since correlated conviction is the natural result of correlated data.

SWING TRADING, NOT DAY TRADING
This system holds positions for days, not minutes or hours. Do not treat a single
cycle's data as a reason to enter and exit within the same session. A candidate that
looks attractive this cycle should still look attractive on a re-check tomorrow if the
underlying thesis is sound - if it wouldn't, that's a sign the setup was too fragile to
act on in the first place. Frequent same-day round-trips also interact with pattern-day-
trading rules at the account level, which is a separate reason this system is built
around multi-day holds rather than intraday scalping.

WHAT GOOD REASONING LOOKS LIKE
A strong justification names specific numbers from what you were actually shown - the
closing price sequence, the percentage change, the current price relative to recent
levels - rather than general statements like "this looks strong" or "momentum is good."
Compare and rule out at least one other candidate by name when it helps clarify why the
one you picked was better, the way a real analyst would justify a choice among several
live options rather than reviewing only the one they preferred in isolation.

OUTPUT FORMAT
Always report your decision using the trade_decision tool. Keep reasoning concise and
concrete - two or three sentences that justify the call with specifics from the data you
were shown, not an exhaustive essay. Every buy decision must include a stop_loss_pct."""


def get_my_status():
    resp = requests.post(f"{CONTROLLER_URL}/heartbeat", json={"agent_name": AGENT_NAME}, timeout=10)
    resp.raise_for_status()
    return resp.json()


def ask_claude(status, market_snapshot):
    prompt = f"""You are managing a swing-trading position with ${status['current_balance']:.2f} available.
These are the top {len(market_snapshot)} candidates right now, screened by momentum from a much larger
universe of symbols - each one already stood out enough to reach you, so treat them as pre-filtered,
not as a random watchlist.
Current market snapshot: {json.dumps(market_snapshot)}"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=[{"type": "text", "text": PLAYBOOK, "cache_control": {"type": "ephemeral"}}],
        tools=[DECISION_TOOL],
        tool_choice={"type": "tool", "name": "trade_decision"},
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        requests.post(f"{CONTROLLER_URL}/usage", json={
            "agent_name": AGENT_NAME,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "cache_creation_tokens": getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
            "cache_read_tokens": getattr(response.usage, "cache_read_input_tokens", 0) or 0,
        }, timeout=10)
    except Exception as e:
        log.warning(f"failed to report token usage: {e}")
    for block in response.content:
        if block.type == "tool_use":
            return block.input
    return {"action": "hold", "reasoning": "no tool call returned"}


def fetch_market_snapshot():
    # Screened server-side (see /screen on the Controller) - only the top-N
    # momentum candidates from the full universe ever reach this agent, which
    # is what keeps token cost flat regardless of how large the universe gets.
    resp = requests.get(f"{CONTROLLER_URL}/screen", timeout=20)
    resp.raise_for_status()
    return resp.json()


# Tracks what this pod saw and decided last cycle, purely in memory - if the pod
# restarts, this just resets to "always ask," which is a safe default, not a bug.
# Only matters if last decision was HOLD: if we're managing an open position,
# always re-check fresh every cycle, since managing a live trade matters more
# than the token savings from skipping.
_last_check = {"symbol": None, "price": None, "action": "hold"}
SKIP_THRESHOLD_PCT = 0.005  # skip only if the top candidate's price moved less than 0.5%


def run_cycle(status):
    snapshot = fetch_market_snapshot()
    top_symbol, top_price = None, None
    if snapshot:
        top_symbol = next(iter(snapshot))
        top_price = snapshot[top_symbol]["price"]

    if (_last_check["action"] == "hold" and top_symbol is not None
            and _last_check["symbol"] == top_symbol and _last_check["price"] is not None
            and abs(top_price - _last_check["price"]) / _last_check["price"] < SKIP_THRESHOLD_PCT):
        log.info(f"no material change since last check ({top_symbol} ~${top_price:.2f}) - skipping Claude call")
        return

    decision = ask_claude(status, snapshot)
    log.info(f"decision: {decision}")

    try:
        requests.post(f"{CONTROLLER_URL}/decision", json={
            "agent_name": AGENT_NAME,
            "action": decision["action"],
            "symbol": decision.get("symbol"),
            "reasoning": decision.get("reasoning"),
        }, timeout=10)
    except Exception as e:
        log.warning(f"failed to report decision: {e}")

    _last_check["symbol"] = top_symbol
    _last_check["price"] = top_price
    _last_check["action"] = decision["action"]

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
    log.info(f"controller response: {resp.status_code} {resp.text}")


if __name__ == "__main__":
    log.info(f"agent {AGENT_NAME} starting - screening the full universe each cycle, top-N candidates via /screen")
    RETRY_SECONDS = 30  # a failed connection is a transient problem, not a market-hours signal
    while True:
        session = None
        try:
            status = get_my_status()
            session = status.get("market_session", "closed")
            if status["status"] != "active":
                log.info(f"{AGENT_NAME} is {status['status']}, sleeping")
            else:
                run_cycle(status)
        except Exception as e:
            log.error(f"cycle failed: {e}")

        if session is None:
            sleep_for = RETRY_SECONDS  # couldn't even reach the Controller - retry soon, don't wait an hour
        else:
            sleep_for = CYCLE_SECONDS if session == "open" else OFF_PEAK_CYCLE_SECONDS
        log.info(f"market session: {session} - sleeping {sleep_for}s")
        time.sleep(sleep_for)
