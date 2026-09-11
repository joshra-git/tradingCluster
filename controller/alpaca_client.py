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
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed
from datetime import datetime, timedelta

log = logging.getLogger("alpaca_client")

API_KEY = os.environ["ALPACA_API_KEY"]
SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
PAPER = os.environ.get("ALPACA_PAPER", "true").lower() == "true"
DATA_BASE_URL = "https://data.alpaca.markets"

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)


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


def discover_candidates(min_price=5.0, top=25):
    results = []

    try:
        movers = fetch_movers(top=top)
        for bucket, source in (("gainers", "mover_gainer"), ("losers", "mover_loser")):
            for rank, item in enumerate(movers.get(bucket, []), start=1):
                symbol = _first_present(item, ["symbol", "Symbol"])
                price = _first_present(item, ["price", "last_price", "close", "Price"])
                pct = _first_present(item, ["percent_change", "change_percent", "pct_change", "Change_percent"])
                if not symbol or price is None or float(price) < min_price:
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
            if not symbol:
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
    price = get_latest_price(symbol)
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=datetime.now() - timedelta(days=lookback_days * 3),
        feed=DataFeed.IEX,
    )
    bars = data_client.get_stock_bars(req)[symbol]
    bars = bars[-lookback_days:]
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
    req = StockLatestQuoteRequest(symbol_or_symbols=symbols, feed=DataFeed.IEX)
    quotes = data_client.get_stock_latest_quote(req)
    prices = {}
    for sym, q in quotes.items():
        price = q.ask_price or q.bid_price
        if price:
            prices[sym] = float(price)
    return prices


def get_batch_bars(symbols, lookback_days=5):
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
        scored.append({
            "symbol": sym,
            "price": round(price, 2),
            f"{lookback_days}d_change_pct": round(change_pct, 2) if change_pct is not None else None,
            "recent_closes": [round(float(b.close), 2) for b in bars],
        })

    scored.sort(key=lambda s: s.get(f"{lookback_days}d_change_pct") or -999, reverse=True)
    return {s["symbol"]: s for s in scored[:top_n]}


def submit_market_order(symbol, side, qty, client_order_id):
    order = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
        client_order_id=client_order_id,
    )
    result = trading_client.submit_order(order)
    return {
        "alpaca_order_id": str(result.id),
        "status": result.status,
        "filled_price": float(result.filled_avg_price) if result.filled_avg_price else None,
    }
