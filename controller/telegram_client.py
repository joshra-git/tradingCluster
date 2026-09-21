"""
Push notifications via Telegram - plain-English updates on what the desk is
doing, so the owner doesn't have to keep checking the Alpaca dashboard or
decode trading jargon to know what happened.

Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID. If either is missing, every
function here is a silent no-op - notifications are a convenience layer bolted
onto the fill-handling code, and a bad token must never be able to break a
trade or crash the Controller.
"""
import html
import logging
import os
from datetime import datetime, timezone

import requests

log = logging.getLogger("telegram_client")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
IS_PAPER = os.environ.get("ALPACA_PAPER", "true").lower() == "true"

_last_update_id = None


def get_updates(timeout=10):
    """Long-polls Telegram for messages sent TO the bot since the last call,
    returning [(chat_id, text), ...] with chat_id as a string so it compares
    cleanly against CHAT_ID (Telegram's API returns it as a JSON int, CHAT_ID
    is a string env var - a silent type mismatch here would just make every
    command look like it's from the wrong chat and get ignored).

    On the first call after the Controller starts, drains and discards
    whatever's already pending instead of replying to messages sent before
    this pod existed."""
    global _last_update_id
    if not BOT_TOKEN or not CHAT_ID:
        return []

    params = {"timeout": timeout}
    if _last_update_id is not None:
        params["offset"] = _last_update_id + 1

    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
            params=params, timeout=timeout + 10,
        )
        resp.raise_for_status()
        results = resp.json().get("result", [])
    except Exception as e:
        log.warning(f"telegram getUpdates failed: {e}")
        return []

    if not results:
        return []

    first_call = _last_update_id is None
    _last_update_id = max(u["update_id"] for u in results)
    if first_call:
        return []

    messages = []
    for u in results:
        msg = u.get("message") or {}
        text = msg.get("text")
        chat = msg.get("chat") or {}
        if text and "id" in chat:
            messages.append((str(chat["id"]), text))
    return messages


def _send(text):
    if not BOT_TOKEN or not CHAT_ID:
        return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=10,
        )
        if resp.status_code != 200:
            log.warning(f"telegram send failed: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        log.warning(f"telegram send failed: {e}")


def _coin_name(symbol):
    """BTC/USD -> BTC. The /USD half is just noise to a beginner."""
    return html.escape(symbol.split("/")[0])


def _esc(text):
    """Everything that isn't a tag WE added - agent names, reasoning, symbols -
    gets escaped. Telegram rejects the whole message on malformed HTML, and
    Claude's own reasoning text is free-form enough that it could contain a
    stray < or & that would otherwise break the tags around it."""
    return html.escape(str(text))


def _trim(text, limit=400):
    if not text:
        return None
    text = text if len(text) <= limit else text[:limit].rstrip() + "..."
    return _esc(text)


def _held_duration(opened_at):
    if not opened_at:
        return None
    seconds = (datetime.now(timezone.utc) - opened_at).total_seconds()
    if seconds < 3600:
        mins = max(1, int(seconds // 60))
        return f"{mins} minute{'s' if mins != 1 else ''}"
    hours = seconds / 3600
    if hours < 48:
        h = max(1, int(hours))
        return f"{h} hour{'s' if h != 1 else ''}"
    days = int(hours // 24)
    return f"{days} day{'s' if days != 1 else ''}"


def _auto_exit_cause(reason):
    """Translates enforce_exits()'s technical reason string into one plain
    sentence. Matched by prefix against the actual rule, not re-derived, so
    it can't say something different from what really happened."""
    if reason.startswith("trailing stop"):
        return ("It had climbed, then turned and started dropping back — the "
                 "automatic safety net locked in the gain before it fell further.")
    if reason.startswith("stop-loss hit"):
        return ("It dropped too far below what we paid, so the automatic safety "
                 "net sold it to limit the loss.")
    if reason.startswith("take-profit hit"):
        return ("It hit the profit target we'd set, so the automatic safety net "
                 "sold it to lock that in.")
    return _esc(reason)


def notify_buy(agent_name, symbol, qty, price, balance_before, balance_after,
                stop_loss_pct=None, reasoning=None):
    coin = _coin_name(symbol)
    agent_name = _esc(agent_name)
    cost = balance_before - balance_after
    pct = (cost / balance_before * 100) if balance_before else 0

    lines = [
        f"\U0001F7E2 <b>Bought some {coin}</b>",
        "",
        f"\U0001F4B5 Spent: <b>${cost:,.2f}</b> ({pct:.0f}% of {agent_name}'s money)",
        f"\U0001F4C8 Price: ${price:,.4f} each, for {qty:g} {coin}",
        f"\U0001F4B0 Cash {agent_name} has left: ${balance_after:,.2f}",
    ]

    if stop_loss_pct:
        stop_price = price * (1 - stop_loss_pct / 100)
        lines += ["", f"\U0001F6E1 Safety net: if it drops to ${stop_price:,.4f} "
                       f"(−{stop_loss_pct:.0f}%), it'll sell automatically to limit the loss."]

    reasoning = _trim(reasoning)
    if reasoning:
        lines += ["", f"\U0001F4AC <i>Why: {reasoning}</i>"]

    _send("\n".join(lines))


def notify_sell(agent_name, symbol, qty, price, entry_price, balance_after,
                 is_auto_exit, opened_at=None, cause_reason=None, reasoning=None):
    coin = _coin_name(symbol)
    agent_name = _esc(agent_name)
    proceeds = qty * price

    if entry_price:
        pnl = (price - entry_price) * qty
        pnl_pct = (price - entry_price) / entry_price * 100
        won = pnl >= 0
        emoji = "\U0001F4B0" if won else "\U0001F53B"
        verb = "made" if won else "lost"
        headline = f"{emoji} <b>Sold {coin} — {verb} ${abs(pnl):,.2f} ({pnl_pct:+.1f}%)</b>"
    else:
        headline = f"\U0001F53B <b>Sold {coin}</b>"

    lines = [headline, "", f"Got: ${proceeds:,.2f} for {qty:g} {coin}"]

    held = _held_duration(opened_at)
    if held:
        lines.append(f"⏱ Held for: {held}")

    lines.append(f"\U0001F4B0 Cash {agent_name} has now: ${balance_after:,.2f}")

    if is_auto_exit and cause_reason:
        lines += ["", f"\U0001F6E1 <i>Why: {_auto_exit_cause(cause_reason)}</i>",
                  "<i>(this happens automatically — nobody had to decide anything)</i>"]
    else:
        reasoning = _trim(reasoning)
        if reasoning:
            lines += ["", f"\U0001F4AC <i>Why: {reasoning}</i>"]

    _send("\n".join(lines))


_REGIME_NOTE = {
    "risk-on": "trading normally",
    "neutral": "being extra careful, using smaller amounts per trade",
    "risk-off": "not buying anything new right now, just watching what's already held",
}


def regime_line(asset_class_label, score, regime):
    note = _REGIME_NOTE.get(regime, "")
    return f"\U0001F321 {asset_class_label} mood: <b>{regime}</b> ({score}/100) — {note}"


def notify_status(total_now, total_at_start, pool_balance, regime_lines, agents_data):
    """Reply to an on-demand /status message - deliberately NOT the same shape
    as the daily summary. That one is framed around '24 hours' because it's a
    daily digest; this one is a live 'how's it doing right now' check, so it's
    framed around performance since this run started and what's actually held
    this second, not a rolling day window.

    agents_data: list of {name, cash, invested, positions: [{symbol, pnl_pct}]}."""
    lines = ["\U0001F4CA <b>Status right now</b>", ""]

    if total_at_start:
        change = total_now - total_at_start
        pct = change / total_at_start * 100
        arrow = "\U0001F4C8" if change >= 0 else "\U0001F4C9"
        verb = "Up" if change >= 0 else "Down"
        lines.append(f"{arrow} <b>{verb} ${abs(change):,.2f} ({pct:+.1f}%)</b> since this run started")
    else:
        lines.append("(not enough history yet to show overall performance)")
    lines.append(f"Total value right now: <b>${total_now:,.2f}</b>")

    lines.append("")
    lines.append("<b>Your bots:</b>")
    for a in agents_data:
        total = a["cash"] + a["invested"]
        lines.append(f"• <b>{_esc(a['name'])}</b>: ${a['cash']:,.2f} cash "
                      f"+ ${a['invested']:,.2f} in coins = ${total:,.2f}")

    lines.append("")
    lines.append("<b>Coins owned:</b>")
    table_lines = []
    for a in agents_data:
        if not a["positions"]:
            continue
        if table_lines:
            table_lines.append("")
        table_lines.append(_esc(a["name"]))
        table_lines.append(f"{'Coin':<6}{'%':>8}{'$':>10}")
        for p in a["positions"]:
            coin = _coin_name(p["symbol"])
            table_lines.append(f"{coin:<6}{p['pnl_pct']:>+7.1f}%{p['pnl_dollar']:>+10.2f}")
    if table_lines:
        # <pre> is Telegram's only way to render fixed-width columns that
        # actually line up - plain text collapses the padding spaces.
        lines.append("<pre>" + "\n".join(table_lines) + "</pre>")
    else:
        lines.append("Nothing right now - it's all sitting as cash.")

    if regime_lines:
        lines.append("")
        lines.extend(regime_lines)

    if pool_balance and pool_balance > 0.01:
        lines.append("")
        lines.append(f"\U0001F4B5 Cash not yet assigned to a bot: ${pool_balance:,.2f}")

    lines.append("")
    lines.append("Paper (practice) account — not real money yet." if IS_PAPER
                 else "⚠️ LIVE account — this is real money.")
    _send("\n".join(lines))


def notify_daily_summary(total_now, total_24h_ago, buys, sells, realised_pnl_24h,
                          regime_lines, agents_data):
    """agents_data: list of {name, balance, positions: [{symbol, pnl_pct}]}."""
    lines = [
        "\U0001F4CA <b>Your 24-hour update</b>",
        "",
        f"Everything you have (cash + coins held): <b>${total_now:,.2f}</b>",
    ]
    if total_24h_ago:
        change = total_now - total_24h_ago
        pct = change / total_24h_ago * 100
        arrow = "\U0001F4C8" if change >= 0 else "\U0001F4C9"
        word = "Up" if change >= 0 else "Down"
        lines.append(f"{arrow} <b>{word} ${abs(change):,.2f} ({pct:+.1f}%)</b> over the last 24 hours")
    else:
        lines.append("(not enough history yet to show a 24-hour change)")

    lines.append("")
    if sells:
        verb = "made" if realised_pnl_24h >= 0 else "lost"
        lines.append(f"Sold something {sells} time(s) in the last 24h — {verb} "
                      f"${abs(realised_pnl_24h):,.2f} on those trades")
    else:
        lines.append("Nothing was sold in the last 24 hours")
    lines.append(f"Bought something new {buys} time(s)")

    if regime_lines:
        lines.append("")
        lines.extend(regime_lines)

    lines.append("")
    lines.append("<b>Your bots right now:</b>")
    for a in agents_data:
        lines.append(f"• <b>{_esc(a['name'])}</b>: ${a['balance']:,.2f} cash")
        if a["positions"]:
            for p in a["positions"]:
                arrow = "\U0001F4C8" if p["pnl_pct"] >= 0 else "\U0001F4C9"
                lines.append(f"   {arrow} {_coin_name(p['symbol'])}: {p['pnl_pct']:+.1f}%")
        else:
            lines.append("   (holding nothing right now)")

    lines.append("")
    lines.append("This is still a paper (practice) account — no real money yet." if IS_PAPER
                 else "⚠️ This is a LIVE account — this is real money.")
    _send("\n".join(lines))


def notify_cost_breaker(tripped, cost_7d, profit_7d, cost_is_real):
    label = "real billed" if cost_is_real else "estimated"
    if tripped:
        lines = [
            "\U0001F6D1 <b>Pausing new trades — Claude is costing more than it's making</b>",
            "",
            f"Last 7 days: spent ${cost_7d:,.2f} ({label}), made ${profit_7d:,.2f} in closed trades.",
            "",
            "Nothing is being sold and nothing already held is affected — "
            "this only stops new positions until the numbers turn around.",
            "This checks again automatically; no action needed unless you want to look into why.",
        ]
    else:
        lines = [
            "✅ <b>Back to normal — trading resumed</b>",
            "",
            f"Last 7 days: spent ${cost_7d:,.2f} ({label}), made ${profit_7d:,.2f} in closed trades.",
            "New positions are allowed again.",
        ]
    _send("\n".join(lines))
