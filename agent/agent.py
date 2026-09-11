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
SYMBOLS = os.environ.get("STRATEGY_SYMBOLS", "SPY,QQQ").split(",")
CYCLE_SECONDS = int(os.environ.get("CYCLE_SECONDS", "300"))
OFF_PEAK_CYCLE_SECONDS = int(os.environ.get("OFF_PEAK_CYCLE_SECONDS", "3600"))            # market-hours cadence: 5 min
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


def get_my_status():
    resp = requests.post(f"{CONTROLLER_URL}/heartbeat", json={"agent_name": AGENT_NAME}, timeout=10)
    resp.raise_for_status()
    return resp.json()


def ask_claude(status, market_snapshot):
    prompt = f"""You are managing a swing-trading position with ${status['current_balance']:.2f} available.
Watchlist: {', '.join(SYMBOLS)}
Current market snapshot: {json.dumps(market_snapshot)}

Decide whether to buy, sell, or hold this cycle. Favor swing positions (held
days, not minutes) over intraday scalping. Every buy MUST include a stop_loss_pct.
Report your decision using the trade_decision tool."""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        tools=[DECISION_TOOL],
        tool_choice={"type": "tool", "name": "trade_decision"},
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        requests.post(f"{CONTROLLER_URL}/usage", json={
            "agent_name": AGENT_NAME,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }, timeout=10)
    except Exception as e:
        log.warning(f"failed to report token usage: {e}")
    for block in response.content:
        if block.type == "tool_use":
            return block.input
    return {"action": "hold", "reasoning": "no tool call returned"}


def fetch_market_snapshot():
    # Routed through the Controller (not Alpaca directly) - agent pods never hold
    # Alpaca credentials, and the NetworkPolicy doesn't let them reach Alpaca anyway.
    resp = requests.get(f"{CONTROLLER_URL}/market-data", params={"symbols": ",".join(SYMBOLS)}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def run_cycle(status):
    snapshot = fetch_market_snapshot()
    decision = ask_claude(status, snapshot)
    log.info(f"decision: {decision}")

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
    log.info(f"agent {AGENT_NAME} starting, watchlist={SYMBOLS}")
    while True:
        session = "closed"  # safe default if the status check itself fails
        try:
            status = get_my_status()
            session = status.get("market_session", "closed")
            if status["status"] != "active":
                log.info(f"{AGENT_NAME} is {status['status']}, sleeping")
            else:
                run_cycle(status)
        except Exception as e:
            log.error(f"cycle failed, will retry next interval: {e}")

        sleep_for = CYCLE_SECONDS if session == "open" else OFF_PEAK_CYCLE_SECONDS
        log.info(f"market session: {session} - sleeping {sleep_for}s")
        time.sleep(sleep_for)
