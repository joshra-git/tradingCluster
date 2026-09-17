"""
A curated universe of large, liquid, established companies.

This exists because Alpaca's "movers" screener returns the day's biggest
percentage gainers - structurally a list of the most volatile stocks in the
market. Buying from that list means repeatedly buying things that have already
spiked, moments before the people sitting on those gains take them. GTBP was
bought that way and gapped down 13% at the open.

These names move less, but they gap less, fill cleanly, and do not run 15%
intraday swings that blow through a stop-loss before it can act.
"""
import alpaca_client

MAX_RUNUP_PCT = 25.0        # already up this much? too late, skip it
MAX_DAILY_SWING_PCT = 8.0   # too choppy to hold a stop through

SAFE_UNIVERSE = [
    "AAPL","MSFT","GOOGL","AMZN","NVDA","META","AVGO","ORCL","CRM","ADBE",
    "CSCO","AMD","INTC","QCOM","TXN","IBM","NOW","INTU",
    "JPM","BAC","WFC","GS","MS","V","MA","AXP","BLK","SCHW","C","PGR",
    "UNH","JNJ","LLY","ABBV","MRK","PFE","TMO","ABT","DHR","AMGN","BMY","CVS",
    "WMT","COST","HD","PG","KO","PEP","MCD","NKE","SBUX","TGT","LOW","CL","MDLZ",
    "CAT","BA","HON","GE","UPS","LMT","RTX","DE","XOM","CVX","COP","NEE","UNP",
    "DIS","NFLX","CMCSA","T","VZ","TMUS",
    "SPY","QQQ","DIA","IWM","VOO","VTI",
]


def screen(lookback_days=5, top_n=5):
    prices = alpaca_client.get_batch_quotes(SAFE_UNIVERSE)
    bars_by_symbol = alpaca_client.get_batch_bars(SAFE_UNIVERSE, lookback_days)

    scored = []
    for sym in SAFE_UNIVERSE:
        if sym not in prices or sym not in bars_by_symbol:
            continue
        bars = bars_by_symbol[sym]
        if len(bars) < 2:
            continue
        price = prices[sym]
        change_pct = (price - float(bars[0].close)) / float(bars[0].close) * 100

        # Only buy things going up. A large cap falling is still a falling knife.
        if change_pct <= 0 or change_pct > MAX_RUNUP_PCT:
            continue

        swings = [(float(b.high) - float(b.low)) / float(b.low) * 100 for b in bars if b.low]
        avg_swing = sum(swings) / len(swings) if swings else 0
        if avg_swing > MAX_DAILY_SWING_PCT:
            continue

        scored.append({
            "symbol": sym,
            "price": round(price, 2),
            f"{lookback_days}d_change_pct": round(change_pct, 2),
            "avg_daily_swing_pct": round(avg_swing, 1),
            "recent_closes": [round(float(b.close), 2) for b in bars],
        })

    scored.sort(key=lambda x: x[f"{lookback_days}d_change_pct"], reverse=True)
    return {x["symbol"]: x for x in scored[:top_n]}
