import { byId } from '../shared/util.js';

export function weightsMode() {
  const r = document.querySelector('input[name="q-weights"]:checked');
  return r ? r.value : 'fresh';
}

export function setWeightsMode(mode) {
  const r = byId('q-weights-' + mode);
  if (r) r.checked = true;
}

export function weightsFlags(desc) {
  const wm = desc.capabilities.checkpoint ? weightsMode() : 'fresh';
  const save = wm === 'scratch' || wm === 'continue';
  const load = wm === 'continue' || wm === 'evaluate';
  return { load_checkpoint: load, save_checkpoint: save, reset_checkpoint_first: save && !load };
}

// ── Checkpoint section ────────────────────────────────────────────────────
export function updateCheckpointStatus(desc) {
  const section = byId('checkpoint-section');
  if (!desc.capabilities.checkpoint) { section.style.display = 'none'; return; }
  section.style.display = '';
  const statusEl = byId('ckpt-status-text');
  statusEl.textContent = 'checking…';
  statusEl.style.color = 'var(--text-dim)';
  fetch('/api/checkpoint-status?algorithm=' + encodeURIComponent(desc.name))
    .then(r => r.json())
    .then(data => {
      byId('ckpt-reset-btn').style.display = data.exists ? '' : 'none';
      if (data.unknown) {
        statusEl.textContent = 'learner unreachable — checkpoint state unknown';
        statusEl.style.color = 'var(--red)';
      } else if (data.exists) {
        const bits = [data.filename];
        if (data.train_steps != null) bits.push(`${data.train_steps} steps`);
        if (data.epsilon != null) bits.push(`ε ${Number(data.epsilon).toFixed(3)}`);
        if (data.entries != null) bits.push(`${data.entries} entries`);
        if (data.mtime) bits.push(new Date(data.mtime * 1000).toLocaleString());
        statusEl.textContent = bits.join(' · ');
        statusEl.style.color = 'var(--green)';
      } else {
        statusEl.textContent = 'no file yet, will load if present at start';
        statusEl.style.color = 'var(--amber)';
      }
    })
    .catch(() => {});
}

export function updateWeightsFields(saTab) {
  const repeats = parseInt(byId('q-repeats').value) || 1;
  ['continue', 'evaluate'].forEach(m => {
    const row = byId('q-weights-' + m + '-row');
    row.classList.toggle('disabled', saTab);
    row.querySelector('input').disabled = saTab;
  });
  if (saTab && (weightsMode() === 'continue' || weightsMode() === 'evaluate')) setWeightsMode('fresh');
  byId('q-weights-sa-hint').style.display = saTab ? '' : 'none';
  const mode = weightsMode();
  const warn = byId('q-weights-warn');
  if (saTab && mode === 'scratch' && repeats < 2) {
    warn.textContent = 'Warm start needs Repeats ≥ 2: repeat 1 trains, repeat 2 continues from it. With 1 repeat SA scores the cold run.';
    warn.style.display = '';
  } else if (mode === 'scratch' && repeats < 2 && !saTab) {
    warn.textContent = 'With 1 repeat this only trains and saves; nothing continues from the checkpoint.';
    warn.style.display = '';
  } else {
    warn.style.display = 'none';
  }
}

export function resetCheckpoint(desc) {
  if (!confirm('Delete the ' + desc.label + ' checkpoint? The next run starts from random weights.')) return Promise.resolve();
  return fetch('/api/checkpoint?algorithm=' + encodeURIComponent(desc.name), {method: 'DELETE'})
    .then(r => r.json().then(d => { if (!r.ok) alert(d.error || ('Reset failed (HTTP ' + r.status + ')')); }))
    .catch(() => alert('Reset failed — could not reach the dashboard.'));
}

let _jamReq = 0;

export const jamOn = desc => desc.capabilities.bottleneck && byId('q-jam-enabled').checked;

export function updateJamFields(desc) {
  byId('jam-section').style.display = desc.capabilities.bottleneck ? '' : 'none';
  const on = jamOn(desc);
  byId('jam-fields').style.display = on ? '' : 'none';
  byId('jam-map-wrap').style.display = on ? '' : 'none';
  if (on) refreshJamPreview();
}

export function refreshJamPreview() {
  const el = byId('jam-preview');
  if (!byId('q-jam-enabled').checked) return;
  const net = byId('q-network').value;
  const n = parseInt(byId('q-jam-edges').value) || 4;
  if (!net) { el.textContent = '—'; return; }
  const my = ++_jamReq;
  el.textContent = 'loading…';
  fetch(`/api/jam-preview?network=${encodeURIComponent(net)}&n=${n}`)
    .then(r => r.json().then(d => (r.ok ? d : {error: d.error || ('HTTP ' + r.status)})))
    .then(d => {
      if (my !== _jamReq) return;
      el.textContent = d.error ? d.error : `${d.edges.join(', ')} (${(d.share * 100).toFixed(1)}% of shortest paths)`;
      if (!d.error) NetMap.geometry(net).then(geo => NetMap.drawJam(byId('jam-map'), geo, d.usage || {}, d.edges || [], 320)).catch(() => {});
    })
    .catch(() => { if (my === _jamReq) el.textContent = 'preview unavailable'; });
}

// ── Build payloads ────────────────────────────────────────────────────────
export function basePayload(desc) {
  const payload = {
    algorithm: desc.name,
    hyperparams: {},
    network_name: byId('q-network').value,
    num_workers: parseInt(byId('q-workers').value) || 2,
    congestion_speed_threshold: 1.0,
    broadcast_rate: parseInt(byId('q-broadcast').value) || 50,
    stats_print_interval: parseInt(byId('q-stats').value) || 100,
    use_gui: byId('q-use-gui').checked,
    capture_trajectories: byId('q-capture-trajectories').checked,
    telemetry: byId('q-telemetry').checked,
    log_proxies: byId('q-log-proxies').checked,
    jam_top_edges: jamOn(desc) ? (parseInt(byId('q-jam-edges').value) || 4) : 0,
    jam_factor: parseFloat(byId('q-jam-factor').value) || 12,
    repeats: parseInt(byId('q-repeats').value) || 1,
    ...weightsFlags(desc),
  };
  if (desc.capabilities.needs_snapshot) {
    const ema = parseFloat(byId('q-speed-ema').value);
    if (Number.isFinite(ema)) payload.speed_ema_alpha = ema;
  }
  const maxMin = parseFloat(byId('q-max-runtime').value);
  if (maxMin > 0) payload.max_runtime_s = Math.round(maxMin * 60);
  const seed = parseInt(byId('q-sumo-seed').value);
  if (Number.isInteger(seed) && seed >= 0) payload.sumo_seed = seed;
  return payload;
}

export async function postQueue(payload) {
  const r = await fetch('/api/queue', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
  const d = await r.json().catch(() => ({}));
  if (!r.ok) alert(d.error || ('Could not add to the queue (HTTP ' + r.status + ')'));
  return r.ok;
}
