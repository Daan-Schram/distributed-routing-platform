import { loadAlgorithms } from '../shared/algorithms.js';
import { startHeaderStatus } from '../shared/util.js';

const PANEL_IDS = ['sim', 'faust', 'kafka', 'learner'];
const MAX_LOG_LINES = 500;

const panelState = {};
PANEL_IDS.forEach(id => { panelState[id] = { lineCount: 0, autoScroll: true }; });

const logEl = id => document.getElementById('log-' + id);
const lcEl = id => document.getElementById('lc-' + id);

// ── Log helpers ───────────────────────────────
function classifyLine(text) {
  if (text === '[process exited]')              return 'exit';
  if (text.startsWith('[dashboard]'))           return 'dash';
  if (text.startsWith('[Step '))                return 'step';
  if (text.startsWith('Final latency stats'))   return 'step';
  if (/error|exception|traceback/i.test(text))  return 'err';
  if (/warn/i.test(text))                       return 'warn';
  if (text.startsWith('[') || text.startsWith('INFO')) return 'sys';
  return 'info';
}

function appendLog(id, text) {
  const el = logEl(id);
  if (!el) return;
  const state = panelState[id];
  const cursor = el.querySelector('.cursor');
  if (cursor) cursor.remove();

  const span = document.createElement('span');
  span.className = 'log-line ' + classifyLine(text);
  span.textContent = text + '\n';
  el.appendChild(span);
  state.lineCount++;
  lcEl(id).textContent = state.lineCount;
  // Keep DOM bounded
  while (el.children.length > MAX_LOG_LINES + 1) el.removeChild(el.firstChild);
  if (state.autoScroll) el.scrollTop = el.scrollHeight;
}

function clearLog(id) {
  const el = logEl(id);
  if (!el) return;
  el.innerHTML = '<span class="cursor"></span>';
  panelState[id].lineCount = 0;
  lcEl(id).textContent = '0';
}

function initLogControls() {
  // ── Per-panel controls ────────────────────────
  document.querySelectorAll('.btn-clear').forEach(btn => {
    btn.addEventListener('click', () => clearLog(btn.dataset.panel));
  });
  document.querySelectorAll('.btn-autoscroll').forEach(btn => {
    btn.addEventListener('click', () => {
      const state = panelState[btn.dataset.panel];
      state.autoScroll = !state.autoScroll;
      btn.textContent = 'Auto↓ ' + (state.autoScroll ? 'ON' : 'OFF');
    });
  });
  PANEL_IDS.forEach(id => {
    logEl(id).addEventListener('scroll', function () {
      const atBottom = this.scrollHeight - this.scrollTop - this.clientHeight < 10;
      if (atBottom && !panelState[id].autoScroll) {
        panelState[id].autoScroll = true;
        const btn = document.querySelector(`.btn-autoscroll[data-panel="${id}"]`);
        if (btn) btn.textContent = 'Auto↓ ON';
      }
    });
  });
}

// ── Resource metrics ──────────────────────────
const latestResource = {};

const VM_CAPACITY = {
  vm1:  { ram_gb: 8,  cpu_cores: 4 },
  vm1b: { ram_gb: 8,  cpu_cores: 4 },
  vm2:  { ram_gb: 8,  cpu_cores: 4 },
  vm3:  { ram_gb: 32, cpu_cores: 4 },
  vm4:  { ram_gb: 8,  cpu_cores: 4 },
  vm5:  { ram_gb: 8,  cpu_cores: 4 },
  vm6:  { ram_gb: 8,  cpu_cores: 4 },
};

const fmtTs = ts => new Date(ts * 1000).toLocaleTimeString();

function fmtMb(mb) {
  if (mb === null || mb === undefined) return '—';
  if (mb >= 1024) return (mb / 1024).toFixed(2) + ' GB';
  return mb.toFixed(1) + ' MB';
}

function recordResource(d) {
  latestResource[d.vm_id + '__' + d.process] = d;
  renderResourceGrid();
}

function clearResources() {
  for (const k in latestResource) delete latestResource[k];
  renderResourceGrid();
}

function renderResourceGrid() {
  const grid = document.getElementById('resource-grid');
  const empty = document.getElementById('res-empty');
  const keys = Object.keys(latestResource);
  if (keys.length === 0) {
    empty.style.display = '';
    grid.querySelectorAll('.resource-row').forEach(r => r.remove());
    return;
  }
  empty.style.display = 'none';

  keys.sort((a, b) => (latestResource[a].layer || '').localeCompare(latestResource[b].layer || '', undefined, {numeric: true, sensitivity: 'base'}));

  keys.forEach(key => {
    const d = latestResource[key];
    const cap = VM_CAPACITY[d.vm_id] || { ram_gb: 8, cpu_cores: 4 };
    const ramPct = Math.min(100, (d.ram_mb / (cap.ram_gb * 1024)) * 100);
    const cpuMax = d.cpu_cores ? d.cpu_cores * 100 : 100;
    const cpuPct = Math.min(100, (d.cpu_pct / cpuMax) * 100);
    const cpuVal = d.cpu_cores
      ? `${d.cpu_pct}% / ${d.cpu_cores * 100}%`
      : `${d.cpu_pct}% (${cap.cpu_cores} cores)`;

    let rowEl = document.getElementById('res-row-' + key);
    if (!rowEl) {
      rowEl = document.createElement('div');
      rowEl.className = 'resource-row';
      rowEl.id = 'res-row-' + key;
    }
    grid.appendChild(rowEl);

    let col3Html;
    if (d.net_in_mb !== null && d.net_in_mb !== undefined) {
      col3Html = `
      <div class="meter-wrap">
        <div class="meter-label">NET I/O</div>
        <div class="meter-val" style="margin-top:4px;">↓${fmtMb(d.net_in_mb)} ↑${fmtMb(d.net_out_mb)}</div>
        <div style="font-size:10px;color:var(--text-dim);margin-top:2px;">cumulative</div>
      </div>`;
    } else if (d.gpu_pct !== null && d.gpu_pct !== undefined) {
      col3Html = `
      <div class="meter-wrap">
        <div class="meter-label">GPU</div>
        <div class="meter-bar"><div class="meter-fill gpu" style="width:${Math.min(100, d.gpu_pct)}%"></div></div>
        <div class="meter-val">${d.gpu_pct}% / ${d.gpu_mem_mb ? d.gpu_mem_mb.toFixed(0) + 'MB' : '—'}</div>
      </div>`;
    } else {
      col3Html = `<div style="color:var(--text-dim);font-size:11px;">no GPU</div>`;
    }

    rowEl.innerHTML = `
    <div>
      <div class="resource-id">${d.vm_id}</div>
      <div class="resource-layer">${d.layer} · ${d.process}</div>
      <div class="resource-ts">Last update: ${fmtTs(d.timestamp)}</div>
    </div>
    <div class="meter-wrap">
      <div class="meter-label">CPU</div>
      <div class="meter-bar"><div class="meter-fill cpu" style="width:${cpuPct}%"></div></div>
      <div class="meter-val">${cpuVal}</div>
    </div>
    <div class="meter-wrap">
      <div class="meter-label">RAM</div>
      <div class="meter-bar"><div class="meter-fill ram" style="width:${ramPct.toFixed(1)}%"></div></div>
      <div class="meter-val">${d.ram_mb.toFixed(0)} MB / ${cap.ram_gb} GB (${d.ram_pct}%)</div>
    </div>
    ${col3Html}
  `;
  });
}

// ── MULTIPLEXED SSE ───────────────────────────
function connectSSE() {
  const sse = new EventSource('/stream/all');
  sse.onmessage = e => {
    if (!e.data) return;
    try {
      const payload = JSON.parse(e.data);
      if (payload.p === 'res') recordResource(payload.d);
      else if (payload.p === 'clear') { PANEL_IDS.forEach(clearLog); clearResources(); }
      else if (PANEL_IDS.includes(payload.p)) appendLog(payload.p, payload.l);
    } catch (err) {
      console.error('SSE parse error:', err);
    }
  };
  sse.onerror = err => console.warn('SSE error, browser will auto-retry:', err);
}

// ── Status polling: dot indicators ────
function pollDots() {
  fetch('/status').then(r => r.json()).then(d => {
    ['faust', 'learner', 'sim'].forEach(k => {  // dotKafka is managed by pollKafka()
      document.getElementById('dot-' + k).className = 'dot ' + (d[k] === 'running' ? 'alive' : '');
    });
  }).catch(() => {});
}

// ── Kafka consumer group polling ──────────────
function pollKafka() {
  const out = document.getElementById('kafka-output');
  const dot = document.getElementById('dot-kafka');
  fetch('/kafka-status')
    .then(r => r.json())
    .then(d => {
      out.textContent = d.output || '(empty)';
      dot.className = 'dot ' + (/Stable/i.test(d.output) ? 'alive' : 'warn');
    })
    .catch(() => {
      out.textContent = 'kafka unreachable';
      dot.className = 'dot error';
    });
}

initLogControls();
connectSSE();
// ── Initial dot state (one-shot, then interval takes over) ──
pollDots();
setInterval(pollDots, 2000);
pollKafka();
setInterval(pollKafka, 3000);
loadAlgorithms().then(startHeaderStatus);
