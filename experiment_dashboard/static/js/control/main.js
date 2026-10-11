import { byId, escHtml, startHeaderStatus, takePrefill } from '../shared/util.js';
import { algorithms, descriptor, loadAlgorithms, platformTunables } from '../shared/algorithms.js';
import { COMMON_IDS, restoreCommon, save, saveCommon, saved } from './state.js';
import { initNetworkPicker, setNetwork } from './network_picker.js';
import { defaultsLine, readCustom, readSa, renderCustom, renderGrid, renderSa } from './params_form.js';
import { basePayload, postQueue, refreshJamPreview, resetCheckpoint, setWeightsMode, updateCheckpointStatus,
         updateJamFields, updateWeightsFields, weightsMode } from './form.js';
import { addGridToQueue, gridParams, lastSaStatus, resetSaSettings, saAvailable, saParams, saStart,
         updateGridCount, updateSchedulePreview } from './search.js';
import { initQueue, refreshQueue } from './queue.js';

const TABS = ['default', 'custom', 'grid', 'sa'];
let desc = null;

const tabOf = () => saved().tab[desc.name] || 'default';
const isPlatform = name => platformTunables().some(p => p.name === name);

function setTab(tab) {
  if (!desc.params.length || (tab === 'sa' && !saAvailable(desc))) tab = 'default';
  saved().tab[desc.name] = tab;
  save();
  document.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  TABS.forEach(t => { byId('panel-' + t).style.display = t === tab ? '' : 'none'; });
  const add = byId('btn-add');
  add.style.display = tab === 'sa' ? 'none' : '';
  add.textContent = tab === 'grid' ? '⊞ Add grid to queue' : '+ Add to queue';
  updateWeightsFields(tab === 'sa');
}

function saveCustom() {
  saved().hp[desc.name] = readCustom(byId('params-custom'), desc.params);
  save();
}

function saveSa() {
  saved().sa[desc.name] = readSa(byId('sa-space-grid'), saParams(desc)).state;
  save();
}

function renderParams() {
  byId('params-section').style.display = desc.params.length ? '' : 'none';
  if (desc.params.length) {
    byId('params-label').textContent = desc.label + ' parameters';
    document.querySelector('.tab[data-tab="sa"]').style.display = saAvailable(desc) ? '' : 'none';
    byId('params-defaults-info').textContent = defaultsLine(desc.params);
    renderCustom(byId('params-custom'), desc.params, saved().hp[desc.name] || {}, saveCustom);
    renderGrid(byId('gs-params-grid'), gridParams(desc), () => updateGridCount(desc));
    updateGridCount(desc);
    if (saAvailable(desc)) renderSa(byId('sa-space-grid'), saParams(desc), saved().sa[desc.name], saveSa);
  }
  setTab(tabOf());
}

function applyAlgorithm(name) {
  desc = descriptor(name) || algorithms()[0];
  byId('q-algorithm').value = desc.name;
  save({ algorithm: desc.name });
  byId('q-speed-ema-group').style.display = desc.capabilities.needs_snapshot ? '' : 'none';
  updateCheckpointStatus(desc);
  updateJamFields(desc);
  renderParams();
}

// ── Add to queue ──────────────────────────────────────────────────────────
async function addToQueue() {
  if (tabOf() === 'grid') { await addGridToQueue(desc); refreshQueue(); return; }
  const payload = basePayload(desc);
  if (tabOf() === 'custom') payload.hyperparams = readCustom(byId('params-custom'), desc.params);
  if (await postQueue(payload)) refreshQueue();
}

function applyValues(algorithm, values) {
  const hp = {};
  Object.entries(values || {}).forEach(([k, v]) => {
    if (isPlatform(k)) byId('q-speed-ema').value = v;
    else hp[k] = v;
  });
  saved().hp[algorithm] = { ...(saved().hp[algorithm] || {}), ...hp };
  saved().tab[algorithm] = 'custom';
  saveCommon();
}

function initSpeedEma() {
  const ema = platformTunables().find(p => p.name === 'speed_ema_alpha');
  const input = byId('q-speed-ema');
  byId('q-speed-ema-label').textContent = ema.label;
  byId('q-speed-ema-label').title = ema.help;
  input.value = ema.default;
  input.min = ema.bounds[0];
  input.max = ema.bounds[1];
}

function wire() {
  byId('q-algorithm').addEventListener('change', e => applyAlgorithm(e.target.value));
  COMMON_IDS.forEach(id => {
    const el = byId(id);
    el.addEventListener('change', saveCommon);
    el.addEventListener('input', saveCommon);
  });
  byId('q-network').addEventListener('change', refreshJamPreview);
  byId('q-repeats').addEventListener('input', () => updateWeightsFields(tabOf() === 'sa'));
  document.querySelectorAll('input[name="q-weights"]').forEach(r => r.addEventListener('change', () => {
    save({ weights: weightsMode() });
    updateWeightsFields(tabOf() === 'sa');
  }));
  byId('ckpt-reset-btn').addEventListener('click', () => resetCheckpoint(desc).then(() => updateCheckpointStatus(desc)));
  byId('q-jam-enabled').addEventListener('change', () => updateJamFields(desc));
  byId('q-jam-edges').addEventListener('change', refreshJamPreview);
  document.querySelectorAll('.tab').forEach(b => b.addEventListener('click', () => setTab(b.dataset.tab)));
  byId('params-reset').addEventListener('click', () => {
    saved().hp[desc.name] = {};
    save();
    renderCustom(byId('params-custom'), desc.params, {}, saveCustom);
  });
  byId('btn-add').addEventListener('click', addToQueue);
  byId('sa-reset-settings').addEventListener('click', () => { resetSaSettings(); saveCommon(); });
  byId('sa-reset-space').addEventListener('click', () => {
    saved().sa[desc.name] = {};
    save();
    renderSa(byId('sa-space-grid'), saParams(desc), {}, saveSa);
  });
  ['sa-n-iter', 'sa-t-init', 'sa-alpha'].forEach(id => byId(id).addEventListener('input', updateSchedulePreview));
  byId('btn-sa-start').addEventListener('click', () => saStart(desc).then(ok => { if (ok) refreshQueue(); }));
  byId('sa-apply-best').addEventListener('click', () => {
    const s = lastSaStatus();
    if (!s || !s.best_solution) return;
    applyValues(s.algorithm, s.best_solution);
    applyAlgorithm(s.algorithm);
  });
}

async function init() {
  await loadAlgorithms();
  startHeaderStatus();
  byId('q-algorithm').innerHTML = algorithms().map(a => `<option value="${a.name}">${escHtml(a.label)}</option>`).join('');
  initSpeedEma();
  restoreCommon();
  setWeightsMode(saved().weights);
  // ── Load network options ──────────────────────────────────────────────────
  initNetworkPicker(await fetch('/api/networks').then(r => r.json()));
  const prefill = takePrefill();
  if (prefill) {
    applyValues(prefill.algorithm, { ...prefill.hyperparams, ...prefill.platform });
    save({ algorithm: prefill.algorithm });
  }
  setNetwork((prefill && prefill.network_name) || saved().common['q-network']);
  wire();
  applyAlgorithm(saved().algorithm);
  saveCommon();
  updateSchedulePreview();
  initQueue();
  fetch('/api/sa/has-state-file').then(r => r.json()).then(d => {
    if (d.exists) byId('sa-state-exists').style.display = '';
  }).catch(() => {});
}

init();
