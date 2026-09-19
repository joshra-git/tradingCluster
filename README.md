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

**For crypto:** a fixed list of twelve established coins — Bitcoin, Ethereum,
Solana, XRP, Cardano, Chainlink and similar. Deliberately boring. The
alternative is asking the exchange "what moved most today", which returns
whatever spiked hardest — usually memecoins about to give it all back.

**For stocks:** around eighty large companies (Apple, JPMorgan, Costco…) plus
broad index funds like SPY and VOO, filtered to skip anything that has already
run more than 25% or that swings violently day to day.

Either way the principle is the same: buying things that have already spiked
means buying from people sitting on profits who are looking to take them.

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

Fill in the four values:

```yaml
  ALPACA_API_KEY: "your paper key"
  ALPACA_SECRET_KEY: "your paper secret"
  ANTHROPIC_API_KEY: "sk-ant-api-..."
  POSTGRES_PASSWORD: "make something up"
```

`POSTGRES_PASSWORD` is internal to your cluster — it isn't registered anywhere,
just pick something.

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
    cur.execute('TRUNCATE agents, trades, capital_events, positions, decisions, market_scans, api_usage, equity_snapshots RESTART IDENTITY CASCADE')
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
