"""
Read-only dashboard. Deliberately has no Alpaca credentials and no write
access to anything - it only ever runs SELECT queries against the same
Postgres ledger the Controller writes to. If this pod got compromised, the
worst it could do is show someone your trade history, not touch your money.
"""
import os
from datetime import datetime, timezone
import psycopg2
import psycopg2.extras
from flask import Flask, render_template_string

DATABASE_URL = os.environ["DATABASE_URL"]
app = Flask(__name__)

SYMBOL_INFO = {
    "SPY": {"name": "SPDR S&P 500 ETF Trust", "desc": "Tracks the S&P 500 — the 500 largest US companies"},
    "QQQ": {"name": "Invesco QQQ Trust", "desc": "Tracks the Nasdaq-100 — mostly large tech and growth companies"},
}

# Published pricing per 1M tokens for the model the agents run (agent/agent.py
# LLM_MODEL). Used only to estimate spend from tokens when real billed figures
# from the Admin API aren't available. Change these whenever LLM_MODEL changes -
# otherwise the dashboard keeps pricing new traffic at the old model's rates.
USD_PER_MTOK_INPUT = 2.00       # claude-sonnet-5
USD_PER_MTOK_OUTPUT = 10.00
USD_PER_MTOK_CACHE_READ = 0.20   # 0.1x input
USD_PER_MTOK_CACHE_WRITE = 2.50  # 1.25x input

# Keep this in sync with daily_call_budget in the tier ConfigMap.
DAILY_CALL_BUDGET = int(os.environ.get("DAILY_CALL_BUDGET", "1400"))

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
    --shadow-card: 0 1px 2px rgba(0,0,0,0.24), 0 8px 24px -12px rgba(0,0,0,0.5);
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
  ::selection { background: rgba(45,212,191,0.28); color: var(--text); }
  a { color: var(--teal); }

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

  main { flex: 1; padding: 2.25rem 3rem 4rem; min-width: 0; max-width: 1320px; }
  section { margin-bottom: 2.75rem; }
  section h2 {
    font-size: 0.72rem; font-weight: 600; color: var(--text-faint); margin: 0 0 1rem;
    text-transform: uppercase; letter-spacing: 0.08em;
  }

  .chips { display: flex; gap: 0.75rem; flex-wrap: wrap; }
  .chip {
    background: var(--surface); border: 1px solid var(--border); border-left: 3px solid var(--teal);
    border-radius: 10px; padding: 0.75rem 1.05rem; min-width: 220px; box-shadow: var(--shadow-card);
    transition: border-color 0.15s ease;
  }
  .chip .sym { font-family: var(--mono); font-weight: 600; font-size: 0.9rem; }
  .chip .name { font-size: 0.8rem; color: var(--text); margin-top: 0.2rem; }
  .chip .desc { font-size: 0.74rem; color: var(--text-faint); margin-top: 0.25rem; }

  .agent-cards { display: flex; gap: 1.1rem; flex-wrap: wrap; }
  .agent-card {
    background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
    padding: 1.25rem 1.5rem; min-width: 250px; flex: 1; position: relative; overflow: hidden;
    box-shadow: var(--shadow-card); transition: border-color 0.15s ease, transform 0.15s ease;
  }
  .agent-card::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 3px; background: var(--text-faint); }
  .agent-card.active::before { background: var(--green); }
  .agent-card.culled::before { background: var(--rose); }
  .agent-card:hover { border-color: var(--text-faint); }
  .agent-card .name { font-size: 0.85rem; color: var(--text-dim); margin-bottom: 0.45rem; font-weight: 500; }
  .agent-card .name a { color: var(--teal); text-decoration: none; }
  .agent-card .name a:hover { text-decoration: underline; }
  .agent-card .balance { font-family: var(--mono); font-size: 1.7rem; font-weight: 500; letter-spacing: -0.01em; }
  .agent-card .tier { font-family: var(--mono); font-size: 0.74rem; color: var(--text-faint); margin-top: 0.4rem; }
  .agent-card .meta { font-size: 0.72rem; color: var(--text-faint); margin-top: 0.65rem; }
  .spark { display: block; margin-top: 0.7rem; }
  .spark-empty { height: 32px; margin-top: 0.7rem; }
  .holdings { margin-top: 0.9rem; padding-top: 0.8rem; border-top: 1px solid var(--border); }
  .holdings-label { font-size: 0.68rem; color: var(--text-faint); margin-bottom: 0.5rem; text-transform: uppercase; letter-spacing: 0.05em; }
  .holding-row { display: flex; align-items: baseline; gap: 0.5rem; font-family: var(--mono); font-size: 0.76rem; padding: 0.2rem 0; }
  .h-sym { font-weight: 600; min-width: 74px; }
  .h-qty { color: var(--text-faint); flex: 1; }
  .h-chg { font-weight: 500; }
  .h-chg.up { color: var(--green); }
  .h-chg.down { color: var(--rose); }
  .h-chg.pending { color: var(--text-faint); font-size: 0.7rem; }
  .h-val { min-width: 58px; text-align: right; }
  .holdings-total { font-family: var(--mono); font-size: 0.72rem; color: var(--text-dim); margin-top: 0.55rem; padding-top: 0.45rem; border-top: 1px dashed var(--border); }

  .feed { border: 1px solid var(--border); border-radius: 12px; overflow: hidden; background: var(--surface); box-shadow: var(--shadow-card); }
  .feed-row { display: flex; gap: 1rem; padding: 0.9rem 1.2rem; border-bottom: 1px solid var(--border); border-left: 3px solid var(--text-faint); transition: background 0.15s ease; }
  .feed-row:last-child { border-bottom: none; }
  .feed-row:hover { background: var(--surface-2); }
  .feed-row.buy { border-left-color: var(--green); }
  .feed-row.sell { border-left-color: var(--rose); }
  .feed-row.hold { border-left-color: var(--text-faint); }
  .feed-time { font-family: var(--mono); color: var(--text-faint); font-size: 0.76rem; width: 68px; flex-shrink: 0; padding-top: 0.1rem; }
  .feed-main { flex: 1; min-width: 0; }
  .feed-line1 { margin-bottom: 0.3rem; }
  .feed-symbol { font-family: var(--mono); font-weight: 600; margin-right: 0.55rem; }
  .feed-symbol-name { font-size: 0.74rem; color: var(--text-faint); margin-right: 0.6rem; }
  .feed-side { font-family: var(--mono); font-size: 0.72rem; text-transform: uppercase; padding: 0.1rem 0.45rem; border-radius: 4px; margin-right: 0.6rem; letter-spacing: 0.02em; }
  .feed-side.buy { background: rgba(74,222,128,0.12); color: var(--green); }
  .feed-side.sell { background: rgba(251,113,133,0.12); color: var(--rose); }
  .feed-side.hold { background: rgba(137,147,168,0.12); color: var(--text-dim); }
  .feed-agent { font-size: 0.74rem; color: var(--text-dim); }
  .feed-reasoning { color: var(--text-dim); font-size: 0.82rem; }
  .feed-status { font-size: 0.72rem; color: var(--text-faint); margin-top: 0.35rem; }

  table { box-shadow: var(--shadow-card); }
  tr:hover td { background: var(--surface-2); }

  .empty {
    color: var(--text-faint); padding: 2.25rem; text-align: center; font-family: var(--mono);
    font-size: 0.85rem; background: var(--surface); border: 1px dashed var(--border); border-radius: 12px;
  }
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
  <div class="rail-stat"><span class="label">Model calls today</span><span class="value">{{ calls_today }}</span></div>
  <div class="rail-stat"><span class="label">Spend today</span><span class="value">A${{ "%.2f"|format(spend_aud_today) }}</span></div>
  <div class="rail-stat"><span class="label">Spend last 7 days</span><span class="value">A${{ "%.2f"|format(spend_aud_week) }}</span></div>
  <div class="rail-note">
    {% if cost_actual %}Actual billed spend from Anthropic{% else %}Estimated from token counts &mdash; set ANTHROPIC_ADMIN_KEY for billed figures{% endif %}.
    US${{ "%.2f"|format(spend_usd_week) }} converted at {{ "%.4f"|format(fx_rate) }}{% if not fx_live %} (fallback rate){% endif %}.
  </div>
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
    <h2>Currently watching</h2>
    <div class="chips">
      {% for w in watching %}
      <div class="chip" style="border-left-color: {{ 'var(--green)' if (w.pct_change or 0) >= 0 else 'var(--rose)' }};">
        <div class="sym">{{ w.symbol }}</div>
        <div class="name">${{ "%.2f"|format(w.price) if w.price else "-" }}</div>
        <div class="desc">{{ "%.2f"|format(w.pct_change) if w.pct_change is not none else "-" }}% over the trend window</div>
      </div>
      {% endfor %}
    </div>
    {% if not watching %}<div class="empty">No shortlist yet &mdash; needs a few discovery scans and an agent cycle first</div>{% endif %}
  </section>

  <section>
    <h2>What each agent is thinking</h2>
    <div class="agent-cards">
      {% for t in thinking %}
      <div class="agent-card active">
        <div class="name">{{ t.agent_name }}</div>
        <div class="balance" style="font-size: 1.1rem;">
          <span class="feed-side {{ t.action }}">{{ t.action }}</span>
          {% if t.symbol %}<span class="sym" style="font-family: var(--mono); margin-left: 0.4rem;">{{ t.symbol }}</span>{% endif %}
        </div>
        <div class="meta" style="margin-top: 0.5rem; color: var(--text-dim); font-size: 0.78rem; line-height: 1.5;">{{ t.reasoning or "" }}</div>
        <div class="meta" style="margin-top: 0.5rem;">as of {{ t.created_at.strftime("%H:%M:%S") }}</div>
      </div>
      {% endfor %}
    </div>
    {% if not thinking %}<div class="empty">No decisions recorded yet</div>{% endif %}
  </section>

  <section>
    <h2>Agents</h2>
    <div class="agent-cards">
      {% for a in agents %}
      <div class="agent-card {{ a.status }}">
        <div class="name"><a href="/agent/{{ a.name }}" style="color:var(--teal);text-decoration:none">{{ a.name }} &rarr;</a></div>
        <div class="balance">${{ "%.2f"|format(a.current_balance) }}</div>
        <div class="tier">cash available &middot; tier ${{ "%.0f"|format(a.min_capital) }} &rarr; ${{ "%.0f"|format(a.max_capital) }}</div>
        {% set spark = sparklines.get(a.name) %}
        {% if spark %}
        <svg class="spark" viewBox="0 0 240 32" preserveAspectRatio="none" width="100%" height="32">
          <polyline points="{{ spark.points }}" fill="none" stroke="{{ 'var(--green)' if spark.up else 'var(--rose)' }}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"></polyline>
        </svg>
        {% else %}
        <div class="spark-empty"></div>
        {% endif %}
        {% set held = holdings_by_agent.get(a.name, []) %}
        {% if held %}
        <div class="holdings">
          <div class="holdings-label">Currently holding</div>
          {% for h in held %}
          <div class="holding-row">
            <span class="h-sym">{{ h.symbol }}</span>
            <span class="h-qty">{{ "%.4f"|format(h.qty) }} @ ${{ "%.4f"|format(h.entry) }}</span>
            {% if h.change_pct is not none %}
            <span class="h-chg {{ 'up' if h.change_pct >= 0 else 'down' }}">
              {{ "+" if h.change_pct >= 0 else "" }}{{ "%.1f"|format(h.change_pct) }}%
            </span>
            <span class="h-val">${{ "%.2f"|format(h.value) }}</span>
            {% else %}
            <span class="h-chg pending">awaiting price</span>
            <span class="h-val">${{ "%.2f"|format(h.value) }}</span>
            {% endif %}
          </div>
          {% endfor %}
          {% set total = held | sum(attribute='value') %}
          <div class="holdings-total">Invested ${{ "%.2f"|format(total) }} &middot; total ${{ "%.2f"|format(total + a.current_balance|float) }}</div>
        </div>
        {% else %}
        <div class="holdings"><div class="holdings-label">Holding nothing &mdash; all cash</div></div>
        {% endif %}
        <div class="meta">{{ a.status }}{% if a.parent_name %} &middot; from {{ a.parent_name }}{% endif %}</div>
      </div>
      {% endfor %}
    </div>
    {% if not agents %}<div class="empty">No agents yet</div>{% endif %}
  </section>

  <section>
    <h2>Model usage &amp; cost</h2>
    {% if usage_days %}
    <table style="width:100%;border-collapse:collapse;background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden">
      <tr>
        <th style="text-align:left;font-weight:500;color:var(--text-faint);font-size:0.72rem;padding:0.65rem 1rem;border-bottom:1px solid var(--border);background:var(--surface-2)">Date</th>
        <th style="text-align:right;font-weight:500;color:var(--text-faint);font-size:0.72rem;padding:0.65rem 1rem;border-bottom:1px solid var(--border);background:var(--surface-2)">Calls</th>
        <th style="text-align:right;font-weight:500;color:var(--text-faint);font-size:0.72rem;padding:0.65rem 1rem;border-bottom:1px solid var(--border);background:var(--surface-2)">Tokens in</th>
        <th style="text-align:right;font-weight:500;color:var(--text-faint);font-size:0.72rem;padding:0.65rem 1rem;border-bottom:1px solid var(--border);background:var(--surface-2)">Tokens out</th>
        <th style="text-align:right;font-weight:500;color:var(--text-faint);font-size:0.72rem;padding:0.65rem 1rem;border-bottom:1px solid var(--border);background:var(--surface-2)">Per call</th>
        <th style="text-align:right;font-weight:500;color:var(--text-faint);font-size:0.72rem;padding:0.65rem 1rem;border-bottom:1px solid var(--border);background:var(--surface-2)">USD</th>
        <th style="text-align:right;font-weight:500;color:var(--text-faint);font-size:0.72rem;padding:0.65rem 1rem;border-bottom:1px solid var(--border);background:var(--surface-2)">AUD</th>
      </tr>
      {% for d in usage_days %}
      <tr>
        <td style="padding:0.6rem 1rem;border-bottom:1px solid var(--border);font-family:var(--mono);font-size:0.8rem">{{ d.day }}</td>
        <td style="padding:0.6rem 1rem;border-bottom:1px solid var(--border);font-family:var(--mono);font-size:0.8rem;text-align:right">{{ d.calls }}</td>
        <td style="padding:0.6rem 1rem;border-bottom:1px solid var(--border);font-family:var(--mono);font-size:0.8rem;text-align:right;color:var(--text-dim)">{{ "{:,}".format(d.inp) }}</td>
        <td style="padding:0.6rem 1rem;border-bottom:1px solid var(--border);font-family:var(--mono);font-size:0.8rem;text-align:right;color:var(--text-dim)">{{ "{:,}".format(d.outp) }}</td>
        <td style="padding:0.6rem 1rem;border-bottom:1px solid var(--border);font-family:var(--mono);font-size:0.8rem;text-align:right;color:var(--text-faint)">${{ "%.4f"|format(d.per_call) }}</td>
        <td style="padding:0.6rem 1rem;border-bottom:1px solid var(--border);font-family:var(--mono);font-size:0.8rem;text-align:right">${{ "%.4f"|format(d.usd) }}</td>
        <td style="padding:0.6rem 1rem;border-bottom:1px solid var(--border);font-family:var(--mono);font-size:0.8rem;text-align:right;font-weight:500">A${{ "%.4f"|format(d.aud) }}</td>
      </tr>
      {% endfor %}
      {% set t_usd = usage_days | sum(attribute='usd') %}
      {% set t_calls = usage_days | sum(attribute='calls') %}
      <tr style="background:var(--surface-2)">
        <td style="padding:0.6rem 1rem;font-family:var(--mono);font-size:0.8rem;font-weight:600">Total</td>
        <td style="padding:0.6rem 1rem;font-family:var(--mono);font-size:0.8rem;text-align:right;font-weight:600">{{ t_calls }}</td>
        <td colspan="3"></td>
        <td style="padding:0.6rem 1rem;font-family:var(--mono);font-size:0.8rem;text-align:right;font-weight:600">${{ "%.4f"|format(t_usd) }}</td>
        <td style="padding:0.6rem 1rem;font-family:var(--mono);font-size:0.8rem;text-align:right;font-weight:600">A${{ "%.4f"|format(t_usd * fx_rate) }}</td>
      </tr>
    </table>
    <div style="color:var(--text-faint);font-size:0.72rem;margin-top:0.6rem">
      {% if cost_actual %}Billed figures from Anthropic.{% else %}Estimated from token counts at Sonnet pricing &mdash; cross-check against console.anthropic.com.{% endif %}
      Converted at {{ "%.4f"|format(fx_rate) }}{% if not fx_live %} (fallback rate){% endif %}.
    </div>
    {% else %}
    <div class="empty">No model calls recorded yet</div>
    {% endif %}
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
        position_rows = query("""
            SELECT p.*, a.name AS agent_name FROM positions p
            JOIN agents a ON a.id = p.agent_id
            WHERE p.qty > 0
            ORDER BY p.symbol
        """)
    except Exception:
        position_rows = []

    holdings_by_agent = {}
    for p_ in position_rows:
        entry = float(p_["avg_entry_price"] or 0)
        last = float(p_["last_price"]) if p_.get("last_price") else None
        qty = float(p_["qty"])
        holdings_by_agent.setdefault(p_["agent_name"], []).append({
            "symbol": p_["symbol"],
            "qty": qty,
            "entry": entry,
            "last": last,
            "value": qty * last if last else qty * entry,
            "change_pct": ((last - entry) / entry * 100) if (last and entry) else None,
            "stale": p_.get("last_price_at") is None,
        })

    # Real spend if the Controller has an admin key; otherwise fall back to an
    # estimate from token counts. The dashboard says plainly which it is showing.
    fx_rate, fx_live = 1.50, False
    try:
        fx = query("SELECT rate, live FROM fx_rates WHERE pair = 'USDAUD'")
        if fx:
            fx_rate, fx_live = float(fx[0]["rate"]), bool(fx[0]["live"])
    except Exception:
        pass

    cost_actual = True
    try:
        row = query("""
            SELECT COALESCE(SUM(usd),0) AS wk FROM api_costs
            WHERE day >= CURRENT_DATE - INTERVAL '6 days'
        """)[0]
        spend_usd_week = float(row["wk"])
        today_row = query("SELECT COALESCE(SUM(usd),0) AS d FROM api_costs WHERE day = CURRENT_DATE")[0]
        spend_usd_today = float(today_row["d"])
        if spend_usd_week == 0:
            raise ValueError("no cost rows yet")
    except Exception:
        cost_actual = False
        try:
            t = query("""
                SELECT COALESCE(SUM(input_tokens),0) AS i, COALESCE(SUM(output_tokens),0) AS o,
                       COALESCE(SUM(cache_read_tokens),0) AS cr, COALESCE(SUM(cache_creation_tokens),0) AS cw
                FROM api_usage WHERE created_at >= CURRENT_DATE - INTERVAL '6 days'
            """)[0]
            spend_usd_week = (t["i"]/1e6*USD_PER_MTOK_INPUT + t["o"]/1e6*USD_PER_MTOK_OUTPUT
                              + t["cr"]/1e6*USD_PER_MTOK_CACHE_READ
                              + t["cw"]/1e6*USD_PER_MTOK_CACHE_WRITE)
            t2 = query("""
                SELECT COALESCE(SUM(input_tokens),0) AS i, COALESCE(SUM(output_tokens),0) AS o
                FROM api_usage WHERE created_at::date = CURRENT_DATE
            """)[0]
            spend_usd_today = (t2["i"]/1e6*USD_PER_MTOK_INPUT + t2["o"]/1e6*USD_PER_MTOK_OUTPUT)
        except Exception:
            spend_usd_week = spend_usd_today = 0.0

    spend_aud_week = spend_usd_week * fx_rate
    spend_aud_today = spend_usd_today * fx_rate

    try:
        usage_rows = query("""
            SELECT created_at::date AS day,
                   COUNT(*) AS calls,
                   COALESCE(SUM(input_tokens),0) AS inp,
                   COALESCE(SUM(output_tokens),0) AS outp,
                   COALESCE(SUM(cache_read_tokens),0) AS cread,
                   COALESCE(SUM(cache_creation_tokens),0) AS cwrite
            FROM api_usage
            GROUP BY 1 ORDER BY 1 DESC LIMIT 10
        """)
    except Exception:
        usage_rows = []

    usage_days = []
    for r in usage_rows:
        # Cache reads bill at 10% of input, cache writes at 125%. Falls back to
        # plain input/output pricing for rows written before caching existed.
        usd = (float(r["inp"])/1e6*USD_PER_MTOK_INPUT + float(r["outp"])/1e6*USD_PER_MTOK_OUTPUT
               + float(r["cread"] or 0)/1e6*USD_PER_MTOK_CACHE_READ
               + float(r["cwrite"] or 0)/1e6*USD_PER_MTOK_CACHE_WRITE)
        usage_days.append({
            "day": r["day"], "calls": r["calls"],
            "inp": int(r["inp"]), "outp": int(r["outp"]),
            "usd": usd, "aud": usd * fx_rate,
            "per_call": usd / r["calls"] if r["calls"] else 0,
        })

    try:
        calls_today = query(
            "SELECT COUNT(*) AS c FROM api_usage WHERE created_at::date = CURRENT_DATE"
        )[0]["c"]
    except Exception:
        calls_today = 0

    try:
        watching = query("SELECT * FROM shortlist_cache ORDER BY pct_change DESC NULLS LAST")
    except Exception:
        watching = []

    try:
        thinking = query("""
            SELECT DISTINCT ON (a.id) a.name AS agent_name, d.action, d.symbol, d.reasoning, d.created_at
            FROM decisions d
            JOIN agents a ON a.id = d.agent_id
            WHERE a.status = 'active'
            ORDER BY a.id, d.created_at DESC
        """)
    except Exception:
        thinking = []

    active_agents = [a for a in agents if a["status"] == "active"]
    total_balance = sum(float(a["current_balance"]) for a in active_agents) + float(pool)

    sparklines = {}
    try:
        eq_rows = query("""
            SELECT agent_id, total, taken_at FROM equity_snapshots
            WHERE agent_id = ANY(%s)
            ORDER BY taken_at DESC
        """, ([a["id"] for a in active_agents],))
    except Exception:
        eq_rows = []
    by_agent_id = {}
    for r in eq_rows:
        by_agent_id.setdefault(r["agent_id"], [])
        if len(by_agent_id[r["agent_id"]]) < 30:
            by_agent_id[r["agent_id"]].append(float(r["total"]))
    for a in active_agents:
        vals = by_agent_id.get(a["id"], [])[::-1]
        if len(vals) < 2:
            continue
        lo, hi = min(vals), max(vals)
        span = (hi - lo) or 1.0
        pts = []
        for i, v in enumerate(vals):
            x = i / (len(vals) - 1) * 240
            y = 28 - ((v - lo) / span * 24)
            pts.append(f"{x:.1f},{y:.1f}")
        sparklines[a["name"]] = {"points": " ".join(pts), "up": vals[-1] >= vals[0]}

    return render_template_string(
        TEMPLATE, agents=agents, trades=trades, agent_count=len(active_agents),
        total_balance=total_balance, pool_balance=float(pool), trades_today=trades_today,
        symbol_info=SYMBOL_INFO, calls_today=calls_today, daily_call_budget=DAILY_CALL_BUDGET,
        watching=watching, thinking=thinking, holdings_by_agent=holdings_by_agent,
        spend_aud_week=spend_aud_week, spend_aud_today=spend_aud_today, usage_days=usage_days,
        spend_usd_week=spend_usd_week, cost_actual=cost_actual,
        fx_rate=fx_rate, fx_live=fx_live, sparklines=sparklines,
    )



# ---------------------------------------------------------------------------
# Agent detail page. Shares the dark theme and token names from the main
# dashboard so both pages read as one product, in a spec-rail + main layout.
# The tokens are inlined below because this dashboard serves no static files
# - it is a single Flask module by design.
# ---------------------------------------------------------------------------

DETAIL_TOKENS = """
:root{
  --bg:#0a0e16; --rail:#0d1220; --surface:#10151f; --surface-2:#171e2c; --border:#1c2334;
  --text:#e4e8f1; --text-dim:#8993a8; --text-faint:#4a5266;
  --teal:#2dd4bf; --amber:#f0a94e; --green:#4ade80; --rose:#fb7185;
  --sans:'Inter',-apple-system,sans-serif; --mono:'JetBrains Mono',ui-monospace,monospace;
}
*,*::before,*::after{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:var(--sans);font-size:15px;line-height:1.55}
a{color:var(--teal);text-decoration:none}
a:hover{text-decoration:underline}
:focus-visible{outline:2px solid var(--teal);outline-offset:2px}
::selection{background:rgba(45,212,191,0.28)}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.kick{font-size:10.5px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--text-faint)}
.r2{height:1px;background:var(--border)}
.tag{display:inline-flex;align-items:center;font-family:var(--mono);font-size:11px;letter-spacing:.02em;padding:3px 10px;border-radius:5px}
.tag-accent{background:rgba(74,222,128,0.12);color:var(--green)}
.tag-outline{border:1px solid var(--border);color:var(--text-dim)}
.spec-row{display:flex;justify-content:space-between;padding:9px 0;font-size:12.5px;border-bottom:1px solid var(--border)}
.spec-row span:first-child{color:var(--text-faint)}
.spec-row span:last-child{font-weight:600;font-family:var(--mono)}
.log-row{display:grid;grid-template-columns:72px 8px 84px minmax(0,1fr) 116px;gap:14px;
         align-items:baseline;padding:11px 0;border-top:1px solid var(--border)}
.log-row:hover{background:var(--surface-2)}
"""

AGENT_DETAIL_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="30">
<title>{{ agent.name }} — Trading desk</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>__TOKENS__</style>
</head>
<body>
<div style="display:grid;grid-template-columns:288px minmax(0,1fr);min-height:100vh">

  <!-- spec rail -->
  <div style="border-right:1px solid var(--border);padding:26px 24px 30px;background:var(--rail)">
    <div style="font-weight:600;font-size:14px;margin-bottom:26px">
      <a href="/" style="color:var(--text-dim)">&larr; Trading desk</a>
    </div>
    <div class="kick" style="margin-bottom:8px">Agent</div>
    <div style="font-weight:600;font-size:26px;line-height:1.15;letter-spacing:-.01em;margin-bottom:10px">{{ agent.name }}</div>
    <div style="display:flex;gap:8px;margin-bottom:22px">
      <span class="tag tag-accent">{{ agent.status }}</span>
      <span class="tag tag-outline">{{ asset_class }}</span>
    </div>

    <div class="r2" style="margin-bottom:18px"></div>
    <div class="kick" style="margin-bottom:12px">Capital tier</div>
    <div style="display:flex;align-items:flex-end;gap:5px;height:56px;margin-bottom:8px">
      {% for step in tier_steps %}
      <div style="flex:1;height:{{ step.height }}%;border-radius:2px;background:{{ 'var(--teal)' if step.current else 'var(--border)' }}"></div>
      {% endfor %}
    </div>
    <div class="num" style="font-size:11.5px;color:var(--text-faint)">
      ${{ "%.0f"|format(agent.min_capital) }} &rarr; ${{ "%.0f"|format(agent.max_capital) }}
      &nbsp;·&nbsp; {{ tier_label }}
    </div>

    <div class="r2" style="margin:18px 0"></div>
    <div class="spec-row"><span>Cash</span><span class="num">${{ "%.2f"|format(cash) }}</span></div>
    <div class="spec-row"><span>Invested</span><span class="num">${{ "%.2f"|format(invested) }}</span></div>
    <div class="spec-row"><span>Open P&amp;L</span><span class="num" style="color:{{ 'var(--green)' if open_pl >= 0 else 'var(--rose)' }}">{{ "+" if open_pl >= 0 else "−" }}${{ "%.2f"|format(open_pl|abs) }}</span></div>
    <div class="spec-row" style="border-bottom:none"><span>Parent</span><span class="num">{{ agent.parent_name or "—" }}</span></div>

    <div class="r2" style="margin:18px 0"></div>
    <div class="kick" style="margin-bottom:10px">Burn · model calls</div>
    <div style="display:flex;align-items:baseline;gap:6px;margin-bottom:8px">
      <span class="num" style="font-weight:600;font-size:22px;color:var(--teal)">{{ calls_today }}</span>
      <span class="num" style="font-size:12px;color:var(--text-faint)">calls today, all agents</span>
    </div>

    <div class="r2" style="margin:18px 0"></div>
    <div class="kick" style="margin-bottom:10px">Sessions</div>
    <div style="display:flex;justify-content:space-between;align-items:baseline;padding:6px 0">
      <span style="font-size:12px;color:var(--text-faint)">New York</span>
      <span class="num" id="nyc" style="font-weight:600;font-size:15px">--:--:--</span>
    </div>
    <div id="nyc-state" style="font-weight:600;font-size:10.5px;letter-spacing:.1em;color:var(--amber);margin-bottom:12px"></div>
    <div style="display:flex;justify-content:space-between;align-items:baseline;padding:6px 0">
      <span style="font-size:12px;color:var(--text-faint)">Brisbane</span>
      <span class="num" id="bne" style="font-weight:600;font-size:15px">--:--:--</span>
    </div>
    <div id="bne-date" style="font-size:11.5px;color:var(--text-faint)"></div>
  </div>

  <!-- main -->
  <div>
    <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:24px;padding:30px 32px 24px">
      <div>
        <div class="kick" style="margin-bottom:12px">Account value</div>
        <div class="num" style="font-weight:600;font-size:64px;line-height:.95;letter-spacing:-.02em">${{ "%.2f"|format(total_value) }}</div>
        <div class="num" style="font-size:15px;margin-top:12px;color:{{ 'var(--green)' if since_spawn >= 0 else 'var(--rose)' }}">
          {{ "+" if since_spawn >= 0 else "−" }}${{ "%.2f"|format(since_spawn|abs) }}
          &nbsp;/&nbsp; {{ "+" if since_spawn_pct >= 0 else "−" }}{{ "%.1f"|format(since_spawn_pct|abs) }}% since spawn
          <span style="color:var(--text-faint)">&nbsp;·&nbsp; running {{ running_for }}</span>
        </div>
      </div>
      <div style="text-align:left;min-width:150px">
        <div class="kick" style="margin-bottom:10px">Decisions</div>
        <div class="num" style="font-weight:600;font-size:20px">{{ decision_count }} total</div>
        <div class="num" style="font-weight:600;font-size:20px;margin-top:2px">{{ trade_count }} orders</div>
      </div>
    </div>

    {% if latest %}
    <div style="background:var(--surface);border-top:1px solid var(--border);border-bottom:1px solid var(--border);padding:24px 32px 26px;border-left:3px solid {{ 'var(--green)' if latest.action == 'buy' else ('var(--rose)' if latest.action == 'sell' else 'var(--text-faint)') }}">
      <div class="kick" style="margin-bottom:12px">
        LATEST DECISION · {{ latest.created_at.strftime("%H:%M:%S") }}
      </div>
      <div style="display:flex;align-items:baseline;gap:16px;margin-bottom:12px">
        <span class="num" style="font-weight:600;font-size:36px;letter-spacing:-.02em;color:{{ 'var(--green)' if latest.action == 'buy' else ('var(--rose)' if latest.action == 'sell' else 'var(--text)') }}">
          {{ latest.action|upper }}{{ " " + latest.symbol if latest.symbol else "" }}
        </span>
      </div>
      <p style="margin:0;max-width:74ch;font-size:15px;line-height:1.5;color:var(--text-dim);text-wrap:pretty">{{ latest.reasoning }}</p>
    </div>
    {% endif %}

    <div style="padding:24px 32px 18px">
      <div style="display:flex;align-items:baseline;justify-content:space-between;margin-bottom:8px">
        <div class="kick">Value over time · {{ history|length }} snapshots</div>
        {% if history|length > 1 %}
        <div class="num" style="font-size:11.5px;color:var(--text-faint)">
          low ${{ "%.2f"|format(hist_low) }} &nbsp;·&nbsp; high ${{ "%.2f"|format(hist_high) }}
        </div>
        {% endif %}
      </div>
      {% if history|length > 1 %}
      <svg viewBox="0 0 900 200" preserveAspectRatio="none" style="width:100%;height:200px;display:block;background:var(--surface);border:1px solid var(--border);border-radius:10px">
        <polygon points="{{ spark_fill }}" fill="rgba(45,212,191,0.14)"></polygon>
        <polyline points="{{ spark_line }}" fill="none" stroke="var(--teal)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"></polyline>
        <line x1="0" y1="190" x2="900" y2="190" stroke="var(--border)" stroke-width="1"></line>
      </svg>
      {% else %}
      <div class="num" style="padding:36px 0;font-size:13px;color:var(--text-faint);text-align:center;background:var(--surface);border:1px dashed var(--border);border-radius:10px">
        Not enough history yet — a snapshot is taken every minute.
      </div>
      {% endif %}
    </div>

    <div class="r2"></div>
    <div style="padding:22px 32px">
      <div class="kick" style="margin-bottom:14px">Holding</div>
      {% if holdings %}
      <div style="display:grid;grid-template-columns:repeat({{ 2 if holdings|length > 1 else 1 }},minmax(0,1fr));border-top:1px solid var(--border)">
        {% for h in holdings %}
        <div style="padding:16px 22px 16px 0;{% if not loop.last and holdings|length > 1 %}border-right:1px solid var(--border){% endif %}">
          <div style="display:flex;align-items:baseline;justify-content:space-between;margin-bottom:4px">
            <span style="font-family:var(--mono);font-weight:600;font-size:22px">{{ h.symbol }}</span>
            {% if h.change_pct is not none %}
            <span class="num" style="font-weight:600;font-size:22px;color:{{ 'var(--green)' if h.change_pct >= 0 else 'var(--rose)' }}">
              {{ "+" if h.change_pct >= 0 else "−" }}{{ "%.1f"|format(h.change_pct|abs) }}%
            </span>
            {% endif %}
          </div>
          <div class="num" style="font-size:12.5px;color:var(--text-faint)">
            {{ "%.4f"|format(h.qty) }} @ ${{ "%.4f"|format(h.entry) }}
            {% if h.last %}&rarr; ${{ "%.4f"|format(h.last) }}{% endif %}
            &nbsp;·&nbsp; ${{ "%.2f"|format(h.value) }}
          </div>
        </div>
        {% endfor %}
      </div>
      {% else %}
      <div class="num" style="font-size:13px;color:var(--text-faint)">
        Holding nothing — everything is sitting as cash.
      </div>
      {% endif %}
    </div>

    <div class="r2"></div>
    <div style="padding:22px 32px 28px">
      <div class="kick" style="margin-bottom:12px">Decision log · {{ decision_count }} total</div>
      {% for d in decisions %}
      <div class="log-row" {% if loop.last %}style="border-bottom:1px solid var(--border)"{% endif %}>
        <span class="num" style="font-size:12px;color:var(--text-faint)">{{ d.created_at.strftime("%H:%M:%S") }}</span>
        <span style="width:8px;height:8px;border-radius:50%;display:block;{% if d.action == 'buy' %}background:var(--green){% elif d.action == 'sell' %}background:var(--rose){% else %}border:1px solid var(--text-faint){% endif %}"></span>
        <span class="num" style="font-weight:600;font-size:13px">{{ d.symbol or "—" }}</span>
        <span style="font-size:13px;color:var(--text-dim)">{{ (d.reasoning or "")[:150] }}</span>
        <span class="num" style="font-size:12px;text-align:right;color:var(--text-faint)">{{ d.action }}</span>
      </div>
      {% else %}
      <div class="num" style="font-size:13px;color:var(--text-faint)">No decisions recorded yet.</div>
      {% endfor %}
    </div>
  </div>
</div>

<script>
function tick(){
  const f=(tz)=>new Intl.DateTimeFormat('en-US',{timeZone:tz,hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit',weekday:'short'});
  const parts=(tz)=>{const o={};f(tz).formatToParts(new Date()).forEach(p=>o[p.type]=p.value);return o};
  const n=parts('America/New_York'), b=parts('Australia/Brisbane');
  document.getElementById('nyc').textContent=`${n.hour}:${n.minute}:${n.second}`;
  document.getElementById('bne').textContent=`${b.hour}:${b.minute}:${b.second}`;
  document.getElementById('bne-date').textContent=new Date().toLocaleDateString('en-US',{timeZone:'Australia/Brisbane',weekday:'long',day:'numeric',month:'short'});
  const mins=parseInt(n.hour,10)*60+parseInt(n.minute,10);
  const wd=!['Sat','Sun'].includes(n.weekday);
  let st='CLOSED';
  if(!wd) st='WEEKEND';
  else if(mins>=570&&mins<960) st='OPEN';
  else if(mins>=240&&mins<570) st='PRE-MARKET';
  else if(mins>=960&&mins<1200) st='AFTER HOURS';
  document.getElementById('nyc-state').textContent=st;
}
tick();setInterval(tick,1000);
</script>
</body>
</html>
""".replace("__TOKENS__", DETAIL_TOKENS)


@app.route("/agent/<name>")
def agent_detail(name):
    rows = query("SELECT * FROM agents WHERE name = %s", (name,))
    if not rows:
        return f"No agent called {name}", 404
    agent = rows[0]

    try:
        pos_rows = query("SELECT * FROM positions WHERE agent_id = %s AND qty > 0 ORDER BY symbol", (agent["id"],))
    except Exception:
        pos_rows = []
    holdings = []
    for p_ in pos_rows:
        entry = float(p_["avg_entry_price"] or 0)
        last = float(p_["last_price"]) if p_.get("last_price") else None
        qty = float(p_["qty"])
        holdings.append({
            "symbol": p_["symbol"], "qty": qty, "entry": entry, "last": last,
            "value": qty * (last or entry),
            "change_pct": ((last - entry) / entry * 100) if (last and entry) else None,
            "cost": qty * entry,
        })

    cash = float(agent["current_balance"])
    invested = sum(h["value"] for h in holdings)
    total_value = cash + invested
    open_pl = invested - sum(h["cost"] for h in holdings)

    spawn_capital = float(agent["min_capital"])
    since_spawn = total_value - spawn_capital
    since_spawn_pct = (since_spawn / spawn_capital * 100) if spawn_capital else 0

    delta = datetime.now(timezone.utc) - agent["created_at"]
    days, rem = delta.days, delta.seconds
    running_for = f"{days}d {rem // 3600}h" if days else f"{rem // 3600}h {(rem % 3600) // 60}m"

    try:
        decisions = query(
            "SELECT * FROM decisions WHERE agent_id = %s ORDER BY created_at DESC LIMIT 12", (agent["id"],))
        decision_count = query(
            "SELECT COUNT(*) AS c FROM decisions WHERE agent_id = %s", (agent["id"],))[0]["c"]
    except Exception:
        decisions, decision_count = [], 0
    latest = decisions[0] if decisions else None

    try:
        trade_count = query("SELECT COUNT(*) AS c FROM trades WHERE agent_id = %s", (agent["id"],))[0]["c"]
    except Exception:
        trade_count = 0
    try:
        calls_today = query("SELECT COUNT(*) AS c FROM api_usage WHERE created_at::date = CURRENT_DATE")[0]["c"]
    except Exception:
        calls_today = 0

    try:
        history = query("""
            SELECT total, taken_at FROM equity_snapshots
            WHERE agent_id = %s ORDER BY taken_at DESC LIMIT 120
        """, (agent["id"],))[::-1]
    except Exception:
        history = []

    spark_line = spark_fill = ""
    hist_low = hist_high = 0.0
    if len(history) > 1:
        vals = [float(h["total"]) for h in history]
        hist_low, hist_high = min(vals), max(vals)
        span = (hist_high - hist_low) or 1.0
        pts = []
        for i, v in enumerate(vals):
            x = i / (len(vals) - 1) * 900
            y = 180 - ((v - hist_low) / span * 160)
            pts.append(f"{x:.1f},{y:.1f}")
        spark_line = " ".join(pts)
        spark_fill = f"{pts[0].split(',')[0]},190 " + " ".join(pts) + f" {pts[-1].split(',')[0]},190"

    lo, hi = float(agent["min_capital"]), float(agent["max_capital"])
    frac = 0 if hi == lo else max(0.0, min(1.0, (total_value - lo) / (hi - lo)))
    current_step = min(4, int(frac * 5))
    tier_steps = [{"height": 20 + i * 20, "current": i == current_step} for i in range(5)]

    return render_template_string(
        AGENT_DETAIL_TEMPLATE,
        agent=agent, asset_class=(agent.get("strategy") or {}).get("asset_class", "stocks"),
        cash=cash, invested=invested, total_value=total_value, open_pl=open_pl,
        since_spawn=since_spawn, since_spawn_pct=since_spawn_pct, running_for=running_for,
        holdings=holdings, decisions=decisions, decision_count=decision_count,
        trade_count=trade_count, calls_today=calls_today, latest=latest,
        history=history, spark_line=spark_line, spark_fill=spark_fill,
        hist_low=hist_low, hist_high=hist_high,
        tier_steps=tier_steps, tier_label=f"tier {current_step + 1} of 5",
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8095)
