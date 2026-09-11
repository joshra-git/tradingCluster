-- Ledger schema. This is the single source of truth for capital and state.
-- Pods never hold authoritative state in memory - they read/write here via the Controller.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS agents (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT UNIQUE NOT NULL,          -- also used as the k8s pod name
    parent_name     TEXT REFERENCES agents(name),  -- NULL for root agents
    strategy        JSONB NOT NULL DEFAULT '{}',   -- strategy params passed to the agent pod
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','culled','graduated')),
    current_balance NUMERIC(14,2) NOT NULL,
    min_capital     NUMERIC(14,2) NOT NULL,        -- tier floor this agent was spawned at
    max_capital     NUMERIC(14,2) NOT NULL,        -- tier ceiling that triggers spawning a sibling
    last_heartbeat  TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trades (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id         UUID NOT NULL REFERENCES agents(id),
    client_order_id  TEXT UNIQUE NOT NULL,   -- idempotency key: agent can safely retry with the same id
    symbol           TEXT NOT NULL,
    side             TEXT NOT NULL CHECK (side IN ('buy','sell')),
    qty              NUMERIC(14,4) NOT NULL,
    stop_loss_pct    NUMERIC(6,4),
    reasoning        TEXT,                    -- Claude's stated reasoning, for the audit trail
    status           TEXT NOT NULL DEFAULT 'proposed'
                        CHECK (status IN ('proposed','rejected','accepted','filled','failed')),
    reject_reason    TEXT,
    alpaca_order_id  TEXT,
    filled_price     NUMERIC(14,4),
    filled_at        TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS capital_events (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id    UUID REFERENCES agents(id),   -- NULL when it's a pool-level event
    event_type  TEXT NOT NULL,  -- injection | spawn_debit | spawn_credit | cull_return | pool_allocation
    amount      NUMERIC(14,2) NOT NULL,
    note        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Single-row table holding cash that hasn't been allocated to an agent yet
-- (e.g. money you inject manually, or profit remainders that don't divide evenly into min_capital)
CREATE TABLE IF NOT EXISTS unallocated_pool (
    id      INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    balance NUMERIC(14,2) NOT NULL DEFAULT 0
);

INSERT INTO unallocated_pool (id, balance) VALUES (1, 0) ON CONFLICT (id) DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_trades_agent_id ON trades(agent_id);
CREATE INDEX IF NOT EXISTS idx_trades_created_at ON trades(created_at);
CREATE INDEX IF NOT EXISTS idx_agents_status ON agents(status);
