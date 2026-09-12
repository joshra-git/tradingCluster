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
    try:
        ref_price = alpaca_client.get_latest_price(body["symbol"])
    except Exception as e:
        return jsonify({"approved": False, "reason": f"could not fetch quote: {e}"}), 502
    body["ref_price"] = ref_price

    todays_pnl = db.todays_trade_pnl(agent["id"])
    account_day_trades = db.account_day_trade_count(days=5)

    approved, reason, sized_qty = risk.validate_proposal(agent, body, cfg, todays_pnl, account_day_trades)

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
            symbol=body["symbol"], side=body["side"], qty=sized_qty, client_order_id=client_order_id
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
    cfg = tier_config.load()
    try:
        pool = db.persistent_candidates(
            min_appearances=cfg["persistence_min_appearances"], lookback_batches=cfg["persistence_lookback"]
        )
        if not pool:
            return jsonify({})  # not enough scan history yet, or nothing persistent right now
        shortlist = alpaca_client.screen_universe(pool, lookback_days=cfg["trend_window_days"], top_n=cfg["screen_top_n"])
        db.save_shortlist(shortlist)  # cache for the dashboard - no extra Alpaca calls needed to read it
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    return jsonify(shortlist)


@app.route("/decision", methods=["POST"])
def record_decision():
    """Logs EVERY decision an agent makes, including holds - so 'what is this
    agent currently thinking' is a real, persistent answer, not something that
    only ever existed in a pod's transient logs."""
    body = request.get_json(force=True)
    agent = db.get_agent(body["agent_name"])
    if agent is None:
        return jsonify({"ok": False, "reason": "unknown agent"}), 404
    db.record_decision(agent["id"], body["action"], body.get("symbol"), body.get("reasoning"))
    return jsonify({"ok": True})


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
    return jsonify({
        "status": agent["status"],
        "current_balance": float(agent["current_balance"]),
        "market_session": market_hours.get_market_session(),
    })


@app.route("/capital/inject", methods=["POST"])
def inject_capital():
    """Manual endpoint: curl this when you add real money to the account."""
    amount = float(request.get_json(force=True)["amount"])
    db.set_pool_balance(db.get_pool_balance() + amount)
    db.log_capital_event(agent_id=None, event_type="injection", amount=amount)
    return jsonify({"pool_balance": db.get_pool_balance()})


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

        try:
            price = alpaca_client.get_latest_price(symbol)
        except Exception as e:
            log.error(f"exit check: could not price {symbol}: {e}")
            continue

        change_pct = (price - entry) / entry * 100
        stop_pct = float(pos["stop_loss_pct"] or cfg["default_stop_loss_pct"])

        reason = None
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
            result = alpaca_client.submit_market_order(
                symbol=symbol, side="sell", qty=int(qty), client_order_id=client_order_id
            )
        except Exception as e:
            log.error(f"exit order failed for {symbol}: {e}")
            continue

        filled_price = result.get("filled_price")
        db.record_trade(
            agent_id=agent["id"], client_order_id=client_order_id, symbol=symbol,
            side="sell", qty=int(qty), stop_loss_pct=None,
            reasoning=f"Automatic exit by Controller - {reason}",
            status="filled" if filled_price else "accepted",
        )
        db.update_trade_status(
            client_order_id, "filled" if filled_price else "accepted",
            alpaca_order_id=result["alpaca_order_id"], filled_price=filled_price,
            filled_at=datetime.now(timezone.utc) if filled_price else None,
        )
        if filled_price:
            db.update_balance(pos["agent_name"], float(agent["current_balance"]) + int(qty) * filled_price)
            db.apply_fill_to_position(agent["id"], symbol, "sell", int(qty), filled_price)


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

    log.info(f"bootstrap: account has ${available:.2f}, seeding {n} agents at ${per_pod:.2f} each")
    for i in range(1, n + 1):
        name = f"agent-{i:03d}"
        strategy = {"symbols": "SPY,QQQ"}
        try:
            db.create_agent(name, min_capital=per_pod, max_capital=cfg["max_capital"], strategy=strategy)
            k8s_client.spawn_agent_pod(name, strategy)
            log.info(f"bootstrap: created {name} with ${per_pod:.2f}")
        except Exception as e:
            log.error(f"bootstrap: failed to create {name}, continuing with remaining agents: {e}")


def run_discovery_scan():
    """Runs on its own schedule, independent of agent count or agent cycle timing -
    so having 2 agents (or 20) doesn't multiply how often the real market gets scanned."""
    cfg = tier_config.load()
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
    db.ensure_decisions_table()
    db.ensure_shortlist_cache_table()
    bootstrap()
    scheduler = BackgroundScheduler()
    scheduler.add_job(reconcile, "interval", seconds=cfg["reconcile_interval_seconds"])
    scheduler.add_job(enforce_exits, "interval", seconds=cfg["exit_check_interval_seconds"],
                       next_run_time=datetime.now())
    scheduler.add_job(run_discovery_scan, "interval", seconds=cfg["discovery_interval_seconds"],
                       next_run_time=datetime.now())  # also fire immediately on startup, not just after the first interval
    scheduler.start()
    app.run(host="0.0.0.0", port=8080)
