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

# Sonnet 4.6 published pricing per 1M tokens - used only to estimate spend from
# tokens actually used. Not the same as your real balance (Anthropic doesn't
# expose that via API) but it shows burn rate, which is the useful part overnight.
# Free tier has no per-token cost, so the useful number is calls against the daily cap.
# Keep this in sync with daily_call_budget in the tier ConfigMap.
DAILY_CALL_BUDGET = int(os.environ.get("DAILY_CALL_BUDGET", "1400"))

MODERNIST_TOKENS = """
:root{
  --color-bg:#f3f2f2; --color-surface:#eae9e9; --color-text:#201e1d;
  --color-accent:#ec3013; --color-divider:color-mix(in srgb,#201e1d 40%,transparent);
  --color-neutral-100:#f8f4f4; --color-accent-100:#fff2ef; --color-accent-200:#ffe0d9;
  --color-accent-700:#ae1800; --color-accent-800:#7c1405;
  --font-heading:"Archivo",system-ui,sans-serif; --font-body:"Archivo",system-ui,sans-serif;
  --font-heading-weight:800;
  --space-2:8px; --space-3:12px; --space-4:16px;
  --radius-md:0px;
}
*,*::before,*::after{box-sizing:border-box}
body{margin:0;background:var(--color-bg);color:var(--color-text);
     font-family:var(--font-body);font-size:15px;line-height:1.55}
h1,h2,h3{font-family:var(--font-heading);font-weight:var(--font-heading-weight);
         line-height:1.12;letter-spacing:-.015em;margin:0}
a{color:var(--color-accent);text-decoration:none}
a:hover{color:var(--color-accent-700)}
:focus-visible{outline:2px solid var(--color-accent);outline-offset:2px}
::selection{background:color-mix(in srgb,var(--color-accent) 30%,transparent)}
.num{font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.kick{font:800 10px var(--font-heading);letter-spacing:.12em;text-transform:uppercase;
      color:color-mix(in srgb,var(--color-text) 55%,transparent)}
.r2{height:2px;background:var(--color-divider)}
.tag{display:inline-flex;align-items:center;font-size:11px;letter-spacing:.02em;
     padding:3px 10px;border-radius:0}
.tag-accent{background:var(--color-accent-100);color:var(--color-accent-800)}
.tag-outline{border:1px solid var(--color-accent);color:var(--color-accent)}
.spec-row{display:flex;justify-content:space-between;padding:9px 0;font-size:12.5px;
          border-bottom:1px solid color-mix(in srgb,var(--color-text) 18%,transparent)}
.spec-row span:first-child{color:color-mix(in srgb,var(--color-text) 55%,transparent)}
.spec-row span:last-child{font-weight:700}
.log-row{display:grid;grid-template-columns:72px 8px 84px minmax(0,1fr) 116px;gap:14px;
         align-items:baseline;padding:11px 0;
         border-top:1px solid color-mix(in srgb,var(--color-text) 18%,transparent)}
.flog-row{display:grid;grid-template-columns:72px 8px 96px 76px minmax(0,1fr) 100px;gap:14px;
         align-items:baseline;padding:11px 0;
         border-top:1px solid color-mix(in srgb,var(--color-text) 18%,transparent)}
.poster{background:var(--color-accent);color:var(--color-bg);padding:26px 32px}
.dot{width:8px;height:8px;display:inline-block;flex-shrink:0}
.dot.buy{background:var(--color-accent)}
.dot.sell{background:var(--color-text)}
.dot.hold{border:1px solid var(--color-divider)}
[hidden]{display:none!important}
"""

TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="20">
<title>Trading desk</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;600;700;800;900&display=swap" rel="stylesheet">
<style>__TOKENS__
.shell{display:flex;min-height:100vh}
.rail{width:250px;flex-shrink:0;border-right:2px solid var(--color-divider);
      background:var(--color-neutral-100);padding:26px 22px 30px;
      position:sticky;top:0;height:100vh;overflow-y:auto}
.brand{font:800 16px var(--font-heading);letter-spacing:-.01em;margin-bottom:24px}
.brand a{color:var(--color-text)}
.tabnav{display:flex;flex-direction:column;gap:2px;margin-bottom:22px}
.tabnav a{display:block;padding:9px 10px;font:700 13px var(--font-heading);
          color:color-mix(in srgb,var(--color-text) 62%,transparent);
          border-left:3px solid transparent}
.tabnav a:hover{color:var(--color-text)}
.tabnav a.active{color:var(--color-accent-700);border-left-color:var(--color-accent);
                  background:var(--color-accent-100)}
.shell-main{flex:1;min-width:0;max-width:1360px;margin:0 auto}
.tab-panel{padding:34px 40px 52px}
.hero-row{display:flex;flex-wrap:wrap;align-items:flex-start;justify-content:space-between;gap:28px;margin-bottom:24px}
.chips{display:flex;flex-wrap:wrap;gap:12px}
.chip{border:1px solid color-mix(in srgb,var(--color-text) 30%,transparent);padding:14px 18px;min-width:190px;flex:1}
.chip .sym{font:800 15px var(--font-heading)}
.chip .sub{font-size:12.5px;margin-top:5px;color:color-mix(in srgb,var(--color-text) 55%,transparent)}
.card-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));
           border-top:2px solid var(--color-divider);border-left:2px solid var(--color-divider)}
.acard{padding:18px 20px;border-right:2px solid var(--color-divider);border-bottom:2px solid var(--color-divider)}
.empty{padding:28px 0;color:color-mix(in srgb,var(--color-text) 45%,transparent);font-size:13px}
</style>
</head>
<body>

<div class="shell">
  <aside class="rail">
    <div class="brand"><a href="/">Trading desk</a></div>
    <nav class="tabnav">
      <a href="#overview" data-tab="overview" class="active">Overview</a>
      <a href="#market" data-tab="market">Market</a>
      <a href="#thinking" data-tab="thinking">Thinking</a>
      <a href="#trades" data-tab="trades">Trades</a>
    </nav>

    <div class="r2" style="margin-bottom:16px"></div>
    <div class="spec-row"><span>Active agents</span><span class="num">{{ agent_count }}</span></div>
    <div class="spec-row"><span>Unallocated pool</span><span class="num">${{ "%.2f"|format(pool_balance) }}</span></div>
    <div class="spec-row"><span>Trades today</span><span class="num">{{ trades_today }}</span></div>
    <div class="spec-row" style="border-bottom:none"><span>Model calls</span><span class="num">{{ calls_today }} / {{ daily_call_budget }}</span></div>

    <div class="r2" style="margin:18px 0"></div>
    <div class="kick" style="margin-bottom:10px">Sessions</div>
    <div style="display:flex;justify-content:space-between;align-items:baseline;padding:6px 0">
      <span style="font-size:12px;color:color-mix(in srgb,var(--color-text) 55%,transparent)">New York</span>
      <span class="num" id="nyc" style="font-weight:700;font-size:15px">--:--:--</span>
    </div>
    <div id="nyc-state" style="font:800 10.5px var(--font-heading);letter-spacing:.1em;color:var(--color-accent-700);margin-bottom:12px"></div>
    <div style="display:flex;justify-content:space-between;align-items:baseline;padding:6px 0">
      <span style="font-size:12px;color:color-mix(in srgb,var(--color-text) 55%,transparent)">Brisbane</span>
      <span class="num" id="bne" style="font-weight:700;font-size:15px">--:--:--</span>
    </div>
    <div id="bne-date" style="font-size:11.5px;color:color-mix(in srgb,var(--color-text) 50%,transparent)"></div>
  </aside>

  <main class="shell-main">

    <section id="overview" class="tab-panel">
      <div class="hero-row">
        <div>
          <div class="kick" style="margin-bottom:12px">Total ledger balance</div>
          <div class="num" style="font:900 76px/.9 var(--font-heading);letter-spacing:-.035em">${{ "%.2f"|format(total_balance) }}</div>
          <div class="num" style="font-size:15px;margin-top:12px;color:color-mix(in srgb,var(--color-text) 65%,transparent)">
            {{ "+" if total_delta >= 0 else "−" }}${{ "%.2f"|format(total_delta|abs) }}
            &nbsp;/&nbsp; {{ "+" if total_delta_pct >= 0 else "−" }}{{ "%.1f"|format(total_delta_pct|abs) }}% since deployed
            &nbsp;·&nbsp; {{ agent_count }} active agent{{ "s" if agent_count != 1 else "" }}
          </div>
        </div>
        <div style="text-align:left;min-width:150px">
          <div class="kick" style="margin-bottom:10px">Today</div>
          <div class="num" style="font:800 20px var(--font-heading)">{{ trades_today }} trades</div>
          <div class="num" style="font:800 20px var(--font-heading);margin-top:2px">{{ calls_today }} / {{ daily_call_budget }} calls</div>
        </div>
      </div>

      {% if fleet_latest %}
      <div class="poster" style="margin-bottom:28px">
        <div style="font:800 10px var(--font-heading);letter-spacing:.14em;margin-bottom:12px;opacity:.85">
          LIVE · {{ fleet_latest.agent_name }} · {{ fleet_latest.created_at.strftime("%H:%M:%S") }}
        </div>
        <div class="num" style="font:900 40px/1 var(--font-heading);letter-spacing:-.03em;margin-bottom:12px">
          {{ fleet_latest.action|upper }}{{ " " + fleet_latest.symbol if fleet_latest.symbol else "" }}
        </div>
        <p style="margin:0;max-width:80ch;font-size:15px;line-height:1.5;text-wrap:pretty">{{ fleet_latest.reasoning }}</p>
      </div>
      {% endif %}

      <div style="margin-bottom:28px">
        <div style="display:flex;align-items:baseline;justify-content:space-between;margin-bottom:8px">
          <div class="kick">Fleet value over time · {{ fleet_history|length }} snapshots</div>
          {% if fleet_history|length > 1 %}
          <div class="num" style="font-size:11.5px;color:color-mix(in srgb,var(--color-text) 55%,transparent)">
            low ${{ "%.2f"|format(fleet_hist_low) }} &nbsp;·&nbsp; high ${{ "%.2f"|format(fleet_hist_high) }}
          </div>
          {% endif %}
        </div>
        {% if fleet_history|length > 1 %}
        <svg viewBox="0 0 900 200" preserveAspectRatio="none" style="width:100%;height:200px;display:block">
          <polygon points="{{ fleet_spark_fill }}" fill="var(--color-accent-200)"></polygon>
          <polyline points="{{ fleet_spark_line }}" fill="none" stroke="var(--color-accent)" stroke-width="3"></polyline>
          <line x1="0" y1="190" x2="900" y2="190" stroke="var(--color-text)" stroke-width="2"></line>
        </svg>
        {% else %}
        <div class="num" style="padding:30px 0;font-size:13px;color:color-mix(in srgb,var(--color-text) 45%,transparent)">
          Not enough history yet — a snapshot is taken every minute.
        </div>
        {% endif %}
      </div>

      <div class="kick" style="margin-bottom:0">Agents</div>
      <div class="card-grid" style="margin-top:14px">
        {% for a in agents %}
        <div class="acard">
          <div style="display:flex;align-items:baseline;justify-content:space-between;margin-bottom:8px">
            <a href="/agent/{{ a.name }}" style="font:800 16px var(--font-heading)">{{ a.name }} &rarr;</a>
            <span class="tag {{ 'tag-accent' if a.status == 'active' else 'tag-outline' }}">{{ a.status }}</span>
          </div>
          <div class="num" style="font:800 26px var(--font-heading);margin-bottom:6px">${{ "%.2f"|format(a.current_balance) }}</div>
          <div style="display:flex;align-items:flex-end;gap:3px;height:22px;margin-bottom:6px">
            {% for step in tier_steps_by_agent[a.name] %}
            <div style="flex:1;height:{{ step.height }}%;background:{{ 'var(--color-accent)' if step.current else 'color-mix(in srgb,var(--color-text) 20%,transparent)' }}"></div>
            {% endfor %}
          </div>
          <div class="num" style="font-size:11px;color:color-mix(in srgb,var(--color-text) 55%,transparent);margin-bottom:12px">
            tier ${{ "%.0f"|format(a.min_capital) }} &rarr; ${{ "%.0f"|format(a.max_capital) }}{% if a.parent_name %} &middot; from {{ a.parent_name }}{% endif %}
          </div>
          {% set held = holdings_by_agent.get(a.name, []) %}
          {% if held %}
          <div class="r2" style="margin-bottom:10px"></div>
          {% for h in held %}
          <div style="display:flex;justify-content:space-between;font-size:12.5px;padding:4px 0">
            <span class="num" style="font-weight:700">{{ h.symbol }}</span>
            <span class="num" style="color:color-mix(in srgb,var(--color-text) 55%,transparent)">{{ "%.4f"|format(h.qty) }} @ ${{ "%.4f"|format(h.entry) }}</span>
            {% if h.change_pct is not none %}
            <span class="num" style="font-weight:700{{ ';color:var(--color-accent-700)' if h.change_pct < 0 else '' }}">{{ "+" if h.change_pct >= 0 else "" }}{{ "%.1f"|format(h.change_pct) }}%</span>
            {% else %}
            <span class="num" style="color:color-mix(in srgb,var(--color-text) 45%,transparent)">awaiting price</span>
            {% endif %}
          </div>
          {% endfor %}
          {% else %}
          <div class="num" style="font-size:12px;color:color-mix(in srgb,var(--color-text) 45%,transparent)">Holding nothing — all cash.</div>
          {% endif %}
        </div>
        {% endfor %}
      </div>
      {% if not agents %}<div class="empty">No agents yet.</div>{% endif %}
    </section>

    <section id="market" class="tab-panel" hidden>
      <div class="kick" style="margin-bottom:14px">Watchlist</div>
      <div class="chips" style="margin-bottom:32px">
        {% for sym, info in symbol_info.items() %}
        <div class="chip">
          <div class="sym">{{ sym }}</div>
          <div class="sub">{{ info.desc }}</div>
        </div>
        {% endfor %}
      </div>

      <div class="kick" style="margin-bottom:14px">Currently watching · screened shortlist</div>
      <div class="chips">
        {% for w in watching %}
        <div class="chip">
          <div class="sym">{{ w.symbol }}</div>
          <div class="sub">${{ "%.2f"|format(w.price) if w.price else "-" }}
            &middot; {{ "+" if (w.pct_change or 0) >= 0 else "" }}{{ "%.2f"|format(w.pct_change) if w.pct_change is not none else "-" }}% over the trend window</div>
        </div>
        {% endfor %}
      </div>
      {% if not watching %}<div class="empty">No shortlist yet — needs a few discovery scans and an agent cycle first.</div>{% endif %}
    </section>

    <section id="thinking" class="tab-panel" hidden>
      <div class="kick" style="margin-bottom:0">What each agent is thinking right now</div>
      <div class="card-grid" style="margin-top:14px;margin-bottom:32px">
        {% for t in thinking %}
        <div class="acard">
          <div style="display:flex;align-items:baseline;gap:10px;margin-bottom:10px">
            <span class="dot {{ t.action }}"></span>
            <span class="num" style="font:800 15px var(--font-heading);text-transform:uppercase">{{ t.action }}</span>
            {% if t.symbol %}<span class="num" style="font:800 15px var(--font-heading)">{{ t.symbol }}</span>{% endif %}
          </div>
          <div style="font-size:13px;color:color-mix(in srgb,var(--color-text) 72%,transparent);margin-bottom:10px">{{ t.reasoning or "" }}</div>
          <div class="num" style="font-size:11.5px;color:color-mix(in srgb,var(--color-text) 50%,transparent)">
            <a href="/agent/{{ t.agent_name }}">{{ t.agent_name }}</a> &middot; {{ t.created_at.strftime("%H:%M:%S") }}
          </div>
        </div>
        {% endfor %}
      </div>
      {% if not thinking %}<div class="empty" style="margin-bottom:32px">No decisions recorded yet.</div>{% endif %}

      <div class="kick" style="margin-bottom:6px">Full decision log · every agent</div>
      {% for d in decision_log %}
      <div class="flog-row" {% if loop.last %}style="border-bottom:1px solid color-mix(in srgb,var(--color-text) 18%,transparent)"{% endif %}>
        <span class="num" style="font-size:12px;color:color-mix(in srgb,var(--color-text) 50%,transparent)">{{ d.created_at.strftime("%H:%M:%S") }}</span>
        <span class="dot {{ d.action }}"></span>
        <span class="num" style="font-size:12.5px"><a href="/agent/{{ d.agent_name }}" style="color:inherit">{{ d.agent_name }}</a></span>
        <span class="num" style="font-weight:700;font-size:13px">{{ d.symbol or "—" }}</span>
        <span style="font-size:13px;color:color-mix(in srgb,var(--color-text) 72%,transparent)">{{ (d.reasoning or "")[:150] }}</span>
        <span class="num" style="font-size:12px;text-align:right;text-transform:uppercase">{{ d.action }}</span>
      </div>
      {% endfor %}
      {% if not decision_log %}<div class="empty">No decisions recorded yet.</div>{% endif %}
    </section>

    <section id="trades" class="tab-panel" hidden>
      <div class="kick" style="margin-bottom:6px">Recent trades</div>
      {% for t in trades %}
      <div class="log-row" {% if loop.last %}style="border-bottom:1px solid color-mix(in srgb,var(--color-text) 18%,transparent)"{% endif %}>
        <span class="num" style="font-size:12px;color:color-mix(in srgb,var(--color-text) 50%,transparent)">{{ t.created_at.strftime("%H:%M:%S") }}</span>
        <span class="dot {{ t.side }}"></span>
        <span class="num" style="font-weight:700;font-size:13px">{{ t.symbol }}</span>
        <span style="font-size:13px;color:color-mix(in srgb,var(--color-text) 72%,transparent)">
          <a href="/agent/{{ t.agent_name }}" style="color:inherit">{{ t.agent_name }}</a> &mdash; {{ t.reasoning or "" }}
        </span>
        <span class="num" style="font-size:12px;text-align:right">{{ t.status }}{% if t.reject_reason %} &middot; {{ t.reject_reason }}{% endif %}</span>
      </div>
      {% endfor %}
      {% if not trades %}<div class="empty">No trades yet — agents are still watching for a setup.</div>{% endif %}
    </section>

  </main>
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
  else if(mins>=570&&mins<960) st='MARKET OPEN';
  else if(mins>=240&&mins<570) st='PRE-MARKET';
  else if(mins>=960&&mins<1200) st='AFTER HOURS';
  document.getElementById('nyc-state').textContent=st;
}
tick();setInterval(tick,1000);

function showTab(id){
  document.querySelectorAll('.tab-panel').forEach(function(p){ p.hidden = (p.id !== id); });
  document.querySelectorAll('.tabnav a').forEach(function(a){ a.classList.toggle('active', a.dataset.tab === id); });
}
document.querySelectorAll('.tabnav a').forEach(function(a){
  a.addEventListener('click', function(e){
    e.preventDefault();
    history.replaceState(null, '', '#' + a.dataset.tab);
    showTab(a.dataset.tab);
  });
});
showTab((location.hash || '#overview').slice(1));
</script>

</body>
</html>
""".replace("__TOKENS__", MODERNIST_TOKENS)


def query(sql, params=()):
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        return cur.fetchall()
    finally:
        conn.close()


def _tier_steps(value, lo, hi):
    """Five-segment capital-tier bar, shared by the fleet overview cards and
    the per-agent detail page. Returns (steps, label)."""
    frac = 0 if hi == lo else max(0.0, min(1.0, (value - lo) / (hi - lo)))
    current = min(4, int(frac * 5))
    steps = [{"height": 20 + i * 20, "current": i == current} for i in range(5)]
    return steps, f"tier {current + 1} of 5"


def _spark(history, key="total"):
    """SVG polyline/polygon points for a 0-900x0-200 sparkline, plus low/high.
    Returns ("", "", 0.0, 0.0) when there isn't enough history to draw a line."""
    if len(history) < 2:
        return "", "", 0.0, 0.0
    vals = [float(h[key]) for h in history]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    pts = []
    for i, v in enumerate(vals):
        x = i / (len(vals) - 1) * 900
        y = 180 - ((v - lo) / span * 160)
        pts.append(f"{x:.1f},{y:.1f}")
    line = " ".join(pts)
    fill = f"{pts[0].split(',')[0]},190 " + line + f" {pts[-1].split(',')[0]},190"
    return line, fill, lo, hi


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

    try:
        decision_log = query("""
            SELECT d.*, a.name AS agent_name FROM decisions d
            JOIN agents a ON a.id = d.agent_id
            ORDER BY d.created_at DESC LIMIT 40
        """)
        fleet_latest = decision_log[0] if decision_log else None
    except Exception:
        decision_log, fleet_latest = [], None

    try:
        fleet_history = query("""
            SELECT date_trunc('minute', taken_at) AS taken_at, SUM(total) AS total
            FROM equity_snapshots GROUP BY 1 ORDER BY 1 DESC LIMIT 180
        """)[::-1]
    except Exception:
        fleet_history = []
    fleet_spark_line, fleet_spark_fill, fleet_hist_low, fleet_hist_high = _spark(fleet_history)

    active_agents = [a for a in agents if a["status"] == "active"]
    total_balance = sum(float(a["current_balance"]) for a in active_agents) + float(pool)
    total_starting_capital = sum(float(a["min_capital"]) for a in active_agents)
    total_delta = total_balance - total_starting_capital
    total_delta_pct = (total_delta / total_starting_capital * 100) if total_starting_capital else 0.0

    tier_steps_by_agent = {
        a["name"]: _tier_steps(float(a["current_balance"]), float(a["min_capital"]), float(a["max_capital"]))[0]
        for a in agents
    }

    return render_template_string(
        TEMPLATE, agents=agents, trades=trades, agent_count=len(active_agents),
        total_balance=total_balance, pool_balance=float(pool), trades_today=trades_today,
        symbol_info=SYMBOL_INFO, calls_today=calls_today, daily_call_budget=DAILY_CALL_BUDGET,
        watching=watching, thinking=thinking, holdings_by_agent=holdings_by_agent,
        decision_log=decision_log, fleet_latest=fleet_latest,
        fleet_history=fleet_history, fleet_spark_line=fleet_spark_line, fleet_spark_fill=fleet_spark_fill,
        fleet_hist_low=fleet_hist_low, fleet_hist_high=fleet_hist_high,
        total_delta=total_delta, total_delta_pct=total_delta_pct,
        tier_steps_by_agent=tier_steps_by_agent,
    )



# ---------------------------------------------------------------------------
# Agent detail page. Implements the "Broadsheet" option from the Modernist
# design system: light ground, Archivo, zero radius, 2px rules, one red accent.
# Shares MODERNIST_TOKENS (defined above, next to the main dashboard template)
# since both pages now use the same design system.
# ---------------------------------------------------------------------------

AGENT_DETAIL_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="30">
<title>{{ agent.name }} — Trading desk</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;600;700;800;900&display=swap" rel="stylesheet">
<style>__TOKENS__</style>
</head>
<body>
<div style="display:grid;grid-template-columns:288px minmax(0,1fr);min-height:100vh">

  <!-- spec rail -->
  <div style="border-right:2px solid var(--color-divider);padding:26px 24px 30px;background:var(--color-neutral-100)">
    <div style="font:800 14px var(--font-heading);margin-bottom:26px">
      <a href="/" style="color:var(--color-text)">&larr; Trading desk</a>
    </div>
    <div class="kick" style="margin-bottom:8px">Agent</div>
    <div style="font:800 30px/1.05 var(--font-heading);letter-spacing:-.02em;margin-bottom:10px">{{ agent.name }}</div>
    <div style="display:flex;gap:8px;margin-bottom:22px">
      <span class="tag tag-accent">{{ agent.status }}</span>
      <span class="tag tag-outline">{{ asset_class }}</span>
    </div>

    <div class="r2" style="margin-bottom:18px"></div>
    <div class="kick" style="margin-bottom:12px">Capital tier</div>
    <div style="display:flex;align-items:flex-end;gap:5px;height:56px;margin-bottom:8px">
      {% for step in tier_steps %}
      <div style="flex:1;height:{{ step.height }}%;background:{{ 'var(--color-accent)' if step.current else 'color-mix(in srgb,var(--color-text) 20%,transparent)' }}"></div>
      {% endfor %}
    </div>
    <div class="num" style="font-size:11.5px;color:color-mix(in srgb,var(--color-text) 55%,transparent)">
      ${{ "%.0f"|format(agent.min_capital) }} &rarr; ${{ "%.0f"|format(agent.max_capital) }}
      &nbsp;·&nbsp; {{ tier_label }}
    </div>

    <div class="r2" style="margin:18px 0"></div>
    <div class="spec-row"><span>Cash</span><span class="num">${{ "%.2f"|format(cash) }}</span></div>
    <div class="spec-row"><span>Invested</span><span class="num">${{ "%.2f"|format(invested) }}</span></div>
    <div class="spec-row"><span>Open P&amp;L</span><span class="num" {% if open_pl < 0 %}style="color:var(--color-accent-700)"{% endif %}>{{ "+" if open_pl >= 0 else "−" }}${{ "%.2f"|format(open_pl|abs) }}</span></div>
    <div class="spec-row" style="border-bottom:none"><span>Parent</span><span class="num">{{ agent.parent_name or "—" }}</span></div>

    <div class="r2" style="margin:18px 0"></div>
    <div class="kick" style="margin-bottom:10px">Burn · model calls</div>
    <div style="display:flex;align-items:baseline;gap:6px;margin-bottom:8px">
      <span class="num" style="font:800 22px var(--font-heading)">{{ calls_today }}</span>
      <span class="num" style="font-size:12px;color:color-mix(in srgb,var(--color-text) 50%,transparent)">calls today, all agents</span>
    </div>

    <div class="r2" style="margin:18px 0"></div>
    <div class="kick" style="margin-bottom:10px">Sessions</div>
    <div style="display:flex;justify-content:space-between;align-items:baseline;padding:6px 0">
      <span style="font-size:12px;color:color-mix(in srgb,var(--color-text) 55%,transparent)">New York</span>
      <span class="num" id="nyc" style="font-weight:700;font-size:15px">--:--:--</span>
    </div>
    <div id="nyc-state" style="font:800 10.5px var(--font-heading);letter-spacing:.1em;color:var(--color-accent-700);margin-bottom:12px"></div>
    <div style="display:flex;justify-content:space-between;align-items:baseline;padding:6px 0">
      <span style="font-size:12px;color:color-mix(in srgb,var(--color-text) 55%,transparent)">Brisbane</span>
      <span class="num" id="bne" style="font-weight:700;font-size:15px">--:--:--</span>
    </div>
    <div id="bne-date" style="font-size:11.5px;color:color-mix(in srgb,var(--color-text) 50%,transparent)"></div>
  </div>

  <!-- main -->
  <div>
    <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:24px;padding:30px 32px 24px">
      <div>
        <div class="kick" style="margin-bottom:12px">Account value</div>
        <div class="num" style="font:900 76px/.9 var(--font-heading);letter-spacing:-.035em">${{ "%.2f"|format(total_value) }}</div>
        <div class="num" style="font-size:15px;margin-top:12px;color:color-mix(in srgb,var(--color-text) 65%,transparent)">
          {{ "+" if since_spawn >= 0 else "−" }}${{ "%.2f"|format(since_spawn|abs) }}
          &nbsp;/&nbsp; {{ "+" if since_spawn_pct >= 0 else "−" }}{{ "%.1f"|format(since_spawn_pct|abs) }}% since spawn
          &nbsp;·&nbsp; running {{ running_for }}
        </div>
      </div>
      <div style="text-align:left;min-width:150px">
        <div class="kick" style="margin-bottom:10px">Decisions</div>
        <div class="num" style="font:800 20px var(--font-heading)">{{ decision_count }} total</div>
        <div class="num" style="font:800 20px var(--font-heading);margin-top:2px">{{ trade_count }} orders</div>
      </div>
    </div>

    {% if latest %}
    <div style="background:var(--color-accent);color:var(--color-bg);padding:24px 32px 26px">
      <div style="font:800 10px var(--font-heading);letter-spacing:.14em;margin-bottom:12px;opacity:.85">
        LATEST DECISION · {{ latest.created_at.strftime("%H:%M:%S") }}
      </div>
      <div style="display:flex;align-items:baseline;gap:16px;margin-bottom:12px">
        <span class="num" style="font:900 44px/1 var(--font-heading);letter-spacing:-.03em">
          {{ latest.action|upper }}{{ " " + latest.symbol if latest.symbol else "" }}
        </span>
      </div>
      <p style="margin:0;max-width:74ch;font-size:15px;line-height:1.5;text-wrap:pretty">{{ latest.reasoning }}</p>
    </div>
    {% endif %}

    <div style="padding:24px 32px 18px">
      <div style="display:flex;align-items:baseline;justify-content:space-between;margin-bottom:8px">
        <div class="kick">Value over time · {{ history|length }} snapshots</div>
        {% if history|length > 1 %}
        <div class="num" style="font-size:11.5px;color:color-mix(in srgb,var(--color-text) 55%,transparent)">
          low ${{ "%.2f"|format(hist_low) }} &nbsp;·&nbsp; high ${{ "%.2f"|format(hist_high) }}
        </div>
        {% endif %}
      </div>
      {% if history|length > 1 %}
      <svg viewBox="0 0 900 200" preserveAspectRatio="none" style="width:100%;height:200px;display:block">
        <polygon points="{{ spark_fill }}" fill="var(--color-accent-200)"></polygon>
        <polyline points="{{ spark_line }}" fill="none" stroke="var(--color-accent)" stroke-width="3"></polyline>
        <line x1="0" y1="190" x2="900" y2="190" stroke="var(--color-text)" stroke-width="2"></line>
      </svg>
      {% else %}
      <div class="num" style="padding:36px 0;font-size:13px;color:color-mix(in srgb,var(--color-text) 45%,transparent)">
        Not enough history yet — a snapshot is taken every minute.
      </div>
      {% endif %}
    </div>

    <div class="r2"></div>
    <div style="padding:22px 32px">
      <div class="kick" style="margin-bottom:14px">Holding</div>
      {% if holdings %}
      <div style="display:grid;grid-template-columns:repeat({{ 2 if holdings|length > 1 else 1 }},minmax(0,1fr));border-top:1px solid color-mix(in srgb,var(--color-text) 18%,transparent)">
        {% for h in holdings %}
        <div style="padding:16px 22px 16px 0;{% if not loop.last and holdings|length > 1 %}border-right:1px solid color-mix(in srgb,var(--color-text) 18%,transparent){% endif %}">
          <div style="display:flex;align-items:baseline;justify-content:space-between;margin-bottom:4px">
            <span style="font:800 24px var(--font-heading)">{{ h.symbol }}</span>
            {% if h.change_pct is not none %}
            <span class="num" style="font:800 24px var(--font-heading);{% if h.change_pct < 0 %}color:var(--color-accent-700){% endif %}">
              {{ "+" if h.change_pct >= 0 else "−" }}{{ "%.1f"|format(h.change_pct|abs) }}%
            </span>
            {% endif %}
          </div>
          <div class="num" style="font-size:12.5px;color:color-mix(in srgb,var(--color-text) 58%,transparent)">
            {{ "%.4f"|format(h.qty) }} @ ${{ "%.4f"|format(h.entry) }}
            {% if h.last %}&rarr; ${{ "%.4f"|format(h.last) }}{% endif %}
            &nbsp;·&nbsp; ${{ "%.2f"|format(h.value) }}
          </div>
        </div>
        {% endfor %}
      </div>
      {% else %}
      <div class="num" style="font-size:13px;color:color-mix(in srgb,var(--color-text) 45%,transparent)">
        Holding nothing — everything is sitting as cash.
      </div>
      {% endif %}
    </div>

    <div class="r2"></div>
    <div style="padding:22px 32px 28px">
      <div class="kick" style="margin-bottom:12px">Decision log · {{ decision_count }} total</div>
      {% for d in decisions %}
      <div class="log-row" {% if loop.last %}style="border-bottom:1px solid color-mix(in srgb,var(--color-text) 18%,transparent)"{% endif %}>
        <span class="num" style="font-size:12px;color:color-mix(in srgb,var(--color-text) 50%,transparent)">{{ d.created_at.strftime("%H:%M:%S") }}</span>
        <span style="width:8px;height:8px;display:block;{% if d.action == 'buy' %}background:var(--color-accent){% elif d.action == 'sell' %}background:var(--color-text){% else %}border:1px solid var(--color-divider){% endif %}"></span>
        <span class="num" style="font-weight:700;font-size:13px">{{ d.symbol or "—" }}</span>
        <span style="font-size:13px;color:color-mix(in srgb,var(--color-text) 72%,transparent)">{{ (d.reasoning or "")[:150] }}</span>
        <span class="num" style="font-size:12px;text-align:right">{{ d.action }}</span>
      </div>
      {% else %}
      <div class="num" style="font-size:13px;color:color-mix(in srgb,var(--color-text) 45%,transparent)">No decisions recorded yet.</div>
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
""".replace("__TOKENS__", MODERNIST_TOKENS)


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

    spark_line, spark_fill, hist_low, hist_high = _spark(history)
    tier_steps, tier_label = _tier_steps(total_value, float(agent["min_capital"]), float(agent["max_capital"]))

    return render_template_string(
        AGENT_DETAIL_TEMPLATE,
        agent=agent, asset_class=(agent.get("strategy") or {}).get("asset_class", "stocks"),
        cash=cash, invested=invested, total_value=total_value, open_pl=open_pl,
        since_spawn=since_spawn, since_spawn_pct=since_spawn_pct, running_for=running_for,
        holdings=holdings, decisions=decisions, decision_count=decision_count,
        trade_count=trade_count, calls_today=calls_today, latest=latest,
        history=history, spark_line=spark_line, spark_fill=spark_fill,
        hist_low=hist_low, hist_high=hist_high,
        tier_steps=tier_steps, tier_label=tier_label,
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8095)
