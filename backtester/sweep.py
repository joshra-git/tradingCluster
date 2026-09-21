"""
Runs the same set of config variants across several consecutive, non-
overlapping historical windows, so a result never gets trusted off one lucky
(or unlucky) stretch of history - exactly the overfitting trap a single
backtest run invites, and exactly what the earlier research on comparable
projects flagged as the most common cause of backtest results not holding up
once they go live.
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


VARIANTS = {
    "default (12% stop, regime on)": {},
    "8% stop": {"crypto_stop_loss_pct": 8},
    "16% stop": {"crypto_stop_loss_pct": 16},
    "regime filter off": {"regime_filter_enabled": False},
}


def main():
    parser = argparse.ArgumentParser(description="Compare config variants across multiple historical windows.")
    parser.add_argument("--window-days", type=int, default=60, help="length of each test window")
    parser.add_argument("--windows", type=int, default=4, help="how many consecutive windows to test")
    parser.add_argument("--cash", type=float, default=499.0)
    args = parser.parse_args()

    lookback_buffer = 260  # days of history needed before the first window for the 200-day average
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.window_days * args.windows + lookback_buffer)

    symbols = list(major_coins.MAJOR_COINS)
    print(f"Fetching/caching {len(symbols)} coins, {start.date()} to {end.date()}...")
    price_data = data.fetch_daily_closes(symbols, start, end)
    got = sum(1 for rows in price_data.values() if rows)
    print(f"Got data for {got}/{len(symbols)} coins\n")

    windows = []
    for i in range(args.windows):
        w_end = end - timedelta(days=args.window_days * (args.windows - 1 - i))
        w_start = w_end - timedelta(days=args.window_days)
        windows.append((w_start.date(), w_end.date()))

    results = {name: [] for name in VARIANTS}
    for name, overrides in VARIANTS.items():
        for w_start, w_end in windows:
            bt = engine.Backtest(price_data, w_start, w_end, initial_cash=args.cash, cfg_overrides=overrides)
            equity_curve, trade_log = bt.run()
            results[name].append(report.summarize(equity_curve, trade_log, args.cash))

    header = "Variant".ljust(30) + "".join(f"W{i + 1}".rjust(9) for i in range(args.windows)) + "Avg".rjust(9)
    print(header)
    print("-" * len(header))
    for name, summaries in results.items():
        returns = [s.get("total_return_pct", 0) for s in summaries]
        avg = sum(returns) / len(returns) if returns else 0
        row = name.ljust(30) + "".join(f"{r:+.1f}%".rjust(9) for r in returns) + f"{avg:+.1f}%".rjust(9)
        print(row)

    print()
    print("Windows tested (oldest to newest):")
    for i, (w_start, w_end) in enumerate(windows):
        print(f"  W{i + 1}: {w_start} to {w_end}")

    print()
    print("A variant that only wins in one window is noise, not an edge.")
    print("Trust whatever holds up (or at least doesn't badly lose) across most windows.")


if __name__ == "__main__":
    main()
