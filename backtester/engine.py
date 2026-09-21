"""
Day-by-day replay of the deterministic parts of the trading system - coin
ranking, the market regime filter, position sizing/stop-loss rules, and exit
logic - against real historical prices, calling the exact same functions
(major_coins.rank_by_momentum, market_regime.score_from_closes,
risk.validate_proposal) production does. See strategy.py for what stands in
for Claude's judgement instead.

Simplifications, stated plainly rather than hidden:
- One simulated agent, not the spawn/cull population. Spawning is about
  capital administration, not signal quality - modeling it here would add
  complexity without adding insight into whether the strategy works.
- One position at a time. Real agents can only hold what their cash allows
  anyway, and this keeps the sizing math directly comparable to a single
  agent's tier config.
- Daily-bar approximation of the trailing stop: a day's HIGH stands in for
  the intraday peak, a day's LOW is checked against the trailing threshold
  from that peak, and a triggered stop is assumed to fill AT the trigger
  price, not favourably. This is standard practice for a daily-bar backtest,
  not a replay of the real 60-second check.
- No fees/slippage modeled yet - the single most common cause of backtest
  results looking better than live results ever will. Treat every number
  this prints as an upper bound until that's added.
- No dip-buy guardrail (crypto_max_24h_runup_pct) - it needs intraday data
  daily bars don't have, so it's left inert here (stats_24h passed as {}),
  never faked.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "controller"))

# major_coins/market_regime transitively import alpaca_client, whose top-level
# code needs ALPACA_API_KEY/ALPACA_SECRET_KEY just to construct client objects
# - never to make a call, since only the pure functions below are used. These
# placeholders make the backtester standalone without touching real credentials.
os.environ.setdefault("ALPACA_API_KEY", "backtester-placeholder")
os.environ.setdefault("ALPACA_SECRET_KEY", "backtester-placeholder")

import major_coins   # noqa: E402
import market_regime  # noqa: E402
import risk           # noqa: E402
import tier_config    # noqa: E402

import strategy  # noqa: E402


def _window(rows, upto_index, n):
    """Last n closes ending at (and including) upto_index, oldest first."""
    start = max(0, upto_index - n + 1)
    return [c for _, c, _, _ in rows[start:upto_index + 1]]


class Backtest:
    def __init__(self, price_data, start_date, end_date, initial_cash=499.0, cfg_overrides=None):
        """price_data: {symbol: [(date_str, close, high, low), ...]}, oldest first."""
        self.price_data = price_data
        self.dates = sorted({d for rows in price_data.values() for d, *_ in rows
                              if start_date.isoformat() <= d <= end_date.isoformat()})
        self.cfg = {**tier_config.DEFAULTS, **(cfg_overrides or {})}
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.position = None  # {symbol, qty, entry_price, high_water_mark}
        self.equity_curve = []
        self.trade_log = []
        self._index_by_date = {sym: {d: i for i, (d, *_r) in enumerate(rows)}
                                for sym, rows in price_data.items()}

    def _row(self, symbol, d):
        idx = self._index_by_date.get(symbol, {}).get(d)
        return self.price_data[symbol][idx] if idx is not None else None

    def _closes_upto(self, symbol, d, n):
        idx = self._index_by_date.get(symbol, {}).get(d)
        if idx is None:
            return []
        return _window(self.price_data[symbol], idx, n)

    def run(self):
        anchor = market_regime.CRYPTO_ANCHOR
        symbols = list(self.price_data.keys())
        todays_realised_pnl = 0.0
        last_date_seen = None

        for d in self.dates:
            if d != last_date_seen:
                todays_realised_pnl = 0.0
                last_date_seen = d

            # --- 1. check the exit on any open position first ---
            if self.position:
                row = self._row(self.position["symbol"], d)
                if row:
                    _, close, high, low = row
                    self.position["high_water_mark"] = max(self.position["high_water_mark"], high)
                    stop_pct = self.cfg["crypto_stop_loss_pct"]
                    trigger = self.position["high_water_mark"] * (1 - stop_pct / 100)
                    if low <= trigger:
                        exit_price = trigger
                        pnl = (exit_price - self.position["entry_price"]) * self.position["qty"]
                        self.cash += self.position["qty"] * exit_price
                        todays_realised_pnl += pnl
                        self.trade_log.append({
                            "date": d, "side": "sell", "symbol": self.position["symbol"],
                            "qty": self.position["qty"], "price": exit_price, "pnl": pnl,
                            "reason": "trailing stop",
                        })
                        self.position = None

            # --- 2. today's market regime score ---
            anchor_closes = self._closes_upto(anchor, d, 260)
            universe_closes = {s: self._closes_upto(s, d, 60) for s in symbols}
            regime = market_regime.score_from_closes(anchor, anchor_closes, universe_closes)
            if self.cfg.get("regime_filter_enabled", True):
                mult = market_regime.exposure_multiplier(regime["score"], self.cfg)
            else:
                mult = 1.0

            # --- 3. rank candidates and let the strategy decide ---
            if self.position is None and mult > 0:
                prices_today, closes_by_symbol = {}, {}
                for s in symbols:
                    row = self._row(s, d)
                    if row:
                        prices_today[s] = row[1]
                        closes_by_symbol[s] = self._closes_upto(s, d, self.cfg["trend_window_days"])
                shortlist = major_coins.rank_by_momentum(
                    list(prices_today.keys()), prices_today, closes_by_symbol,
                    lookback_days=self.cfg["trend_window_days"], top_n=self.cfg["screen_top_n"],
                )
                decision = strategy.decide(shortlist, holding=None, cash=self.cash)
                if decision:
                    sym = decision["symbol"]
                    price = prices_today[sym]
                    scaled_cfg = {**self.cfg, "max_position_pct": self.cfg["max_position_pct"] * mult}
                    proposal = {
                        "symbol": sym, "side": "buy",
                        "qty": (self.cash * scaled_cfg["max_position_pct"]) / price,
                        "stop_loss_pct": self.cfg["crypto_stop_loss_pct"],
                        "asset_class": "crypto", "ref_price": price, "stats_24h": {},
                    }
                    approved, _reason, sized_qty = risk.validate_proposal(
                        {"current_balance": self.cash}, proposal, scaled_cfg,
                        todays_pnl=todays_realised_pnl, account_day_trade_count=0, claimed_symbols=[],
                    )
                    if approved and sized_qty > 0:
                        self.cash -= sized_qty * price
                        self.position = {"symbol": sym, "qty": sized_qty,
                                          "entry_price": price, "high_water_mark": price}
                        self.trade_log.append({
                            "date": d, "side": "buy", "symbol": sym,
                            "qty": sized_qty, "price": price, "pnl": None, "reason": None,
                        })

            # --- 4. mark equity for the day ---
            held_value = 0.0
            if self.position:
                row = self._row(self.position["symbol"], d)
                if row:
                    held_value = self.position["qty"] * row[1]
            self.equity_curve.append((d, self.cash + held_value))

        return self.equity_curve, self.trade_log
