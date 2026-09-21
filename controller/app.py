import logging
from zoneinfo import ZoneInfo
import uuid
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

import db
import risk
import alpaca_client
import k8s_client
import tier_config
import market_regime
import major_coins
import market_hours
import safe_universe
import anthropic_admin
import telegram_client

# Log in Brisbane time, not UTC. Container logs default to UTC, which makes it
# genuinely hard to line up "when did it buy that" against your own day.
class _BrisbaneFormatter(logging.Formatter):
    _TZ = ZoneInfo("Australia/Brisbane")

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=self._TZ)
        return dt.strftime(datefmt or "%H:%M:%S")


_handler = logging.StreamHandler()
_handler.setFormatter(_BrisbaneFormatter("%(asctime)s AEST  %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_handler], force=True)
log = logging.getLogger("controller")

# basicConfig above applies to every logger in the process, including
# APScheduler's own - which announces every job starting and finishing at INFO
# level ("Running job...", "...executed successfully"). That's pure scheduler
# bookkeeping, not something that happened in the trading system, and it was
# drowning out the lines that actually matter. Raised to WARNING: APScheduler
# logs a genuine failure via .exception()/.error(), both above WARNING, so a
# broken job (like the reconcile() crash this caught) still surfaces - only
# the "I started, I finished" noise goes quiet.
logging.getLogger("apscheduler").setLevel(logging.WARNING)
# Same problem, different source: Flask's dev server logs every single HTTP
# request/response line by pod IP - "POST /heartbeat 200", "GET /screen 200" -
# once per agent per cycle. That's traffic, not an event; the endpoint
# handlers themselves already log the events that matter (a fill, an exit, a
# regime score). An actual server error still surfaces through Flask's own
# exception handling, which isn't gated by this logger.
logging.getLogger("werkzeug").setLevel(logging.WARNING)

app = Flask(__name__)

# Whether the cost-vs-profit circuit breaker is currently blocking new
# positions - recomputed once per reconcile() pass (not per heartbeat, which
# is far more frequent than this needs to be), read by heartbeat() to gate
# can_act. Module-level is fine: a pod restart just re-evaluates fresh on the
# next reconcile pass, a safe default rather than a risky one.
_cost_breaker = {"tripped": False}


# ---------------------------------------------------------------------------
# Agent-facing API. Agent pods ONLY ever call these two endpoints - they never
# touch Postgres or Alpaca directly.
# ---------------------------------------------------------------------------

@app.route("/propose", methods=["POST"])
def propose():
    body = request.get_json(force=True)
    agent_name = body["agent_name"]
    agent = db.get_agent(agent_name)
    if agent is None or agent["status"] != "active":
        return jsonify({"approved": False, "reason": "unknown or inactive agent"}), 404

    cfg = tier_config.load()

    # attach a reference price so the risk layer can size the position correctly
    asset_class = db.agent_asset_class(agent)
    body["asset_class"] = asset_class
    try:
        ref_price = alpaca_client.price_for(body["symbol"], asset_class)
    except Exception as e:
        return jsonify({"approved": False, "reason": f"could not fetch quote: {e}"}), 502
    body["ref_price"] = ref_price

    todays_pnl = db.todays_trade_pnl(agent["id"])
    account_day_trades = db.account_day_trade_count(days=5)

    # One order in flight per agent. Until a fill is confirmed the ledger balance
    # is stale, so approving a second buy would spend money the agent no longer
    # has - this is exactly how one agent ended up holding 3x its allocation.
    if body["side"] == "buy" and db.agent_pending_buys(agent["id"]) > 0:
        return jsonify({
            "approved": False,
            "reason": "an earlier buy from this agent hasn't been confirmed filled yet",
        }), 200

    # Large caps mostly allow fractional shares, which is what makes a $650
    # stock buyable on a $300 budget. Only ask once per symbol.
    if asset_class != "crypto":
        body["fractionable"] = alpaca_client.is_fractionable(body["symbol"])

    if cfg.get("regime_filter_enabled", True):
        row = db.latest_regime(asset_class)
        if row:
            mult = market_regime.exposure_multiplier(int(row["score"]), cfg)
            if mult <= 0:
                return jsonify({"approved": False,
                                "reason": f"market regime is {row['regime']} - no new positions"}), 200
            cfg = {**cfg, "max_position_pct": cfg["max_position_pct"] * mult}

    # Same enforcement shape as the regime check above: can_act only slows how
    # OFTEN an agent gets asked (heartbeat's should_think cadence) - it never
    # blocks a buy that reaches this endpoint anyway, e.g. during a slower
    # holding-mode check-in. Without a real reject here too, a tripped cost
    # breaker would only be a rate limiter, not the guarantee it's meant to be.
    if body["side"] == "buy" and not alpaca_client.PAPER and _cost_breaker["tripped"]:
        return jsonify({"approved": False,
                        "reason": "Claude spend has outrun trading profit this week - no new positions"}), 200

    claimed = db.symbols_claimed_by_others(agent["id"])
    approved, reason, sized_qty = risk.validate_proposal(
        agent, body, cfg, todays_pnl, account_day_trades, claimed_symbols=claimed
    )

    client_order_id = f"{agent_name}-{uuid.uuid4()}"
    trade = db.record_trade(
        agent_id=agent["id"],
        client_order_id=client_order_id,
        symbol=body["symbol"],
        side=body["side"],
        qty=sized_qty,
        stop_loss_pct=body.get("stop_loss_pct"),
        reasoning=body.get("reasoning"),
        status="accepted" if approved else "rejected",
    )

    if not approved:
        db.update_trade_status(client_order_id, "rejected", reject_reason=reason)
        return jsonify({"approved": False, "reason": reason}), 200

    try:
        result = alpaca_client.submit_market_order(
            symbol=body["symbol"], side=body["side"], qty=sized_qty,
            client_order_id=client_order_id, asset_class=asset_class
        )
    except Exception as e:
        db.update_trade_status(client_order_id, "failed", reject_reason=str(e))
        return jsonify({"approved": True, "executed": False, "error": str(e)}), 502

    filled_price = result.get("filled_price")
    db.update_trade_status(
        client_order_id, "filled" if filled_price else "accepted",
        alpaca_order_id=result["alpaca_order_id"], filled_price=filled_price,
        filled_at=datetime.now(timezone.utc) if filled_price else None,
    )

    if filled_price:
        balance_before = float(agent["current_balance"])
        delta = sized_qty * filled_price * (1 if body["side"] == "sell" else -1)
        new_balance = balance_before + delta
        entry_price = None
        if body["side"] == "sell":
            pos = db.get_position(agent["id"], body["symbol"])
            entry_price = float(pos["avg_entry_price"]) if pos and pos["avg_entry_price"] else None
        db.update_balance(agent_name, new_balance)
        db.apply_fill_to_position(agent["id"], body["symbol"], body["side"], sized_qty, filled_price,
                                   body.get("stop_loss_pct"))
        if body["side"] == "buy":
            telegram_client.notify_buy(agent_name, body["symbol"], sized_qty, filled_price,
                                        balance_before, new_balance,
                                        stop_loss_pct=body.get("stop_loss_pct"),
                                        reasoning=body.get("reasoning"))
        else:
            telegram_client.notify_sell(agent_name, body["symbol"], sized_qty, filled_price, entry_price,
                                         new_balance, is_auto_exit=False,
                                         opened_at=pos["opened_at"] if pos else None,
                                         reasoning=body.get("reasoning"))

    return jsonify({"approved": True, "executed": True, "sizing_note": reason, "order": result}), 200


@app.route("/screen", methods=["GET"])
def screen():
    """Returns the shortlist for whichever market the requesting agent trades.
    Stocks go through the stored discovery scans; crypto ranks its (much
    smaller) tradable universe directly, since Alpaca's screener is stocks-only."""
    cfg = tier_config.load()
    agent_name = request.args.get("agent_name")
    asset_class = "stocks"
    if agent_name:
        agent = db.get_agent(agent_name)
        if agent:
            asset_class = db.agent_asset_class(agent)

    try:
        if asset_class == "crypto" and cfg.get("crypto_universe_mode", "major") == "major":
            shortlist = major_coins.screen(
                lookback_days=cfg["trend_window_days"], top_n=cfg["screen_top_n"]
            )
        elif asset_class == "crypto":
            shortlist = alpaca_client.screen_crypto(
                lookback_days=cfg["trend_window_days"], top_n=cfg["screen_top_n"]
            )
        elif cfg.get("universe_mode", "safe") == "safe":
            shortlist = safe_universe.screen(
                lookback_days=cfg["trend_window_days"], top_n=cfg["screen_top_n"]
            )
        else:
            pool = db.persistent_candidates(
                min_appearances=cfg["persistence_min_appearances"],
                lookback_batches=cfg["persistence_lookback"],
            )
            if not pool:
                return jsonify({})
            shortlist = alpaca_client.screen_universe(
                pool, lookback_days=cfg["trend_window_days"], top_n=cfg["screen_top_n"]
            )
        # Drop anything another agent already holds. Without this an agent can
        # pick the same coin every cycle, get rejected by the diversification
        # rule, and burn a model call each time repeating itself.
        if agent_name and cfg.get("enforce_diversification", True):
            agent_row = db.get_agent(agent_name)
            if agent_row:
                taken = set(db.symbols_claimed_by_others(agent_row["id"]))
                shortlist = {k: v for k, v in shortlist.items() if k not in taken}

        # Same idea, a different rule: drop crypto candidates the dip-buy
        # guardrail would block right now anyway (already up too much in the
        # last 24h with no pullback yet). Without this, a fast-climbing coin
        # stays the #1 momentum pick every cycle, gets proposed, gets
        # rejected by risk.py for the exact same reason, and burns a model
        # call each time - this is what kept happening with AVAX. This is a
        # deterministic Alpaca lookup, not a Claude call - cheap insurance
        # against paying repeatedly for an answer the risk layer already knows.
        if asset_class == "crypto" and cfg.get("dip_buy_guardrails_enabled", True) and shortlist:
            max_runup = cfg.get("crypto_max_24h_runup_pct", 10)
            min_pullback = cfg.get("crypto_min_pullback_from_high_pct", 2)
            blocked = []
            for sym in list(shortlist):
                try:
                    stats = alpaca_client.get_crypto_24h_stats(sym)
                except Exception as e:
                    log.warning(f"24h stats lookup failed for {sym}, leaving it in the shortlist: {e}")
                    continue
                if stats and (stats["change_24h_pct"] > max_runup
                              or stats["pullback_from_high_pct"] < min_pullback):
                    blocked.append(sym)
            if blocked:
                shortlist = {k: v for k, v in shortlist.items() if k not in blocked}

        db.save_shortlist(shortlist)
    except Exception as e:
        log.error(f"screen failed for {asset_class}: {e}")
        return jsonify({"error": str(e)}), 502
    return jsonify(shortlist)


@app.route("/discover-debug", methods=["GET"])
def discover_debug():
    """Raw, unparsed screener responses - for confirming the actual field names
    Alpaca returns before trusting discover_candidates()'s parsing of them."""
    try:
        return jsonify({
            "movers_raw": alpaca_client.fetch_movers(top=5),
            "most_actives_raw": alpaca_client.fetch_most_actives(top=5),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/market-data", methods=["GET"])
def market_data():
    symbols = request.args.get("symbols", "").split(",")
    snapshot = {}
    for symbol in symbols:
        symbol = symbol.strip()
        if not symbol:
            continue
        try:
            snapshot[symbol] = alpaca_client.get_daily_snapshot(symbol)
        except Exception as e:
            snapshot[symbol] = {"error": str(e)}
    return jsonify(snapshot)


@app.route("/heartbeat", methods=["POST"])
def heartbeat():
    agent_name = request.get_json(force=True)["agent_name"]
    db.heartbeat(agent_name)
    agent = db.get_agent(agent_name)
    cfg = tier_config.load()
    used = db.calls_today()
    budget = cfg["daily_call_budget"]

    asset_class = db.agent_asset_class(agent)
    session = market_hours.session_for(asset_class, cfg)
    holding = db.agent_has_position(agent["id"])

    # Tell the agent what it already owns. Without this the model decides in a
    # vacuum every cycle - it cannot judge "add to this" vs "diversify into
    # something else", because it has no idea it already holds anything.
    holdings = []
    for pos in db.list_open_positions():
        if pos["agent_name"] != agent_name:
            continue
        entry = float(pos["avg_entry_price"] or 0)
        qty = float(pos["qty"])
        try:
            now_price = alpaca_client.price_for(pos["symbol"], asset_class)
        except Exception:
            now_price = entry
        holdings.append({
            "symbol": pos["symbol"],
            "qty": round(qty, 6),
            "bought_at": round(entry, 4),
            "now": round(now_price, 4),
            "value": round(qty * now_price, 2),
            "change_pct": round((now_price - entry) / entry * 100, 2) if entry else 0,
        })

    # Can this agent actually do anything with the cash it has? If it is fully
    # invested, there is no decision to make - exits are enforced deterministically
    # by the Controller with no model call, so asking Claude "still holding?" every
    # cycle is pure spend for no information. This is the single biggest lever on
    # running cost: a fully invested agent thinks rarely instead of constantly.
    spare = float(agent["current_balance"])
    deployable = spare * cfg["max_position_pct"]
    can_act = deployable >= cfg["min_cash_to_act"]

    # Regime brake. A missing or stale reading yields 1.0 - the filter can
    # only ever reduce exposure, never silently block everything on a failure.
    exposure = 1.0
    regime_label = "unknown"
    if cfg.get("regime_filter_enabled", True):
        row = db.latest_regime(asset_class)
        if row:
            exposure = market_regime.exposure_multiplier(int(row["score"]), cfg)
            regime_label = row["regime"]
    if exposure <= 0:
        can_act = False

    # Cost-vs-profit circuit breaker. Only ever blocks NEW positions, exactly
    # like the regime brake above - existing holdings and their stop-losses
    # are completely unaffected. Paper accounts never trip this; testing at a
    # net cost while tuning is explicitly fine. Live is a different question -
    # this is what actually enforces "must not cost more than it makes."
    cost_breaker = not alpaca_client.PAPER and _cost_breaker["tripped"]
    if cost_breaker:
        can_act = False

    think, interval, why = market_hours.should_think(asset_class, session, can_act, cfg)
    if exposure <= 0 and not think:
        why = f"market regime is {regime_label} - not opening new positions"
    elif cost_breaker and not think:
        why = "Claude spend has outrun trading profit this week - not opening new positions"

    return jsonify({
        "status": agent["status"],
        "current_balance": float(agent["current_balance"]),
        "holdings": holdings,
        "can_act": can_act,
        "regime": regime_label,
        "exposure_multiplier": exposure,
        "should_think": think,
        "idle_reason": why,
        "deployable": round(deployable, 2),
        "asset_class": asset_class,
        "market_session": session,
        "holding_position": holding,
        "next_cycle_seconds": interval,
        "calls_today": used,
        "budget_remaining": max(budget - used, 0),
    })


@app.route("/capital/inject", methods=["POST"])
def inject_capital():
    """Manual endpoint: curl this when you add real money to the account."""
    amount = float(request.get_json(force=True)["amount"])
    db.set_pool_balance(float(db.get_pool_balance()) + amount)
    db.log_capital_event(agent_id=None, event_type="injection", amount=amount)
    return jsonify({"pool_balance": float(db.get_pool_balance())})


@app.route("/decision", methods=["POST"])
def record_decision():
    """Logs EVERY decision including holds, so 'what is this agent thinking' is a
    persistent answer rather than something that only existed in a pod's logs."""
    body = request.get_json(force=True)
    agent = db.get_agent(body["agent_name"])
    if agent is None:
        return jsonify({"ok": False, "reason": "unknown agent"}), 404
    db.record_decision(agent["id"], body["action"], body.get("symbol"), body.get("reasoning"))
    return jsonify({"ok": True})


@app.route("/usage", methods=["POST"])
def report_usage():
    body = request.get_json(force=True)
    db.record_usage(
        body["agent_name"], body["input_tokens"], body["output_tokens"],
        cache_creation_tokens=body.get("cache_creation_tokens", 0),
        cache_read_tokens=body.get("cache_read_tokens", 0),
    )
    return jsonify({"ok": True})


@app.route("/agents", methods=["GET"])
def list_agents():
    return jsonify(db.list_active_agents())


@app.route("/positions", methods=["GET"])
def list_positions():
    return jsonify(db.list_open_positions())


# ---------------------------------------------------------------------------
# Reconcile loop - this is where scaling decisions actually happen.
# Runs on a timer, not on every request, so config changes and balance
# changes are picked up together on a predictable cadence.
# ---------------------------------------------------------------------------

def sync_orders():
    """THE most important loop in this system. submit_order() returns before the
    fill happens, so without this the ledger never learns that money left the
    account - an agent would keep 'spending' a balance it no longer has, and no
    position would ever be recorded (which also means stop-losses could never
    fire, since enforce_exits iterates open positions).

    Runs frequently, is idempotent, and only ever acts on a trade once because it
    moves the row out of 'accepted' as soon as it resolves."""
    for t in db.pending_orders():
        try:
            o = alpaca_client.get_order(t["alpaca_order_id"])
        except Exception as e:
            log.error(f"order sync: could not read {t['alpaca_order_id']}: {e}")
            continue

        status = o["status"]
        if status in ("canceled", "cancelled", "expired", "rejected", "suspended"):
            db.update_trade_status(t["client_order_id"], "failed", reject_reason=f"order {status}")
            log.info(f"order sync: {t['symbol']} {status} - no money moved")
            continue

        if status != "filled" or not o["filled_price"]:
            continue  # still working; check again next pass

        qty = o["filled_qty"] or float(t["qty"])
        price = o["filled_price"]
        agent = db.get_agent(t["agent_name"])
        if agent is None:
            continue

        db.update_trade_status(
            t["client_order_id"], "filled",
            alpaca_order_id=t["alpaca_order_id"], filled_price=price,
            filled_at=datetime.now(timezone.utc),
        )
        balance_before = float(agent["current_balance"])
        delta = qty * price * (1 if t["side"] == "sell" else -1)
        new_balance = balance_before + delta
        entry_price = None
        if t["side"] == "sell":
            pos = db.get_position(agent["id"], t["symbol"])
            entry_price = float(pos["avg_entry_price"]) if pos and pos["avg_entry_price"] else None
        db.update_balance(t["agent_name"], new_balance)
        db.apply_fill_to_position(
            agent["id"], t["symbol"], t["side"], qty, price,
            float(t["stop_loss_pct"]) if t["stop_loss_pct"] else None,
        )
        log.info(f"order sync: {t['agent_name']} {t['side']} {qty} {t['symbol']} "
                 f"filled at ${price:,.4f} - balance now ${new_balance:,.2f}")
        if t["side"] == "buy":
            telegram_client.notify_buy(t["agent_name"], t["symbol"], qty, price,
                                        balance_before, new_balance,
                                        stop_loss_pct=float(t["stop_loss_pct"]) if t["stop_loss_pct"] else None,
                                        reasoning=t.get("reasoning"))
        else:
            telegram_client.notify_sell(t["agent_name"], t["symbol"], qty, price, entry_price,
                                         new_balance, is_auto_exit=False,
                                         opened_at=pos["opened_at"] if pos else None,
                                         reasoning=t.get("reasoning"))


def _reconcile_position_qty(pos, agent_row, pos_asset_class, broker_qtys):
    """Trues the ledger's recorded qty up against what Alpaca actually holds,
    BEFORE any exit logic runs against it. Without this, a position closed
    outside the Controller (sold manually, or resolved by an order this
    ledger never learned about) leaves enforce_exits() trying to sell a
    quantity that no longer exists, forever, failing with 'insufficient
    balance' every single pass until someone notices and fixes it by hand -
    which is exactly what was happening. This only ever adjusts the ledger;
    it never places an order at the broker itself.

    Returns the (possibly corrected) qty to use this pass, or None if the
    position was fully gone and has already been closed out in the ledger -
    the caller should skip straight to the next position."""
    symbol = pos["symbol"]
    ledger_qty = float(pos["qty"])
    # Missing entirely from broker_qtys means the broker holds NONE of it -
    # a dict lookup default of 0, not "no data, skip" (which .get(symbol)
    # alone would wrongly imply, since a fully-closed position is exactly why
    # the symbol wouldn't be a key at all).
    actual_qty = broker_qtys.get(symbol, 0.0)
    if actual_qty >= ledger_qty - 0.0001:
        return ledger_qty  # broker has as much or more - nothing to true up

    if actual_qty <= 0.0001:
        # Gone entirely - closed outside the Controller. Credit the agent at
        # today's price (we have no record of the actual external fill price)
        # and clear it so enforce_exits() stops retrying something that isn't
        # there.
        try:
            price = alpaca_client.price_for(symbol, pos_asset_class)
        except Exception:
            price = float(pos["last_price"] or pos["avg_entry_price"] or 0)
        if not price or not agent_row:
            log.error(f"position reconcile: {symbol} is gone at the broker for "
                      f"{pos['agent_name']} but could not price it to credit the ledger")
            return ledger_qty
        proceeds = ledger_qty * price
        new_balance = float(agent_row["current_balance"]) + proceeds
        db.update_balance(pos["agent_name"], new_balance)
        db.set_position_qty(pos["agent_id"], symbol, 0)
        db.log_capital_event(agent_row["id"], "external_close_reconciled", proceeds,
                              note=f"{symbol} was closed outside the Controller - "
                                   f"reconciled at ~${price:,.4f}, not the actual fill price")
        log.warning(f"position reconcile: {pos['agent_name']}'s {symbol} was gone at the "
                    f"broker (closed manually?) - credited ${proceeds:,.2f} at today's price "
                    f"and cleared it from the ledger")
        telegram_client.notify_sell(
            pos["agent_name"], symbol, ledger_qty, price, float(pos["avg_entry_price"] or 0),
            new_balance, is_auto_exit=True,
            cause_reason="closed outside the Controller — the ledger just caught up automatically",
        )
        return None

    # Broker has some, just less than the ledger thinks (typically fee/
    # rounding drift compounding over several fills) - true it down so a real
    # exit can actually sell what's really there instead of being rejected.
    log.warning(f"position reconcile: {pos['agent_name']}'s {symbol} ledger qty "
                f"{ledger_qty} > broker qty {actual_qty} - truing the ledger down")
    db.set_position_qty(pos["agent_id"], symbol, actual_qty)
    return actual_qty


def enforce_exits():
    """Deterministic exit rules, run by the Controller every reconcile pass rather
    than waiting on an agent's next cycle or an LLM call succeeding. A stop-loss that
    depends on an API being available isn't really a stop-loss - this is the backstop.
    Claude can still decide to sell earlier for its own reasons; this just guarantees
    the mechanical rules always fire."""
    cfg = tier_config.load()
    take_profit_pct = cfg["take_profit_pct"]

    broker_prices = alpaca_client.broker_position_prices()
    broker_qtys = alpaca_client.broker_position_qtys()
    for pos in db.list_open_positions():
        symbol = pos["symbol"]
        entry = float(pos["avg_entry_price"] or 0)
        if entry <= 0 or float(pos["qty"]) <= 0:
            continue

        agent_row = db.get_agent(pos["agent_name"])
        pos_asset_class = db.agent_asset_class(agent_row) if agent_row else "stocks"

        qty = _reconcile_position_qty(pos, agent_row, pos_asset_class, broker_qtys)
        if qty is None:
            continue

        try:
            # Prefer the broker's own price for held positions - it is the
            # consolidated tape, not a single-venue quote.
            price = broker_prices.get(symbol)
            if price is None:
                price = alpaca_client.price_for(symbol, pos_asset_class)
        except Exception as e:
            log.error(f"exit check: could not price {symbol}: {e}")
            continue

        db.update_position_price(pos["agent_id"], symbol, price)
        change_pct = (price - entry) / entry * 100
        # Crypto moves far harder than stocks, so an 8% stop there would fire on
        # ordinary noise. Its thresholds are configured separately.
        if pos_asset_class == "crypto":
            default_stop = cfg["crypto_stop_loss_pct"]
            take_profit_pct = cfg["crypto_take_profit_pct"]
        else:
            default_stop = cfg["default_stop_loss_pct"]
        stop_pct = float(pos["stop_loss_pct"] or default_stop)

        reason = None
        if cfg["trailing_stop_enabled"]:
            # Measure the stop down from the highest price seen, not from entry.
            # A position that keeps climbing drags its own stop up behind it and is
            # never force-sold for "winning too much"; it only exits when the move
            # actually turns over by stop_pct from its peak.
            db.update_high_water_mark(agent_row["id"] if agent_row else pos["agent_id"], symbol, price)
            hwm = max(float(pos["high_water_mark"] or 0), entry, price)
            drop_from_peak = (price - hwm) / hwm * 100
            if drop_from_peak <= -stop_pct:
                gain = (hwm - entry) / entry * 100
                reason = (f"trailing stop: fell {abs(drop_from_peak):.2f}% from its peak of "
                          f"${hwm:,.4f} (which was {gain:+.1f}% above entry)")
        else:
            if change_pct <= -stop_pct:
                reason = f"stop-loss hit: {change_pct:.2f}% vs -{stop_pct:.2f}% limit"
            elif change_pct >= take_profit_pct:
                reason = f"take-profit hit: {change_pct:.2f}% vs +{take_profit_pct:.2f}% target"

        if not reason:
            continue

        log.info(f"EXIT {symbol} for {pos['agent_name']}: {reason}")
        client_order_id = f"exit-{pos['agent_name']}-{uuid.uuid4()}"
        agent = db.get_agent(pos["agent_name"])
        try:
            sell_qty = qty if pos_asset_class == "crypto" else int(qty)
            result = alpaca_client.submit_market_order(
                symbol=symbol, side="sell", qty=sell_qty,
                client_order_id=client_order_id, asset_class=pos_asset_class
            )
        except Exception as e:
            log.error(f"exit order failed for {symbol}: {e}")
            continue

        filled_price = result.get("filled_price")
        db.record_trade(
            agent_id=agent["id"], client_order_id=client_order_id, symbol=symbol,
            side="sell", qty=sell_qty, stop_loss_pct=None,
            reasoning=f"Automatic exit by Controller - {reason}",
            status="filled" if filled_price else "accepted",
        )
        db.update_trade_status(
            client_order_id, "filled" if filled_price else "accepted",
            alpaca_order_id=result["alpaca_order_id"], filled_price=filled_price,
            filled_at=datetime.now(timezone.utc) if filled_price else None,
        )
        if filled_price:
            new_balance = float(agent["current_balance"]) + sell_qty * filled_price
            db.update_balance(pos["agent_name"], new_balance)
            db.apply_fill_to_position(agent["id"], symbol, "sell", sell_qty, filled_price)
            telegram_client.notify_sell(pos["agent_name"], symbol, sell_qty, filled_price, entry,
                                         new_balance, is_auto_exit=True,
                                         opened_at=pos["opened_at"], cause_reason=reason)


def reconcile():
    cfg = tier_config.load()
    agents = db.list_active_agents()

    for agent in agents:
        balance = float(agent["current_balance"])
        max_capital = float(agent["max_capital"])
        min_capital = float(agent["min_capital"])
        floor = min_capital * cfg["cull_floor_fraction"]

        # --- scale up: spawn siblings while there's enough profit above the ceiling ---
        while balance >= max_capital:
            child_name = f"{agent['name']}-{uuid.uuid4().hex[:6]}"
            try:
                child = db.spawn_sibling_transaction(agent["name"], child_name, min_capital, agent["strategy"])
                k8s_client.spawn_agent_pod(child_name, agent["strategy"])
                log.info(f"spawned {child_name} from {agent['name']}")
                balance -= min_capital
            except Exception as e:
                log.error(f"spawn failed for {agent['name']}: {e}")
                break

        # --- scale down: cull agents that have dropped near zero ---
        # Cull on TOTAL value, not cash. An agent that is fully invested has
        # almost no cash by definition - judging it on cash alone kills healthy
        # agents for the crime of having bought something.
        held_value = sum(
            float(p["qty"]) * float(p["last_price"] or p["avg_entry_price"] or 0)
            for p in db.list_open_positions() if p["agent_name"] == agent["name"]
        )
        total_value = balance + held_value
        if held_value > 0:
            log.debug(f"{agent['name']}: cash {balance:.2f} + positions {held_value:.2f} = {total_value:.2f}")

        if total_value <= floor:
            log.info(f"culling {agent['name']} at total value {total_value:.2f} "
                     f"(cash {balance:.2f} + positions {held_value:.2f}, floor {floor})")
            db.set_status(agent["name"], "culled")
            db.set_pool_balance(float(db.get_pool_balance()) + max(balance, 0))
            db.log_capital_event(agent["id"], "cull_return", max(balance, 0))
            k8s_client.delete_agent_pod(agent["name"])

    # --- deploy pool capital toward agents that are actually trending up ---
    # Record each agent's value so the detail page has real history to draw.
    try:
        positions = db.list_open_positions()
        for agent in agents:
            invested = sum(
                float(p["qty"]) * float(p["last_price"] or p["avg_entry_price"] or 0)
                for p in positions if p["agent_name"] == agent["name"]
            )
            db.record_equity_snapshot(agent["id"], float(agent["current_balance"]), invested)
    except Exception as e:
        log.error(f"equity snapshot failed: {e}")

    # Idle pool cash never just sits there below a lump-sum threshold. As the
    # total (agents + pool) clears another whole min_capital share, a new
    # agent spawns funded directly FROM THE POOL - never by debiting an
    # existing agent's own trading balance, which is what the old
    # spawn_sibling_transaction call here was actually doing despite
    # decrementing the pool number alongside it. Whatever's left over (not
    # enough for a whole new agent) tops up agents that are actually trending
    # up rather than waiting around - new capital still never goes to
    # something currently losing just because it exists.
    pool = float(db.get_pool_balance())
    active_agents = [a for a in agents if a["status"] == "active"]

    if pool > 0 and active_agents:
        total_capital = sum(float(a["current_balance"]) for a in active_agents) + pool
        target_count = max(len(active_agents), int(total_capital // cfg["min_capital"]))

        while len(active_agents) < target_count and pool >= cfg["min_capital"]:
            child_name = f"agent-{uuid.uuid4().hex[:6]}"
            classes = tier_config.enabled_asset_classes(cfg)
            strategy = {"asset_class": classes[len(active_agents) % len(classes)]}
            try:
                db.spawn_from_pool_transaction(child_name, cfg["min_capital"], strategy,
                                                cfg["min_capital"], cfg["max_capital"])
                k8s_client.spawn_agent_pod(child_name, strategy)
                pool -= cfg["min_capital"]
                log.info(f"pool spawn: created {child_name} with ${cfg['min_capital']:.2f} "
                         f"(pool now ${pool:.2f})")
                active_agents = db.list_active_agents()
            except Exception as e:
                log.error(f"pool spawn failed: {e}")
                break

        # >= 0, not > 0: a brand-new agent with no trade history yet also reads
        # as 0 here, and there's no evidence it's failing - only an agent with
        # an actual recorded loss gets excluded from a top-up. The stricter
        # "> 0, genuinely proving itself" bar still applies to spawning a new
        # sibling elsewhere; this is just about not letting cash rot.
        not_losing = [a for a in active_agents
                      if db.rolling_pnl_trend(a["id"], cfg["trend_window_days"]) >= 0]
        if pool > 0 and not_losing:
            share = pool / len(not_losing)
            for agent in not_losing:
                db.update_balance(agent["name"], float(agent["current_balance"]) + share)
                db.log_capital_event(agent["id"], "pool_topup", share,
                                      note="idle pool cash distributed rather than left unused")
            log.info(f"pool topup: distributed ${pool:.2f} across {len(not_losing)} agent(s) not currently losing")
            pool = 0

        db.set_pool_balance(pool)
        agents = db.list_active_agents()  # refresh so the drift check below sees this pass's changes

    # --- cost-vs-profit circuit breaker (live accounts only) ---
    # Testing on paper is explicitly fine to run at a net cost while tuning
    # the balance. Once real money is involved, spend must never be allowed
    # to run ahead of what the system is actually making - so this compares
    # trailing 7-day REALIZED profit only (not unrealized gains sitting in
    # open positions, which could still evaporate) against trailing 7-day
    # actual Claude cost. Only ever blocks new positions; never forces a
    # sale, exactly like the regime brake.
    try:
        if not alpaca_client.PAPER:
            window_hours = 168
            # Need at least one closed trade before profit means anything -
            # otherwise day one of going live has $0 realized profit against
            # any nonzero cost, tripping the breaker before the first trade
            # ever gets a chance to close and permanently blocking it.
            _, sells = db.trade_counts_since(window_hours)
            cost, cost_is_real = db.actual_cost_since(window_hours)
            profit = db.realised_pnl_since(window_hours)
            newly_tripped = sells > 0 and cost > profit
            if newly_tripped != _cost_breaker["tripped"]:
                _cost_breaker["tripped"] = newly_tripped
                label = "estimated" if not cost_is_real else "billed"
                if newly_tripped:
                    log.warning(f"COST BREAKER TRIPPED: 7-day {label} spend ${cost:.2f} > "
                                f"7-day realised profit ${profit:.2f} - blocking new positions")
                    telegram_client.notify_cost_breaker(True, cost, profit, cost_is_real)
                else:
                    log.info(f"cost breaker cleared: 7-day {label} spend ${cost:.2f} <= "
                             f"7-day realised profit ${profit:.2f}")
                    telegram_client.notify_cost_breaker(False, cost, profit, cost_is_real)
    except Exception as e:
        log.error(f"cost breaker check failed: {e}")

    # --- sanity check: does the ledger's total match what Alpaca actually holds? ---
    try:
        held_total = sum(
            float(p["qty"]) * float(p["last_price"] or p["avg_entry_price"] or 0)
            for p in positions
        )
        ledger_total = (sum(float(a["current_balance"]) for a in agents)
                         + float(db.get_pool_balance()) + held_total)
        account = alpaca_client.get_account()
        drift = abs(ledger_total - account["portfolio_value"])
        if drift > 1.0:
            log.warning(f"LEDGER DRIFT: ledger={ledger_total} alpaca={account['portfolio_value']} drift={drift}")
    except Exception as e:
        log.error(f"reconciliation sanity check failed: {e}")


def bootstrap():
    """Runs once at Controller startup. If no agents exist yet, reads the REAL Alpaca
    account balance and splits ALL of it evenly across initial_pod_count agents -
    not min_capital each with the remainder left as unaccounted broker cash the
    ledger has no row for. min_capital itself stays at the configured tier floor
    (it's what sets each agent's cull threshold), separate from how much cash it
    actually starts with. Safe to run on every restart - it's a no-op once any
    agent exists."""
    if db.agent_count() > 0:
        log.info("agents already exist in the ledger, skipping auto-bootstrap")
        return

    cfg = tier_config.load()
    n = cfg["initial_pod_count"]
    floor = cfg["min_capital"]

    try:
        account = alpaca_client.get_account()
    except Exception as e:
        log.error(f"bootstrap: could not read Alpaca account balance, skipping: {e}")
        return

    available = account["cash"]
    needed = floor * n
    if available < needed:
        log.warning(f"bootstrap: account has ${available:.2f} but needs ${needed:.2f} "
                    f"for {n} agents at ${floor:.2f} each - not seeding anything")
        return

    per_pod = available / n
    classes = tier_config.enabled_asset_classes(cfg)
    log.info(f"bootstrap: account has ${available:.2f}, seeding {n} agents at ${per_pod:.2f} each "
             f"(floor ${floor:.2f}) across {classes}")
    for i in range(1, n + 1):
        name = f"agent-{i:03d}"
        # Round-robin across whatever is enabled. One class enabled = every agent
        # gets it; both enabled = agents alternate, giving the 50/50 split.
        asset_class = classes[(i - 1) % len(classes)]
        strategy = {"asset_class": asset_class}
        try:
            db.create_agent(name, min_capital=floor, max_capital=cfg["max_capital"],
                             strategy=strategy, initial_balance=per_pod)
            k8s_client.spawn_agent_pod(name, strategy)
            log.info(f"bootstrap: created {name} trading {asset_class} with ${per_pod:.2f}")
        except Exception as e:
            log.error(f"bootstrap: failed to create {name}, continuing with remaining agents: {e}")


def sync_api_costs():
    """Pull REAL spend from Anthropic rather than estimating it from tokens, and
    refresh the USD->AUD rate. Both degrade gracefully: no admin key means the
    dashboard keeps showing its token-based estimate, clearly labelled as one."""
    cfg = tier_config.load()

    rate, live = anthropic_admin.fetch_usd_to_aud(fallback=cfg["usd_aud_fallback_rate"])
    try:
        db.upsert_fx_rate("USDAUD", rate, live)
    except Exception as e:
        log.error(f"could not store FX rate: {e}")

    if not anthropic_admin.is_enabled():
        log.info("no ANTHROPIC_ADMIN_KEY set - spend will stay an estimate from token counts")
        return

    rows = anthropic_admin.fetch_cost_report(days=cfg["cost_report_days"])
    for r in rows:
        try:
            db.upsert_api_cost(r["date"], r["usd"])
        except Exception as e:
            log.error(f"could not store cost for {r['date']}: {e}")
    if rows:
        total = sum(r["usd"] for r in rows)
        log.info(f"cost report: {len(rows)} days, ${total:.4f} USD total "
                 f"(${total * rate:.2f} AUD at {rate:.4f})")


def refresh_regime():
    """Scores the broad market so agents can size down (or stop) when the tide
    turns. Pure arithmetic on bars we already fetch - no model calls, no extra
    API keys. Failure here is non-fatal: no reading means no opinion, and the
    system trades as it would have anyway."""
    cfg = tier_config.load()
    for ac in tier_config.enabled_asset_classes(cfg):
        try:
            r = market_regime.compute(ac)
            db.record_regime(ac, r["score"], r["regime"], r["components"])
            log.info(f"regime [{ac}]: {r['score']}/100 = {r['regime']} "
                     f"(exposure {market_regime.exposure_multiplier(r['score'], cfg):.0%})")
        except Exception as e:
            log.error(f"regime scoring failed for {ac}: {e}")


def run_discovery_scan():
    """Runs on its own schedule, independent of agent count or agent cycle timing -
    so having 2 agents (or 20) doesn't multiply how often the real market gets scanned."""
    cfg = tier_config.load()
    if not cfg["stocks_enabled"]:
        return  # crypto ranks its own universe directly; no scan history needed
    try:
        candidates = alpaca_client.discover_candidates(min_price=cfg["min_price_floor"])
        batch_id = db.record_market_scan(candidates)
        log.info(f"discovery scan {batch_id}: {len(candidates)} candidates recorded")
    except Exception as e:
        log.error(f"discovery scan failed: {e}")


def _status_snapshot():
    """Gathers the data for the scheduled daily summary - framed around the
    last 24 hours, since that's what a daily digest is for. See
    _live_status_snapshot() for the differently-framed on-demand /status
    reply. Pure read - never touches the ledger."""
    agents = db.list_active_agents()
    positions = db.list_open_positions()

    held_value = sum(
        float(p["qty"]) * float(p["last_price"] or p["avg_entry_price"] or 0)
        for p in positions
    )
    total_now = (sum(float(a["current_balance"]) for a in agents)
                 + float(db.get_pool_balance()) + held_value)
    total_24h_ago = db.equity_total_hours_ago(24)
    buys, sells = db.trade_counts_since(24)
    realised_pnl_24h = db.realised_pnl_since(24)

    asset_classes = {db.agent_asset_class(a) for a in agents}
    regime_lines = []
    for ac in sorted(asset_classes):
        row = db.latest_regime(ac)
        if row:
            regime_lines.append(telegram_client.regime_line(ac.capitalize(), row["score"], row["regime"]))

    agents_data = []
    for a in agents:
        agent_positions = []
        for p in positions:
            if p["agent_name"] != a["name"]:
                continue
            entry = float(p["avg_entry_price"] or 0)
            last = float(p["last_price"] or entry)
            pnl_pct = ((last - entry) / entry * 100) if entry else 0.0
            agent_positions.append({"symbol": p["symbol"], "pnl_pct": pnl_pct})
        agents_data.append({
            "name": a["name"],
            "balance": float(a["current_balance"]),
            "positions": agent_positions,
        })

    return total_now, total_24h_ago, buys, sells, realised_pnl_24h, regime_lines, agents_data


def daily_summary():
    """Fires once a day via a cron job, timed to the owner's own active-hours
    config rather than a fixed clock time."""
    try:
        telegram_client.notify_daily_summary(*_status_snapshot())
    except Exception as e:
        log.error(f"daily summary failed: {e}")


def _live_status_snapshot():
    """Data for an on-demand /status reply - framed around performance since
    this run started and what's held right now, not a rolling 24h window."""
    agents = db.list_active_agents()
    positions = db.list_open_positions()

    held_value = sum(
        float(p["qty"]) * float(p["last_price"] or p["avg_entry_price"] or 0)
        for p in positions
    )
    pool_balance = float(db.get_pool_balance())
    total_now = sum(float(a["current_balance"]) for a in agents) + pool_balance + held_value
    total_at_start = db.equity_total_at_earliest()

    asset_classes = {db.agent_asset_class(a) for a in agents}
    regime_lines = []
    for ac in sorted(asset_classes):
        row = db.latest_regime(ac)
        if row:
            regime_lines.append(telegram_client.regime_line(ac.capitalize(), row["score"], row["regime"]))

    agents_data = []
    for a in agents:
        agent_positions = []
        invested = 0.0
        for p in positions:
            if p["agent_name"] != a["name"]:
                continue
            entry = float(p["avg_entry_price"] or 0)
            last = float(p["last_price"] or entry)
            qty = float(p["qty"])
            pnl_pct = ((last - entry) / entry * 100) if entry else 0.0
            pnl_dollar = (last - entry) * qty if entry else 0.0
            agent_positions.append({"symbol": p["symbol"], "pnl_pct": pnl_pct, "pnl_dollar": pnl_dollar})
            invested += qty * last
        agents_data.append({
            "name": a["name"],
            "cash": float(a["current_balance"]),
            "invested": invested,
            "positions": agent_positions,
        })

    return total_now, total_at_start, pool_balance, regime_lines, agents_data


def poll_telegram_commands():
    """Checks for a /status message and replies with a live snapshot - a
    check-in you can trigger any time, not a copy of the scheduled daily
    digest. Only ever replies to the one configured chat_id; a message from
    anyone else is silently ignored."""
    try:
        for chat_id, text in telegram_client.get_updates():
            if chat_id != telegram_client.CHAT_ID:
                continue
            command = text.strip().split()[0].split("@")[0].lower() if text.strip() else ""
            if command == "/status":
                telegram_client.notify_status(*_live_status_snapshot())
    except Exception as e:
        log.error(f"telegram command poll failed: {e}")


if __name__ == "__main__":
    cfg = tier_config.load()
    db.ensure_usage_table()
    db.ensure_positions_table()
    db.ensure_market_scans_table()
    db.ensure_equity_snapshots_table()
    db.ensure_api_costs_table()
    db.ensure_regime_table()
    db.ensure_decisions_table()
    db.ensure_shortlist_cache_table()
    bootstrap()
    # Without this, APScheduler's own internal clock defaults to UTC - the log
    # LINE prefix would say AEST (via _BrisbaneFormatter above) while anything
    # APScheduler prints itself, like "next run at: ... UTC" inside a job
    # message, stays in UTC. Same mismatch fixed at its actual source instead
    # of two clocks disagreeing in the same line.
    scheduler = BackgroundScheduler(timezone="Australia/Brisbane")
    # The scheduler now has an explicit timezone (AEST), so a NAIVE datetime
    # passed as next_run_time gets localized as if it were already AEST rather
    # than treated as UTC - and the container's system clock is UTC, so
    # datetime.now() here would silently be read as 10 hours in the past,
    # every restart. Must be timezone-aware to mean what it says: right now.
    right_now = datetime.now(ZoneInfo("Australia/Brisbane"))
    scheduler.add_job(sync_orders, "interval", seconds=cfg["order_sync_interval_seconds"],
                       next_run_time=right_now)
    scheduler.add_job(reconcile, "interval", seconds=cfg["reconcile_interval_seconds"])
    scheduler.add_job(enforce_exits, "interval", seconds=cfg["exit_check_interval_seconds"],
                       next_run_time=right_now)
    scheduler.add_job(refresh_regime, "interval", seconds=cfg["regime_refresh_seconds"],
                       next_run_time=right_now)
    scheduler.add_job(sync_api_costs, "interval", seconds=cfg["cost_sync_interval_seconds"],
                       next_run_time=right_now)
    scheduler.add_job(run_discovery_scan, "interval", seconds=cfg["discovery_interval_seconds"],
                       next_run_time=right_now)  # also fire immediately on startup, not just after the first interval
    # Once a day, right as the owner's active hours end - reuses the same
    # timezone/hour config crypto pacing already uses, so it lands as he's
    # wrapping up rather than at an arbitrary clock time.
    scheduler.add_job(daily_summary, "cron", hour=cfg["crypto_active_end_hour"], minute=0,
                       timezone=cfg["crypto_timezone"])
    # Long-polls Telegram for a /status message every ~15s (each call itself
    # waits up to 10s for one to arrive, so this isn't hammering the API).
    scheduler.add_job(poll_telegram_commands, "interval", seconds=15, next_run_time=right_now)
    scheduler.start()
    app.run(host="0.0.0.0", port=8080)
