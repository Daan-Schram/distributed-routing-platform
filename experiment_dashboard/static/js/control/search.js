import { byId, fmtDuration, fmtParam } from '../shared/util.js';
import { platformTunables } from '../shared/algorithms.js';
import { readGrid, readSa } from './params_form.js';
import { basePayload, postQueue } from './form.js';

const cartesian = arrays => arrays.reduce((acc, arr) => acc.flatMap(a => arr.map(v => [...a, v])), [[]]);

const tunables = desc => desc.capabilities.needs_snapshot ? platformTunables() : [];

// ── Grid Search ───────────────────────────────────────────────────────────
export const gridParams = desc => [...desc.params, ...tunables(desc)];

export function updateGridCount(desc) {
  const { values } = readGrid(byId('gs-params-grid'), gridParams(desc));
  const counts = values.map(v => v.length);
  const total = counts.reduce((a, b) => a * b, 1);
  byId('gs-combo-count').textContent = counts.length
    ? `→ ${total} combination${total !== 1 ? 's' : ''} (${counts.join(' × ')})`
    : 'Select at least one parameter to vary.';
}

export async function addGridToQueue(desc) {
  const { keys, values } = readGrid(byId('gs-params-grid'), gridParams(desc));
  if (!keys.length) { alert('Select at least one parameter to vary.'); return; }
  const platform = new Set(platformTunables().map(p => p.name));
  const now = new Date();
  const pad = n => String(n).padStart(2, '0');
  const ts = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}_${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
  const groupId = `grid_${ts}_${Math.random().toString(16).slice(2, 6)}`;
  const base = basePayload(desc);
  for (const combo of cartesian(values)) {
    const payload = { ...base, hyperparams: {}, group_id: groupId };
    keys.forEach((k, i) => { if (platform.has(k)) payload[k] = combo[i]; else payload.hyperparams[k] = combo[i]; });
    if (!await postQueue(payload)) return;
  }
}

// ── SA Search ─────────────────────────────────────────────────────────────
const SA_DEFAULTS = { 'sa-n-iter': 80, 'sa-t-init': 800, 'sa-alpha': 0.97, 'sa-sigma': 0.4, 'sa-n-perturb': 1 };
const tfmt = v => v >= 100 ? v.toFixed(0) : v.toFixed(1);

export const saParams = desc => [...desc.params, ...tunables(desc)].filter(p => p.search);
export const saAvailable = desc => desc.capabilities.learns && desc.params.some(p => p.search);

export function resetSaSettings() {
  Object.entries(SA_DEFAULTS).forEach(([id, v]) => { byId(id).value = v; });
  updateSchedulePreview();
}

// Live preview under the SA settings inputs: where temperature lands by the last iteration
export function updateSchedulePreview() {
  const el = byId('sa-schedule-preview');
  const t0 = parseFloat(byId('sa-t-init').value);
  const a  = parseFloat(byId('sa-alpha').value);
  const n  = parseInt(byId('sa-n-iter').value);
  if (isNaN(t0) || isNaN(a) || isNaN(n) || n < 1 || a <= 0) { el.textContent = ''; return; }
  const Tmid = t0 * Math.pow(a, Math.round(n / 2));
  const Tend = t0 * Math.pow(a, n);
  const p = T => T > 0 ? Math.exp(-50 / T) : 0;   // chance of accepting a +50s worse move
  el.textContent = `T: ${tfmt(t0)} → ${tfmt(Tmid)} (½) → ${tfmt(Tend)} after ${n} iters`
    + `  ·  P(accept +50s) ≈ ${p(t0).toFixed(2)} → ${p(Tmid).toFixed(2)} → ${p(Tend).toFixed(3)}`;
}

export async function saStart(desc) {
  const { param_space, fixed } = readSa(byId('sa-space-grid'), saParams(desc));
  if (!param_space.length) { alert('Tick at least one parameter to search.'); return false; }
  const base = basePayload(desc);
  const cfg = {
    algorithm: desc.name,
    network_name: base.network_name,
    num_workers: base.num_workers,
    broadcast_rate: base.broadcast_rate,
    stats_print_interval: base.stats_print_interval,
    use_gui: base.use_gui,
    capture_trajectories: base.capture_trajectories,
    sumo_seed: base.sumo_seed,
    telemetry: base.telemetry,
    log_proxies: base.log_proxies,
    jam_top_edges: base.jam_top_edges,
    jam_factor: base.jam_factor,
    repeats: base.repeats,
    n_iter: parseInt(byId('sa-n-iter').value) || 80,
    max_runtime_s: Math.round((parseFloat(byId('q-max-runtime').value) || 0) * 60) || 900,
    t_init: parseFloat(byId('sa-t-init').value) || 800,
    alpha: parseFloat(byId('sa-alpha').value) || 0.97,
    sigma: parseFloat(byId('sa-sigma').value) || 0.4,
    n_perturb: parseInt(byId('sa-n-perturb').value) || 1,
    resume: byId('sa-resume').checked,
    warm_start: base.save_checkpoint,
    score_mode: byId('sa-score-mode').value,
    param_space,
    fixed: base.speed_ema_alpha !== undefined ? { speed_ema_alpha: base.speed_ema_alpha, ...fixed } : fixed,
  };
  const r = await fetch('/api/sa/start', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(cfg)});
  const d = await r.json();
  if (d.error) { alert(d.error); return false; }
  return true;
}

const ACTIVE = ['running', 'starting', 'stopping'];
let _last = null;  // most recent status payload (for Apply to Custom)
let _lastError = null;

export const lastSaStatus = () => _last;
export const saActive = s => !!s && ACTIVE.includes(s.status);

export function runsLeft(s) {
  if (s.runs_remaining == null) return '';
  let out = `${s.runs_remaining} run${s.runs_remaining === 1 ? '' : 's'} left`;
  if (s.eta_s != null) out += ` · ≈ ${fmtDuration(s.eta_s)} (avg ${fmtDuration(s.avg_run_s)}/run)`;
  else if (s.max_runtime_s) out += ` · ≤ ${fmtDuration(s.runs_remaining * s.max_runtime_s)} at the runtime cap`;
  return out;
}

// Temperature decay curve on the status card, with a marker at the current iteration.
// Iterations are 0-based 0..n_iter-1, so the x-axis spans that range.
function renderTempChart(s) {
  const host = byId('sa-temp-chart');
  const t0 = s.t_init, a = s.alpha;
  const denom = Math.max(1, (s.n_iter || 1) - 1);
  if (!t0 || !a) { host.innerHTML = ''; return; }
  const N = Math.min(denom, 120), pts = [];
  for (let k = 0; k <= N; k++) {
    const i = denom * k / N;
    const T = t0 * Math.pow(a, i);
    pts.push(((i / denom) * 100).toFixed(2) + ',' + ((1 - T / t0) * 100).toFixed(2));
  }
  const cur = s.iteration || 0;
  const cx = Math.min(100, (cur / denom) * 100).toFixed(2);
  host.innerHTML = `<svg viewBox="0 0 100 100" preserveAspectRatio="none" style="width:100%;height:54px;display:block;">`
    + `<line x1="${cx}" y1="0" x2="${cx}" y2="100" stroke="var(--green)" stroke-width="1" vector-effect="non-scaling-stroke" opacity="0.7"/>`
    + `<polyline points="${pts.join(' ')}" fill="none" stroke="var(--blue)" stroke-width="1.5" vector-effect="non-scaling-stroke"/></svg>`;
  byId('sa-temp-caption').textContent = `start ${tfmt(t0)} · now ${tfmt(t0 * Math.pow(a, cur))} · end ${tfmt(t0 * Math.pow(a, denom))}`;
}

function render(s) {
  _last = s;
  byId('sa-status-card').style.display = '';
  byId('sa-message').textContent = s.message || '';
  const parts = [];
  if (s.repeats) parts.push(`${s.repeats} rep` + (s.warm_start ? ' · warm' : '') + (s.score_mode ? ' · ' + s.score_mode : ''));
  if (s.jam_top_edges) parts.push(`J${s.jam_top_edges}×${s.jam_factor}`);
  if (s.capture_trajectories) parts.push('traj');
  byId('sa-settings').textContent = parts.join('  ');
  // Iterations are 0-based; current candidate number is iteration + 1.
  const nIter = s.n_iter || 1;
  const done = Math.min(nIter, (s.iteration || 0) + 1);
  byId('sa-progress-bar').style.width = Math.min(100, Math.round(done / nIter * 100)) + '%';
  byId('sa-progress-text').textContent = (s.confirming ? 'confirming · ' : '') + done + ' / ' + nIter
    + (s.runs_remaining != null ? ' · ' + runsLeft(s) : '');
  byId('sa-temperature').textContent = s.temperature != null ? s.temperature.toFixed(1) : '—';
  renderTempChart(s);
  byId('sa-current-att').textContent = s.current_att != null ? s.current_att.toFixed(0) + 's' : '—';
  byId('sa-best-att').textContent = s.best_att != null
    ? s.best_att.toFixed(0) + 's' + (s.best_att_confirmed != null ? ' (≈' + s.best_att_confirmed.toFixed(0) + 's)' : '')
    : '—';
  if (s.best_solution) {
    byId('sa-best-solution').textContent = Object.entries(s.best_solution)
      .map(([k, v]) => k.padEnd(22) + fmtParam(v)).join('\n');
    byId('sa-apply-best').style.display = '';
  }
  if (s.history && s.history.length) {
    byId('sa-history').textContent = [...s.history].reverse().map(h => {
      const mark = h.accepted ? '✓' : '✗';
      const reasonLbl = {too_few: '<20 veh', no_result: 'no data', last_missing: 'last: no ATT', error: 'run failed'}[h.reason] || 'invalid';
      const attStr = h.att == null ? reasonLbl : 'ATT=' + h.att.toFixed(0) + 's';
      const best = (h.att != null && h.att === s.best_att) ? ' ★' : '';
      return String(h.iteration + 1).padStart(3) + '  ' + mark + '  ' + attStr.padStart(10) + '  T=' + h.temperature.toFixed(1).padStart(7) + best;
    }).join('\n');
  }
}

function setStartEnabled(on) {
  const btn = byId('btn-sa-start');
  btn.disabled = !on;
  btn.style.opacity = on ? '' : '0.5';
}

export function onSaStatus(s) {
  if (saActive(s)) {
    setStartEnabled(false);
    render(s);
    return;
  }
  // Search ended — stop polling, re-enable the button, and clear the card.
  // The completed run's full details live on the Analyze page now.
  setStartEnabled(true);
  byId('sa-status-card').style.display = 'none';
  if (s && s.status === 'error' && s.message && s.message !== _lastError) {
    _lastError = s.message;
    alert('SA search failed: ' + s.message);
  }
}
