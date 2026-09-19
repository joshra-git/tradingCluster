# CLAUDE.md — trading desk project

Context for anyone (human or model) picking this up. Read this before changing
anything: several decisions here look arbitrary but were made deliberately, and
several bugs were expensive to find.

---

## What this is

A self-managing trading system. A population of AI agents each own a slice of
capital, decide what to buy, and multiply or die based on results.

The core idea: **capital that proves itself gets more capital; capital that
fails gets reclaimed.** Everything else serves that.

The owner is in **Brisbane, Australia** and is **new to trading**. Two things
follow from that, and they shape a lot of decisions:

- Timezone matters. US market hours are the middle of his night, which is why
  crypto became attractive — it trades while he is awake to watch it.
- Explanations must be in plain English. The agent prompt explicitly bans
  trading jargon. Do not "improve" that by making it more technical.

---

## Architecture

```
Agent pods (N)          stateless; ask the Controller, call Claude, propose
      |
      v
Controller (1 replica)  the ONLY writer. Holds all credentials.
      |                 Enforces risk. Spawns/culls agents. Runs exits.
      v
Postgres                the ledger. Single source of truth.
      ^
      |
Dashboard               read-only. No broker credentials at all.
```

### Why the Controller is a single replica

It is the single writer to both Postgres and the broker. Two replicas would
reintroduce double-spend races on the same real money. **Do not scale it.**

### Why agents are stateless

An agent holds nothing in memory that matters. Every cycle it asks the
Controller "what is my balance, what do I hold, what should I look at". If the
pod dies mid-cycle, a fresh one picks up from the ledger with nothing lost.

### Why agents cannot reach Postgres or Alpaca

NetworkPolicy blocks it at the network layer, not just by convention. Agents
have only an `ANTHROPIC_API_KEY` and can reach the Controller. If an agent pod
were compromised, it could not touch money or data directly.

---

## The decision flow

1. **Discovery** (Controller, every 15 min, stocks only) — Alpaca's screener
   returns market-wide gainers, losers, most-actives. Warrants/rights tickers
   and sub-$5 prices are filtered out. Results land in `market_scans`.
2. **Persistence filter** — only symbols appearing in ≥2 of the last 3 scans
   survive. Kills single-scan noise.
3. **Shortlist** — survivors ranked by 5-day momentum, top 5 go forward.
   *Crypto skips steps 1–2 entirely*. With `crypto_universe_mode: major`
   (the default) it ranks a fixed list of twelve established coins in
   `controller/major_coins.py` — BTC, ETH, SOL, XRP and similar. This exists
   because Alpaca's movers screener returns whatever spiked hardest today,
   which is how PEPE and HYPE kept reaching the shortlist. Set it to anything
   else to fall back to the movers path.
4. **Judgement** — the agent sends the shortlist plus its own holdings to
   Claude, which returns buy/sell/hold with reasoning and a mandatory stop-loss.
5. **Risk layer** (`controller/risk.py`) — deterministic vetoes. Claude
   proposes; this decides. **Never put an LLM call in this file.**
6. **Execution** — Controller submits to Alpaca. Only the Controller ever does.

### Risk checks, in order

| Check | Rule |
| --- | --- |
| Stop-loss | Mandatory on every buy, no exceptions |
| Diversification | If another agent holds this symbol, blocked. One agent per name. |
| Position size | Max `max_position_pct` of that agent's balance |
| Whole shares | Stocks round **down** to whole shares; crypto allows fractions |
| Daily loss | Agent halts for the day past `max_daily_loss_pct` |
| PDT | Account-wide day-trade limit. **Skipped for crypto** (no PDT rule) |

### Capital scaling (the backbone — do not redesign)

- Agent starts at `min_capital`.
- Balance crosses `max_capital` → Controller spawns a sibling seeded at
  `min_capital`, debited from the parent. Parent keeps running with the rest.
- Balance drops below `min_capital * cull_floor_fraction` → agent culled,
  remaining balance returns to `unallocated_pool`.
- Injected capital only goes to agents with a positive rolling P&L trend.

`max_capital` is **per agent, not per account**. At min 250 / max 500, one
agent doubling its own money triggers a spawn.

### Trailing stops, not fixed take-profit

`trailing_stop_enabled` (default on) measures the stop down from the highest
price seen since entry (`positions.high_water_mark`), not from the entry price.
A position that keeps climbing drags its stop up behind it and is never
force-sold for "winning too much" — it exits only when the move actually turns
over by `stop_loss_pct` from its peak. This replaced a fixed `take_profit_pct`,
which capped every winner at +15% regardless of whether it had further to run.
Costs no model calls; it is pure arithmetic in `enforce_exits()`.

### Exits are deterministic, not AI

`enforce_exits()` runs every 60s in the Controller. It prices every open
position and sells on stop-loss or take-profit. This deliberately does **not**
involve Claude — a stop-loss that depends on an API call succeeding is not a
stop-loss. Claude can still choose to sell earlier for its own reasons.

---

## Market regime filter

`controller/market_regime.py` scores the broad market 0-100 every hour and
scales how much agents may deploy. Pure arithmetic on bars already fetched —
no model calls, no extra API keys, no external services.

Four components, 25 points each:
1. Anchor (BTC/USD for crypto, SPY for stocks) vs its 200-day average
2. Anchor vs its 50-day average
3. Breadth — how many coins/stocks in the universe are above their own 50-day
   average. An anchor holding up while everything else falls is a narrow,
   fragile market, and the anchor alone cannot show that.
4. 20-day direction of the anchor

The scoring of "above vs below an average" is deliberately asymmetric: below a
**rising** average scores 8 (a pullback in an uptrend), above a **falling** one
scores 12 (a bounce in a downtrend). The second is the more dangerous place to
be buying, so it is not rewarded as if it were strength.

| Score | Regime | Effect |
| --- | --- | --- |
| 70-100 | risk-on | full position sizes |
| 40-69 | neutral | half position sizes |
| 0-39 | risk-off | no new positions |

Risk-off blocks **new entries only**. Existing holdings are untouched and their
stop-losses keep running — the filter can never force a sale, and a missing or
failed regime reading yields a multiplier of 1.0 rather than silently halting
everything. Failing open is correct here: the brake exists to reduce exposure
in bad conditions, not to stop trading whenever a data fetch hiccups.

Thresholds live in the ConfigMap (`regime_risk_on_threshold`,
`regime_risk_off_threshold`, `regime_neutral_multiplier`) and the whole thing
switches off with `regime_filter_enabled: false`. No override mechanism by
design — a brake you can disable whenever it is inconvenient is not a brake.

**Not backtested.** The components and thresholds are reasoned choices, not
validated ones. A trigger firing does not make it correct.

## Cost control

Claude API calls cost money, so pacing is deliberate. The Controller decides
the interval and hands it to the agent on each heartbeat — agents do not
compute their own pacing.

**Stocks** follow the US session: 90s when open, 10 min otherwise, 1 hour on
weekends.
**Crypto** has no session, so it follows *the owner's waking hours*
(`crypto_active_start_hour`/`end_hour`, Brisbane): 2 min when he is awake,
30 min when not. This is the point of crypto for him — trading happens when he
can watch it.

Also: `holding_slowdown_factor` (3×) slows an agent that already holds
something, because exits are automatic and it is waiting on a price, not
hunting. And agents skip the Claude call entirely when the shortlist top
candidate has moved less than 0.5% since last check.

**Hunting vs holding is the core pacing split.** An agent with deployable cash
is hunting and looks every `cycle_hunting_seconds` (90s) — a good entry is
time-sensitive and missing it costs more than the call. An agent already holding
looks every `cycle_holding_seconds` (1h), because the trailing stop handles the
exit without any model involvement; the periodic check exists only so the model
can bail early for a judgement reason. Market shut = zero calls either way.

**Agents only think when a decision is possible.** `market_hours.should_think()`
returns False when the market is shut (roughly two thirds of a weekday plus all
weekend for stocks), or when the agent is fully invested. Both cases would be
paying the model to tell you something the database already knows. Combined
with hourly pacing during the open, this is ~13-65 calls/week rather than ~5,000.

**The biggest cost lever is `can_act`.** A fully invested agent has no decision
to make — exits are enforced deterministically without any model call — so the
Controller tells it to stop thinking until it has `min_cash_to_act` deployable
again. Without this, running cost exceeded the owner's entire profit target
($50-64/week of API calls chasing $25/week of profit). Do not remove it.

`daily_call_budget` caps total calls/day across all agents. It was originally
a Gemini free-tier limit; now it is a **cost circuit breaker**. Keep it — a
crash-looping agent could otherwise run up a real bill overnight.

---

## Cost tracking

`controller/anthropic_admin.py` pulls **real billed spend** from Anthropic's
Usage & Cost Admin API every 6 hours, stored in `api_costs`. This needs an
`ANTHROPIC_ADMIN_KEY` (an `sk-ant-admin...` key, different from the inference
key) and an **organisation** — the Admin API does not exist for individual
accounts. Without the key everything falls back to a token-based estimate,
clearly labelled as such on the dashboard.

Anthropic bills in USD. The AUD figure uses a USD→AUD rate fetched daily from
Frankfurter (free, keyless, ECB-backed), stored in `fx_rates`, falling back to
`usd_aud_fallback_rate` if unreachable. The dashboard marks a fallback rate so a
stale number is never mistaken for a live one.

## Database

| Table | Holds |
| --- | --- |
| `agents` | name, status, balance, min/max capital, parent, `strategy` JSONB (contains `asset_class`) |
| `trades` | every proposal incl. rejected ones, with reasoning and fill price |
| `positions` | current holdings per agent/symbol, plus `last_price` for the dashboard |
| `decisions` | every decision incl. holds — this is what the UI reads |
| `capital_events` | every dollar moved between agents/pool |
| `unallocated_pool` | single row, uncommitted cash |
| `api_usage` | tokens per call, for burn tracking |
| `equity_snapshots` | per-agent value over time, for the detail page chart |
| `market_scans` | discovery results (stocks only) |
| `api_costs` | real billed USD per day from the Admin API |
| `fx_rates` | USD→AUD rate, with a flag for whether it is live or fallback |
| `market_regime` | hourly regime score, label and component breakdown |

Tables are created/migrated at Controller startup via `ensure_*_table()`
functions using `CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`.
Add new columns that way, not with a migration tool.

---

## Hard-won gotchas

These cost hours. Do not rediscover them.

**ConfigMap volume sync lags ~60s.** Applying a ConfigMap does not immediately
change what the pod reads. Always verify with
`kubectl -n trading exec deploy/controller -- cat /etc/trading-config/config.yaml`
before concluding a change did not work. Restarting the pod *before* the sync
completes makes it read the old values.

**The dashboard port drifts.** The app's `app.run(port=...)`, the Dockerfile
`EXPOSE`, and the k8s Service must all agree (currently **8095**). This broke
repeatedly because older copies of `dashboard.py` had 8090. Symptom:
port-forward connects then errors with "connection refused" *inside* the pod.

**Alpaca paper account reset invalidates the API keys.** There is no API to
reset a paper account — dashboard only. After a reset, patch the secret with
new keys or everything 401s. `reset-cluster.sh` handles the aftermath.

**Alpaca screener is stocks-only.** No crypto movers/most-actives endpoint.
Hence the different crypto path.

**Crypto orders need `TimeInForce.GTC`.** `DAY` is invalid for a 24/7 market.

**Many stocks are not fractionable.** Position sizing must `int()` down for
stocks. Crypto is fine fractional. This caused repeated "asset X is not
fractionable" rejections.

**PDT rule.** Under $25k, more than 3 day-trades in 5 rolling days gets the
account flagged. Applies to stocks only. At this account size it is always
relevant.

**Leftover positions silently eat buying power.** `TRUNCATE`-ing our tables
does not sell anything at the broker. Wiping the ledger while positions are
open leaves the account margined with no record of why. Always
`close_all_positions()` first, or reset the paper account.

**Alpaca orders do not fill synchronously.** `submit_order()` returns
`pending_new` with no fill price; the fill lands moments later. `sync_orders()`
polls for this every 20s and is what actually moves the ledger. Before it
existed, balances never decremented (one agent bought 3x its allocation),
positions were never recorded, and **stop-losses could never fire** because
`enforce_exits()` iterates open positions and the table was always empty. Never
assume a submitted order is a completed one.

**Jinja escapes HTML entities inside `{{ }}`.** Use the literal character
(`−`) not `&minus;` in expressions.

**WSL + Windows:** never move a `.git` folder through Explorer — it injects
`:Zone.Identifier` files that corrupt refs. Clone fresh on each machine.

---

## Current state

- Trading **crypto** from a fixed list of twelve major coins. Flip both
  `stocks_enabled` and `crypto_enabled` true for a 50/50 split across agents.
- Using **Claude** (`claude-sonnet-4-6`). Gemini was tried and abandoned: its
  free tier for newer Flash models is **20 requests/day**, not 1500.
- 2 agents, ~$499 each, spawn at double. `max_position_pct: 0.5`.
- Crypto exits: fixed **+10% target, -20% stop** (`crypto_trailing_stop: false`).
  Stocks keep the trailing stop. The wide crypto stop is deliberate — the intent
  is to hold through normal swings, so expect to see red numbers without the
  system acting.
- `enforce_diversification: false` — agents may hold the same coin. With only
  twelve majors, blocking duplicates left the second agent with nothing to pick.
- Regime filter live, currently scoring ~93/100 (risk-on), so it applies no
  brake. It only bites if conditions deteriorate.
- Main dashboard is dark-themed; the agent detail page (`/agent/<name>`) uses
  the light **Modernist** design system. They do not match visually.

## Known gaps / next work

Roughly in priority order.

1. **No self-healing for missing agent pods.** If an agent Deployment is
   deleted, the ledger still lists the agent but nothing recreates the pod.
   `k8s_client.pod_exists()` exists and is unused — wire it into `reconcile()`.
   This has bitten us more than once.
2. **`min_price_floor` only applies to the movers list**, not most-actives, so
   sub-$5 stocks still reach the shortlist (FTFT at $3.29 did).
3. **CLAUDE.md and README drift.** Several features were added after the
   original write-up. Check `controller/` for modules not mentioned here
   before assuming the docs are complete.
4. **Stale log wording.** Crypto orders log "will go through when the market
   next opens" — crypto never closes. Also "quiet." reads oddly; should be
   "outside your active hours".
5. **Dashboard/detail page theme mismatch** (dark vs light).
6. **No cleanup on growing tables.** `decisions`, `market_scans`,
   `equity_snapshots` grow forever. `prune_equity_snapshots()` exists but is
   never called.
7. **Correlation is only per-symbol.** Two agents can hold INTC and AMD — both
   semiconductors — which is one bet wearing two hats.
8. **No market-holiday awareness.** Thanksgiving looks like a normal weekday.
9. **No backtester.** Everything in this system is a hypothesis. Alpaca gives
   7+ years of bars and the scanning/ranking/exit logic is all deterministic,
   so a replay would cost almost nothing in model calls and would answer in an
   afternoon what live running takes months to reveal. This is the single
   highest-value unbuilt thing.
10. **Two identical agents.** Both run the same prompt against the same
   shortlist and reliably reach the same conclusion. They are capital buckets,
   not competing strategies. Genuine diversity would mean giving them
   *different* strategies, not identical ones and hoping they diverge.
11. **Time-of-day analysis is possible but unbuilt.** All the data is being
   collected; nothing queries it yet. Crypto does not write to `market_scans`
   at all, which would need fixing first.

---

## Things that are deliberate — do not "fix"

- Controller at 1 replica.
- Risk layer contains no LLM call.
- Agents hold no durable state.
- Dashboard has no broker credentials.
- Plain-English agent prompt with banned jargon list.
- The spawn/cull capital mechanism (owner explicitly wants this kept).
- Holding is a valid, common outcome. Agents choosing not to trade is correct
  behaviour, not a bug.

---

## Operations

```bash
# deploy everything (never touches existing secrets)
./run-all.sh

# rebuild one component
docker build -t trading-controller:latest ./controller
kind load docker-image trading-controller:latest --name trading
kubectl -n trading rollout restart deployment/controller

# watch
kubectl -n trading logs -f deploy/controller
kubectl -n trading logs -f -l app=trading-agent
kubectl -n trading port-forward svc/dashboard 8091:8095   # -> localhost:8091

# inspect state
kubectl -n trading exec deploy/controller -- python3 -c "
import db
for a in db.list_active_agents(): print(a['name'], a['current_balance'])"

# what the broker actually thinks
kubectl -n trading exec deploy/controller -- python3 -c "
import alpaca_client
acct = alpaca_client.trading_client.get_account()
print('cash', acct.cash, 'buying power', acct.buying_power)
for p in alpaca_client.trading_client.get_all_positions(): print(p.symbol, p.qty, p.unrealized_pl)"

# change config (takes ~60s to reach the pod, no restart needed)
kubectl -n trading get configmap trading-tier-config -o jsonpath='{.data.config\.yaml}' > /tmp/cfg.yaml
# edit /tmp/cfg.yaml
kubectl -n trading create configmap trading-tier-config --from-file=config.yaml=/tmp/cfg.yaml --dry-run=client -o yaml | kubectl apply -f -
```

Secrets: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ANTHROPIC_API_KEY`,
`POSTGRES_PASSWORD` in secret `trading-secrets`.

---

## A note on expectations

The owner has asked about getting to "almost 0% chance of loss". That is not
achievable and chasing it makes the system worse — it pushes toward larger
positions on higher conviction, which is how accounts blow up. Professional
quant funds run 50–60% win rates and profit because winners outsize losers.

The risk layer is worth more than any improvement to the prediction side,
precisely because it assumes being wrong. If asked to loosen stop-losses or
remove position caps to chase returns, say so plainly.

This runs on a **paper account**. Before anything touches real money, the
account-vs-ledger reconciliation needs to be trustworthy — it has drifted
several times already.
