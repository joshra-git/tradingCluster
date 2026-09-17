"""
Thin wrapper around alpaca-py. The Controller is the ONLY thing in this
system that ever imports this module - agent pods never talk to Alpaca directly.
"""
import os
import logging
import requests
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.requests import (
    StockLatestQuoteRequest, StockBarsRequest,
    CryptoLatestQuoteRequest, CryptoBarsRequest,
)
from alpaca.trading.requests import GetAssetsRequest
from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed
from datetime import datetime, timedelta

log = logging.getLogger("alpaca_client")

API_KEY = os.environ["ALPACA_API_KEY"]
SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
PAPER = os.environ.get("ALPACA_PAPER", "true").lower() == "true"
DATA_BASE_URL = "https://data.alpaca.markets"

# Quality filters - these exist because the scanner asks for "top gainers",
# which structurally surfaces stocks that have already spiked.
MAX_RUNUP_PCT = float(os.environ.get("MAX_RUNUP_PCT", "25"))          # skip if already up this much
MAX_DAILY_SWING_PCT = float(os.environ.get("MAX_DAILY_SWING_PCT", "8"))  # skip violently choppy names
MIN_AVG_VOLUME = float(os.environ.get("MIN_AVG_VOLUME", "500000"))    # skip illiquid names

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)
# Crypto data needs no API key for market data, but passing them is harmless
# and keeps a single consistent construction pattern.
crypto_data_client = CryptoHistoricalDataClient(API_KEY, SECRET_KEY)


def get_account():
    acct = trading_client.get_account()
    return {
        "cash": float(acct.cash),
        "portfolio_value": float(acct.portfolio_value),
        "day_trade_count": int(acct.daytrade_count) if acct.daytrade_count is not None else 0,
        "pattern_day_trader": acct.pattern_day_trader,
    }


def _screener_headers():
    return {"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": SECRET_KEY}


def fetch_movers(top=25):
    """Real market-wide gainers/losers from Alpaca's own screener - not a hand-typed list.
    Called directly via REST since exact SDK response field names aren't fully pinned down;
    parsing below is defensive about which key names show up."""
    resp = requests.get(
        f"{DATA_BASE_URL}/v1beta1/screener/stocks/movers",
        headers=_screener_headers(), params={"top": top}, timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_most_actives(top=25):
    resp = requests.get(
        f"{DATA_BASE_URL}/v1beta1/screener/stocks/most-actives",
        headers=_screener_headers(), params={"top": top}, timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _first_present(d, keys):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _looks_like_derivative(symbol):
    """Warrants, rights, and units trade under recognizable ticker patterns
    (a dot suffix like .WS/.RT, or a trailing W) - these are NOT common stock,
    they're speculative derivative instruments that make terrible 'promising
    company' candidates even when they clear a price floor."""
    return "." in symbol or symbol.endswith("W") or symbol.endswith("WS")


def discover_candidates(min_price=5.0, top=25):
    """One discovery pass across the real market: gainers, losers, and most-active
    by volume. Applies a price floor and excludes warrant/rights tickers. This is
    what replaces a hand-typed universe list - the market tells us what's moving."""
    results = []

    try:
        movers = fetch_movers(top=top)
        for bucket, source in (("gainers", "mover_gainer"), ("losers", "mover_loser")):
            for rank, item in enumerate(movers.get(bucket, []), start=1):
                symbol = _first_present(item, ["symbol", "Symbol"])
                price = _first_present(item, ["price", "last_price", "close", "Price"])
                pct = _first_present(item, ["percent_change", "change_percent", "pct_change", "Change_percent"])
                if not symbol or _looks_like_derivative(symbol) or price is None or float(price) < min_price:
                    continue
                results.append({"symbol": symbol, "source": source, "rank": rank,
                                 "pct_change": pct, "price": price, "volume": None})
    except Exception as e:
        log.error(f"fetch_movers failed: {e}")

    try:
        actives = fetch_most_actives(top=top)
        for rank, item in enumerate(actives.get("most_actives", []), start=1):
            symbol = _first_present(item, ["symbol", "Symbol"])
            volume = _first_present(item, ["volume", "trade_count", "Volume"])
            if not symbol or _looks_like_derivative(symbol):
                continue
            results.append({"symbol": symbol, "source": "most_active", "rank": rank,
                             "pct_change": None, "price": None, "volume": volume})
    except Exception as e:
        log.error(f"fetch_most_actives failed: {e}")

    if not results:
        log.warning("discover_candidates found nothing this pass - check API response shape via /discover-debug")

    return results


def get_latest_price(symbol):
    req = StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX)
    quote = data_client.get_stock_latest_quote(req)[symbol]
    return float(quote.ask_price or quote.bid_price)


def get_daily_snapshot(symbol, lookback_days=5):
    """Latest price plus a rough recent trend - just enough for the agent to reason
    about direction and momentum without holding any Alpaca credentials itself."""
    price = get_latest_price(symbol)
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=datetime.now() - timedelta(days=lookback_days * 3),  # buffer for weekends/holidays
        feed=DataFeed.IEX,
    )
    bars = data_client.get_stock_bars(req)[symbol]
    bars = bars[-lookback_days:]  # trim the buffer window back down to what was actually asked for
    if len(bars) >= 2:
        change_pct = (price - float(bars[0].close)) / float(bars[0].close) * 100
    else:
        change_pct = None
    return {
        "price": round(price, 2),
        f"{lookback_days}d_change_pct": round(change_pct, 2) if change_pct is not None else None,
        "recent_closes": [round(float(b.close), 2) for b in bars],
    }


def get_batch_quotes(symbols):
    """One Alpaca call for many symbols, instead of one call per symbol.
    Returns {symbol: price}, silently skipping symbols with no quote available."""
    req = StockLatestQuoteRequest(symbol_or_symbols=symbols, feed=DataFeed.IEX)
    quotes = data_client.get_stock_latest_quote(req)
    prices = {}
    for sym, q in quotes.items():
        price = q.ask_price or q.bid_price
        if price:
            prices[sym] = float(price)
    return prices


def get_batch_bars(symbols, lookback_days=5):
    """One Alpaca call for many symbols' daily bars, instead of one call per symbol."""
    req = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Day,
        start=datetime.now() - timedelta(days=lookback_days * 3),
        feed=DataFeed.IEX,
    )
    bar_set = data_client.get_stock_bars(req)
    bars_by_symbol = {}
    for sym in symbols:
        try:
            bars = bar_set[sym][-lookback_days:]
            if bars:
                bars_by_symbol[sym] = bars
        except (KeyError, IndexError):
            continue
    return bars_by_symbol


def screen_universe(symbols, lookback_days=5, top_n=5):
    """Cheap, non-LLM screen across the whole universe: two batched Alpaca calls
    total, regardless of how large the universe is. Ranks by 5-day momentum and
    returns only the top N candidates - this is what keeps Claude's per-cycle
    token cost flat even as the universe grows to 100+ symbols."""
    prices = get_batch_quotes(symbols)
    bars_by_symbol = get_batch_bars(symbols, lookback_days)

    scored = []
    for sym in symbols:
        if sym not in prices or sym not in bars_by_symbol:
            continue
        bars = bars_by_symbol[sym]
        price = prices[sym]
        if len(bars) >= 2:
            change_pct = (price - float(bars[0].close)) / float(bars[0].close) * 100
        else:
            change_pct = None
        # Reject what has already gone parabolic. Buying something up 70% in a
        # week means buying from people sitting on profits who are looking to
        # take them - which is how GTBP was bought near its top.
        if change_pct is not None and change_pct > MAX_RUNUP_PCT:
            continue

        # Reject violently choppy names. A stock swinging 15% a day gaps through
        # stop-losses, so the exit fills far below where the stop was set.
        highs_lows = [(float(b.high) - float(b.low)) / float(b.low) * 100 for b in bars if b.low]
        avg_swing = sum(highs_lows) / len(highs_lows) if highs_lows else 0
        if avg_swing > MAX_DAILY_SWING_PCT:
            continue

        # Reject thin volume - illiquid names cannot be exited cleanly.
        avg_vol = sum(float(b.volume or 0) for b in bars) / len(bars)
        if avg_vol < MIN_AVG_VOLUME:
            continue

        scored.append({
            "symbol": sym,
            "price": round(price, 2),
            f"{lookback_days}d_change_pct": round(change_pct, 2) if change_pct is not None else None,
            "avg_daily_swing_pct": round(avg_swing, 1),
            "avg_volume": int(avg_vol),
            "recent_closes": [round(float(b.close), 2) for b in bars],
        })

    scored.sort(key=lambda s: s.get(f"{lookback_days}d_change_pct") or -999, reverse=True)
    return {s["symbol"]: s for s in scored[:top_n]}


def get_order(alpaca_order_id):
    """Fetch an order's current state. Needed because submit_order returns before
    the fill lands - Alpaca answers with pending_new and no fill price, then fills
    asynchronously. Without polling this, the ledger never learns what happened."""
    o = trading_client.get_order_by_id(alpaca_order_id)
    return {
        "status": str(o.status).split(".")[-1].lower(),
        "filled_qty": float(o.filled_qty or 0),
        "filled_price": float(o.filled_avg_price) if o.filled_avg_price else None,
    }


_FRACTIONABLE_CACHE = {}


def is_fractionable(symbol):
    """Most large caps allow fractional shares; thin micro caps do not. Asking
    Alpaca beats guessing - guessing is what produced the repeated
    'asset X is not fractionable' rejections. Cached because this rarely
    changes and the answer is needed on every sizing decision."""
    if symbol in _FRACTIONABLE_CACHE:
        return _FRACTIONABLE_CACHE[symbol]
    try:
        asset = trading_client.get_asset(symbol)
        result = bool(getattr(asset, "fractionable", False))
    except Exception as e:
        log.warning(f"could not check fractionability of {symbol}, assuming whole shares: {e}")
        result = False
    _FRACTIONABLE_CACHE[symbol] = result
    return result


def submit_market_order(symbol, side, qty, client_order_id, asset_class="stocks"):
    # Crypto trades around the clock, so DAY (expires at the close) is invalid -
    # it needs GTC. Crypto also supports fractional quantities, whereas many
    # stocks do not, which is why qty is only forced to a whole number upstream
    # for stocks.
    tif = TimeInForce.GTC if asset_class == "crypto" else TimeInForce.DAY
    order = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
        time_in_force=tif,
        client_order_id=client_order_id,  # Alpaca de-dupes on this - safe to retry
    )
    result = trading_client.submit_order(order)
    return {
        "alpaca_order_id": str(result.id),
        "status": result.status,
        "filled_price": float(result.filled_avg_price) if result.filled_avg_price else None,
    }


# ---------------------------------------------------------------------------
# Crypto. Alpaca's screener endpoints are stocks-only, so discovery here works
# differently: there are only a few dozen tradable pairs, so we can rank the
# entire universe directly rather than needing a movers list to narrow it first.
# ---------------------------------------------------------------------------

def list_crypto_symbols():
    """Every crypto pair this account can actually trade."""
    req = GetAssetsRequest(asset_class=AssetClass.CRYPTO, status=AssetStatus.ACTIVE)
    assets = trading_client.get_all_assets(req)
    return [a.symbol for a in assets if a.tradable]


def get_crypto_batch_quotes(symbols):
    req = CryptoLatestQuoteRequest(symbol_or_symbols=symbols)
    quotes = crypto_data_client.get_crypto_latest_quote(req)
    prices = {}
    for sym, q in quotes.items():
        price = q.ask_price or q.bid_price
        if price:
            prices[sym] = float(price)
    return prices


def get_crypto_batch_bars(symbols, lookback_days=5):
    req = CryptoBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Day,
        start=datetime.now() - timedelta(days=lookback_days * 3),
    )
    bar_set = crypto_data_client.get_crypto_bars(req)
    out = {}
    for sym in symbols:
        try:
            bars = bar_set[sym][-lookback_days:]
            if bars:
                out[sym] = bars
        except (KeyError, IndexError):
            continue
    return out


def get_crypto_price(symbol):
    req = CryptoLatestQuoteRequest(symbol_or_symbols=symbol)
    quote = crypto_data_client.get_crypto_latest_quote(req)[symbol]
    return float(quote.ask_price or quote.bid_price)


def _base_coin(symbol):
    """UNI/USD, UNI/USDC, UNI/USDT and UNI/BTC are all the same coin quoted
    against different currencies. Without collapsing them, a shortlist of five
    can be two actual coins wearing five names - and the diversification rule
    cannot catch it, because the ticker strings genuinely differ."""
    return symbol.split("/")[0]


def screen_crypto(lookback_days=5, top_n=5):
    """Rank the whole tradable crypto universe by recent momentum."""
    symbols = list_crypto_symbols()
    if not symbols:
        return {}
    prices = get_crypto_batch_quotes(symbols)
    bars_by_symbol = get_crypto_batch_bars(symbols, lookback_days)

    scored = []
    for sym in symbols:
        if sym not in prices or sym not in bars_by_symbol:
            continue
        bars = bars_by_symbol[sym]
        price = prices[sym]
        if len(bars) >= 2:
            change_pct = (price - float(bars[0].close)) / float(bars[0].close) * 100
        else:
            continue
        scored.append({
            "symbol": sym,
            "price": round(price, 4),
            f"{lookback_days}d_change_pct": round(change_pct, 2),
            "recent_closes": [round(float(b.close), 4) for b in bars],
        })

    scored.sort(key=lambda x: x[f"{lookback_days}d_change_pct"], reverse=True)
    # Keep only the USD pair of each coin. Stablecoin pairs track the USD pair
    # within a fraction of a percent, and BTC-denominated pairs are priced in
    # bitcoin, which makes dollar position sizing meaningless.
    seen, unique = set(), []
    for x in scored:
        base = _base_coin(x["symbol"])
        if base in seen or not x["symbol"].endswith("/USD"):
            continue
        seen.add(base)
        unique.append(x)

    return {x["symbol"]: x for x in unique[:top_n]}


def broker_position_prices():
    """The broker's own consolidated price for everything we hold. IEX quotes
    (what get_latest_price uses) come from a single small exchange and were
    running 12-20% above reality on thin names - which inflated trailing-stop
    high-water marks and made stops fire at the wrong level."""
    out = {}
    try:
        for p in trading_client.get_all_positions():
            if p.current_price:
                out[p.symbol] = float(p.current_price)
    except Exception as e:
        log.error(f"could not read broker position prices: {e}")
    return out


def price_for(symbol, asset_class):
    return get_crypto_price(symbol) if asset_class == "crypto" else get_latest_price(symbol)
