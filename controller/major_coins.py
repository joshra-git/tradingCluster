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
    "AAVE/USD",
    "SHIB/USD",
    "CRV/USD",
    "GRT/USD",
    "ARB/USD",
    "FIL/USD",
    "XTZ/USD",
    # --- added: confirmed tradable with deep historical bars (>200 daily
    # bars each as of 2026-09-22, no thin/spun-up-then-dead listings) ---
    "POL/USD",      # Polygon's current token; replaces the old MATIC/USD
                     # listing below, which Alpaca no longer trades
    "SKY/USD",       # MakerDAO's rebrand; replaces the old MKR/USD listing
    "BAT/USD",
    "LDO/USD",
    "SUSHI/USD",
    "YFI/USD",
    "RENDER/USD",
]

# Pruned as of 2026-09-22 (confirmed untradable on Alpaca via zero-bar
# historical fetch, per the backtester's own discovery of this - see
# CLAUDE.md's "Known gaps" #12): MATIC/USD (Polygon rebranded to POL, added
# above), MKR/USD (MakerDAO rebranded to SKY, added above), ATOM/USD,
# ALGO/USD, NEAR/USD, OP/USD, INJ/USD, SUI/USD, APT/USD, SAND/USD, MANA/USD.
# Re-add if Alpaca ever re-lists one - check with tradable_universe() first.

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


def rank_by_momentum(symbols, prices, closes_by_symbol, lookback_days=20, top_n=5):
    """The actual ranking rule - pure arithmetic, no network calls. Both the
    live Controller (via screen() below) and the backtester call this exact
    function, so a backtest can never silently rank coins differently than
    production does.

    prices: {symbol: current_price}. closes_by_symbol: {symbol: [close, ...]},
    oldest first, at least `lookback_days` of daily closes.

    Deliberately does NOT filter out falling coins the way the stocks path does -
    the intent here is to hold through swings, so the stop-loss protects the
    downside rather than the entry filter.
    """
    scored = []
    for sym in symbols:
        if sym not in prices or sym not in closes_by_symbol:
            continue
        closes = closes_by_symbol[sym]
        if len(closes) < 2:
            continue
        price = prices[sym]
        change_pct = (price - closes[0]) / closes[0] * 100
        scored.append({
            "symbol": sym,
            "price": float(f"{price:.8g}"),
            f"{lookback_days}d_change_pct": round(change_pct, 2),
            "recent_closes": [float(f"{c:.8g}") for c in closes[-10:]],
        })

    scored.sort(key=lambda x: x[f"{lookback_days}d_change_pct"], reverse=True)
    return {x["symbol"]: x for x in scored[:top_n]}


def screen(lookback_days=20, top_n=5):
    """Live wrapper: fetches current quotes + bars, then hands off to
    rank_by_momentum()."""
    symbols = tradable_universe()
    if not symbols:
        return {}

    prices = alpaca_client.get_crypto_batch_quotes(symbols)
    bars_by_symbol = alpaca_client.get_crypto_batch_bars(symbols, lookback_days)
    closes_by_symbol = {sym: [float(b.close) for b in bars] for sym, bars in bars_by_symbol.items()}

    return rank_by_momentum(symbols, prices, closes_by_symbol, lookback_days, top_n)
