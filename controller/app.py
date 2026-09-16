import logging
import uuid
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

import db
import risk
import alpaca_client
import k8s_client
import tier_config
import market_hours

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("controller")

app = Flask(__name__)


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
        delta = sized_qty * filled_price * (1 if body["side"] == "sell" else -1)
        db.update_balance(agent_name, float(agent["current_balance"]) + delta)
        db.apply_fill_to_position(agent["id"], body["symbol"], body["side"], sized_qty, filled_price,
                                   body.get("stop_loss_pct"))

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
        if asset_class == "crypto":
            shortlist = alpaca_client.screen_crypto(
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

    think, interval, why = market_hours.should_think(asset_class, session, can_act, cfg)

    return jsonify({
        "status": agent["status"],
        "current_balance": float(agent["current_balance"]),
        "holdings": holdings,
        "can_act": can_act,
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
    db.set_pool_balance(db.get_pool_balance() + amount)
    db.log_capital_event(agent_id=None, event_type="injection", amount=amount)
    return jsonify({"pool_balance": db.get_pool_balance()})


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
        delta = qty * price * (1 if t["side"] == "sell" else -1)
        db.update_balance(t["agent_name"], float(agent["current_balance"]) + delta)
        db.apply_fill_to_position(
            agent["id"], t["symbol"], t["side"], qty, price,
            float(t["stop_loss_pct"]) if t["stop_loss_pct"] else None,
        )
        log.info(f"order sync: {t['agent_name']} {t['side']} {qty} {t['symbol']} "
                 f"filled at ${price:,.4f} - balance now ${float(agent['current_balance']) + delta:,.2f}")


def enforce_exits():
    """Deterministic exit rules, run by the Controller every reconcile pass rather
    than waiting on an agent's next cycle or an LLM call succeeding. A stop-loss that
    depends on an API being available isn't really a stop-loss - this is the backstop.
    Claude can still decide to sell earlier for its own reasons; this just guarantees
    the mechanical rules always fire."""
    cfg = tier_config.load()
    take_profit_pct = cfg["take_profit_pct"]

    for pos in db.list_open_positions():
        symbol = pos["symbol"]
        qty = float(pos["qty"])
        entry = float(pos["avg_entry_price"] or 0)
        if entry <= 0 or qty <= 0:
            continue

        agent_row = db.get_agent(pos["agent_name"])
        pos_asset_class = db.agent_asset_class(agent_row) if agent_row else "stocks"
        try:
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
            db.update_balance(pos["agent_name"], float(agent["current_balance"]) + sell_qty * filled_price)
            db.apply_fill_to_position(agent["id"], symbol, "sell", sell_qty, filled_price)


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
        if balance <= floor:
            log.info(f"culling {agent['name']} at balance {balance} (floor {floor})")
            db.set_status(agent["name"], "culled")
            db.set_pool_balance(db.get_pool_balance() + balance)
            db.log_capital_event(agent["id"], "cull_return", balance)
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

    pool = db.get_pool_balance()
    if pool >= cfg["min_capital"]:
        trending = [a for a in agents
                    if a["status"] == "active"
                    and db.rolling_pnl_trend(a["id"], cfg["trend_window_days"]) > 0]
        if trending:
            share = pool / len(trending)
            for agent in trending:
                if share >= cfg["min_capital"]:
                    try:
                        db.spawn_sibling_transaction(
                            agent["name"], f"{agent['name']}-{uuid.uuid4().hex[:6]}",
                            cfg["min_capital"], agent["strategy"],
                        )
                        pool -= cfg["min_capital"]
                        k8s_client.spawn_agent_pod(f"{agent['name']}-pool", agent["strategy"])
                    except Exception as e:
                        log.error(f"pool allocation failed for {agent['name']}: {e}")
            db.set_pool_balance(pool)

    # --- sanity check: does the ledger's total match what Alpaca actually holds? ---
    try:
        ledger_total = sum(float(a["current_balance"]) for a in agents) + float(db.get_pool_balance())
        account = alpaca_client.get_account()
        drift = abs(ledger_total - account["portfolio_value"])
        if drift > 1.0:
            log.warning(f"LEDGER DRIFT: ledger={ledger_total} alpaca={account['portfolio_value']} drift={drift}")
    except Exception as e:
        log.error(f"reconciliation sanity check failed: {e}")


def bootstrap():
    """Runs once at Controller startup. If no agents exist yet, reads the REAL Alpaca
    account balance and splits it across initial_pod_count agents at min_capital each.
    Safe to run on every restart - it's a no-op once any agent exists."""
    if db.agent_count() > 0:
        log.info("agents already exist in the ledger, skipping auto-bootstrap")
        return

    cfg = tier_config.load()
    n = cfg["initial_pod_count"]
    per_pod = cfg["min_capital"]

    try:
        account = alpaca_client.get_account()
    except Exception as e:
        log.error(f"bootstrap: could not read Alpaca account balance, skipping: {e}")
        return

    available = account["cash"]
    needed = per_pod * n
    if available < needed:
        log.warning(f"bootstrap: account has ${available:.2f} but needs ${needed:.2f} "
                    f"for {n} agents at ${per_pod:.2f} each - not seeding anything")
        return

    classes = tier_config.enabled_asset_classes(cfg)
    log.info(f"bootstrap: account has ${available:.2f}, seeding {n} agents at ${per_pod:.2f} each "
             f"across {classes}")
    for i in range(1, n + 1):
        name = f"agent-{i:03d}"
        # Round-robin across whatever is enabled. One class enabled = every agent
        # gets it; both enabled = agents alternate, giving the 50/50 split.
        asset_class = classes[(i - 1) % len(classes)]
        strategy = {"asset_class": asset_class}
        try:
            db.create_agent(name, min_capital=per_pod, max_capital=cfg["max_capital"], strategy=strategy)
            k8s_client.spawn_agent_pod(name, strategy)
            log.info(f"bootstrap: created {name} trading {asset_class} with ${per_pod:.2f}")
        except Exception as e:
            log.error(f"bootstrap: failed to create {name}, continuing with remaining agents: {e}")


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


if __name__ == "__main__":
    cfg = tier_config.load()
    db.ensure_usage_table()
    db.ensure_positions_table()
    db.ensure_market_scans_table()
    db.ensure_equity_snapshots_table()
    db.ensure_decisions_table()
    db.ensure_shortlist_cache_table()
    bootstrap()
    scheduler = BackgroundScheduler()
    scheduler.add_job(sync_orders, "interval", seconds=cfg["order_sync_interval_seconds"],
                       next_run_time=datetime.now())
    scheduler.add_job(reconcile, "interval", seconds=cfg["reconcile_interval_seconds"])
    scheduler.add_job(enforce_exits, "interval", seconds=cfg["exit_check_interval_seconds"],
                       next_run_time=datetime.now())
    scheduler.add_job(run_discovery_scan, "interval", seconds=cfg["discovery_interval_seconds"],
                       next_run_time=datetime.now())  # also fire immediately on startup, not just after the first interval
    scheduler.start()
    app.run(host="0.0.0.0", port=8080)
