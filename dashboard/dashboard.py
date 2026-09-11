"""
Read-only dashboard. Deliberately has no Alpaca credentials and no write
access to anything - it only ever runs SELECT queries against the same
Postgres ledger the Controller writes to. If this pod got compromised, the
worst it could do is show someone your trade history, not touch your money.
"""
import os
import psycopg2
import psycopg2.extras
from flask import Flask, render_template_string

DATABASE_URL = os.environ["DATABASE_URL"]
app = Flask(__name__)

SYMBOL_INFO = {
    "SPY": {"name": "SPDR S&P 500 ETF Trust", "desc": "Tracks the S&P 500 — the 500 largest US companies"},
    "QQQ": {"name": "Invesco QQQ Trust", "desc": "Tracks the Nasdaq-100 — mostly large tech and growth companies"},
}

# Sonnet 4.6 published pricing per 1M tokens - used only to estimate spend from
# tokens actually used. Not the same as your real balance (Anthropic doesn't
# expose that via API) but it shows burn rate, which is the useful part overnight.
PRICE_INPUT_PER_M = 3.00
PRICE_OUTPUT_PER_M = 15.00

TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="20">
<title>Trading desk</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #0a0e16;
    --rail: #0d1220;
    --surface: #10151f;
    --surface-2: #171e2c;
    --border: #1c2334;
    --text: #e4e8f1;
    --text-dim: #8993a8;
    --text-faint: #4a5266;
    --teal: #2dd4bf;
    --amber: #f0a94e;
    --green: #4ade80;
    --rose: #fb7185;
    --sans: 'Inter', -apple-system, sans-serif;
    --mono: 'JetBrains Mono', ui-monospace, monospace;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    font-family: var(--sans);
    background: var(--bg);
    color: var(--text);
    font-size: 14px;
    line-height: 1.5;
    display: flex;
    min-height: 100vh;
  }

  aside {
    width: 300px;
    flex-shrink: 0;
    background: var(--rail);
    border-right: 1px solid var(--border);
    padding: 1.75rem 1.5rem;
    position: sticky;
    top: 0;
    height: 100vh;
    overflow-y: auto;
  }
  .brand { display: flex; align-items: center; gap: 0.55rem; margin-bottom: 1.75rem; }
  .brand .pulse-dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--teal); box-shadow: 0 0 8px var(--teal);
    animation: pulse 2s ease-in-out infinite;
  }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
  .brand h1 { font-size: 1.05rem; font-weight: 600; margin: 0; letter-spacing: -0.01em; }

  .clock-block { border-left: 3px solid var(--border); padding: 0.15rem 0 0.15rem 0.9rem; margin-bottom: 1.25rem; }
  .clock-block.is-teal { border-left-color: var(--teal); }
  .clock-block.is-green { border-left-color: var(--green); }
  .clock-block.is-amber { border-left-color: var(--amber); }
  .clock-block .place { color: var(--text-dim); font-size: 0.76rem; margin-bottom: 0.3rem; }
  .clock-block .time { font-family: var(--mono); font-size: 1.7rem; font-weight: 500; letter-spacing: -0.02em; }
  .clock-block .sub { color: var(--text-faint); font-size: 0.72rem; margin-top: 0.2rem; font-family: var(--mono); }
  .clock-block .status { color: var(--text-dim); font-size: 0.76rem; margin-top: 0.25rem; }
  .clock-block .status strong { color: var(--text); font-weight: 500; }

  .rail-divider { height: 1px; background: var(--border); margin: 1.5rem 0; }

  .rail-stat { display: flex; justify-content: space-between; align-items: baseline; padding: 0.55rem 0; border-bottom: 1px solid var(--border); }
  .rail-stat:last-child { border-bottom: none; }
  .rail-stat .label { color: var(--text-dim); font-size: 0.78rem; }
  .rail-stat .value { font-family: var(--mono); font-size: 0.95rem; font-weight: 500; }
  .rail-stat .value.hero { font-size: 1.4rem; color: var(--teal); }
  .rail-note { color: var(--text-faint); font-size: 0.68rem; margin-top: 0.6rem; line-height: 1.5; }

  main { flex: 1; padding: 2.25rem 3rem 4rem; min-width: 0; }
  section { margin-bottom: 2.5rem; }
  section h2 { font-size: 0.85rem; font-weight: 600; color: var(--text-dim); margin: 0 0 0.9rem; }

  .chips { display: flex; gap: 0.75rem; flex-wrap: wrap; }
  .chip { background: var(--surface); border: 1px solid var(--border); border-left: 3px solid var(--teal); border-radius: 8px; padding: 0.7rem 1rem; min-width: 220px; }
  .chip .sym { font-family: var(--mono); font-weight: 600; font-size: 0.9rem; }
  .chip .name { font-size: 0.8rem; color: var(--text); margin-top: 0.15rem; }
  .chip .desc { font-size: 0.74rem; color: var(--text-faint); margin-top: 0.2rem; }

  .agent-cards { display: flex; gap: 1rem; flex-wrap: wrap; }
  .agent-card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 1.1rem 1.4rem; min-width: 230px; flex: 1; position: relative; overflow: hidden; }
  .agent-card::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 3px; background: var(--text-faint); }
  .agent-card.active::before { background: var(--green); }
  .agent-card.culled::before { background: var(--rose); }
  .agent-card .name { font-size: 0.85rem; color: var(--text-dim); margin-bottom: 0.4rem; }
  .agent-card .balance { font-family: var(--mono); font-size: 1.6rem; font-weight: 500; }
  .agent-card .tier { font-family: var(--mono); font-size: 0.74rem; color: var(--text-faint); margin-top: 0.35rem; }
  .agent-card .meta { font-size: 0.72rem; color: var(--text-faint); margin-top: 0.6rem; }

  .feed { border: 1px solid var(--border); border-radius: 10px; overflow: hidden; background: var(--surface); }
  .feed-row { display: flex; gap: 1rem; padding: 0.85rem 1.1rem; border-bottom: 1px solid var(--border); border-left: 3px solid var(--text-faint); }
  .feed-row:last-child { border-bottom: none; }
  .feed-row.buy { border-left-color: var(--green); }
  .feed-row.sell { border-left-color: var(--rose); }
  .feed-row.hold { border-left-color: var(--text-faint); }
  .feed-time { font-family: var(--mono); color: var(--text-faint); font-size: 0.76rem; width: 68px; flex-shrink: 0; padding-top: 0.1rem; }
  .feed-main { flex: 1; min-width: 0; }
  .feed-line1 { margin-bottom: 0.25rem; }
  .feed-symbol { font-family: var(--mono); font-weight: 600; margin-right: 0.55rem; }
  .feed-symbol-name { font-size: 0.74rem; color: var(--text-faint); margin-right: 0.6rem; }
  .feed-side { font-family: var(--mono); font-size: 0.72rem; text-transform: uppercase; padding: 0.05rem 0.4rem; border-radius: 4px; margin-right: 0.6rem; }
  .feed-side.buy { background: rgba(74,222,128,0.12); color: var(--green); }
  .feed-side.sell { background: rgba(251,113,133,0.12); color: var(--rose); }
  .feed-side.hold { background: rgba(137,147,168,0.12); color: var(--text-dim); }
  .feed-agent { font-size: 0.74rem; color: var(--text-dim); }
  .feed-reasoning { color: var(--text-dim); font-size: 0.82rem; }
  .feed-status { font-size: 0.72rem; color: var(--text-faint); margin-top: 0.3rem; }

  .empty { color: var(--text-faint); padding: 2rem; text-align: center; font-family: var(--mono); font-size: 0.85rem; }
</style>
</head>
<body>

<aside>
  <div class="brand"><span class="pulse-dot"></span><h1>Trading desk</h1></div>

  <div class="clock-block is-teal">
    <div class="place">Brisbane, Australia</div>
    <div class="time" id="bne-time">--:--:--</div>
    <div class="sub" id="bne-date">-</div>
    <div class="status">Your local time</div>
  </div>

  <div class="clock-block" id="nyc-block">
    <div class="place">New York, US</div>
    <div class="time" id="nyc-time">--:--:--</div>
    <div class="sub" id="nyc-date">-</div>
    <div class="status" id="nyc-status">Checking market hours&hellip;</div>
  </div>

  <div class="rail-divider"></div>

  <div class="rail-stat"><span class="label">Total ledger balance</span><span class="value hero">${{ "%.2f"|format(total_balance) }}</span></div>
  <div class="rail-stat"><span class="label">Active agents</span><span class="value">{{ agent_count }}</span></div>
  <div class="rail-stat"><span class="label">Unallocated pool</span><span class="value">${{ "%.2f"|format(pool_balance) }}</span></div>
  <div class="rail-stat"><span class="label">Trades today</span><span class="value">{{ trades_today }}</span></div>
  <div class="rail-stat"><span class="label">Est. Claude spend today</span><span class="value">${{ "%.4f"|format(claude_spend_today) }}</span></div>
  <div class="rail-note">Spend is estimated from tokens used, not a real balance &mdash; Anthropic doesn't expose live credit balance via API. Check console.anthropic.com for the exact figure.</div>
</aside>

<main>

  <section>
    <h2>Watchlist</h2>
    <div class="chips">
      {% for sym, info in symbol_info.items() %}
      <div class="chip">
        <div class="sym">{{ sym }}</div>
        <div class="name">{{ info.name }}</div>
        <div class="desc">{{ info.desc }}</div>
      </div>
      {% endfor %}
    </div>
  </section>

  <section>
    <h2>Agents</h2>
    <div class="agent-cards">
      {% for a in agents %}
      <div class="agent-card {{ a.status }}">
        <div class="name">{{ a.name }}</div>
        <div class="balance">${{ "%.2f"|format(a.current_balance) }}</div>
        <div class="tier">${{ "%.0f"|format(a.min_capital) }} &rarr; ${{ "%.0f"|format(a.max_capital) }}</div>
        <div class="meta">{{ a.status }}{% if a.parent_name %} &middot; from {{ a.parent_name }}{% endif %}</div>
      </div>
      {% endfor %}
    </div>
    {% if not agents %}<div class="empty">No agents yet</div>{% endif %}
  </section>

  <section>
    <h2>Recent trades</h2>
    <div class="feed">
      {% for t in trades %}
      <div class="feed-row {{ t.side }}">
        <div class="feed-time">{{ t.created_at.strftime("%H:%M:%S") }}</div>
        <div class="feed-main">
          <div class="feed-line1">
            <span class="feed-symbol">{{ t.symbol }}</span>
            <span class="feed-symbol-name">{{ symbol_info.get(t.symbol, {}).get("name", "") }}</span>
            <span class="feed-side {{ t.side }}">{{ t.side }}</span>
            <span class="feed-agent">{{ t.agent_name }}</span>
          </div>
          <div class="feed-reasoning">{{ t.reasoning or "" }}</div>
          <div class="feed-status">{{ t.status }}{% if t.reject_reason %} &mdash; {{ t.reject_reason }}{% endif %}</div>
        </div>
      </div>
      {% endfor %}
    </div>
    {% if not trades %}<div class="empty">No trades yet &mdash; agents are still watching for a setup</div>{% endif %}
  </section>

</main>

<script>
function partsFor(tz) {
  const fmt = new Intl.DateTimeFormat('en-US', {
    timeZone: tz, hour12: false, weekday: 'short',
    hour: '2-digit', minute: '2-digit', second: '2-digit'
  });
  const parts = {};
  fmt.formatToParts(new Date()).forEach(p => parts[p.type] = p.value);
  return parts;
}
function tick() {
  const bne = partsFor('Australia/Brisbane');
  document.getElementById('bne-time').textContent = `${bne.hour}:${bne.minute}:${bne.second}`;
  document.getElementById('bne-date').textContent = new Date().toLocaleDateString('en-US', { timeZone: 'Australia/Brisbane', weekday: 'short', month: 'short', day: 'numeric' });

  const nyc = partsFor('America/New_York');
  document.getElementById('nyc-time').textContent = `${nyc.hour}:${nyc.minute}:${nyc.second}`;
  document.getElementById('nyc-date').textContent = new Date().toLocaleDateString('en-US', { timeZone: 'America/New_York', weekday: 'short', month: 'short', day: 'numeric' });

  const h = parseInt(nyc.hour, 10), m = parseInt(nyc.minute, 10);
  const mins = h * 60 + m;
  const isWeekday = !['Sat', 'Sun'].includes(nyc.weekday);
  const open = 9 * 60 + 30, close = 16 * 60, preOpen = 4 * 60, afterClose = 20 * 60;

  let label, blockClass;
  if (!isWeekday) { label = 'Closed &mdash; weekend'; blockClass = ''; }
  else if (mins >= open && mins < close) { label = '<strong>Market open</strong>'; blockClass = 'is-green'; }
  else if (mins >= preOpen && mins < open) { label = 'Pre-market'; blockClass = 'is-amber'; }
  else if (mins >= close && mins < afterClose) { label = 'After-hours'; blockClass = 'is-amber'; }
  else { label = 'Closed'; blockClass = ''; }

  document.getElementById('nyc-status').innerHTML = label;
  document.getElementById('nyc-block').className = 'clock-block ' + blockClass;
}
tick();
setInterval(tick, 1000);
</script>

</body>
</html>
"""


def query(sql, params=()):
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        return cur.fetchall()
    finally:
        conn.close()


@app.route("/")
def dashboard():
    agents = query("SELECT * FROM agents ORDER BY status = 'active' DESC, created_at DESC")
    trades = query("""
        SELECT t.*, a.name AS agent_name FROM trades t
        JOIN agents a ON a.id = t.agent_id
        ORDER BY t.created_at DESC LIMIT 30
    """)
    pool = query("SELECT balance FROM unallocated_pool WHERE id = 1")[0]["balance"]
    trades_today = query("SELECT COUNT(*) AS c FROM trades WHERE created_at::date = CURRENT_DATE")[0]["c"]

    try:
        row = query("""
            SELECT COALESCE(SUM(input_tokens), 0) AS in_tok, COALESCE(SUM(output_tokens), 0) AS out_tok
            FROM api_usage WHERE created_at::date = CURRENT_DATE
        """)[0]
        input_tok, output_tok = row["in_tok"], row["out_tok"]
    except Exception:
        input_tok, output_tok = 0, 0
    claude_spend_today = (input_tok / 1_000_000 * PRICE_INPUT_PER_M) + (output_tok / 1_000_000 * PRICE_OUTPUT_PER_M)

    active_agents = [a for a in agents if a["status"] == "active"]
    total_balance = sum(float(a["current_balance"]) for a in active_agents) + float(pool)

    return render_template_string(
        TEMPLATE, agents=agents, trades=trades, agent_count=len(active_agents),
        total_balance=total_balance, pool_balance=float(pool), trades_today=trades_today,
        symbol_info=SYMBOL_INFO, claude_spend_today=claude_spend_today,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8090)
