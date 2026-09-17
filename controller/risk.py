"""
Every proposal from an agent pod passes through here before it touches Alpaca.
The model proposes; this file has the final veto. Keep these checks boring and
deterministic on purpose - this is not a place for another LLM call.
"""


def validate_proposal(agent, proposal, config, todays_pnl, account_day_trade_count,
                      claimed_symbols=None):
    """
    agent: dict from db.get_agent()
    proposal: dict with symbol, side, qty, stop_loss_pct (for buys)
    config: parsed tier config
    claimed_symbols: symbols other agents already hold or have pending orders for
    Returns (approved: bool, reason: str | None, sized_qty: int)
    """
    balance = float(agent["current_balance"])
    qty = float(proposal["qty"])
    side = proposal["side"]
    symbol = proposal["symbol"]
    claimed_symbols = claimed_symbols or []
    # Crypto trades in fractions and has no pattern-day-trader rule, so two of
    # the checks below simply do not apply to it.
    is_crypto = proposal.get("asset_class") == "crypto"
    # Crypto is always fractional; stocks depend on the asset. Fractional sizing
    # is what lets a $650 share fit inside a $300 position budget at all.
    allows_fractions = is_crypto or proposal.get("fractionable", False)

    # 1. Mandatory stop-loss on every buy. No exceptions - this is the one rule
    #    that most directly caps how bad a single bad call can get.
    if side == "buy" and not proposal.get("stop_loss_pct"):
        return False, "buy proposals must include stop_loss_pct", 0

    # 2. Diversification: one agent per symbol. If another agent is already in this
    #    name, this one has to find something else. Without this, agents looking at
    #    an identical shortlist reliably converge on the same pick, which quietly
    #    doubles exposure to a single company while looking like two positions.
    #    Only applies to buys - selling out of something is always allowed.
    if side == "buy" and symbol in claimed_symbols:
        return False, f"another agent already holds {symbol} - keeping the eggs in different baskets", 0

    # 3. Max position size as a fraction of this agent's own balance.
    max_position_pct = config.get("max_position_pct", 0.25)
    ref_price = proposal.get("ref_price", 0)
    est_cost = qty * ref_price
    if side == "buy" and est_cost > balance * max_position_pct:
        # Whole shares only - Alpaca rejects fractional quantities on many stocks,
        # and rounding down is the safe direction (never spend more than intended).
        budget = balance * max_position_pct
        raw_qty = budget / max(ref_price, 0.01)
        if allows_fractions:
            max_qty = round(raw_qty, 6)
            if max_qty <= 0:
                return False, f"position budget of ${budget:,.2f} is too small to buy any {symbol}", 0
        else:
            max_qty = int(raw_qty)
            if max_qty < 1:
                return False, (f"one whole share of {symbol} costs more than this agent's "
                               f"per-position limit of ${budget:,.2f}, and it cannot be "
                               f"bought in fractions"), 0
        return True, f"resized to respect max_position_pct ({max_position_pct:.0%})", max_qty

    # 4. Daily loss circuit breaker, per agent.
    max_daily_loss_pct = config.get("max_daily_loss_pct", 0.10)
    if todays_pnl < -balance * max_daily_loss_pct:
        return False, "daily loss limit hit for this agent - halted for today", 0

    # 5. PDT awareness. This is an ACCOUNT-WIDE limit on Alpaca's side, not per-agent,
    #    so it's checked against the account total, not this agent alone.
    pdt_threshold = config.get("pdt_day_trade_limit", 3)
    if not is_crypto and account_day_trade_count >= pdt_threshold and balance < 25000:
        return False, "approaching PDT day-trade limit for the account - blocking further same-day round trips", 0

    if side == "buy" and not allows_fractions and int(qty) < 1:
        return False, "proposed quantity rounds down to zero whole shares", 0

    if allows_fractions:
        return True, None, round(qty, 6)
    return True, None, int(qty) if side == "buy" else qty
