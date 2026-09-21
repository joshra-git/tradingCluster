"""
Historical daily crypto bars for the backtester, cached locally so repeated
runs and parameter sweeps don't keep re-hitting Alpaca.

Deliberately independent of controller/alpaca_client.py's live-trading setup:
Alpaca crypto market data needs no API key at all, so this needs none either,
and this module never touches the Controller's broker credentials.
"""
import json
import os

from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame

CACHE_DIR = os.path.join(os.path.dirname(__file__), "data_cache")
_client = CryptoHistoricalDataClient()


def _cache_path(symbol, start, end):
    safe = symbol.replace("/", "-")
    return os.path.join(CACHE_DIR, f"{safe}_{start.date()}_{end.date()}.json")


def fetch_daily_closes(symbols, start, end):
    """Returns {symbol: [(date_str, close, high, low), ...]}, oldest first.
    Fetched once per (symbol, date range) then cached to disk - a symbol with
    no data for the range (delisted, launched after `start`) comes back as an
    empty list rather than raising, since callers already handle sparse/missing
    history gracefully (this is exactly what real trading has to tolerate too)."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    out = {}
    to_fetch = []
    for sym in symbols:
        path = _cache_path(sym, start, end)
        if os.path.exists(path):
            with open(path) as f:
                out[sym] = json.load(f)
        else:
            to_fetch.append(sym)

    if to_fetch:
        req = CryptoBarsRequest(symbol_or_symbols=to_fetch, timeframe=TimeFrame.Day, start=start, end=end)
        bar_set = _client.get_crypto_bars(req)
        for sym in to_fetch:
            try:
                bars = bar_set[sym]
            except (KeyError, IndexError):
                bars = []
            rows = [(b.timestamp.date().isoformat(), float(b.close), float(b.high), float(b.low)) for b in bars]
            out[sym] = rows
            with open(_cache_path(sym, start, end), "w") as f:
                json.dump(rows, f)

    return out
