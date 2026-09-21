"""Turns a backtest's equity curve + trade log into a plain summary."""


def summarize(equity_curve, trade_log, initial_cash):
    if not equity_curve:
        return {"error": "no data - check the date range and that price data was fetched"}

    final_equity = equity_curve[-1][1]
    total_return_pct = (final_equity - initial_cash) / initial_cash * 100

    peak = initial_cash
    max_drawdown_pct = 0.0
    for _, eq in equity_curve:
        peak = max(peak, eq)
        if peak:
            max_drawdown_pct = max(max_drawdown_pct, (peak - eq) / peak * 100)

    sells = [t for t in trade_log if t["side"] == "sell"]
    wins = [t for t in sells if t["pnl"] and t["pnl"] > 0]
    losses = [t for t in sells if t["pnl"] and t["pnl"] <= 0]
    win_rate = (len(wins) / len(sells) * 100) if sells else 0.0

    return {
        "start_date": equity_curve[0][0], "end_date": equity_curve[-1][0],
        "initial_cash": initial_cash, "final_equity": round(final_equity, 2),
        "total_return_pct": round(total_return_pct, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "num_trades": len(sells), "win_rate_pct": round(win_rate, 1),
        "avg_win": round(sum(t["pnl"] for t in wins) / len(wins), 2) if wins else 0,
        "avg_loss": round(sum(t["pnl"] for t in losses) / len(losses), 2) if losses else 0,
    }


def print_report(summary):
    if "error" in summary:
        print(summary["error"])
        return
    print("=" * 60)
    print(f"Backtest: {summary['start_date']} to {summary['end_date']}")
    print("=" * 60)
    print(f"Starting cash:     ${summary['initial_cash']:,.2f}")
    print(f"Ending value:      ${summary['final_equity']:,.2f}")
    print(f"Total return:      {summary['total_return_pct']:+.2f}%")
    print(f"Worst drawdown:    -{summary['max_drawdown_pct']:.2f}%")
    print(f"Trades closed:     {summary['num_trades']}")
    print(f"Win rate:          {summary['win_rate_pct']:.1f}%")
    print(f"Avg win / loss:    ${summary['avg_win']:,.2f} / ${summary['avg_loss']:,.2f}")
    print("=" * 60)
    print("Caveats: single naive strategy (always sizes to the max allowed,")
    print("unlike Claude), no fees/slippage modeled, daily-bar stop approximation.")
    print("Read this as an upper bound on the scaffolding, not a promise.")
