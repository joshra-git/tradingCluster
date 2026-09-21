"""
Stand-in for Claude's judgement. Deliberately dumb on purpose: the point of
this backtester is to test whether the deterministic scaffolding underneath
Claude - coin ranking, the market regime brake, position sizing, the trailing
stop - has any edge on its own, independent of whether Claude's picks are
good. Replaying Claude's actual historical decisions would cost real API
money per run and isn't reproducible run to run, so it's out of scope here.

Swap this out later for something smarter, or for replayed real historical
`decisions` once there's enough logged live history to make that meaningful.
"""


def decide(shortlist, holding, cash):
    """Buy the top-ranked candidate if there's cash and something to buy.
    Never sells - exits are the engine's trailing-stop job, exactly like
    production, where Claude doesn't control exits either."""
    if holding is not None or not shortlist or cash <= 0:
        return None
    top_symbol = next(iter(shortlist))
    return {"symbol": top_symbol}
