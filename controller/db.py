"""
All state lives here. The Controller is the only thing that reads/writes this
database - agent pods never connect to it directly (enforced at the network
policy level, not just by convention).
"""
import os
import uuid
import psycopg2
import psycopg2.extras
from contextlib import contextmanager

DATABASE_URL = os.environ["DATABASE_URL"]  # postgres://user:pass@host:5432/dbname


@contextmanager
def get_conn():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_agent(name):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM agents WHERE name = %s", (name,))
        return cur.fetchone()


def list_active_agents():
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM agents WHERE status = 'active'")
        return cur.fetchall()


def create_agent(name, min_capital, max_capital, strategy, parent_name=None, initial_balance=None):
    balance = initial_balance if initial_balance is not None else min_capital
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """INSERT INTO agents (name, parent_name, strategy, current_balance, min_capital, max_capital)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
            (name, parent_name, psycopg2.extras.Json(strategy), balance, min_capital, max_capital),
        )
        return cur.fetchone()


def update_balance(name, new_balance):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE agents SET current_balance = %s, updated_at = now() WHERE name = %s",
            (new_balance, name),
        )


def set_status(name, status):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE agents SET status = %s, updated_at = now() WHERE name = %s", (status, name))


def heartbeat(name):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE agents SET last_heartbeat = now() WHERE name = %s", (name,))


def record_trade(agent_id, client_order_id, symbol, side, qty, stop_loss_pct, reasoning, status="proposed"):
    """Insert is idempotent on client_order_id - a retried proposal with the same id
    just returns the existing row instead of creating a duplicate."""
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """INSERT INTO trades (agent_id, client_order_id, symbol, side, qty, stop_loss_pct, reasoning, status)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (client_order_id) DO UPDATE SET client_order_id = EXCLUDED.client_order_id
               RETURNING *""",
            (agent_id, client_order_id, symbol, side, qty, stop_loss_pct, reasoning, status),
        )
        return cur.fetchone()


def update_trade_status(client_order_id, status, reject_reason=None, alpaca_order_id=None,
                         filled_price=None, filled_at=None):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """UPDATE trades SET status = %s, reject_reason = %s, alpaca_order_id = %s,
               filled_price = %s, filled_at = %s WHERE client_order_id = %s""",
            (status, reject_reason, alpaca_order_id, filled_price, filled_at, client_order_id),
        )


def agent_pending_buys(agent_id):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM trades
            WHERE agent_id = %s AND side = 'buy' AND status = 'accepted'
              AND alpaca_order_id IS NOT NULL
        """, (agent_id,))
        return cur.fetchone()[0]


def pending_orders():
    """Trades we submitted but never saw fill. These are the ones whose money has
    left the account without the ledger knowing."""
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT t.*, a.name AS agent_name FROM trades t
            JOIN agents a ON a.id = t.agent_id
            WHERE t.status = 'accepted' AND t.alpaca_order_id IS NOT NULL
            ORDER BY t.created_at
        """)
        return cur.fetchall()


def todays_trade_pnl(agent_id):
    """Realised profit/loss today. Deliberately NOT cash flow - a buy is not a
    loss, and treating it as one halts an agent the instant it invests."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT COALESCE(SUM(amount), 0) FROM capital_events
            WHERE agent_id = %s AND event_type = 'realised_pnl'
              AND created_at::date = CURRENT_DATE
        """, (agent_id,))
        return cur.fetchone()[0]


def rolling_pnl_trend(agent_id, days):
    """Sum of realized P&L over the trailing N days - a crude 'is this agent trending up' signal.
    TODO: swap for a proper moving-average / drawdown-aware version once you have real trade history."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT COALESCE(SUM(CASE WHEN side = 'sell' THEN qty * filled_price
                                         ELSE -qty * filled_price END), 0)
               FROM trades
               WHERE agent_id = %s AND status = 'filled'
                 AND filled_at >= now() - (%s || ' days')::interval""",
            (agent_id, days),
        )
        return cur.fetchone()[0]


def account_day_trade_count(days=5):
    """Rough same-day round-trip counter ACROSS ALL AGENTS, because the PDT rule applies to the
    whole Alpaca account, not per pod. This is a simplification (a true day-trade is a matched
    buy+sell of the same symbol same day) - good enough as an early-warning check, not a
    substitute for reading Alpaca's own day_trade_count field on the account."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT COUNT(*) FROM trades
               WHERE status = 'filled' AND filled_at >= now() - (%s || ' days')::interval""",
            (days,),
        )
        return cur.fetchone()[0]


def get_pool_balance():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT balance FROM unallocated_pool WHERE id = 1")
        return cur.fetchone()[0]


def set_pool_balance(balance):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE unallocated_pool SET balance = %s WHERE id = 1", (balance,))


def log_capital_event(agent_id, event_type, amount, note=None):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO capital_events (agent_id, event_type, amount, note) VALUES (%s, %s, %s, %s)",
            (agent_id, event_type, amount, note),
        )


def agent_count():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM agents")
        return cur.fetchone()[0]


def ensure_usage_table():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS api_usage (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                agent_name TEXT NOT NULL,
                input_tokens INT NOT NULL,
                output_tokens INT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        # self-migrating: safe to run against a table that already existed before caching was added
        cur.execute("ALTER TABLE api_usage ADD COLUMN IF NOT EXISTS cache_creation_tokens INT NOT NULL DEFAULT 0")
        cur.execute("ALTER TABLE api_usage ADD COLUMN IF NOT EXISTS cache_read_tokens INT NOT NULL DEFAULT 0")


def record_usage(agent_name, input_tokens, output_tokens, cache_creation_tokens=0, cache_read_tokens=0):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO api_usage (agent_name, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens)
               VALUES (%s, %s, %s, %s, %s)""",
            (agent_name, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens),
        )


def ensure_regime_table():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS market_regime (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                asset_class TEXT NOT NULL,
                score INT NOT NULL,
                regime TEXT NOT NULL,
                components JSONB,
                computed_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_regime_time ON market_regime(asset_class, computed_at DESC)")


def record_regime(asset_class, score, regime, components):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO market_regime (asset_class, score, regime, components) VALUES (%s,%s,%s,%s)",
            (asset_class, score, regime, psycopg2.extras.Json(components)),
        )


def latest_regime(asset_class):
    """Most recent score. Returns None if nothing recorded yet, and callers
    treat that as 'no opinion' rather than blocking trades - a missing regime
    reading must never silently halt the system."""
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT * FROM market_regime WHERE asset_class = %s
            ORDER BY computed_at DESC LIMIT 1
        """, (asset_class,))
        return cur.fetchone()


def ensure_api_costs_table():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS api_costs (
                day DATE PRIMARY KEY,
                usd NUMERIC(14,6) NOT NULL,
                fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fx_rates (
                pair TEXT PRIMARY KEY,
                rate NUMERIC(14,6) NOT NULL,
                live BOOLEAN NOT NULL DEFAULT false,
                fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)


def upsert_api_cost(day, usd):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO api_costs (day, usd, fetched_at) VALUES (%s, %s, now())
            ON CONFLICT (day) DO UPDATE SET usd = EXCLUDED.usd, fetched_at = now()
        """, (day, usd))


def upsert_fx_rate(pair, rate, live):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO fx_rates (pair, rate, live, fetched_at) VALUES (%s, %s, %s, now())
            ON CONFLICT (pair) DO UPDATE SET rate = EXCLUDED.rate, live = EXCLUDED.live, fetched_at = now()
        """, (pair, rate, live))


def ensure_equity_snapshots_table():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS equity_snapshots (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                agent_id UUID NOT NULL REFERENCES agents(id),
                cash NUMERIC(14,2) NOT NULL,
                invested NUMERIC(14,2) NOT NULL,
                total NUMERIC(14,2) NOT NULL,
                taken_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_equity_agent_time ON equity_snapshots(agent_id, taken_at)")


def record_equity_snapshot(agent_id, cash, invested):
    """One row per agent per reconcile pass. This is the only record of how an
    agent's value moved over time - without it, a value-over-time chart would
    have nothing real to draw."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO equity_snapshots (agent_id, cash, invested, total) VALUES (%s, %s, %s, %s)",
            (agent_id, cash, invested, cash + invested),
        )


def get_position(agent_id, symbol):
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM positions WHERE agent_id = %s AND symbol = %s", (agent_id, symbol))
        return cur.fetchone()


# Same Sonnet 5 pricing the dashboard uses for its token-based estimate
# (dashboard/dashboard.py's USD_PER_MTOK_* constants) - kept here too since
# the Controller and dashboard are separate containers with no shared module.
# Update both places if the agents' model ever changes.
_USD_PER_MTOK_INPUT = 2.00
_USD_PER_MTOK_OUTPUT = 10.00
_USD_PER_MTOK_CACHE_READ = 0.20
_USD_PER_MTOK_CACHE_WRITE = 2.50


def actual_cost_since(hours=168):
    """Real billed USD cost if the Admin API has been populating api_costs,
    else a token-based estimate at current model pricing - same fallback the
    dashboard already shows, just usable here too so the cost-vs-profit
    breaker doesn't depend on the dashboard being open. Returns (cost, is_real)."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT COALESCE(SUM(usd), 0) FROM api_costs
            WHERE day >= CURRENT_DATE - (%s || ' hours')::interval
        """, (hours,))
        real = float(cur.fetchone()[0])
        if real > 0:
            return real, True

        cur.execute("""
            SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0),
                   COALESCE(SUM(cache_read_tokens),0), COALESCE(SUM(cache_creation_tokens),0)
            FROM api_usage WHERE created_at >= now() - (%s || ' hours')::interval
        """, (hours,))
        inp, outp, cr, cc = cur.fetchone()
        est = (float(inp) / 1e6 * _USD_PER_MTOK_INPUT + float(outp) / 1e6 * _USD_PER_MTOK_OUTPUT
               + float(cr) / 1e6 * _USD_PER_MTOK_CACHE_READ + float(cc) / 1e6 * _USD_PER_MTOK_CACHE_WRITE)
        return est, False


def realised_pnl_since(hours=24):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT COALESCE(SUM(amount), 0) FROM capital_events
            WHERE event_type = 'realised_pnl' AND created_at >= now() - (%s || ' hours')::interval
        """, (hours,))
        return float(cur.fetchone()[0])


def equity_total_at_earliest():
    """Sum of each active agent's very FIRST equity snapshot - the closest
    thing to 'since this run started', since a ledger reset truncates
    equity_snapshots along with everything else. An agent spawned partway
    through counts from its own first snapshot, not the whole system's."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT COALESCE(SUM(total), 0) FROM (
                SELECT DISTINCT ON (agent_id) agent_id, total
                FROM equity_snapshots
                ORDER BY agent_id, taken_at ASC
            ) earliest
        """)
        return float(cur.fetchone()[0])


def equity_total_hours_ago(hours=24):
    """Sum of each active agent's most recent equity snapshot taken at or before
    `hours` ago - the closest thing to 'what the whole desk was worth back then'
    without a snapshot landing on an exact boundary. An agent with no snapshot
    that old yet (e.g. spawned since) is left out rather than guessed at."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT COALESCE(SUM(total), 0) FROM (
                SELECT DISTINCT ON (agent_id) agent_id, total
                FROM equity_snapshots
                WHERE taken_at <= now() - (%s || ' hours')::interval
                ORDER BY agent_id, taken_at DESC
            ) recent
        """, (hours,))
        return float(cur.fetchone()[0])


def trade_counts_since(hours=24):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT side, COUNT(*) FROM trades
            WHERE status = 'filled' AND filled_at >= now() - (%s || ' hours')::interval
            GROUP BY side
        """, (hours,))
        counts = dict(cur.fetchall())
        return counts.get("buy", 0), counts.get("sell", 0)


def prune_equity_snapshots(keep_days=60):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM equity_snapshots WHERE taken_at < now() - (%s || ' days')::interval",
                    (keep_days,))


def agent_asset_class(agent):
    """Which market this agent trades. Stored in its strategy blob at creation;
    defaults to stocks for any agent created before asset classes existed."""
    strategy = agent.get("strategy") or {}
    return strategy.get("asset_class", "stocks")


def symbols_claimed_by_others(agent_id):
    """Symbols another agent has already committed to - either holding outright,
    or with an order submitted that hasn't filled yet. Both count as 'taken',
    because an order sitting unfilled at the broker is still real exposure
    waiting to happen, not a free slot."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT symbol FROM positions
            WHERE agent_id <> %s AND qty > 0
            UNION
            SELECT DISTINCT symbol FROM trades
            WHERE agent_id <> %s AND side = 'buy' AND status = 'accepted'
              AND created_at >= now() - interval '1 day'
        """, (agent_id, agent_id))
        return [r[0] for r in cur.fetchall()]


def agent_has_position(agent_id):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM positions WHERE agent_id = %s AND qty > 0", (agent_id,))
        return cur.fetchone()[0] > 0


def calls_today():
    """Total model calls made today across ALL agents - the daily budget is an
    account-wide pool, not per-agent, since the provider rate limit applies to
    the API key rather than to any individual pod."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM api_usage WHERE created_at::date = CURRENT_DATE")
        return cur.fetchone()[0]


def usage_today():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0)
            FROM api_usage WHERE created_at::date = CURRENT_DATE
        """)
        return cur.fetchone()


def ensure_positions_table():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                agent_id UUID NOT NULL REFERENCES agents(id),
                symbol TEXT NOT NULL,
                qty NUMERIC(14,4) NOT NULL DEFAULT 0,
                avg_entry_price NUMERIC(14,4),
                stop_loss_pct NUMERIC(6,4),
                opened_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                UNIQUE (agent_id, symbol)
            )
        """)
        cur.execute("ALTER TABLE positions ADD COLUMN IF NOT EXISTS last_price NUMERIC(14,6)")
        # Highest price seen since entry. The trailing stop is measured down from
        # this, not from the entry price - that is what lets a winner keep running
        # while still protecting the gain it has already made.
        cur.execute("ALTER TABLE positions ADD COLUMN IF NOT EXISTS high_water_mark NUMERIC(14,6)")
        cur.execute("ALTER TABLE positions ADD COLUMN IF NOT EXISTS last_price_at TIMESTAMPTZ")


def apply_fill_to_position(agent_id, symbol, side, qty, price, stop_loss_pct=None):
    """Keeps a running position per (agent, symbol) so 'what do I currently hold'
    is a direct read instead of summing every historical trade each time."""
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM positions WHERE agent_id = %s AND symbol = %s FOR UPDATE", (agent_id, symbol))
        pos = cur.fetchone()

        if side == "buy":
            if pos is None:
                cur.execute(
                    """INSERT INTO positions (agent_id, symbol, qty, avg_entry_price, stop_loss_pct, opened_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, now(), now())""",
                    (agent_id, symbol, qty, price, stop_loss_pct),
                )
            else:
                existing_qty = float(pos["qty"])
                new_qty = existing_qty + qty
                if existing_qty <= 0.0001:
                    cur.execute(
                        "UPDATE positions SET qty=%s, avg_entry_price=%s, stop_loss_pct=%s, opened_at=now(), updated_at=now() WHERE id=%s",
                        (new_qty, price, stop_loss_pct, pos["id"]),
                    )
                else:
                    new_avg = (existing_qty * float(pos["avg_entry_price"]) + qty * price) / new_qty
                    cur.execute(
                        "UPDATE positions SET qty=%s, avg_entry_price=%s, updated_at=now() WHERE id=%s",
                        (new_qty, new_avg, pos["id"]),
                    )
        elif side == "sell":
            if pos is None:
                return
            # Realised profit or loss on the portion being sold. This is what a
            # daily loss limit should measure - not cash flow, which counts every
            # purchase as a loss and halts an agent the moment it buys anything.
            entry = float(pos["avg_entry_price"] or 0)
            if entry:
                realised = qty * (price - entry)
                cur.execute(
                    "INSERT INTO capital_events (agent_id, event_type, amount, note) "
                    "VALUES (%s, 'realised_pnl', %s, %s)",
                    (agent_id, realised, symbol),
                )  # selling with no tracked position - nothing to reconcile against
            new_qty = float(pos["qty"]) - qty
            if new_qty <= 0.0001:
                cur.execute("DELETE FROM positions WHERE id = %s", (pos["id"],))
            else:
                cur.execute("UPDATE positions SET qty=%s, updated_at=now() WHERE id=%s", (new_qty, pos["id"]))


def update_high_water_mark(agent_id, symbol, price):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            UPDATE positions SET high_water_mark = %s
            WHERE agent_id = %s AND symbol = %s
              AND (high_water_mark IS NULL OR high_water_mark < %s)
        """, (price, agent_id, symbol, price))


def set_position_qty(agent_id, symbol, qty):
    """Directly overwrites a position's recorded quantity - for truing the
    ledger up to what the broker actually holds, not for a normal fill (that's
    apply_fill_to_position, which adjusts by a delta and tracks cost basis).
    qty <= 0 removes the row entirely, same as a sell that closes a position."""
    with get_conn() as conn:
        cur = conn.cursor()
        if qty <= 0.0001:
            cur.execute("DELETE FROM positions WHERE agent_id = %s AND symbol = %s", (agent_id, symbol))
        else:
            cur.execute(
                "UPDATE positions SET qty = %s, updated_at = now() WHERE agent_id = %s AND symbol = %s",
                (qty, agent_id, symbol),
            )


def update_position_price(agent_id, symbol, price):
    """Piggybacks on the price the exit checker already fetches every 60s, so the
    dashboard can show live gain/loss without holding broker credentials itself."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE positions SET last_price = %s, last_price_at = now() WHERE agent_id = %s AND symbol = %s",
            (price, agent_id, symbol),
        )


def list_open_positions():
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT p.*, a.name AS agent_name FROM positions p
            JOIN agents a ON a.id = p.agent_id
            WHERE p.qty > 0
            ORDER BY p.updated_at DESC
        """)
        return cur.fetchall()


def ensure_market_scans_table():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS market_scans (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                scan_batch UUID NOT NULL,
                symbol TEXT NOT NULL,
                source TEXT NOT NULL,
                rank INT,
                pct_change NUMERIC(10,4),
                volume BIGINT,
                price NUMERIC(14,4),
                scanned_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_market_scans_symbol ON market_scans(symbol)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_market_scans_batch ON market_scans(scan_batch)")


def record_market_scan(candidates):
    """One scan_batch id groups everything found in a single discovery pass,
    so 'appeared in 2 of the last 3 scans' means 3 distinct passes, not 3 rows."""
    batch_id = str(uuid.uuid4())
    with get_conn() as conn:
        cur = conn.cursor()
        for c in candidates:
            cur.execute(
                """INSERT INTO market_scans (scan_batch, symbol, source, rank, pct_change, volume, price)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (batch_id, c["symbol"], c["source"], c.get("rank"), c.get("pct_change"),
                 c.get("volume"), c.get("price")),
            )
    return batch_id


def persistent_candidates(min_appearances=2, lookback_batches=3):
    """The noise filter: only symbols that showed up in at least min_appearances
    of the last lookback_batches distinct scans count as a real signal, not a blip."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            WITH recent_batches AS (
                SELECT scan_batch, MIN(scanned_at) AS batch_time
                FROM market_scans
                GROUP BY scan_batch
                ORDER BY batch_time DESC
                LIMIT %s
            )
            SELECT symbol, COUNT(DISTINCT scan_batch) AS appearances
            FROM market_scans
            WHERE scan_batch IN (SELECT scan_batch FROM recent_batches)
            GROUP BY symbol
            HAVING COUNT(DISTINCT scan_batch) >= %s
            ORDER BY appearances DESC
        """, (lookback_batches, min_appearances))
        return [row[0] for row in cur.fetchall()]


def ensure_decisions_table():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS decisions (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                agent_id UUID NOT NULL REFERENCES agents(id),
                action TEXT NOT NULL,
                symbol TEXT,
                reasoning TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)


def record_decision(agent_id, action, symbol, reasoning):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO decisions (agent_id, action, symbol, reasoning) VALUES (%s, %s, %s, %s)",
            (agent_id, action, symbol, reasoning),
        )


def ensure_shortlist_cache_table():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS shortlist_cache (
                symbol TEXT PRIMARY KEY,
                price NUMERIC(14,4),
                pct_change NUMERIC(10,4),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)


def save_shortlist(shortlist_dict):
    """Called whenever /screen actually computes a fresh shortlist - piggybacks on
    that existing computation so the dashboard can read it cheaply without ever
    triggering its own Alpaca calls just because someone loaded the page."""
    with get_conn() as conn:
        cur = conn.cursor()
        # Upsert rather than delete-then-insert: both agents hit /screen at the
        # same moment, and one would delete rows while the other was inserting
        # them, producing a duplicate-key error and a 502.
        cur.execute("DELETE FROM shortlist_cache WHERE symbol <> ALL(%s)",
                    (list(shortlist_dict.keys()) or [''],))
        for sym, data in shortlist_dict.items():
            pct_key = next((k for k in data if k.endswith("_change_pct")), None)
            pct = data.get(pct_key) if pct_key else None
            cur.execute(
                """INSERT INTO shortlist_cache (symbol, price, pct_change)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (symbol) DO UPDATE
                   SET price = EXCLUDED.price, pct_change = EXCLUDED.pct_change,
                       updated_at = now()""",
                (sym, data.get("price"), pct),
            )


def spawn_from_pool_transaction(child_name, amount, strategy, min_capital, max_capital):
    """A new agent funded directly by the unallocated pool - not a sibling debited
    from any existing agent's own trading balance. parent_name is NULL: this
    capital didn't come from another agent proving itself, it came from cash
    that was sitting idle. Caller still owns decrementing and persisting the
    pool's own balance via set_pool_balance() in the same reconcile pass."""
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """INSERT INTO agents (name, parent_name, strategy, current_balance, min_capital, max_capital)
               VALUES (%s, NULL, %s, %s, %s, %s) RETURNING *""",
            (child_name, psycopg2.extras.Json(strategy), amount, min_capital, max_capital),
        )
        child = cur.fetchone()
        cur.execute(
            "INSERT INTO capital_events (agent_id, event_type, amount, note) VALUES (%s, 'pool_allocation', %s, %s)",
            (child["id"], amount, "new agent funded from unallocated pool"),
        )
        return child


def spawn_sibling_transaction(parent_name, child_name, amount, strategy):
    """Atomic debit-parent / credit-child / create-child-row. If this fails partway,
    the whole transaction rolls back - you never end up with an orphaned credit."""
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM agents WHERE name = %s FOR UPDATE", (parent_name,))
        parent = cur.fetchone()
        if parent is None or parent["current_balance"] < amount:
            raise ValueError("parent agent missing or insufficient balance for spawn")

        new_parent_balance = parent["current_balance"] - amount
        cur.execute("UPDATE agents SET current_balance = %s, updated_at = now() WHERE name = %s",
                    (new_parent_balance, parent_name))

        cur.execute(
            """INSERT INTO agents (name, parent_name, strategy, current_balance, min_capital, max_capital)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
            (child_name, parent_name, psycopg2.extras.Json(strategy), amount,
             parent["min_capital"], parent["max_capital"]),
        )
        child = cur.fetchone()

        cur.execute("INSERT INTO capital_events (agent_id, event_type, amount) VALUES (%s, 'spawn_debit', %s)",
                    (parent["id"], amount))
        cur.execute("INSERT INTO capital_events (agent_id, event_type, amount) VALUES (%s, 'spawn_credit', %s)",
                    (child["id"], amount))
        return child


def ensure_telegram_state_table():
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS telegram_state (
                id INTEGER PRIMARY KEY DEFAULT 1,
                last_update_id BIGINT,
                CHECK (id = 1)
            )
        """)


def get_telegram_offset():
    """The last Telegram update_id we've already handled, persisted so an
    ordinary code redeploy doesn't reset it. Without this, every restart's
    module-level _last_update_id = None looked identical to a genuinely
    fresh bot, so get_updates() drained and silently discarded any /status
    sent in the gap around a routine redeploy - not just a real first start."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT last_update_id FROM telegram_state WHERE id = 1")
        row = cur.fetchone()
        return row[0] if row else None


def set_telegram_offset(value):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO telegram_state (id, last_update_id) VALUES (1, %s)
               ON CONFLICT (id) DO UPDATE SET last_update_id = EXCLUDED.last_update_id"""
            , (value,)
        )
