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
- **One agent per symbol** — if another agent already holds a name, this one
  has to find something else, so an identical shortlist can't make two agents
  quietly double up on the same bet.
- **Max position size** as a fraction of that agent's own balance, so one bad
  call can't wipe out a disproportionate share of its capital. Stocks round
  down to whole shares; crypto is allowed fractional sizing.
- **Daily loss circuit breaker** — an agent that's down more than a set % on
  the day stops trading until tomorrow.
- **Account-wide PDT awareness** — the pattern-day-trader rule applies to the
  whole Alpaca account, not per-agent, so this check looks at all agents'
  combined activity, not just one. It doesn't apply to crypto at all — there's
  no day-trade rule for it.
- **Crypto dip-buy guardrail** — crypto-only: won't buy something already up
  more than a configured % in the last 24 hours unless it's pulled back at
  least a little from that high. The point is entering on a pullback, not at
  the peak. Pre-filtered out of the shortlist too, before Claude even sees
  it — see "Trading only when it's worth paying for" below for why.
- **Cost-vs-profit circuit breaker, live accounts only** — if trailing 7-day
  Claude spend has run ahead of trailing 7-day *realised* profit, new
  positions are blocked until that turns around. Paper is explicitly exempt;
  testing at a net cost while tuning the balance is fine. Nothing already
  held is ever touched by this.

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

## Exits: a trailing stop, not a fixed take-profit

Selling used to be a straight two-sided rule: stop-loss below entry, fixed
take-profit above it. The take-profit side capped every winner at the same
percentage regardless of whether it had further to run, so it's been replaced
by a **trailing stop**: the stop is measured down from the highest price seen
since entry, not from the entry price itself. A position that keeps climbing
drags its stop up behind it and is never force-sold for "winning too much" —
it only exits once the move actually turns over by the stop distance from its
peak. Crypto uses a wider stop distance than stocks (it swings harder), but
the same trailing mechanism, not a separate fixed target.

This check runs every 60 seconds in the Controller against live prices,
independent of any agent's own cycle or any model call — a stop-loss that
depends on an API call succeeding isn't really a stop-loss. Claude can still
choose to sell earlier for its own reasons; this is just the backstop that
always fires regardless.

**Crypto also has a profit-lock ratchet on top of the trailing stop.** A wide
stop is fine while a position is only marginally up — that's normal noise —
but on a small account, giving back a wide percentage of an already-real
dollar gain can wipe most of it out before the stop ever fires. Once a
position's peak unrealised profit crosses `crypto_profit_lock_trigger_usd`
(default $5), it switches to a much tighter trailing distance
(`crypto_profit_lock_stop_pct`, default 4%) for the rest of its life, so a
real win gets protected instead of given back to the same wide band that
tolerated the climb. It's a ratchet, not a flat take-profit — small
positions still get room to breathe, real gains get locked in hard.

## The market regime brake

A momentum strategy buys things going up. In a broad downtrend most things go
down, so it either finds nothing or buys weak bounces that fail.

Every hour the Controller scores the whole market out of 100 — is Bitcoin (or
the S&P) above its long-term average, is that average rising, how many other
coins are participating, which way has it moved recently. Four components,
25 points each.

- **70 or above** — normal trading
- **40 to 69** — half-sized positions
- **Below 40** — no new buys at all

It only ever blocks *new* purchases. Anything already held stays, and its
stop-loss keeps running. If the scoring itself fails, it assumes normal
conditions rather than halting — a brake that jams on whenever a data fetch
hiccups is worse than no brake.

This costs nothing to run: it is arithmetic on price data already being
fetched, with no AI involved.

Worth being honest about what it does: it should mean **fewer bad weeks**, not
more good ones. Trend filters cut losses in poor conditions and cost you some
upside by lagging recoveries. It has not been backtested.

## Trading only when it's worth paying for

Claude API calls cost real money per call, so the system doesn't think at a
constant rate around the clock, and stocks and crypto are paced differently
because "market hours" doesn't mean anything for crypto.

**Stocks** follow the NYSE session: a check every 90 seconds while the market
is open, once every 10 minutes pre-market/after-hours/weekday-closed, and
once an hour on weekends.

**Crypto never closes, and — as of this pass — no longer paces to the owner's
waking hours either.** It used to slow to once every 30 minutes overnight,
specifically so trading happened while someone was awake to watch it. That
reasoning got weaker once push notifications (below) made "seeing it happen"
available any time, and the owner explicitly chose round-the-clock pacing
over keeping the quiet-hours gate, knowing it costs more to run. The
cost-vs-profit breaker above exists to guard the other side of that trade.

On top of that base pacing:

- **Hunting vs. holding.** An agent with cash to deploy checks often — a good
  entry is time-sensitive. An agent that's already fully invested has nothing
  to decide (the trailing stop handles exits without any model call), so it
  slows down to an occasional check-in whose only purpose is letting Claude
  bail out early for a judgement reason.
- **A cheap pre-filter runs before every paid call.** Even on a hunting
  cycle, the agent asks in plain Python first: did a held position cross a
  profit/loss threshold worth a judgement call, has the shortlist actually
  moved, or did the *previous* cycle genuinely execute a trade? A proposal
  that got *rejected* used to count the same as one that executed, which
  meant a candidate the risk layer kept blocking (already up too much in
  24h, say) would force a fresh paid call every single cycle to ask the same
  question and get the same answer — one incident burned roughly two-thirds
  of a day's calls this way before being caught. Fixed by only counting an
  actually-executed trade as "something changed." The same rejection reason
  is now also pre-filtered out of the shortlist itself, so Claude never sees
  a candidate that's guaranteed to be turned down.
- **A daily call budget** across all agents acts as a hard circuit breaker —
  originally sized around a free-tier limit, it's now kept specifically so a
  crash-looping agent can't run up a real bill overnight.
- **The cost-vs-profit breaker** (see above) is the newest and most direct
  lever: on a live account specifically, it stops opening new positions the
  moment trailing spend outruns trailing realised profit, rather than
  trusting call-count pacing alone to keep the two in a sane relationship.

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
- **Actual token usage per call**, used to estimate spend by default. Where
  available, **real billed USD spend** is pulled directly from Anthropic's
  Usage & Cost Admin API every 6 hours instead — this needs an admin-level API
  key and an organisation account, so it's an upgrade over the token estimate,
  not a replacement requirement. The dashboard converts that USD figure to AUD
  using a daily-fetched exchange rate, and clearly marks the number as
  estimated or a stale fallback rate whenever it can't get a live one — a
  guessed number is never presented as if it were real.

## What it's watching

**For crypto:** a fixed, hand-maintained list (currently around thirty coins)
rather than a live ranking, because Alpaca doesn't expose market-cap data and
its "movers" screener returns whatever spiked hardest today — which is how
memecoins kept reaching the shortlist. On top of the list itself, a **dip-buy
guardrail** blocks buying something that's already up sharply in the last 24
hours unless it's pulled back at least a little from that high — entering on
a pullback, not at the peak.

**For stocks:** around eighty large, liquid companies (Apple, JPMorgan,
Costco…) plus broad index funds like SPY and VOO, filtered to skip anything
already up more than 25% in the lookback window or averaging more than an 8%
daily high/low swing — too late, or too choppy to hold a stop through.

Either way the principle is the same: buying things that have already spiked
means buying from people sitting on profits who are looking to take them.

## The dashboard

A read-only view with no trading credentials of its own — it only ever reads
the same ledger the Controller writes to. Shows: live Brisbane and New York
clocks (so you can see trading windows at a glance, not just guess), current
market session, every agent's balance and tier, and a live feed of trades
with full reasoning attached.

## Push notifications, so you don't have to keep checking

An optional Telegram bot (no cost, your own bot token) sends a plain-English
message the moment money actually moves — a buy with the cost and the
stop-loss price, a sell with the $/% gain or loss and why it happened, in the
same no-jargon voice as everything else the system explains. A once-daily
digest covers the last 24 hours; sending `/status` any time gets a live
snapshot back instead — everything currently held, its return right now, and
total performance since the system was last reset — without waiting for the
scheduled digest or opening the dashboard. It only ever replies to the one
chat you configured; a stray message from anyone else who found the bot is
silently ignored.

## Paper vs. live

The Controller reads a single `ALPACA_PAPER` flag to decide which Alpaca
endpoint it talks to. Right now the cluster runs one environment at a time —
currently **paper** (Alpaca's simulated account against real market prices) —
rather than two independently deployed copies. Switching to live means real
money, real broker credentials, and real stakes on the same risk logic; there
is no separate live manifest set yet, so doing that today means changing the
flag and secrets on the one deployment that exists, deliberately, not
something to do by accident.

## The backtester

A standalone replay of the deterministic half of this system — momentum
ranking, the market regime brake, position sizing, exits — against real
historical Alpaca price data. Lives in `backtester/`, runs entirely outside
Kubernetes (no cluster, no Postgres, no broker credentials), and reuses the
exact same ranking and regime-scoring functions the live Controller calls, so
a backtest result can't quietly measure different logic than what's actually
running.

It deliberately does **not** replay Claude's own judgement — that would cost
real API money per historical run and wouldn't even give a repeatable answer.
It stands in a simple, dumb rule instead, purely to test whether the
mechanical rules underneath Claude have any edge on their own. Already
useful for exactly what it's for: a stop-loss distance that looked like a
clear win on one 60-day window turned out to be one lucky window carrying the
average once tested across four; the market regime brake, by contrast, won
in every one of those four windows.

Known limits, not hidden: no fees or slippage modeled yet (the most common
reason a backtest looks better than live trading ever will), and the trailing
stop is approximated from daily highs/lows rather than a true 60-second
replay.

## Open ideas, discussed but not yet built

Roughly in priority order:

- **No self-healing for missing agent pods.** If an agent's Kubernetes
  Deployment gets deleted, the ledger still lists it as active but nothing
  recreates the pod — this has caused real confusion more than once.
- **Correlation-aware position limits** — SPY and QQQ move together most of
  the time. Right now each agent's risk check only looks at its own
  position; nothing stops two agents from both going long the same direction
  at once, which doubles real exposure without either agent "knowing" it.
- **Position sizing tied to stop-loss distance**, instead of a flat % of
  balance — sizing down automatically when Claude sets a wide/cautious stop,
  rather than treating a 1%-stop trade and a 10%-stop trade the same way.
- **No awareness of US market holidays** — the market-hours check knows
  weekday/weekend and clock time, but would treat a holiday like Thanksgiving
  as a normal trading day.
- **No cleanup on growing tables** — `decisions`, `market_scans`, and
  `equity_snapshots` grow forever with no pruning currently wired in.
- **Real market data is still limited to price + a short trend window** — no
  volume, no news/sentiment, no broader technical indicators. Claude has
  repeatedly and correctly said it wants more signal than this before
  committing capital confidently.
- **A third of the crypto watchlist isn't actually tradable on Alpaca** — 10
  of the 30 coins in `major_coins.py` return no data at all when the
  backtester tries to fetch their history. Harmless for live trading (already
  filtered out before anything is proposed), just dead weight worth pruning.
- **A weekly spending ceiling was named but never wired up.**
  `weekly_cost_target_aud` sits in config unused — a fixed absolute cap,
  distinct from the cost-vs-profit breaker above (which is relative to actual
  performance, not a flat number). Discussed, deliberately deferred.

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
  An optional Anthropic Admin key (real billed-cost tracking) and optional
  Telegram bot token + chat ID (push notifications) live here too — all
  `optional: true` in the Deployment spec, so a missing one degrades
  gracefully instead of crashing the pod.

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

The base ledger (`db/schema.sql`, applied on Postgres's first boot) holds
`agents`, `trades`, `capital_events`, `unallocated_pool`, `positions`, and
`api_usage`. The Controller has since grown several more tables at startup
via `CREATE TABLE IF NOT EXISTS` — no migration tool, just idempotent
create/alter statements run every time it boots:

| Table | Holds |
| --- | --- |
| `agents` | name, status, balance, min/max capital, parent, strategy JSON |
| `trades` | every proposal incl. rejected ones, with reasoning and fill price |
| `positions` | current holdings per agent/symbol, plus last price and high-water mark |
| `capital_events` | every dollar moved between agents/pool |
| `unallocated_pool` | single row, uncommitted cash |
| `api_usage` | tokens per call, for the token-based spend estimate |
| `decisions` | every decision incl. holds — what the dashboard reads |
| `market_scans` | discovery results (stocks only) |
| `shortlist_cache` | latest computed shortlist, so the dashboard can read it without triggering its own broker calls |
| `market_regime` | hourly regime score, label, and component breakdown |
| `api_costs` | real billed USD per day, from the Admin API |
| `fx_rates` | USD→AUD rate, flagged live or fallback |
| `equity_snapshots` | per-agent value over time, for the detail-page chart |

## Images

Three images (`trading-controller`, `trading-agent`, `trading-dashboard`),
built locally and loaded directly into the `kind` cluster — no external
container registry involved. Controller listens on 8080, dashboard on
**8095** (this has drifted before — if a port-forward connects but then
refuses the connection, check `grep app.run dashboard/dashboard.py` against
the Service port first). Both are internal to the cluster and reached
locally via `kubectl port-forward`.

# Setting this up from scratch

Written for someone starting with a fresh Windows machine and no prior setup.
Follow it top to bottom; each step assumes the ones before it.

Budget about an hour, most of it waiting for downloads.

## What you're installing and why

| Thing | What it does here |
| --- | --- |
| WSL | Runs Linux inside Windows. Everything below lives in here. |
| Docker Desktop | Runs the containers. Kubernetes needs it. |
| kind | Runs a small Kubernetes cluster on your own machine. |
| kubectl | The command you use to talk to that cluster. |
| Alpaca account | The broker. Provides market data and executes trades. |
| Anthropic account | Provides Claude, which makes the trading decisions. |

Two accounts, both free to start. Alpaca's paper trading uses fake money
against real market prices — nothing here touches real money unless you
deliberately switch it over later.

---

## 1. WSL (Windows Subsystem for Linux)

Open **PowerShell as Administrator** and run:

```powershell
wsl --install
```

Restart when it asks. On first boot it will ask you to create a Linux username
and password — these are separate from your Windows login, and the password is
invisible as you type it (that's normal, keep going).

Check it worked. Open a terminal and type `wsl`, then:

```bash
lsb_release -a
```

You should see Ubuntu. From here on, **every command goes in this Linux
terminal**, not PowerShell, unless it says otherwise.

### A warning that will save you hours

Do not move project folders around using Windows Explorer once they contain a
`.git` folder. Windows attaches hidden `Zone.Identifier` markers to files, and
these corrupt git repositories in ways that are genuinely painful to unpick.
Copy files using Linux commands, or `git clone` fresh on each machine.

If you ever see `Zone.Identifier` in an error message, this is why:

```bash
find . -name "*:Zone.Identifier" -delete
```

---

## 2. Docker Desktop

Download from **docker.com/products/docker-desktop** — the **Windows** version,
even though you'll use it from Linux.

After installing, open Docker Desktop and go to
**Settings → Resources → WSL Integration**. Turn on the toggle for your Ubuntu
distro. This is easy to miss and nothing works without it.

Restart Docker Desktop, then back in your Linux terminal:

```bash
docker ps
```

You want an empty table, not a connection error. If you get
`Cannot connect to the Docker daemon`, Docker Desktop isn't running or the WSL
integration toggle is off.

Note: `sudo systemctl start docker` does **not** work here and will tell you
systemd isn't running. That's expected — Docker Desktop on the Windows side is
the daemon, there's nothing to start inside Linux.

---

## 3. kubectl and kind

```bash
# kubectl
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl
kubectl version --client

# kind
curl -Lo ./kind https://kind.sigs.k8s.io/dl/latest/kind-linux-amd64
chmod +x ./kind
sudo mv ./kind /usr/local/bin/kind
kind version
```

Both commands should print a version. If `kind: command not found` appears
later in a new terminal, `/usr/local/bin` isn't on your PATH — add it to
`~/.bashrc` or `~/.zshrc`.

---

## 4. Alpaca (the broker)

1. Sign up at **alpaca.markets**. Free, and you do not need to deposit
   anything to use paper trading.
2. Once in, make sure you are on the **Paper Trading** account — there's a
   toggle or account selector, usually top-left. This matters.
3. Generate API keys from the dashboard. You get a **key** and a **secret**.
   The secret is shown once, so copy both somewhere safe immediately.
4. Note your paper account's starting balance. The default is usually
   $100,000, which is far more than this system is configured for — see the
   capital settings section below.

Paper keys and live keys are entirely separate credentials. Paper keys will
not work against the live endpoint and vice versa.

**If you ever reset your paper account**, Alpaca invalidates the old API keys
and issues new ones. Everything will start failing with authentication errors
until you update them. There is no API for resetting a paper account — it's
done in their dashboard only.

---

## 5. Anthropic (Claude)

1. Sign up at **console.anthropic.com**.
2. Go to **Settings → API Keys** and create one. It starts with `sk-ant-api`.
   Copy it immediately — it is only shown once, and cannot be retrieved later.
3. Go to **Settings → Billing** and add some credit. A few dollars is plenty;
   this system costs cents per week once configured.

This is billed per use and is **separate from any Claude.ai subscription**.
Having Claude Pro does not give you API credit.

If the agents fail with "credit balance is too low", this is the fix — top up
in the Console.

### Optional: real spend tracking

If you want the dashboard to show actual billed costs rather than an estimate,
create an **Admin key** at **Settings → Admin Keys** (starts with
`sk-ant-admin`). This requires an organisation rather than an individual
account — set one up under **Settings → Organization** if the Admin Keys page
isn't there. Without it everything still works; the cost figures are just
estimated from token counts.

---

## 6. Get the project running

```bash
cd ~
git clone <your-repo-url> tradingCluster
cd tradingCluster

# create your secrets file from the template
cp k8s/01-secrets.example.yaml k8s/01-secrets.yaml
nano k8s/01-secrets.yaml
```

Fill in the four required values:

```yaml
  ALPACA_API_KEY: "your paper key"
  ALPACA_SECRET_KEY: "your paper secret"
  ANTHROPIC_API_KEY: "sk-ant-api-..."
  POSTGRES_PASSWORD: "make something up"
```

`POSTGRES_PASSWORD` is internal to your cluster — it isn't registered anywhere,
just pick something.

Two more fields in the same file are optional — leave them blank to skip:
`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` for push notifications (message
`@BotFather` on Telegram to create a bot and get a token; the file has the
full steps). Nothing breaks without them, notifications just stay off.

**Before you commit anything to git:**

```bash
echo "k8s/01-secrets.yaml" >> .gitignore
echo "*:Zone.Identifier" >> .gitignore
echo "__pycache__/" >> .gitignore
```

Once a secret is in git history, removing it later is difficult. Do this first.

Then bring everything up:

```bash
chmod +x run-all.sh deploy.sh
./run-all.sh
```

First run takes several minutes — it creates the cluster, builds three
container images, and starts everything. Subsequent runs are much faster.

---

## 7. Check it worked

```bash
kubectl -n trading get pods
```

You want `controller`, `postgres-0`, `dashboard` and at least one `agent-...`
pod, all `Running`.

```bash
kubectl -n trading logs deploy/controller --tail=30 | grep -i bootstrap
```

This should say it created agents with a starting balance. If it says
`account has $X but needs $Y`, your capital settings don't match your account
balance — see below.

Watch an agent think:

```bash
kubectl -n trading logs -f -l app=trading-agent
```

Open the dashboard (leave this running in its own terminal tab):

```bash
kubectl -n trading port-forward svc/dashboard 8091:8095
```

Then visit **http://localhost:8091** in your browser.

---

## 8. Capital settings

The system splits your account across agents. These live in
`k8s/02-configmap-tiers.yaml`:

```yaml
min_capital: 250        # what each agent starts with
max_capital: 500        # agent doubles its money -> spawns a sibling
initial_pod_count: 2    # how many agents to create at startup
```

`min_capital × initial_pod_count` must be **less than or equal to** your
account's cash, or bootstrap refuses to seed anything rather than
half-creating agents.

To change these on a running cluster:

```bash
kubectl -n trading get configmap trading-tier-config -o jsonpath='{.data.config\.yaml}' > /tmp/cfg.yaml
nano /tmp/cfg.yaml
kubectl -n trading create configmap trading-tier-config --from-file=config.yaml=/tmp/cfg.yaml --dry-run=client -o yaml | kubectl apply -f -
```

**Important:** changes take up to 60 seconds to reach the running pods. Verify
before assuming it didn't work:

```bash
kubectl -n trading exec deploy/controller -- cat /etc/trading-config/config.yaml
```

Restarting the Controller before that sync completes makes it read the old
values — a genuinely confusing failure mode.

---

## 9. Starting over

To wipe the ledger and reseed from scratch:

```bash
kubectl -n trading delete deployment -l app=trading-agent --ignore-not-found
kubectl -n trading exec deploy/controller -- python3 -c "
import db
with db.get_conn() as conn:
    cur = conn.cursor()
    cur.execute('TRUNCATE agents, trades, capital_events, positions, decisions, market_scans, api_usage, equity_snapshots, shortlist_cache RESTART IDENTITY CASCADE')
    cur.execute('UPDATE unallocated_pool SET balance = 0')
print('cleared')
"
kubectl -n trading rollout restart deployment/controller
```

**This does not sell anything.** It only clears our bookkeeping. If the account
holds positions, they stay open with nothing tracking them. Close them first:

```bash
kubectl -n trading exec deploy/controller -- python3 -c "
import alpaca_client
for r in alpaca_client.trading_client.close_all_positions(cancel_orders=True):
    print(r.symbol, r.status)
"
```

---

## 10. Common problems

**`Cannot connect to the Docker daemon`** — Docker Desktop isn't running, or
WSL Integration is off in its settings.

**`kind: command not found` in a new terminal** — PATH issue. Add
`/usr/local/bin` to your shell profile.

**Dashboard port-forward connects then says "connection refused"** — the port
inside the container doesn't match the Service. Check
`grep app.run dashboard/dashboard.py` matches the Service port (8095), rebuild
if not.

**Agents log "credit balance is too low"** — top up at console.anthropic.com.

**Agents log 401 or authentication errors from Alpaca** — your keys are wrong,
or you reset the paper account and they were invalidated.

**Bootstrap says "not seeding anything"** — `min_capital × initial_pod_count`
exceeds your account cash. Lower them or fund the account.

**Config changes appear to do nothing** — wait 60 seconds and check what the
pod actually has mounted, per section 8.

**No agent pods exist but the ledger lists agents** — the Controller only
creates agents when the ledger is empty. Either clear the ledger (section 9)
or spawn pods manually:

```bash
kubectl -n trading exec deploy/controller -- python3 -c "
import db, k8s_client
for a in db.list_active_agents():
    k8s_client.spawn_agent_pod(a['name'], a['strategy'])
    print('spawned', a['name'])
"
```

---

## A note before you start

This trades with fake money by default and should stay that way until you've
watched it run for several weeks and understand what it does.

It is a learning project, not a proven strategy. It can and will lose money.
The risk limits in it — stop-losses, position caps, daily loss limits — matter
more than anything else in the codebase, and loosening them to chase bigger
returns is how accounts get emptied. If the returns disappoint, that is
information about the strategy, not a reason to remove the safety rails.
