"""
Thin wrapper around alpaca-py. The Controller is the ONLY thing in this
system that ever imports this module - agent pods never talk to Alpaca directly.
"""
import os
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed
from datetime import datetime, timedelta

API_KEY = os.environ["ALPACA_API_KEY"]
SECRET_KEY = os.environ["ALPACA_SECRET_KEY"]
PAPER = os.environ.get("ALPACA_PAPER", "true").lower() == "true"

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


def submit_market_order(symbol, side, qty, client_order_id):
    order = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
        client_order_id=client_order_id,  # Alpaca de-dupes on this - safe to retry
    )
    result = trading_client.submit_order(order)
    return {
        "alpaca_order_id": str(result.id),
        "status": result.status,
        "filled_price": float(result.filled_avg_price) if result.filled_avg_price else None,
    }
