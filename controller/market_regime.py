"""
Market regime scoring - 0 to 100, where 100 is risk-on.

Why this exists: a momentum strategy buys things going up. In a broad downtrend
most things go down, so the strategy either finds nothing or buys weak bounces
that fail. Trend filters cut losses in bad conditions; they also cost upside by
lagging recoveries. This is a brake, not an accelerator.

Everything here uses Alpaca bar data the Controller already fetches. No new API
keys, no external services, no model calls.

IMPORTANT: thresholds and weights below are reasoned guesses, not backtested.
"""
import logging

import alpaca_client
import major_coins

log = logging.getLogger("market_regime")

CRYPTO_ANCHOR = "BTC/USD"
STOCK_ANCHOR = "SPY"


def _sma(values, window):
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def _closes(bars):
    return [float(b.close) for b in bars]


def _score_vs_average(price, avg, rising):
    """25 if above a rising average, 0 if below a falling one. The middle cases
    are deliberately unequal: below a rising average is a pullback in an uptrend;
    above a falling one is a bounce in a downtrend, which is the more dangerous
    place to be buying."""
    if avg is None or not avg:
        return 12
    above = price > avg
    if above and rising:
        return 25
    if above and not rising:
        return 12
    if not above and rising:
        return 8
    return 0


def _fetch_history(symbol, asset_class, days=260):
    if asset_class == "crypto":
        bars = alpaca_client.get_crypto_batch_bars([symbol], lookback_days=days)
    else:
        bars = alpaca_client.get_batch_bars([symbol], lookback_days=days)
    return bars.get(symbol, [])


def compute(asset_class="crypto"):
    anchor = CRYPTO_ANCHOR if asset_class == "crypto" else STOCK_ANCHOR
    components = {}

    try:
        closes = _closes(_fetch_history(anchor, asset_class))
    except Exception as e:
        log.error(f"regime: could not fetch {anchor} history: {e}")
        return {"score": 50, "regime": "unknown", "components": {},
                "note": f"could not read {anchor}, defaulting to neutral"}

    if len(closes) < 60:
        return {"score": 50, "regime": "unknown", "components": {},
                "note": f"only {len(closes)} bars of {anchor}, need 60+"}

    price = closes[-1]

    sma200 = _sma(closes, 200)
    sma200_prev = _sma(closes[:-20], 200) if len(closes) >= 220 else None
    rising200 = (sma200 is not None and sma200_prev is not None and sma200 > sma200_prev)
    components["anchor_vs_200d"] = _score_vs_average(price, sma200, rising200)

    sma50 = _sma(closes, 50)
    sma50_prev = _sma(closes[:-10], 50) if len(closes) >= 60 else None
    rising50 = (sma50 is not None and sma50_prev is not None and sma50 > sma50_prev)
    components["anchor_vs_50d"] = _score_vs_average(price, sma50, rising50)

    # Breadth: an anchor holding up while everything else falls is a narrow,
    # fragile market. The anchor alone cannot show that.
    try:
        if asset_class == "crypto":
            universe = major_coins.tradable_universe()
            all_bars = alpaca_client.get_crypto_batch_bars(universe, lookback_days=60)
        else:
            import safe_universe
            universe = safe_universe.SAFE_UNIVERSE
            all_bars = alpaca_client.get_batch_bars(universe, lookback_days=60)

        above, total = 0, 0
        for sym in universe:
            c = _closes(all_bars.get(sym) or [])
            avg = _sma(c, 50)
            if avg:
                total += 1
                if c[-1] > avg:
                    above += 1
        pct = (above / total) if total else 0.5
        components["breadth"] = round(pct * 25)
        components["_breadth_detail"] = f"{above}/{total} above their 50-day average"
    except Exception as e:
        log.warning(f"regime: breadth check failed ({e}), scoring neutral")
        components["breadth"] = 12

    if len(closes) >= 21:
        change = (closes[-1] - closes[-21]) / closes[-21] * 100
        components["momentum_20d"] = 25 if change > 10 else 18 if change > 0 else 8 if change > -10 else 0
        components["_momentum_detail"] = f"{anchor} {change:+.1f}% over 20 days"
    else:
        components["momentum_20d"] = 12

    score = sum(v for k, v in components.items() if not k.startswith("_"))
    return {"score": score, "regime": label_for(score), "components": components,
            "anchor": anchor, "anchor_price": round(price, 4)}


def label_for(score, cfg=None):
    cfg = cfg or {}
    if score >= cfg.get("regime_risk_on_threshold", 70):
        return "risk-on"
    if score >= cfg.get("regime_risk_off_threshold", 40):
        return "neutral"
    return "risk-off"


def exposure_multiplier(score, cfg=None):
    """How much of its normal position size an agent may deploy.
    risk-on  -> full size
    neutral  -> half, because a market neither trending up nor collapsing is
                where momentum strategies bleed most
    risk-off -> no new positions. Existing holdings and their stop-losses are
                untouched; this only blocks new entries."""
    cfg = cfg or {}
    regime = label_for(score, cfg)
    if regime == "risk-on":
        return 1.0
    if regime == "neutral":
        return cfg.get("regime_neutral_multiplier", 0.5)
    return 0.0
