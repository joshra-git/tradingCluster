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


def todays_trade_pnl(agent_id):
    """Rough realized P&L for today, used by the daily-loss circuit breaker."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT COALESCE(SUM(CASE WHEN side = 'sell' THEN qty * filled_price
                                         ELSE -qty * filled_price END), 0)
               FROM trades
               WHERE agent_id = %s AND status = 'filled' AND filled_at::date = CURRENT_DATE""",
            (agent_id,),
        )
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
                return  # selling with no tracked position - nothing to reconcile against
            new_qty = float(pos["qty"]) - qty
            if new_qty <= 0.0001:
                cur.execute("DELETE FROM positions WHERE id = %s", (pos["id"],))
            else:
                cur.execute("UPDATE positions SET qty=%s, updated_at=now() WHERE id=%s", (new_qty, pos["id"]))


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
        cur.execute("DELETE FROM shortlist_cache")
        for sym, data in shortlist_dict.items():
            pct_key = next((k for k in data if k.endswith("_change_pct")), None)
            pct = data.get(pct_key) if pct_key else None
            cur.execute(
                "INSERT INTO shortlist_cache (symbol, price, pct_change) VALUES (%s, %s, %s)",
                (sym, data.get("price"), pct),
            )


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
