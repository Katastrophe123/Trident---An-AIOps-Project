/* ===========================================================================
   AIOps Console — all the JavaScript, in reading order.

   The whole page is one loop:

       every 2 seconds:  GET /api/overview  ->  redraw the service cards
       every 4 seconds:  GET /api/logs      ->  redraw the log table

   There is no framework. Three helpers do all the DOM work:

       $(sel)            find one element
       bind(root, name)  find the element marked data-bind="name"
       text(el, value)   set its text

   Sparkline history is kept in the browser (the services only report their
   current reading), which is why a reload restarts the trend lines.
   =========================================================================== */

'use strict';

// --------------------------------------------------------------- 1. helpers
const $ = (selector, root = document) => root.querySelector(selector);
const bind = (root, name) => root.querySelector(`[data-bind="${name}"]`);
const text = (element, value) => { if (element) element.textContent = value; };

/** 1284 -> "1,284"; 12900 -> "12.9K". Keeps a stat tile from overflowing. */
function compact(value) {
  if (value === null || value === undefined) return '—';
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  if (Math.abs(n) >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
  if (Math.abs(n) >= 10_000) return (n / 1000).toFixed(1) + 'K';
  return n.toLocaleString();
}

/** Milliseconds, with the unit, at a sensible precision. */
function ms(value) {
  if (value === null || value === undefined) return '—';
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  return n >= 100 ? `${Math.round(n)} ms` : `${n.toFixed(1)} ms`;
}

/** Icons double up the status so colour is never the only channel. */
const STATUS_ICON = {
  healthy: '●',      // ●  filled circle
  degraded: '▲',     // ▲  triangle
  unhealthy: '■',    // ■  square
  unreachable: '✕',  // ✕  cross
  unknown: '○',      // ○  hollow circle
};

const STATUS_WORD = {
  healthy: 'healthy',
  degraded: 'degraded',
  unhealthy: 'unhealthy',
  unreachable: 'unreachable',
  unknown: 'unknown',
};

// ------------------------------------------------------- 2. sparkline drawing
/**
 * Draw a sparkline into an <svg>.
 *
 * Follows the house mark specs: a 2px line with round joins, a 10%-opacity
 * area wash underneath, a hairline baseline, and an end dot carrying a 2px
 * ring in the surface colour. The last segment uses the accent colour so the
 * newest reading stands out from the de-emphasised history.
 */
function drawSparkline(svg, values) {
  if (!svg) return;
  const WIDTH = 160;
  const HEIGHT = 36;
  const PAD = 3;                      // keeps the 8px dot inside the box
  svg.replaceChildren();

  if (!values || values.length < 2) return;

  const max = Math.max(...values);
  const min = Math.min(...values);
  // A flat series would divide by zero; give it a nominal range so it
  // draws as a centred straight line rather than collapsing.
  const span = max - min || Math.max(max, 1);

  const x = (i) => PAD + (i * (WIDTH - PAD * 2)) / (values.length - 1);
  const y = (v) => HEIGHT - PAD - ((v - min) / span) * (HEIGHT - PAD * 2);

  const points = values.map((v, i) => [x(i), y(v)]);
  const svgNS = 'http://www.w3.org/2000/svg';
  const make = (tag, attrs, cls) => {
    const node = document.createElementNS(svgNS, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    if (cls) node.setAttribute('class', cls);
    return node;
  };

  // recessive hairline baseline
  svg.appendChild(make('line', {
    x1: 0, y1: HEIGHT - PAD, x2: WIDTH, y2: HEIGHT - PAD,
  }, 'spark__grid'));

  const line = points.map(([px, py]) => `${px.toFixed(1)},${py.toFixed(1)}`).join(' ');

  // area wash
  svg.appendChild(make('polygon', {
    points: `${PAD},${HEIGHT - PAD} ${line} ${WIDTH - PAD},${HEIGHT - PAD}`,
  }, 'spark__area'));

  // history in the dim step
  svg.appendChild(make('polyline', { points: line }, 'spark__line'));

  // the newest segment in the accent step
  if (points.length >= 2) {
    const [a, b] = points.slice(-2);
    svg.appendChild(make('polyline', {
      points: `${a[0].toFixed(1)},${a[1].toFixed(1)} ${b[0].toFixed(1)},${b[1].toFixed(1)}`,
    }, 'spark__head'));
  }

  // end dot: r=4 gives the 8px marker the specs ask for
  const [lastX, lastY] = points[points.length - 1];
  svg.appendChild(make('circle', { cx: lastX.toFixed(1), cy: lastY.toFixed(1), r: 4 }, 'spark__dot'));

  // The numbers are always readable as text in the tile; this is the hover layer.
  const title = document.createElementNS(svgNS, 'title');
  title.textContent = `now ${values[values.length - 1].toFixed(1)} · min ${min.toFixed(1)} · max ${max.toFixed(1)}`;
  svg.appendChild(title);
}

// ------------------------------------------------------------- 3. app state
/** Per-service rolling history for the sparklines: { orders: {latency:[], memory:[]} } */
const history = {};
const HISTORY_POINTS = 24;

function remember(service, key, value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return;
  history[service] ??= {};
  const series = (history[service][key] ??= []);
  series.push(Number(value));
  if (series.length > HISTORY_POINTS) series.shift();
}

// --------------------------------------------------------- 4. rendering: cards
const servicesHost = bind(document, 'services');
const serviceTemplate = $('#tpl-service');
/** Cards are created once and then updated, so the DOM is not rebuilt every poll. */
const cards = new Map();

function cardFor(name) {
  if (cards.has(name)) return cards.get(name);
  const node = serviceTemplate.content.firstElementChild.cloneNode(true);
  servicesHost.appendChild(node);
  cards.set(name, node);
  return node;
}

function renderService(snapshot) {
  const card = cardFor(snapshot.name);
  const status = STATUS_WORD[snapshot.status] ? snapshot.status : 'unknown';

  text(bind(card, 'name'), snapshot.name);
  text(bind(card, 'url'), snapshot.url);

  const pill = bind(card, 'status-pill');
  pill.dataset.status = status;
  text(bind(card, 'status-icon'), STATUS_ICON[status]);
  text(bind(card, 'status-text'), STATUS_WORD[status]);

  text(bind(card, 'detail'), snapshot.detail || '');

  text(bind(card, 'requests'), compact(snapshot.requests));
  text(bind(card, 'errors'), compact(snapshot.errors));
  text(
    bind(card, 'error-rate'),
    snapshot.error_rate === null || snapshot.error_rate === undefined
      ? ''
      : `${(snapshot.error_rate * 100).toFixed(1)}% of requests`,
  );

  const latency = snapshot.recent_average_latency_ms;
  const latencyValue = bind(card, 'latency');
  text(latencyValue, ms(latency));
  // Over a second of average latency is the slow-dependency signature.
  latencyValue.dataset.alert = String(Number(latency) >= 1000);
  text(
    bind(card, 'latency-p95'),
    snapshot.recent_p95_latency_ms ? `p95 ${ms(snapshot.recent_p95_latency_ms)}` : '',
  );

  text(bind(card, 'memory'), snapshot.memory_mb ? `${snapshot.memory_mb.toFixed(0)} MB` : '—');

  remember(snapshot.name, 'latency', latency);
  remember(snapshot.name, 'memory', snapshot.memory_mb);
  drawSparkline(bind(card, 'spark-latency'), history[snapshot.name]?.latency);
  drawSparkline(bind(card, 'spark-memory'), history[snapshot.name]?.memory);

  // dependencies
  const deps = bind(card, 'deps');
  deps.replaceChildren();
  for (const dep of snapshot.dependencies || []) {
    const li = document.createElement('li');
    const ok = dep.status === 'healthy';
    li.dataset.ok = String(ok);
    li.textContent = dep.latency_ms ? `${dep.name} · ${ms(dep.latency_ms)}` : dep.name;
    if (!ok && dep.error) li.title = dep.error;
    deps.appendChild(li);
  }

  // injected faults / unreachable reason
  const faultParts = [];
  if (snapshot.error) faultParts.push(snapshot.error);
  const chaos = snapshot.chaos || {};
  if (chaos.latency_ms) faultParts.push(`injected delay ${chaos.latency_ms} ms`);
  if (chaos.memory_leak_active) faultParts.push(`leaking (${chaos.leaked_mb} MiB so far)`);
  if (chaos.held_db_connections) faultParts.push(`holding ${chaos.held_db_connections} DB connections`);
  text(bind(card, 'fault'), faultParts.join(' · '));
}

// ------------------------------------------------------- 5. rendering: orders
function renderOrders(counts) {
  const host = bind(document, 'orders-bars');
  const empty = bind(document, 'orders-empty');
  const entries = Object.entries(counts || {});

  if (entries.length === 0) {
    host.replaceChildren();
    empty.hidden = false;
    return;
  }
  empty.hidden = true;

  // Fixed order, so a bar never swaps position between polls.
  const ORDER = ['confirmed', 'rejected', 'failed', 'pending'];
  entries.sort((a, b) => ORDER.indexOf(a[0]) - ORDER.indexOf(b[0]));
  const max = Math.max(...entries.map(([, v]) => v), 1);

  host.replaceChildren();
  for (const [outcome, count] of entries) {
    const row = document.createElement('div');
    row.className = 'bar-row';
    row.dataset.outcome = outcome;

    const label = document.createElement('span');
    label.className = 'bar-row__label';
    label.textContent = outcome;

    const track = document.createElement('div');
    track.className = 'bar-row__track';
    const fill = document.createElement('div');
    fill.className = 'bar-row__fill';
    fill.style.width = `${(count / max) * 100}%`;
    track.appendChild(fill);

    const value = document.createElement('span');
    value.className = 'bar-row__value';
    value.textContent = count.toLocaleString();

    row.append(label, track, value);
    host.appendChild(row);
  }
}

// --------------------------------------------------------- 6. rendering: logs
/** Fields that are plumbing rather than detail worth showing in the table. */
const LOG_SKIP = new Set(['timestamp', 'epoch', 'level', 'service', 'logger', 'event', 'request_id']);

function renderLogs(records) {
  const body = bind(document, 'logs');
  const empty = bind(document, 'logs-empty');
  body.replaceChildren();

  if (!records || records.length === 0) {
    empty.hidden = false;
    return;
  }
  empty.hidden = true;

  for (const record of records) {
    const row = document.createElement('tr');

    const time = document.createElement('td');
    time.textContent = (record.timestamp || '').slice(11, 19);

    const level = document.createElement('td');
    const levelSpan = document.createElement('span');
    levelSpan.className = 'logs__level';
    levelSpan.dataset.level = record.level || 'INFO';
    levelSpan.textContent = record.level || 'INFO';
    level.appendChild(levelSpan);

    const service = document.createElement('td');
    service.textContent = record.service || '';

    const event = document.createElement('td');
    event.className = 'logs__event';
    event.textContent = record.event || '';

    const detail = document.createElement('td');
    detail.className = 'logs__detail';
    detail.textContent = Object.entries(record)
      .filter(([k, v]) => !LOG_SKIP.has(k) && v !== null && v !== '')
      .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`)
      .join('  ');

    row.append(time, level, service, event, detail);
    body.appendChild(row);
  }
}

function renderManual(scenarios) {
  const host = bind(document, 'manual-list');
  if (host.childElementCount > 0) return;   // static content, render once
  const template = $('#tpl-manual');

  for (const scenario of Object.values(scenarios || {})) {
    const node = template.content.firstElementChild.cloneNode(true);
    text(bind(node, 'title'), scenario.title);
    text(bind(node, 'inject'), scenario.inject);
    text(bind(node, 'fix'), scenario.fix);
    text(bind(node, 'why'), scenario.why);
    host.appendChild(node);
  }

  // One listener for every copy button, added after the rows exist.
  for (const button of host.querySelectorAll('[data-copy]')) {
    button.addEventListener('click', async () => {
      const command = button.previousElementSibling.textContent;
      try {
        await navigator.clipboard.writeText(command);
        const original = button.textContent;
        button.textContent = 'Copied';
        setTimeout(() => { button.textContent = original; }, 1200);
      } catch {
        // Clipboard needs a secure context; selecting the text still works.
        button.textContent = 'Select it';
      }
    });
  }
}

// ----------------------------------------------------------- 7. the poll loop
async function refreshOverview() {
  try {
    const response = await fetch('/api/overview');
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();

    for (const snapshot of data.services) renderService(snapshot);
    renderOrders(data.orders_by_status);
    renderManual(data.manual_scenarios);

    // Overall state is the worst individual state, so one sick service shows.
    const statuses = data.services.map((s) => s.status);
    let overall = 'healthy';
    if (statuses.includes('unreachable') || statuses.includes('unhealthy')) overall = 'unhealthy';
    else if (statuses.includes('degraded')) overall = 'degraded';

    const pill = bind(document, 'overall-pill');
    pill.dataset.status = overall;
    text(bind(document, 'overall-icon'), STATUS_ICON[overall]);
    text(
      bind(document, 'overall-text'),
      overall === 'healthy' ? 'all services healthy' : `system ${STATUS_WORD[overall]}`,
    );
    text(bind(document, 'last-updated'), `updated ${new Date().toLocaleTimeString()}`);
  } catch (error) {
    const pill = bind(document, 'overall-pill');
    pill.dataset.status = 'unknown';
    text(bind(document, 'overall-icon'), STATUS_ICON.unknown);
    text(bind(document, 'overall-text'), 'console cannot reach its API');
    text(bind(document, 'last-updated'), String(error.message || error));
  }
}

async function refreshLogs() {
  if (!bind(document, 'log-autorefresh').checked) return;
  const service = bind(document, 'log-service').value;
  const level = bind(document, 'log-level').value;
  const params = new URLSearchParams({ limit: '60' });
  if (service) params.set('service', service);
  if (level) params.set('level', level);

  try {
    const response = await fetch(`/api/logs?${params}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    renderLogs((await response.json()).records);
  } catch {
    // A failed log fetch should not blank the table; leave the last view.
  }
}

// ------------------------------------------------------------ 8. chaos buttons
/** data-chaos value -> the request the button makes. */
const CHAOS_ACTIONS = {
  'latency-on':  ['/api/chaos/latency?service=inventory&milliseconds=3000', 'Injected a 3s delay into inventory'],
  'latency-off': ['/api/chaos/latency?service=inventory&milliseconds=0', 'Cleared the inventory delay'],
  'leak-on':     ['/api/chaos/memory-leak?service=orders&enabled=true', 'Started the memory leak in orders'],
  'leak-off':    ['/api/chaos/memory-leak?service=orders&enabled=false', 'Stopped the leak and freed the memory'],
  'db-on':       ['/api/chaos/db-exhaust?service=payments&connections=20', 'payments is holding 20 DB connections'],
  'db-off':      ['/api/chaos/db-exhaust?service=payments&connections=0', 'Released the held DB connections'],
  'reset':       ['/api/chaos/reset', 'Reset every in-process fault'],
};

function wireChaosButtons() {
  const feedback = bind(document, 'chaos-feedback');

  for (const button of document.querySelectorAll('[data-chaos]')) {
    button.addEventListener('click', async () => {
      const [url, message] = CHAOS_ACTIONS[button.dataset.chaos];
      button.disabled = true;
      text(feedback, 'working…');
      try {
        const response = await fetch(url, { method: 'POST' });
        if (!response.ok) {
          const body = await response.json().catch(() => ({}));
          throw new Error(body.detail || `HTTP ${response.status}`);
        }
        text(feedback, message);
        await refreshOverview();   // show the effect immediately
      } catch (error) {
        text(feedback, `failed: ${error.message || error}`);
      } finally {
        button.disabled = false;
      }
    });
  }
}

// ---------------------------------------------------------------- 9. the theme
function wireThemeToggle() {
  const button = $('#theme-toggle');
  const label = bind(document, 'theme-label');
  const saved = localStorage.getItem('aiops-theme');
  if (saved) document.documentElement.dataset.theme = saved;

  const sync = () => text(label, document.documentElement.dataset.theme === 'dark' ? 'Light' : 'Dark');
  sync();

  button.addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    localStorage.setItem('aiops-theme', next);
    sync();
    // Sparklines read their colours from CSS variables, so redraw after a swap.
    refreshOverview();
  });
}

// ------------------------------------------------------------------ 10. start
wireChaosButtons();
wireThemeToggle();
bind(document, 'log-service').addEventListener('change', refreshLogs);
bind(document, 'log-level').addEventListener('change', refreshLogs);

refreshOverview();
refreshLogs();
setInterval(refreshOverview, 2000);
setInterval(refreshLogs, 4000);
