# Trading desk — how this actually works

This is a Claude-driven trading system that manages its own population of
"agents," each responsible for a slice of capital. It's built around one core
idea: **capital that proves itself gets more capital; capital that fails gets
reclaimed.** Everything below explains the thinking, not the infrastructure.

## The basic loop

Each agent is a single, disposable process with no memory of its own. Every
cycle, it:

1. Asks the Controller "what's my current balance and status."
2. Asks the Controller for real market data — current price plus a 5-day
   trend for each symbol on its watchlist.
3. Hands that to Claude and asks for one decision: **buy, sell, or hold** —
   with mandatory reasoning, and a required stop-loss on any buy.
4. Sends that decision back to the Controller as a *proposal*, not an order.

The agent never talks to the broker directly and never holds real state in
memory. If it crashes, nothing is lost — a fresh copy just asks the
Controller the same three questions and picks up where the ledger says it
left off.

## The Controller is the only thing that can say yes

Every proposal passes through a fixed set of deterministic checks before
anything is actually sent to Alpaca:

- **Mandatory stop-loss** on every buy — no exceptions.
- **Max position size** as a fraction of that agent's own balance, so one bad
  call can't wipe out a disproportionate share of its capital.
- **Daily loss circuit breaker** — an agent that's down more than a set % on
  the day stops trading until tomorrow.
- **Account-wide PDT awareness** — the pattern-day-trader rule applies to the
  whole Alpaca account, not per-agent, so this check looks at all agents'
  combined activity, not just one.

Claude decides *what* to do. This layer decides *whether it's allowed to*.
That split is deliberate — an LLM should never be the last line of defense
between an idea and real money.

## Capital scaling: agents reproduce on success, die on failure

This is the part that makes the system self-managing rather than something
you have to babysit:

- Every agent has a **floor** (`min_capital`) and a **ceiling**
  (`max_capital`). It starts at the floor.
- **Cross the ceiling** (balance doubles) → the Controller spawns a sibling
  agent, seeded at the floor, funded by debiting the parent. The parent keeps
  running with what's left. A strategy that's working gets to compound *and*
  multiply.
- **Drop near zero** (below a floor fraction) → the agent gets culled. Its
  remaining balance returns to an unallocated pool rather than vanishing.
- **New capital** (you injecting cash, or leftover pool money) only gets
  deployed toward agents that are actually showing a positive trend — it
  doesn't get thrown at something that's currently losing just because it
  exists.

This is the one piece of logic explicitly kept fixed by request — the risk
rules above it can change, but this scaling mechanism is the backbone.

## Trading only when it's worth paying for

Claude API calls cost real money per call, so the system doesn't think at a
constant rate around the clock:

- **During NYSE market hours** (9:30am–4pm ET, Mon–Fri): a full decision
  cycle every 5 minutes.
- **Outside those hours** (pre-market, after-hours, weekends): one check per
  hour instead — enough to notice something material without burning tokens
  overnight for no reason.

This alone cut real cycle volume from ~288/day to ~95/day per agent, which is
roughly a 3x reduction in spend without losing responsiveness when it
actually matters.

## What gets remembered

Nothing is inferred after the fact — every meaningful event is written down
as it happens:

- **Every trade proposal** — including the ones that got rejected, and why —
  with Claude's full stated reasoning attached. This is the audit trail: you
  can always answer "why did it do that" months later.
- **A running position per agent per symbol** — current quantity and average
  entry price, updated on every fill, so "what do we currently hold" is a
  direct read, not something recalculated from scratch each time.
- **Every dollar that moves** between agents (spawns, culls, manual
  injections) — so the capital's history is traceable, not just its current
  number.
- **Actual token usage per call**, used to estimate real spend. Worth being
  precise about this one: Anthropic doesn't expose a live credit balance
  through the API at all — this is an honest estimate from real usage, not
  the real number, and the dashboard says so.

## What it's watching

Currently two symbols, deliberately simple:

- **SPY** (SPDR S&P 500 ETF Trust) — the 500 largest US companies.
- **QQQ** (Invesco QQQ Trust) — the Nasdaq-100, mostly large tech/growth
  names.

Both are index funds, not single companies — betting on either is betting on
a basket, not a stock pick.

## The dashboard

A read-only view with no trading credentials of its own — it only ever reads
the same ledger the Controller writes to. Shows: live Brisbane and New York
clocks (so you can see trading windows at a glance, not just guess), current
market session, every agent's balance and tier, and a live feed of trades
with full reasoning attached.

## Two separate environments

There are two independently configured copies of this system:

- **Paper** — Alpaca's simulated account, currently sized for testing at
  larger numbers ($50k/$100k tiers) so the scaling logic is easy to observe.
- **Live** — real Alpaca account, real money, sized to the actual starting
  capital ($50/$100 tiers). These are kept deliberately separate — different
  credentials, different risk stakes — and changes to one don't
  automatically apply to the other.

## Open ideas, discussed but not yet built

A few improvements came up while researching how similar systems handle
risk, not yet implemented:

- **Correlation-aware position limits** — SPY and QQQ move together most of
  the time. Right now each agent's risk check only looks at its own
  position; nothing stops two agents from both going long the same direction
  at once, which doubles real exposure without either agent "knowing" it.
- **Position sizing tied to stop-loss distance**, instead of a flat % of
  balance — sizing down automatically when Claude sets a wide/cautious stop,
  rather than treating a 1%-stop trade and a 10%-stop trade the same way.
- **No explicit take-profit logic** — right now, when to sell is entirely up
  to Claude's judgment each cycle, with no deterministic backstop the way the
  stop-loss is one.
- **No awareness of US market holidays** — the market-hours check knows
  weekday/weekend and clock time, but would treat a holiday like Thanksgiving
  as a normal trading day.
- **Real market data is still limited to price + 5-day trend** — no volume,
  no news/sentiment, no broader technical indicators. Claude has repeatedly
  and correctly said it wants more signal than this before committing capital
  confidently.

# Technical architecture

Everything above is the thinking. This section is the actual infrastructure
it runs on, for reference.

## Cluster

A local Kubernetes cluster (`kind`), one namespace (`trading`) holding every
component below. Paper and live are two separately-run copies of the same
manifests — same code, different `ConfigMap` values, different `Secret`
credentials, distinguished by an `ALPACA_PAPER` flag on the Controller.

## Components

- **Controller** — a Deployment (1 replica, deliberately never more) running
  a Flask API plus a background scheduler. It's the only thing with Alpaca
  credentials, the only thing with write access to Postgres, and — via a
  ServiceAccount with RBAC on `apps/deployments` — the only thing that can
  create or delete agent pods.
- **Agent pods** — each a Deployment (self-healing on crash), spawned
  dynamically by the Controller through the Kubernetes API rather than from
  static manifests. Each carries only an `ANTHROPIC_API_KEY` and a watchlist;
  no Alpaca credentials, no direct database access.
- **Postgres** — a StatefulSet with a PersistentVolumeClaim, auto-initialized
  from `db/schema.sql` on first boot via a mounted ConfigMap.
- **Dashboard** — a Deployment with no credentials beyond read-only Postgres
  access. No write path to anything.

## Configuration and secrets

- `trading-tier-config` (ConfigMap) is mounted as a volume, not passed as
  environment variables, specifically so it can be edited and re-applied
  without a pod restart — the Controller re-reads it every reconcile pass
  (default every 60s).
- `trading-secrets` (Secret) holds the Alpaca key pair, the Anthropic key,
  and the Postgres password. Only the Controller gets the Alpaca and
  Anthropic keys; Postgres and the dashboard get only the DB password.

## Networking

A default-deny NetworkPolicy covers the whole namespace, with explicit
allows layered on top: Controller ↔ Postgres, dashboard ↔ Postgres (read),
agents ↔ Controller. Agent pods have no network path to Postgres or Alpaca
at all — that boundary is enforced by the network layer itself, not just
application code. One caveat worth knowing: plain Kubernetes NetworkPolicy
can't filter by domain name, only by pod/namespace/IP — so "allowed to reach
the internet" is currently all-or-nothing per pod, not scoped to specific
external hosts. True FQDN-level filtering would need a CNI like Cilium.

## Database

Six tables in Postgres: `agents` (balance, tier, status per agent),
`trades` (every proposal, approved or not, with reasoning), `positions`
(current holdings per agent/symbol), `capital_events` (every dollar moved
between agents or the pool), `unallocated_pool` (uncommitted cash), and
`api_usage` (tokens per call, for the spend estimate).

## Images

Three images (`trading-controller`, `trading-agent`, `trading-dashboard`),
built locally and loaded directly into the `kind` cluster — no external
container registry involved. Controller listens on 8080, dashboard on 8090,
both internal to the cluster and reached locally via `kubectl port-forward`.