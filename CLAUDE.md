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
   (the default) it ranks a fixed list of established coins in
   `controller/major_coins.py` (currently 30 — BTC, ETH, SOL, XRP and
   similar) — this exists because Alpaca's movers screener returns whatever
   spiked hardest today, which is how PEPE and HYPE kept reaching the
   shortlist. Set it to anything else to fall back to the movers path.
   **Not all 30 are actually tradable on Alpaca** — the backtester surfaced
   that 10 of them (MATIC, ATOM, ALGO, NEAR, OP, INJ, SUI, APT, SAND, MANA)
   return no historical data at all, i.e. Alpaca doesn't list them. Live
   trading is unaffected (`tradable_universe()` already intersects against
   `list_crypto_symbols()`), but it's dead weight on the list.
   Before Claude ever sees the shortlist, two more filters run in `/screen`
   itself: symbols another agent already holds are dropped (diversification,
   see below), and — for crypto — anything the dip-buy guardrail would reject
   anyway (already up too much in 24h) is dropped too. Both exist because a
   candidate that's guaranteed to be rejected downstream used to keep
   reaching Claude and burning a paid call for a rejection that was already
   knowable for free; see "Cost control" below.
4. **Judgement** — the agent sends the shortlist plus its own holdings to
   Claude, which returns buy/sell/hold with reasoning and a mandatory stop-loss.
5. **Risk layer** (`controller/risk.py`) — deterministic vetoes. Claude
   proposes; this decides. **Never put an LLM call in this file.**
6. **Execution** — Controller submits to Alpaca. Only the Controller ever does.

### Risk checks, in order

| Check | Rule |
| --- | --- |
| Stop-loss | Mandatory on every buy, no exceptions |
| Diversification | If another agent holds this symbol, blocked. One agent per name. Currently **on** (`enforce_diversification` defaults to `true` — not set in the live ConfigMap at all, so it falls through to that default). |
| Dip-buy guardrail (crypto) | Won't buy something already up more than `crypto_max_24h_runup_pct` (10%) in 24h without at least `crypto_min_pullback_from_high_pct` pullback from that high. Enforced here as the real backstop; also pre-filtered out of the shortlist in `/screen` so it doesn't cost a call to find out. |
| Position size | Max `max_position_pct` of that agent's balance, scaled down further by the market regime multiplier |
| Whole shares | Stocks round **down** to whole shares; crypto allows fractions |
| Daily loss | Agent halts for the day past `max_daily_loss_pct` |
| PDT | Account-wide day-trade limit. **Skipped for crypto** (no PDT rule) |
| Cost-vs-profit (live only) | See "Cost control" below — a live account whose trailing 7-day Claude spend exceeds its trailing 7-day realised profit gets new positions blocked outright until that turns around. Inert on paper by design. |

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

Crypto uses this same trailing mechanism now, just with a wider stop distance
(`crypto_stop_loss_pct`, currently 12%) than stocks — it isn't on a separate
fixed +target/-stop band any more. `crypto_take_profit_pct` still exists in
config but is currently dead: it's only read in the `else` branch that runs
when `trailing_stop_enabled` is off, which it isn't.

### Profit-lock ratchet (crypto only)

A flat 12% trail is fine while a position is only marginally up — that's
normal noise, not worth selling into. But on a small account, giving back 12%
of an already-real dollar gain feels much bigger than "12%" sounds: a
position that peaked at +$8.64 unrealised can drop 10% off that peak (still
inside the 12% band) and be sitting at a loss, having never triggered
anything. That's not a bug, it's just a percentage-based stop meeting a
dollar-denominated intuition.

Once a crypto position's **peak** unrealised profit (`(hwm - entry) * qty`)
has ever crossed `crypto_profit_lock_trigger_usd` (default $5), `enforce_exits()`
switches that position from `crypto_stop_loss_pct` to the much tighter
`crypto_profit_lock_stop_pct` (default 4%) for the rest of its life — it
doesn't flip back even if price dips and current profit falls back under $5,
since the trigger is on the peak, not the live value. This is deliberately a
*ratchet*, not a flat take-profit: a position that's only up $1-2 still gets
the wide 12% band (don't sell into ordinary noise before there's a real gain
to protect), while a position that's earned real money gets that money
protected hard rather than given back to the same band that tolerated it on
the way up. Exit reason string is prefixed `"profit lock:"` instead of
`"trailing stop:"` so Telegram's `_auto_exit_cause()` can give it its own
plain-English explanation.

Chosen over two alternatives: a flat tighter trail (e.g. 12% → 5% everywhere)
was rejected because the backtester already showed a comparable tightening
losing in 3 of 4 historical windows — tighter isn't automatically better, it
chokes off bigger runners too. A hard $5 take-profit (sell immediately at
$5, full stop) was rejected because it doesn't scale with position size — a
bigger position hitting $5 might be giving up a much bigger move for a small,
arbitrary number.

### Position quantity is reconciled against the broker before every exit check

`enforce_exits()` compares each ledger position's qty against what Alpaca
actually holds (`alpaca_client.broker_position_qtys()`) before running any
stop-loss logic, via `_reconcile_position_qty()`. Two cases:
- **Gone entirely** (closed outside the Controller — the owner sold manually
  from the Alpaca dashboard) → credited to the agent at today's price (not
  the actual external fill price, which isn't recorded) and cleared from the
  ledger, with a Telegram notification explaining what happened.
- **Partially short** (small fee/rounding drift compounding over several
  fills) → the ledger qty is trued down to what's actually there.

Without this, a manually-sold position leaves `enforce_exits()` trying to
sell a quantity that doesn't exist, forever, failing with "insufficient
balance" every single pass until someone notices and fixes it by hand — which
is exactly what was happening before this existed. This only ever adjusts the
ledger; it never places an order at the broker itself.

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
**Crypto** has no session and, as of this pass, **no owner-waking-hours gating
either** — it thinks on the same cadence around the clock
(`cycle_crypto_active_seconds`, 2 min, whether hunting). The original design
paced crypto to the owner's waking hours specifically so trading happened
while he could watch it; that reasoning got weaker once Telegram notifications
(see below) made "watching it happen" available regardless of when a trade
actually fires, and the owner explicitly chose 24/7-same-cadence over keeping
the quiet-hours gate. `crypto_timezone` and `crypto_active_end_hour` still
exist in config, but now only to time the once-daily Telegram summary — not
to decide whether to think. This raised real per-day Claude spend
meaningfully; see the cost-vs-profit breaker below, which now guards the
other side of that trade-off.

**Hunting vs holding is the core pacing split.** An agent with deployable cash
is hunting and looks every `cycle_hunting_seconds` (stocks 90s) /
`cycle_crypto_active_seconds` (crypto 2 min) — a good entry is time-sensitive
and missing it costs more than the call. An agent already holding looks every
`cycle_holding_seconds` (1h), because the trailing stop handles the exit
without any model involvement; the periodic check exists only so the model
can bail early for a judgement reason. Market shut (stocks only now) = zero
calls.

**Before a hunting cycle even calls Claude, `agent.py`'s `_worth_asking()`
decides in plain Python whether it's worth paying for.** It asks again only
if: a held position just crossed a profit/loss threshold worth a judgement
call, the shortlist's fingerprint has genuinely moved (price buckets shifted
by more than `SKIP_THRESHOLD_PCT`, 2%), or the **previous cycle actually
executed a trade**. That last condition used to read "the previous cycle
*proposed* a trade" — which meant a rejected proposal looked identical to an
executed one and forced a guaranteed re-ask next cycle. If Claude kept
proposing the same candidate (which it did — a fast-climbing coin stays the
#1 momentum pick), that's an unbounded loop: propose, get rejected by a
deterministic rule that isn't going to change in 2 minutes, forced re-ask,
repeat. One incident burned roughly 150 of a day's ~230 calls on the exact
same rejected proposal for one coin, for nearly 4 hours straight, before being
caught. Fixed by only counting an **executed** trade as "something changed" —
a rejected or failed proposal now falls back to normal fingerprint-based
gating like anything else. If `_worth_asking()` looks like it isn't
suppressing much, check for this pattern again before assuming the pre-filter
itself is broken.

**Agents only think when a decision is possible.** `market_hours.should_think()`
returns False when the stock market is shut, or when the agent is fully
invested (any asset class). Both cases would be paying the model to tell you
something the database already knows.

**The biggest cost lever is `can_act`.** A fully invested agent has no decision
to make — exits are enforced deterministically without any model call — so the
Controller tells it to stop thinking until it has `min_cash_to_act` deployable
again. Do not remove it. `can_act` is also now false when the market regime is
risk-off, or (live only) when the cost-vs-profit breaker below is tripped.

`daily_call_budget` caps total calls/day across all agents. It was originally
a Gemini free-tier limit; now it is a **cost circuit breaker**. Keep it — a
crash-looping agent could otherwise run up a real bill overnight.

### Cost-vs-profit circuit breaker (live accounts only)

Nothing tied actual Claude spend to actual trading performance until this
existed — `daily_call_budget` is a call-count ceiling, not a dollar-vs-profit
comparison, and `weekly_cost_target_aud` was declared in config but never
wired to anything. `reconcile()` now computes, once per 60s pass:
trailing-7-day **realised** profit only (`db.realised_pnl_since()` — not
unrealised gains on open positions, which could still evaporate) against
trailing-7-day actual cost (`db.actual_cost_since()` — real billed USD from
`api_costs` when available, else the same token estimate the dashboard uses).

- **Paper never trips this.** Testing at a net cost while tuning the balance
  is explicitly fine — the owner said so directly. Gated on
  `not alpaca_client.PAPER`.
- **Needs at least one closed trade in the window before it evaluates at all.**
  Without this, day one of going live has $0 realised profit against any
  nonzero cost, which would trip the breaker before the very first live trade
  ever got a chance to close — permanently deadlocking the system before it
  could trade at all.
- When tripped, **only new positions are blocked** — same shape as the regime
  brake. Nothing is sold, nothing already held is touched, stop-losses keep
  running. Enforced in **two** places, matching the regime brake's own
  pattern: `heartbeat()`'s `can_act` (slows how often the agent is asked) AND
  a hard reject inside `/propose` itself (`_cost_breaker["tripped"]`) —
  `can_act` alone only changes cadence, it does not stop a buy that reaches
  `/propose` anyway during a slower holding-mode check-in. Skipping the
  second one would make this a rate limiter, not the guarantee it's for.
- Telegram alert on the transition in either direction (tripped / cleared),
  not spammed every pass — state tracked in the module-level `_cost_breaker`
  dict, recomputed fresh on every Controller restart (a safe default, not a
  risky one, since a restart mid-trip just re-evaluates from real data on the
  next reconcile pass rather than trusting stale in-memory state).

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

## Telegram notifications

`controller/telegram_client.py` — plain-English push notifications, added so
the owner doesn't have to keep checking the Alpaca dashboard. Needs
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (both optional secret keys); if
either is missing, every function in the module is a silent no-op — a bad
token must never be able to break a trade or crash the Controller.

- **Fill notifications** — wired into all three places money actually moves
  (`/propose`, `sync_orders()`, `enforce_exits()`), including the automatic
  position-reconciliation close described above. Buys show cost, % of
  balance, and the stop-loss price in plain terms. Sells show $/% gain-or-loss
  and, for automatic exits, a plain-English cause translated from the
  technical reason string (categorical match on the reason prefix, not a
  re-derivation of the numbers — so the message can't say something different
  from what actually happened).
- **Daily summary** — a cron job (`daily_summary()`) timed to
  `crypto_active_end_hour`/`crypto_timezone`, framed around the last 24 hours:
  total value, 24h change, trade count, realised P&L on closed trades, market
  regime, per-agent live position breakdown as an aligned `<pre>` table
  (Coin / % / $ columns — Telegram's HTML has no real table, `<pre>` monospace
  is the only way fixed-width columns actually line up).
- **`/status` on demand** — the Controller long-polls `getUpdates` every ~25s
  (each call itself long-polls Telegram for up to 8s, so this isn't
  hammering the API) and replies only to the one configured `chat_id` — a
  message from anyone else is silently ignored, so even a leaked bot username
  can't be used to query trading status. Deliberately a **different** frame
  than the daily summary, not a copy of it: performance **since this run
  started** (`db.equity_total_at_earliest()`, the earliest equity snapshot per
  agent — a ledger reset zeroes this cleanly since it truncates
  `equity_snapshots` too) rather than a rolling 24h window. Also shows, per
  held position, the actual price that would trigger an automatic sell right
  now (`app.compute_stop()`, the same function `enforce_exits()` itself calls
  to act — never a separately re-derived number that could drift from what
  will really happen) and whether the crypto profit-lock ratchet has engaged
  on it.
  On a genuinely first-ever startup (no persisted offset in `telegram_state`),
  any backlog of messages sent before the bot existed is drained and
  discarded, not replied to. On every subsequent restart, the polling offset
  resumes from `db.get_telegram_offset()`/`set_telegram_offset()` instead of
  resetting - see the gotcha below, this used to silently eat a `/status`
  sent around any ordinary redeploy, not just a real first start.
- All outbound text goes through `html.escape()` before insertion — Claude's
  own reasoning is free-form enough that a stray `<` or `&` would otherwise
  break the HTML formatting Telegram expects, or get the whole message
  silently rejected.
- Every message notes paper vs live (`IS_PAPER`, mirrors `alpaca_client.PAPER`)
  so a notification can never misleadingly claim "not real money" once live.

## Database

| Table | Holds |
| --- | --- |
| `agents` | name, status, balance, min/max capital, parent, `strategy` JSONB (contains `asset_class`) |
| `trades` | every proposal incl. rejected ones, with reasoning and fill price |
| `positions` | current holdings per agent/symbol, plus `last_price`/`high_water_mark` for the dashboard and trailing stop |
| `decisions` | every decision incl. holds — this is what the UI reads |
| `capital_events` | every dollar moved between agents/pool, incl. `pool_topup`, `external_close_reconciled`, `reconciliation_credit` event types added for the fixes below |
| `unallocated_pool` | single row, uncommitted cash |
| `api_usage` | tokens per call, for burn tracking |
| `equity_snapshots` | per-agent value over time, for the detail page chart and for `/status`'s "since this run started" baseline |
| `market_scans` | discovery results (stocks only) |
| `shortlist_cache` | latest computed shortlist, so the dashboard can read it without triggering its own broker calls |
| `api_costs` | real billed USD per day from the Admin API |
| `fx_rates` | USD→AUD rate, with a flag for whether it is live or fallback |
| `market_regime` | hourly regime score, label and component breakdown |

Tables are created/migrated at Controller startup via `ensure_*_table()`
functions using `CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`.
Add new columns that way, not with a migration tool.

---

## Hard-won gotchas

These cost hours. Do not rediscover them.

**Every code redeploy looked exactly like the bot's first-ever start to the
Telegram poller, silently eating a `/status` sent around it.**
`telegram_client._last_update_id` lived only in memory, reset to `None` on
every pod restart - indistinguishable from a genuinely new bot. `get_updates()`
deliberately drains and discards any backlog on that first call so it never
replies to messages sent before the pod existed, which is correct for a real
first start but wrong for an ordinary redeploy: during active development
(several restarts an hour) it meant `/status` looked "stuck" any time it was
sent in the narrow window around a restart - not broken, just eaten by design
for the wrong reason. Fixed by persisting the offset to Postgres
(`telegram_state`, `db.get_telegram_offset()`/`set_telegram_offset()`) and
loading it at startup before the poller's first call - only a table with no
saved row (an actual first-ever start) still drains silently now.

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

**A Postgres `NUMERIC` comes back as `Decimal`, and `Decimal + float` raises
`TypeError`.** `db.get_pool_balance()` (and similarly-typed getters) return a
raw `Decimal` unless the caller casts it. APScheduler runs each scheduled job
in its own try/except, so a crash in one job (`reconcile()`, say) doesn't
crash the pod or even stop the *other* jobs — it just logs "raised an
exception" and silently skips that job's work forever, every single pass. A
missing `float()` cast broke `reconcile()` (cull checks, pool distribution,
equity snapshots, the drift monitor — everything in that function) for **7+
hours overnight** before being noticed, precisely because nothing else looked
broken. If something ledger-adjacent seems to have "just stopped happening,"
check for a raised-and-silently-caught scheduler exception before assuming
it's a logic bug.

**Giving `BackgroundScheduler` an explicit timezone changes how naive
datetimes are interpreted.** `next_run_time=datetime.now()` (used on several
jobs to fire immediately at startup) returns a *naive* value — and the
container's system clock is UTC, so that naive value silently *is* UTC, just
unlabelled. Once the scheduler has an explicit `timezone="Australia/Brisbane"`
(added so log lines stop mixing AEST prefixes with UTC-embedded APScheduler
text), it reads a naive datetime as if it were *already* AEST — silently
shifting every startup job 10 hours into the past on every single restart,
which showed up as a "Run time of job ... was missed by 4:00:00" warning.
Fix: pass a timezone-aware datetime (`datetime.now(ZoneInfo("Australia/Brisbane"))`),
never a naive one, anywhere the scheduler itself has an explicit timezone.

**A dict keyed by the broker's raw symbol format never matches a lookup by
ledger format.** Alpaca crypto positions come back as `"ADAUSD"` (no
separator); every ledger table uses `"ADA/USD"`. `broker_position_prices()`
was written specifically to fix stops firing at the wrong level from a
single-exchange IEX quote, but it keyed its returned dict by the broker's raw
symbol while every caller looked it up by ledger symbol — a guaranteed miss,
silently falling back to the exact IEX price it was written to avoid. It had
never actually been doing anything since the day it was added. Any function
that both reads broker data by symbol *and* gets looked up by ledger-format
symbol needs the same `_ledger_symbol()` normalization
(`alpaca_client._ledger_symbol()`) — don't assume Alpaca's wire format matches
what's stored in Postgres just because a symbol string "looks" the same at a
glance.

**A position closed outside the Controller (sold manually from the Alpaca
dashboard) desyncs the ledger silently, and `enforce_exits()` will retry it
forever.** Not a hypothetical — happened twice in one session (AVAX, then
GRT). The ledger has no way to learn a position is gone except by checking;
`enforce_exits()` now does this every pass via `_reconcile_position_qty()`
(see "Position quantity is reconciled..." above) before running any stop
logic. If "insufficient balance" errors repeat every single pass for the same
symbol, this is almost certainly why — check `alpaca_client.trading_client.get_all_positions()`
against the ledger's `positions` table directly rather than assuming it's a
sizing bug.

---

## Current state

- Trading **crypto only** (`stocks_enabled: false`) from a list of 30 coins in
  `controller/major_coins.py` (10 of which aren't actually tradable on Alpaca
  — see the decision-flow note above). Flip both `stocks_enabled` and
  `crypto_enabled` true for a 50/50 split across agents.
- Using **Claude Sonnet 5** (`claude-sonnet-5`) — migrated off Sonnet 4.6
  mid-project (newer generation, cheaper: $2/$10 per Mtok vs $3/$15). Gemini
  was tried before either and abandoned: its free tier for newer Flash models
  is **20 requests/day**, not 1500.
- Agent reasoning is capped at 600 characters via the tool schema's
  `maxLength` (advisory, not strictly enforced — Claude has gone slightly
  over) plus a hard instruction in the playbook. This replaced multi-paragraph
  per-candidate essays that were driving output tokens (and cost) far higher
  than the "3-4 short sentences" the prompt always asked for.
- `min_capital: 250`, `max_capital: 500`, 2 agents at bootstrap. Bootstrap now
  divides *all* available broker cash evenly across the initial agents
  (`available / n`) rather than seeding each at a fixed `min_capital` and
  leaving the remainder with no ledger row at all — that remainder was real
  broker cash the ledger simply didn't know existed.
- Ongoing idle pool cash (from culls, injections, or reconciliation credits)
  no longer waits to accumulate a full `min_capital` before a lump-sum spawn.
  `reconcile()` now targets `floor(total_capital / min_capital)` agents,
  spawning a new one (funded directly from the pool via
  `db.spawn_from_pool_transaction()` — never by debiting an existing agent's
  own trading balance, which is what the old code was actually doing despite
  decrementing the pool number alongside it) once the total clears another
  whole share, and tops up agents that aren't currently losing with whatever's
  left over rather than letting it sit unused.
- Crypto exits use the **same trailing-stop mechanism as stocks** now, just
  with a wider stop distance (`crypto_stop_loss_pct: 12%`) — not a separate
  fixed +target/-stop band. `crypto_take_profit_pct` (20%) still exists in
  config but is currently dead code (only read when `trailing_stop_enabled`
  is off, which it isn't).
- `enforce_diversification` defaults to **true** (it isn't actually set in
  the live ConfigMap, so it falls through to that default) — agents may
  *not* currently hold the same coin. Earlier notes here said `false`; check
  the live ConfigMap before trusting either claim, this has flipped before.
- Regime filter live, currently scoring ~93/100 (risk-on), so it applies no
  brake. It only bites if conditions deteriorate.
- Crypto no longer has owner-waking-hours quiet cadence — same pacing 24/7.
  See "Cost control" above for why, and for the cost-vs-profit breaker that
  now guards the other side of that trade-off on a live account.
- Telegram notifications live (buy/sell fills, daily summary, on-demand
  `/status`) — see "Telegram notifications" above.
- A standalone backtester exists (`backtester/`) — see below, no longer a gap.
- Main dashboard is dark-themed; the agent detail page (`/agent/<name>`) uses
  the light **Modernist** design system. They do not match visually.

## Backtester

`backtester/` — a standalone, offline replay of the deterministic scaffolding
(momentum ranking, market regime, position sizing, exits) against real
historical Alpaca bars. Deliberately outside `controller/`: no Kubernetes, no
Postgres, no broker credentials, no Claude calls, run with
`../.venv/bin/python3 run.py` or `sweep.py`.

- **Reuses the actual production functions**, not reimplementations —
  `major_coins.rank_by_momentum()` and `market_regime.score_from_closes()`
  were split out of their live-fetching wrappers (`screen()`/`compute()`)
  specifically so both the live Controller and the backtester call the exact
  same math. A backtest result measuring different logic than what runs live
  would be worse than no backtester at all.
- **Does not replay Claude's judgement.** Re-running Claude against a year of
  history costs real money per run and isn't reproducible. `strategy.py`
  stands in a deliberately naive rule ("buy the top-ranked candidate when
  there's cash") purely to test whether the scaffolding underneath Claude has
  any edge on its own — not a claim about what Claude would have picked.
- **Known limitations, stated in the code, not hidden**: daily-bar
  approximation of the trailing stop (a day's high/low stands in for the
  intraday peak/trigger — standard practice for a daily-bar backtest, not a
  replay of the real 60-second check), no fees/slippage modeled yet (the
  single most common real cause of backtest results looking better than live
  ever will), no dip-buy guardrail (needs intraday data daily bars don't
  have).
- `sweep.py` runs several config variants across multiple non-overlapping
  historical windows and prints a comparison table — built specifically
  because a single-window result already caught itself lying once: an 8%
  stop looked like a clear win on one window (+7.4%) and turned out to be one
  lucky window carrying the average (lost in 3 of 4 windows tested). The
  regime filter, by contrast, won in all 4 windows tested — a result that
  actually held up.
- `git status` note: `backtester/data_cache/` (fetched historical bars, JSON,
  gitignored) is disposable and safe to delete; re-fetches and re-caches on
  next run.

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
9. **Backtester exists now (`backtester/`) but stops at Phase 4.** It answers
   "does the deterministic scaffolding have edge on its own" — it does not
   yet model fees/slippage, doesn't have the dip-buy guardrail (needs
   intraday data), and Phase 5 (replaying real logged Claude decisions
   instead of the naive strategy stub) needs months more of live
   `decisions` history to be meaningful. Still the single highest-value
   *partially*-unbuilt thing.
10. **Two identical agents.** Both run the same prompt against the same
   shortlist and reliably reach the same conclusion. They are capital buckets,
   not competing strategies. Genuine diversity would mean giving them
   *different* strategies, not identical ones and hoping they diverge.
11. **Time-of-day analysis is possible but unbuilt.** All the data is being
   collected; nothing queries it yet. Crypto does not write to `market_scans`
   at all, which would need fixing first.
12. **10 of the 30 `major_coins.py` entries aren't tradable on Alpaca**
   (MATIC, ATOM, ALGO, NEAR, OP, INJ, SUI, APT, SAND, MANA — confirmed via
   the backtester's historical fetch returning zero bars for all ten).
   Harmless for live trading (`tradable_universe()` already filters against
   `list_crypto_symbols()`), but dead weight worth pruning.
13. **`weekly_cost_target_aud` is declared in config but wired to nothing.**
   An absolute weekly spending ceiling, distinct from the cost-vs-profit
   breaker above (which is relative to actual profit, not a fixed number).
   Discussed, deliberately not built yet — the owner deferred it.

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
- The cost-vs-profit circuit breaker only ever enforces on a live account,
  never paper. The owner explicitly said running at a net cost on paper while
  tuning the balance is fine — don't add a paper-side version of this "to be
  safe," that defeats the point of testing.
- New capital (pool topups) only ever goes to agents that aren't currently
  losing, never to a currently-losing one to "even things out." Don't add a
  path that moves money away from a winning agent's own earned balance to
  equalise agents — that's a different, unrequested feature and it directly
  contradicts "capital that proves itself gets more capital."

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

# backtester (standalone, no cluster needed - see "Backtester" above)
cd backtester
../.venv/bin/python3 run.py --days 365 --cash 499
../.venv/bin/python3 sweep.py --window-days 60 --windows 4
```

Secrets in `trading-secrets`: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`,
`ANTHROPIC_API_KEY`, `POSTGRES_PASSWORD` (required); `ANTHROPIC_ADMIN_KEY`
(optional, real billed-cost tracking); `TELEGRAM_BOT_TOKEN` /
`TELEGRAM_CHAT_ID` (optional, both-or-neither, notifications). All the
optional ones are `optional: true` in the Deployment's secretKeyRef — a
missing key degrades gracefully (token-based cost estimate; notifications
become a silent no-op) rather than crashing the pod.

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
