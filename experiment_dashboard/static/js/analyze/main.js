import { byId, escHtml, fmtParam, formatGroupLabel, makeStatSection, reformatDate, startHeaderStatus, statCell,
         writePrefill } from '../shared/util.js';
import { chartWrap, destroyChart, legendChartOptions, makeChart } from '../shared/charts.js';
import { labelOf, loadAlgorithms, paramOf, referenceAlgorithms } from '../shared/algorithms.js';
import { SIDEBAR_LIMIT, candidateSummary, cachedExperiment, fetchExperiment, fetchGroupData, fetchLists, jamBadge,
         jamKey, repBadge, repeatChip, searchText, state, stopGroupRefresh, syncUrl, variedCols } from './common.js';
import { renderAlgorithmMetrics, renderBottleneckMap, renderCharts, renderConfig, renderResources, renderSummary,
         renderTripDistribution } from './panels.js';
import { renderRoutes } from './routes.js';
import { enterCompareMode, exitCompareMode, selectCompareRun, toggleCompare } from './compare.js';

// ── Sidebar ──────────────────────────────────────────────
const filters = { algorithm: 'all', network: 'all' };
let sidebarShowAll = false;
let sidebarQuery = '';

function applyFilters() {
  const list = byId('sidebar-list');
  const q = sidebarQuery.trim().toLowerCase();
  list.querySelectorAll('.exp-item').forEach(el => {
    const algMatch = filters.algorithm === 'all' || el.dataset.algorithm === filters.algorithm;
    const netMatch = filters.network === 'all' || el.dataset.network === filters.network;
    const qMatch = !q || (el.dataset.search || '').includes(q);
    const hidden = !(algMatch && netMatch && qMatch);
    el.classList.toggle('hidden', hidden);
    // Also hide the children container for group items
    if (el.classList.contains('exp-group')) {
      const gid = el.dataset.groupId;
      if (gid) {
        const children = byId('children-' + escHtml(gid));
        if (children) children.style.display = hidden ? 'none' : '';
      }
    }
  });
  const standalone = [...list.querySelectorAll('.exp-item:not(.exp-group)')].filter(el => !el.classList.contains('hidden'));
  const limit = (!q && !sidebarShowAll) ? SIDEBAR_LIMIT : Infinity;
  standalone.forEach((el, i) => el.classList.toggle('more-hidden', i >= limit));
  const more = byId('sidebar-show-more');
  if (more) {
    const extra = Math.max(0, standalone.length - limit);
    more.style.display = extra > 0 ? '' : 'none';
    more.textContent = `Show ${extra} more run${extra === 1 ? '' : 's'}…`;
  }
  list.querySelectorAll('.date-group').forEach(g => {
    const anyVisible = [...g.querySelectorAll('.exp-item')].some(el => !el.classList.contains('hidden') && !el.classList.contains('more-hidden'));
    g.classList.toggle('hidden', !anyVisible);
  });
}

function buildPills(key, values, label) {
  const container = byId('filter-' + key);
  container.innerHTML = ['all', ...values].map(v =>
    `<span class="filter-pill${filters[key] === v ? ' active' : ''}" data-val="${escHtml(v)}">${v === 'all' ? 'All' : escHtml(label(v))}</span>`
  ).join('');
  container.querySelectorAll('.filter-pill').forEach(pill => {
    pill.addEventListener('click', function () {
      filters[key] = this.dataset.val;
      buildPills(key, values, label);
      applyFilters();
    });
  });
}

function buildSidebar(experiments, groups) {
  state.allExperiments = experiments;
  state.allGroups = groups || [];
  const list = byId('sidebar-list');

  const groupIdSet = new Set(state.allGroups.map(g => g.group_id));
  const standaloneExps = experiments.filter(e => !e.group_id || !groupIdSet.has(e.group_id));

  const items = [];
  standaloneExps.forEach(e => items.push({ kind: 'exp', sortKey: e.date + ' ' + e.time, date: e.date, data: e }));
  state.allGroups.forEach(g => {
    const gExps = experiments.filter(e => e.group_id === g.group_id);
    const sortKey = gExps.length
      ? gExps.map(e => e.date + ' ' + e.time).sort().reverse()[0]
      : (g.date || '0000-00-00') + ' 00:00:00';
    items.push({ kind: 'group', sortKey, date: sortKey.slice(0, 10), data: g });
  });

  items.sort((a, b) => b.sortKey.localeCompare(a.sortKey));

  if (!items.length) {
    list.innerHTML = '<div style="padding:20px;color:var(--text-dim);font-size:13px;">No experiments found</div>';
    return;
  }

  const all = [...standaloneExps, ...state.allGroups];
  const algs = [...new Set(all.map(e => e.algorithm).filter(Boolean))].sort();
  const networks = [...new Set(all.map(e => e.network_name || 'unknown'))].sort();

  byId('sidebar-filters').style.display = '';
  if (algs.length > 1) {
    byId('filter-block-algorithm').style.display = '';
    buildPills('algorithm', algs, labelOf);
  }
  if (networks.length > 1) {
    byId('filter-block-network').style.display = '';
    buildPills('network', networks, v => v);
  }

  // Group items by date
  const byDate = {};
  items.forEach(item => {
    if (!byDate[item.date]) byDate[item.date] = [];
    byDate[item.date].push(item);
  });

  let html = '';
  Object.keys(byDate).sort((a, b) => b.localeCompare(a)).forEach(date => {
    html += `<div class="date-group"><div class="date-label">${reformatDate(date)}</div>`;
    byDate[date].forEach(item => {
      if (item.kind === 'exp') {
        const e = item.data;
        const network = e.network_name || 'unknown';
        html += `<div class="exp-item" data-run-id="${e.run_id}" data-algorithm="${escHtml(e.algorithm || '')}" data-network="${escHtml(network)}" data-search="${escHtml(searchText(e))}">
          <div style="min-width:0; flex:1;">
            <div class="exp-type">${escHtml(labelOf(e.algorithm))}${jamBadge(e)}${repBadge(e)}</div>
            <div class="exp-network">${escHtml(network)}</div>
          </div>
          <span class="exp-time">${e.time.slice(0, 5)}</span>
        </div>`;
      } else {
        const g = item.data;
        const network = g.network_name || 'unknown';
        const label = formatGroupLabel(g);
        const safeid = escHtml(g.group_id);
        html += `<div class="exp-item exp-group" data-group-id="${safeid}" data-algorithm="${escHtml(g.algorithm || '')}" data-network="${escHtml(network)}" data-search="${escHtml(searchText({ ...g, label }))}">
          <span class="exp-group-badge" title="Grid search group">⊞</span>
          <div style="min-width:0; flex:1;">
            <div class="exp-type">${escHtml(label)}${jamBadge(g)}</div>
            <div class="exp-network">${escHtml(network)} &middot; ${g.count} run${g.count !== 1 ? 's' : ''}</div>
          </div>
          <span class="exp-time">${item.sortKey.slice(11, 16)}</span>
          <button class="exp-group-expand" data-group-id="${safeid}" title="Show individual runs">&#9658;</button>
        </div>
        <div class="exp-group-children" id="children-${safeid}"></div>`;
      }
    });
    html += '</div>';
  });

  html += '<div class="show-more-row" id="sidebar-show-more" style="display:none"></div>';
  list.innerHTML = html;
  byId('sidebar-show-more').addEventListener('click', () => { sidebarShowAll = true; applyFilters(); });
  applyFilters();

  // Standalone experiment click
  list.querySelectorAll('.exp-item:not(.exp-group)').forEach(el => {
    el.addEventListener('click', function () {
      clearAllSelected();
      this.classList.add('selected');
      loadExperiment(this.dataset.runId);
    });
  });

  // Group header click: load group summary + auto-expand children
  list.querySelectorAll('.exp-item.exp-group').forEach(el => {
    el.addEventListener('click', function (e) {
      if (e.target.closest('.exp-group-expand')) return;
      clearAllSelected();
      this.classList.add('selected');
      loadGroup(this.dataset.groupId);
      const container = byId('children-' + escHtml(this.dataset.groupId));
      if (container && !container.classList.contains('open')) {
        toggleGroupChildren(this.dataset.groupId);
      }
    });
  });

  // Expand arrow click: toggle children list
  list.querySelectorAll('.exp-group-expand').forEach(btn => {
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      toggleGroupChildren(this.dataset.groupId);
    });
  });
}

function clearAllSelected() {
  byId('sidebar-list').querySelectorAll('.exp-item, .exp-child-item').forEach(el => el.classList.remove('selected'));
}

function toggleGroupChildren(groupId) {
  const safeId = escHtml(groupId);
  const container = byId('children-' + safeId);
  const btn = document.querySelector(`.exp-group-expand[data-group-id="${safeId}"]`);
  if (!container) return;

  const isOpen = container.classList.contains('open');
  if (isOpen) {
    container.classList.remove('open');
    if (btn) btn.classList.remove('open');
    return;
  }

  container.classList.add('open');
  if (btn) btn.classList.add('open');

  if (container.dataset.loaded) return;
  container.dataset.loaded = '1';
  container.innerHTML = '<div style="padding:5px 16px 5px 32px;color:var(--text-dim);font-size:12px;">Loading…</div>';

  fetchGroupData(groupId)
    .then(data => {
      const exps = data.experiments || [];
      if (!exps.length) { container.innerHTML = '<div style="padding:5px 16px 5px 32px;color:var(--text-dim);font-size:12px;">No runs found</div>'; return; }
      const cands = GroupView.groupByCandidate(exps);
      const cols = variedCols(exps);
      const multi = cands.some(cd => cd.runs.length > 1);
      let html = `<div class="exp-cand-head">${cands.length} candidate${cands.length !== 1 ? 's' : ''} · ${exps.length} run${exps.length !== 1 ? 's' : ''}</div>`;
      cands.forEach((cd, idx) => {
        const sm = candidateSummary(cd, idx, cols);
        const cls = cd.isBest ? ' exp-child-best' : '';
        html += `<div class="exp-child-item${cls}" data-run-id="${escHtml(sm.runId || '')}" data-group-id="${escHtml(groupId)}" data-cand="${escHtml(cd.key)}">
          <div style="min-width:0;flex:1;">
            <div class="exp-type" style="font-size:12px;">${escHtml(sm.label)} ${escHtml(sm.score)}${cd.isBest ? ' ★' : ''} <span style="color:var(--text-dim);font-weight:400;">${escHtml(sm.dec)}</span></div>
            ${sm.params ? `<div class="exp-network" style="font-size:10px;white-space:normal;">${escHtml(sm.params)}</div>` : ''}
          </div>
          ${multi ? `<button class="exp-cand-toggle" data-cand="${escHtml(cd.key)}" title="Show repeats">▶</button>` : ''}
        </div>
        <div class="exp-rep-list" data-cand="${escHtml(cd.key)}">`;
        cd.runs.forEach((r, i) => {
          const cfg = r.config || {};
          const att = r.att && r.att.mean != null ? `${Math.round(r.att.mean)} s` : (r.status === 'done' ? '—' : r.status);
          html += `<div class="exp-child-item exp-rep-item" data-run-id="${escHtml(r.run_id)}" data-group-id="${escHtml(groupId)}">
            <div style="min-width:0;flex:1;">
              <div class="exp-type" style="font-size:11px;">${repBadge(cfg) || `<span style="color:var(--text-dim)">run ${i + 1}</span>`} ${escHtml(att)}${r.timed_out ? ' ⏱' : ''}</div>
            </div>
          </div>`;
        });
        html += `</div>`;
      });
      container.innerHTML = html;
      container.querySelectorAll('.exp-child-item').forEach(el => {
        el.addEventListener('click', function (ev) {
          if (ev.target.closest('.exp-cand-toggle')) return;
          if (!this.dataset.runId) return;
          clearAllSelected();
          this.classList.add('selected');
          loadExperiment(this.dataset.runId);
        });
      });
      container.querySelectorAll('.exp-cand-toggle').forEach(btn => {
        btn.addEventListener('click', function (ev) {
          ev.stopPropagation();
          const lst = container.querySelector(`.exp-rep-list[data-cand="${this.dataset.cand}"]`);
          const open = lst.classList.toggle('open');
          this.classList.toggle('open', open);
        });
      });
    })
    .catch(() => { container.innerHTML = '<div style="padding:5px 16px 5px 32px;color:var(--red);font-size:12px;">Failed to load</div>'; });
}

// ── Load experiment ──────────────────────────────────────
function loadExperiment(runId) {
  state.selectedRunId = runId;
  state.activeGroupChild = runId;
  syncUrl();
  const panel = byId('right-panel');
  if (!cachedExperiment(runId)) panel.innerHTML = '<div class="empty-state">Loading…</div>';
  fetchExperiment(runId)
    .then(d => { if (state.selectedRunId === runId) renderResults(d); })
    .catch(() => { panel.innerHTML = '<div class="empty-state">Failed to load experiment</div>'; });
}

// ── Render all sections ──────────────────────────────────
function renderResults(d) {
  const panel = byId('right-panel');
  panel.innerHTML = '';

  // Delete button only in single mode
  const delRow = document.createElement('div');
  delRow.style.cssText = 'display:flex; justify-content:flex-end; margin-bottom:4px;';
  delRow.innerHTML = `<button id="delete-exp-btn" class="text-btn danger">Delete experiment</button>`;
  panel.appendChild(delRow);

  panel.appendChild(renderConfig(d.config, d));
  const bm = renderBottleneckMap(d);
  if (bm) panel.appendChild(bm);
  panel.appendChild(renderSummary(d));
  panel.appendChild(renderCharts(d, ''));
  panel.appendChild(renderRoutes(d));

  [renderAlgorithmMetrics(d, ''), renderTripDistribution(d, ''), renderResources(d.resources, '')]
    .forEach(c => { if (c) panel.appendChild(c); });
}

const openModal = id => byId(id).classList.add('open');
const closeModal = id => byId(id).classList.remove('open');

document.querySelectorAll('[data-close]').forEach(b => b.addEventListener('click', () => closeModal(b.dataset.close)));

// ── Delete modal ─────────────────────────────────────────
byId('delete-modal-confirm').addEventListener('click', () => {
  closeModal('delete-modal');
  const runId = state.selectedRunId;
  fetch('/api/experiment/' + runId, { method: 'DELETE' })
    .then(r => r.json())
    .then(() => {
      const el = document.querySelector(`.exp-item[data-run-id="${runId}"]`);
      if (el) el.remove();
      state.selectedRunId = null;
      syncUrl();
      byId('right-panel').innerHTML = '<div class="empty-state">Experiment deleted</div>';
    });
});

byId('right-panel').addEventListener('click', e => {
  if (e.target.id === 'delete-exp-btn') { openModal('delete-modal'); return; }
  const btn = e.target.closest('#delete-group-btn');
  if (!btn) return;
  const gid = btn.dataset.groupId;
  const n = parseInt(btn.dataset.doneCount, 10);
  byId('delete-group-modal-body').textContent =
    `This permanently removes ${n} experiment file${n !== 1 ? 's' : ''} for group "${gid}" and cancels all pending queue items. This cannot be undone.`;
  byId('delete-group-modal-confirm').dataset.groupId = gid;
  openModal('delete-group-modal');
});

// ── Delete group modal ───────────────────────────────────
byId('delete-group-modal-confirm').addEventListener('click', function () {
  const gid = this.dataset.groupId;
  closeModal('delete-group-modal');
  stopGroupRefresh();
  fetch('/api/group/' + gid, { method: 'DELETE' })
    .then(r => r.json())
    .then(() => {
      state.selectedGroupId = null;
      state.selectedRunId = null;
      syncUrl();
      byId('right-panel').innerHTML = '<div class="empty-state">Group deleted</div>';
      fetchLists().then(([exps, groups]) => buildSidebar(exps, groups)).catch(() => {});
    });
});

// ── Delete all modal ─────────────────────────────────────
byId('delete-all-btn').addEventListener('click', () => openModal('delete-all-modal'));
byId('delete-all-modal-confirm').addEventListener('click', () => {
  closeModal('delete-all-modal');
  fetch('/api/experiments', { method: 'DELETE' })
    .then(r => r.json())
    .then(() => {
      byId('sidebar-list').innerHTML = '<div style="padding:20px;color:var(--text-dim);font-size:13px;">No experiments found</div>';
      byId('sidebar-filters').style.display = 'none';
      state.selectedRunId = null;
      state.selectedGroupId = null;
      state.allExperiments = [];
      syncUrl();
      byId('right-panel').innerHTML = '<div class="empty-state">All experiments deleted</div>';
    });
});

// ── Groups ──────────────────────────────────────────────
function loadGroup(groupId) {
  stopGroupRefresh();
  state.selectedGroupId = groupId;
  state.selectedRunId = null;
  state.activeGroupChild = null;
  syncUrl();
  const panel = byId('right-panel');
  panel.innerHTML = '<div class="empty-state">Loading group…</div>';

  const active = d => d.experiments.some(e => e.status === 'running' || e.status === 'queued');
  fetch('/api/group/' + groupId)
    .then(r => r.json())
    .then(data => {
      renderGroup(data);
      if (!active(data)) return;
      state.groupRefreshTimer = setInterval(() => {
        if (state.selectedGroupId !== groupId) { stopGroupRefresh(); return; }
        fetch('/api/group/' + groupId)
          .then(r => r.json())
          .then(d => {
            if (state.activeGroupChild !== null) return;
            renderGroup(d);
            if (!active(d)) stopGroupRefresh();
          })
          .catch(() => {});
      }, 5000);
    })
    .catch(() => { panel.innerHTML = '<div class="empty-state">Failed to load group</div>'; });
}

const groupViewHelpers = {
  escHtml, makeChart, destroyChart, chartWrap, statCell, makeStatSection, legendChartOptions, fmtHyperval: fmtParam,
  jamBadge, jamKey, repeatChip, referenceAlgorithms, labelOf, paramOf, writePrefill,
  getVariedCols: variedCols,
  allExperiments: () => state.allExperiments,
  rerender: () => { if (state.selectedGroupId && state.groupLastData && state.groupLastData.meta.group_id === state.selectedGroupId) renderGroup(state.groupLastData); },
  openRun: (rid) => {
    state.activeGroupChild = rid;
    clearAllSelected();
    const item = byId('sidebar-list').querySelector(`.exp-item[data-run-id="${rid}"], .exp-child-item[data-run-id="${rid}"]`);
    if (item) { item.classList.add('selected'); item.scrollIntoView({ block: 'nearest' }); }
    loadExperiment(rid);
  },
};

function renderGroup(data) {
  const panel = byId('right-panel');
  state.groupLastData = data;
  if (!data.experiments.length) { panel.innerHTML = '<div class="empty-state">No runs in this group yet</div>'; return; }
  GroupView.render(panel, data, groupViewHelpers);
}

function selectSidebar(selector) {
  const el = document.querySelector(selector);
  if (!el) return;
  clearAllSelected();
  el.classList.add('selected');
  const reps = el.closest('.exp-rep-list');
  if (reps) { reps.classList.add('open'); const t = reps.parentElement.querySelector(`.exp-cand-toggle[data-cand="${reps.dataset.cand}"]`); if (t) t.classList.add('open'); }
  el.scrollIntoView({ block: 'nearest' });
}

function expandGroupThen(groupId, fn) {
  const container = byId('children-' + escHtml(groupId));
  if (container && !container.classList.contains('open')) toggleGroupChildren(groupId);
  fetchGroupData(groupId).then(fn).catch(() => {});
}

function restoreFromUrl() {
  const q = new URLSearchParams(location.search);
  const known = id => id && state.allExperiments.some(e => e.run_id === id);
  const a = q.get('a'), b = q.get('b'), run = q.get('run'), group = q.get('group');
  const wantCompare = known(a) || known(b);

  state.restoring = true;
  try {
    stopGroupRefresh();
    if (state.compareMode && !wantCompare) exitCompareMode();
    if (!wantCompare) { state.compareRunIds.a = null; state.compareRunIds.b = null; }

    if (wantCompare) {
      state.selectedRunId = null;
      state.selectedGroupId = null;
      if (state.compareMode) exitCompareMode();
      enterCompareMode();
      if (known(a)) selectCompareRun(a, 'a');
      if (known(b)) selectCompareRun(b, 'b');
    } else if (known(run)) {
      const e = state.allExperiments.find(x => x.run_id === run);
      const gid = e.group_id && state.allGroups.some(g => g.group_id === e.group_id) ? e.group_id : null;
      if (gid) {
        selectSidebar(`.exp-item.exp-group[data-group-id="${gid}"]`);
        expandGroupThen(gid, () => selectSidebar(`.exp-child-item[data-run-id="${run}"]`));
      } else {
        selectSidebar(`.exp-item[data-run-id="${run}"]`);
      }
      loadExperiment(run);
    } else if (group && state.allGroups.some(g => g.group_id === group)) {
      selectSidebar(`.exp-item.exp-group[data-group-id="${group}"]`);
      expandGroupThen(group, () => {});
      loadGroup(group);
    } else {
      state.selectedRunId = null;
      state.selectedGroupId = null;
      clearAllSelected();
      if (location.search) history.replaceState(null, '', location.pathname);
      if (!state.compareMode) byId('right-panel').innerHTML = '<div class="empty-state">Select an experiment from the list</div>';
    }
  } finally {
    state.restoring = false;
  }
}

// ── Init ─────────────────────────────────────────────────
byId('sidebar-search').addEventListener('input', function () {
  sidebarQuery = this.value;
  applyFilters();
});
byId('compare-btn').addEventListener('click', toggleCompare);
window.addEventListener('popstate', restoreFromUrl);
startHeaderStatus();

loadAlgorithms()
  .then(fetchLists)
  .then(([experiments, groups]) => {
    buildSidebar(experiments, groups);
    restoreFromUrl();
  })
  .catch(() => {
    byId('sidebar-list').innerHTML = '<div style="padding:20px;color:var(--text-dim);">Failed to load experiments</div>';
  });
