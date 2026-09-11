"""
Every proposal from an agent pod passes through here before it touches Alpaca.
Claude proposes; this file has the final veto. Keep these checks boring and
deterministic on purpose - this is not a place for another LLM call.
"""


def validate_proposal(agent, proposal, config, todays_pnl, account_day_trade_count):
    """
    agent: dict from db.get_agent()
    proposal: dict with symbol, side, qty, stop_loss_pct (for buys)
    config: parsed tier config (see controller/config.py)
    Returns (approved: bool, reason: str | None, sized_qty: float)
    """
    balance = float(agent["current_balance"])
    qty = float(proposal["qty"])
    side = proposal["side"]

    # 1. Mandatory stop-loss on every buy. No exceptions - this is the one rule
    #    that most directly caps how bad a single bad call can get.
    if side == "buy" and not proposal.get("stop_loss_pct"):
        return False, "buy proposals must include stop_loss_pct", 0

    # 2. Max position size as a fraction of this agent's own balance.
    #    Estimate cost using the proposal's reference price if provided, otherwise
    #    the caller (app.py) should size against a fetched quote before calling this.
    max_position_pct = config.get("max_position_pct", 0.25)
    est_cost = qty * proposal.get("ref_price", 0)
    if side == "buy" and est_cost > balance * max_position_pct:
        max_qty = (balance * max_position_pct) / max(proposal.get("ref_price", 1), 0.01)
        return True, f"resized to respect max_position_pct ({max_position_pct:.0%})", max_qty

    # 3. Daily loss circuit breaker, per agent.
    max_daily_loss_pct = config.get("max_daily_loss_pct", 0.10)
    if todays_pnl < -balance * max_daily_loss_pct:
        return False, "daily loss limit hit for this agent - halted for today", 0

    # 4. PDT awareness. This is an ACCOUNT-WIDE limit on Alpaca's side, not per-agent,
    #    so it's checked against the account total, not this agent alone.
    #    Real accounts under $25k get flagged after 3 day-trades in 5 rolling days.
    pdt_threshold = config.get("pdt_day_trade_limit", 3)
    if account_day_trade_count >= pdt_threshold and balance < 25000:
        return False, "approaching PDT day-trade limit for the account - blocking further same-day round trips", 0

    return True, None, qty
