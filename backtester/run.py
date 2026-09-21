"""
CLI entry point. Runs standalone - no Kubernetes, no Postgres, no broker
credentials, no Claude calls. Only needs the `alpaca-py`, `pyyaml`, and
`requests` packages already listed in controller/requirements.txt.

Examples:
    python3 run.py --days 365 --cash 499
    python3 run.py --days 180 --stop-loss-pct 8
    python3 run.py --days 365 --no-regime-filter
"""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "controller"))
os.environ.setdefault("ALPACA_API_KEY", "backtester-placeholder")
os.environ.setdefault("ALPACA_SECRET_KEY", "backtester-placeholder")

import major_coins  # noqa: E402

import data     # noqa: E402
import engine   # noqa: E402
import report   # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Backtest the deterministic scaffolding against real history.")
    parser.add_argument("--days", type=int, default=365, help="how many days of history to test")
    parser.add_argument("--cash", type=float, default=499.0, help="starting cash")
    parser.add_argument("--stop-loss-pct", type=float, default=None,
                         help="override crypto_stop_loss_pct (default: tier config's own default, 12%%)")
    parser.add_argument("--no-regime-filter", action="store_true",
                         help="disable the market regime brake, to see what it's actually buying you")
    parser.add_argument("--trade-log", action="store_true", help="print every simulated trade")
    args = parser.parse_args()

    end = datetime.now(timezone.utc)
    # Extra lookback before the test window so day 1 already has 260 days of
    # history behind it for the 200-day average - otherwise the regime filter
    # spends its first ~9 months reporting 'unknown' and defaulting neutral.
    start = end - timedelta(days=args.days + 260)

    symbols = list(major_coins.MAJOR_COINS)

    print(f"Fetching/caching {len(symbols)} coins, {start.date()} to {end.date()} "
          f"(cached after the first run)...")
    price_data = data.fetch_daily_closes(symbols, start, end)
    got = {s: len(rows) for s, rows in price_data.items() if rows}
    missing = [s for s in symbols if not price_data.get(s)]
    print(f"Got data for {len(got)}/{len(symbols)} coins" +
          (f" - no data for: {', '.join(missing)}" if missing else ""))

    cfg_overrides = {}
    if args.stop_loss_pct is not None:
        cfg_overrides["crypto_stop_loss_pct"] = args.stop_loss_pct
    if args.no_regime_filter:
        cfg_overrides["regime_filter_enabled"] = False

    test_start = end - timedelta(days=args.days)
    bt = engine.Backtest(price_data, test_start.date(), end.date(),
                          initial_cash=args.cash, cfg_overrides=cfg_overrides)
    equity_curve, trade_log = bt.run()

    summary = report.summarize(equity_curve, trade_log, args.cash)
    report.print_report(summary)

    if args.trade_log:
        print()
        print("Trade log:")
        for t in trade_log:
            if t["side"] == "buy":
                print(f"  {t['date']}  BUY  {t['symbol']:10s} qty={t['qty']:.4f} @ ${t['price']:.4f}")
            else:
                print(f"  {t['date']}  SELL {t['symbol']:10s} qty={t['qty']:.4f} @ ${t['price']:.4f}  "
                      f"pnl=${t['pnl']:+.2f}  ({t['reason']})")


if __name__ == "__main__":
    main()
