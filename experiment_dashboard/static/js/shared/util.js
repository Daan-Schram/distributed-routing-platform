import { labelOf } from './algorithms.js';

// ── Helpers ─────────────────────────────────────────────
export function escHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

export const byId = id => document.getElementById(id);

export function fmt(v, decimals = 2, unit = '') {
  if (v === null || v === undefined) return '—';
  const n = typeof v === 'number' ? v.toFixed(decimals) : v;
  return unit ? `${n} ${unit}` : String(n);
}

export function fmtParam(v) {
  if (typeof v !== 'number') return String(v);
  if (v !== 0 && Math.abs(v) < 0.001) {
    const exp = Math.floor(Math.log10(Math.abs(v)));
    return `${parseFloat((v / Math.pow(10, exp)).toPrecision(3))}e${exp}`;
  }
  return String(parseFloat(v.toPrecision(6)));
}

export function reformatDate(yyyymmdd) {
  const parts = String(yyyymmdd).split('-');
  return parts.length === 3 ? `${parts[2]}-${parts[1]}-${parts[0]}` : yyyymmdd;
}

export function formatGroupLabel(g) {
  const m = g.group_id.match(/^grid_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})_/);
  if (m) return `Grid · ${m[3]}-${m[2]}-${m[1]} ${m[4]}:${m[5]}`;
  const s = g.group_id.match(/^sa_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})_/);
  if (s) return `SA · ${s[3]}-${s[2]}-${s[1]} ${s[4]}:${s[5]}`;
  return g.label || g.group_id;
}

export function fmtDuration(sec) {
  return sec >= 3600 ? `${Math.floor(sec / 3600)} h ${Math.round((sec % 3600) / 60)} min` : `${Math.max(1, Math.round(sec / 60))} min`;
}

export function statCell(label, val, unit = '', cls = '') {
  return `<div class="stat-cell${cls ? ' ' + cls : ''}">
    <div class="stat-label">${label}</div>
    <div class="stat-val">${val}<span class="stat-unit">${unit ? ' ' + unit : ''}</span></div>
  </div>`;
}

export function makeStatSection(label, cellHtmlArr) {
  const div = document.createElement('div');
  div.innerHTML = `<div class="stat-section-label">${label}</div>
    <div class="stat-row">${cellHtmlArr.join('')}</div>`;
  return div;
}

const KEY = 'ddqr_prefill';

export function writePrefill(prefill) {
  try { localStorage.setItem(KEY, JSON.stringify(prefill)); } catch (e) {}
  window.location.href = '/';
}

export function takePrefill() {
  try {
    const raw = localStorage.getItem(KEY);
    localStorage.removeItem(KEY);
    return raw ? JSON.parse(raw) : null;
  } catch (e) { return null; }
}

// ── Header status pill ────────────────────────────────────────────────────
function update() {
  Promise.all([
    fetch('/api/quick-status').then(r => r.json()),
    fetch('/api/queue/status').then(r => r.json()),
  ]).then(([qs, qst]) => {
    const dot = document.getElementById('hst-dot');
    const text = document.getElementById('hst-text');
    if (qs.experiment_running && qs.config && Object.keys(qs.config).length) {
      dot.className = 'hst-dot running';
      let label = '● ' + labelOf(qs.config.algorithm) + ' on ' + (qs.config.network_name || '?');
      if (qst.queued > 0) label += ' · +' + qst.queued + ' in queue';
      text.textContent = label;
    } else if (qst.queued > 0 || qst.running > 0) {
      dot.className = 'hst-dot';
      text.textContent = 'Idle · +' + (qst.queued + qst.running) + ' in queue';
    } else {
      dot.className = 'hst-dot';
      text.textContent = 'No experiment running';
    }
  }).catch(() => {});
}

export function startHeaderStatus() {
  update();
  setInterval(update, 3000);
}
