import { escHtml, fmtParam } from '../shared/util.js';
import { labelOf } from '../shared/algorithms.js';

export const state = {
  selectedRunId: null,
  selectedGroupId: null,
  activeGroupChild: null,
  groupRefreshTimer: null,
  groupLastData: null,
  compareMode: false,
  compareView: 'sidebyside',
  compareRunIds: { a: null, b: null },
  allExperiments: [],
  allGroups: [],
  restoring: false,
};

export function stopGroupRefresh() {
  if (state.groupRefreshTimer) { clearInterval(state.groupRefreshTimer); state.groupRefreshTimer = null; }
}

export function syncUrl() {
  if (state.restoring) return;
  const q = new URLSearchParams();
  if (state.compareMode) {
    if (state.compareRunIds.a) q.set('a', state.compareRunIds.a);
    if (state.compareRunIds.b) q.set('b', state.compareRunIds.b);
  } else if (state.selectedRunId) {
    q.set('run', state.selectedRunId);
  } else if (state.selectedGroupId) {
    q.set('group', state.selectedGroupId);
  }
  const search = q.toString() ? '?' + q.toString() : '';
  if (search === location.search) return;
  history.pushState(null, '', location.pathname + search);
}

const _exp = {};
const _groups = {};

export const cachedExperiment = runId => _exp[runId] || null;

export function fetchExperiment(runId) {
  if (_exp[runId]) return Promise.resolve(_exp[runId]);
  return fetch('/api/experiment/' + runId).then(r => r.json()).then(d => (_exp[runId] = d));
}

export function fetchGroupData(groupId) {
  if (!_groups[groupId]) {
    _groups[groupId] = fetch('/api/group/' + groupId)
      .then(r => r.json())
      .catch(() => { delete _groups[groupId]; return { experiments: [] }; });
  }
  return _groups[groupId];
}

export function fetchLists() {
  return Promise.all([
    fetch('/api/experiments').then(r => r.json()),
    fetch('/api/groups').then(r => r.json()),
  ]);
}

export function jamKey(m) {
  m = m || {};
  return m.bottleneck == null ? null : `${m.bottleneck} | ${m.bottleneck_edges || ''}`;
}

export function jamBadge(m) {
  m = m || {};
  let txt = null, cls = '';
  if (m.jam_requested > 0 && m.bottleneck === 'requested but not applied') { txt = '⚠ jam n/a'; cls = ' na'; }
  else if (m.jam_requested > 0 && m.jam_factor) txt = `${m.jam_requested}×${Number(m.jam_factor)}`;
  return txt ? `<span class="jam-badge${cls}" title="Bottleneck: ${escHtml(m.bottleneck || '')}${m.bottleneck_edges ? ' (' + escHtml(m.bottleneck_edges) + ')' : ''}">${escHtml(txt)}</span>` : '';
}

export function repBadge(m) {
  m = m || {};
  if (!m.repeat_index) return '';
  const cls = m.loaded_checkpoint === true ? ' warm' : (m.warm_start && m.loaded_checkpoint === false) ? ' bad' : '';
  const title = m.loaded_checkpoint === true ? 'warm start' : (m.warm_start && m.loaded_checkpoint === false) ? 'warm start failed' : m.warm_start ? 'warm start (unconfirmed)' : 'cold start';
  return `<span class="rep-badge${cls}" title="repeat ${m.repeat_index} of ${m.repeats || '?'} · ${title}">r${m.repeat_index}/${m.repeats || '?'}</span>`;
}

export const stripTags = html => html.replace(/<[^>]+>/g, '');

export function warmStartInfo(cfg, prevCfg) {
  cfg = cfg || {};
  if (cfg.warm_start == null && cfg.loaded_checkpoint == null) return null;
  const steps = cfg.train_steps_at_start != null ? cfg.train_steps_at_start : cfg.checkpoint_train_steps_before;
  const from = steps != null ? ` from ${Number(steps).toLocaleString()} steps` : '';
  let text, cls = '';
  if (cfg.loaded_checkpoint === true)       { text = 'warm start' + from; cls = 'good'; }
  else if (cfg.loaded_checkpoint === false) { text = cfg.warm_start ? '⚠ warm start failed' : 'cold'; cls = cfg.warm_start ? 'bad' : ''; }
  else                                      { text = cfg.warm_start ? 'warm start' + from + ' (unconfirmed)' : 'cold'; }
  if (cfg.loaded_checkpoint !== false && cfg.warm_start && prevCfg && prevCfg.timed_out) { text += ' · ⚠ from timed-out repeat'; cls = 'bad'; }
  return { text, cls };
}

export function repeatChip(cfg, prevCfg) {
  cfg = cfg || {};
  if (!cfg.repeat_index) return '';
  const ws = warmStartInfo(cfg, prevCfg);
  const color = ws && ws.cls === 'bad' ? 'var(--red)' : ws && ws.cls === 'good' ? 'var(--green)' : 'var(--text-dim)';
  const txt = `repeat ${cfg.repeat_index} of ${cfg.repeats || '?'}` + (ws ? ' · ' + ws.text : '');
  return `<span style="font-size:10px;color:${color};white-space:nowrap;">${escHtml(txt)}</span>`;
}

export function prevRepeatOf(cfg, experiments) {
  if (!cfg.queue_item_id || !cfg.repeat_index || cfg.repeat_index < 2) return null;
  const prev = experiments.find(x => x.queue_item_id === cfg.queue_item_id && x.repeat_index === cfg.repeat_index - 1);
  return prev || null;
}

export function variedCols(experiments) {
  const paramMaps = experiments.map(e => GroupView.paramsOf(e.config));
  if (!paramMaps.length) return [];
  const allKeys = [...new Set(paramMaps.flatMap(p => Object.keys(p)))];
  return allKeys.filter(k => new Set(paramMaps.map(p => JSON.stringify(p[k]))).size > 1);
}

export function candidateSummary(cd, idx, cols) {
  const label = cd.confirm ? 'confirm' : `#${cd.iteration != null ? cd.iteration + 1 : idx + 1}`;
  const score = cd.score != null ? `${Math.round(cd.score)} s` : cd.status === 'done' ? 'invalid' : cd.status;
  const params = cols.map(c => `${c.replace(/_/g, ' ')} ${fmtParam(cd.params[c])}`).join(' · ');
  const dec = cd.confirm ? '' : cd.sa ? (cd.score == null ? '✗' : cd.sa.accepted ? '✓' : '✗') : '';
  return { runId: cd.pick ? cd.pick.run_id : null, label, score, params, dec };
}

export function pickBestChild(exps) {
  const best = GroupView.groupByCandidate(exps || []).find(c => c.isBest);
  if (!best) return { bestId: null, bestAtt: null, isSaScore: false };
  return { bestId: best.pick ? best.pick.run_id : null, bestAtt: best.score, isSaScore: !!best.sa };
}

export const SIDEBAR_LIMIT = 30;

export function searchText(e) {
  const bits = [e.run_id, e.group_id, e.algorithm, labelOf(e.algorithm), e.network_name, e.date, e.time, e.label,
                stripTags(jamBadge(e)), stripTags(repBadge(e))];
  return bits.filter(Boolean).join(' ').toLowerCase();
}
