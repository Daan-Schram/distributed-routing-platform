// ── Persistence ───────────────────────────────────────────────────────────
const KEY = 'ddqr_control';

export const COMMON_IDS = [
  'q-network', 'q-speed-ema', 'q-workers', 'q-repeats', 'q-broadcast', 'q-stats', 'q-max-runtime', 'q-sumo-seed',
  'q-use-gui', 'q-capture-trajectories', 'q-telemetry', 'q-log-proxies', 'q-jam-enabled', 'q-jam-edges', 'q-jam-factor',
  'sa-n-iter', 'sa-t-init', 'sa-alpha', 'sa-sigma', 'sa-n-perturb', 'sa-score-mode',
];

function load() {
  try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch (e) { return {}; }
}

const st = Object.assign({ common: {}, algorithm: null, tab: {}, hp: {}, sa: {}, weights: 'fresh' }, load());

export const saved = () => st;

export function save(patch) {
  Object.assign(st, patch || {});
  try { localStorage.setItem(KEY, JSON.stringify(st)); } catch (e) {}
}

export function saveCommon() {
  COMMON_IDS.forEach(id => {
    const el = document.getElementById(id);
    if (el) st.common[id] = el.type === 'checkbox' ? el.checked : el.value;
  });
  save();
}

export function restoreCommon() {
  COMMON_IDS.forEach(id => {
    const el = document.getElementById(id);
    if (!el || st.common[id] === undefined || id === 'q-network') return;
    if (el.type === 'checkbox') el.checked = st.common[id];
    else el.value = st.common[id];
  });
}
