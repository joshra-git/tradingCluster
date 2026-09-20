"""
A fixed list of major, established cryptocurrencies.

Hardcoded rather than ranked live because Alpaca does not expose market-cap
rankings, and its "movers" screener returns whatever spiked hardest today -
which is how PEPE and HYPE kept reaching the shortlist. This list is boring on
purpose. Edit it directly to add or remove a coin.
"""
import alpaca_client

MAJOR_COINS = [
    "BTC/USD",
    "ETH/USD",
    "SOL/USD",
    "XRP/USD",
    "DOGE/USD",
    "ADA/USD",
    "LINK/USD",
    "AVAX/USD",
    "DOT/USD",
    "LTC/USD",
    "BCH/USD",
    "UNI/USD",
    "MATIC/USD",
    "AAVE/USD",
    "ATOM/USD",
    "ALGO/USD",
    "SHIB/USD",
    "CRV/USD",
    "MKR/USD",
    "GRT/USD",
    "NEAR/USD",
    "ARB/USD",
    "OP/USD",
    "INJ/USD",
    "SUI/USD",
    "APT/USD",
    "FIL/USD",
    "XTZ/USD",
    "SAND/USD",
    "MANA/USD",
]

_TRADABLE_CACHE = None


def tradable_universe():
    """Intersect our list with what this account can actually trade."""
    global _TRADABLE_CACHE
    if _TRADABLE_CACHE is not None:
        return _TRADABLE_CACHE
    try:
        available = set(alpaca_client.list_crypto_symbols())
        _TRADABLE_CACHE = [c for c in MAJOR_COINS if c in available]
    except Exception:
        _TRADABLE_CACHE = list(MAJOR_COINS)
    return _TRADABLE_CACHE


def screen(lookback_days=20, top_n=5):
    """Rank the major coins by momentum over the lookback window.

    Deliberately does NOT filter out falling coins the way the stocks path does -
    the intent here is to hold through swings, so the stop-loss protects the
    downside rather than the entry filter.
    """
    symbols = tradable_universe()
    if not symbols:
        return {}

    prices = alpaca_client.get_crypto_batch_quotes(symbols)
    bars_by_symbol = alpaca_client.get_crypto_batch_bars(symbols, lookback_days)

    scored = []
    for sym in symbols:
        if sym not in prices or sym not in bars_by_symbol:
            continue
        bars = bars_by_symbol[sym]
        if len(bars) < 2:
            continue
        price = prices[sym]
        change_pct = (price - float(bars[0].close)) / float(bars[0].close) * 100
        scored.append({
            "symbol": sym,
            "price": float(f"{price:.8g}"),
            f"{lookback_days}d_change_pct": round(change_pct, 2),
            "recent_closes": [float(f"{float(b.close):.8g}") for b in bars[-10:]],
        })

    scored.sort(key=lambda x: x[f"{lookback_days}d_change_pct"], reverse=True)
    return {x["symbol"]: x for x in scored[:top_n]}
