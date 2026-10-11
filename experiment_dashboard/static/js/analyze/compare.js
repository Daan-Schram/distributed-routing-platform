import { byId, escHtml, fmt, formatGroupLabel, makeStatSection, reformatDate } from '../shared/util.js';
import { A_COLOR, B_COLOR, chartWrap, destroyAllCharts, makeChart, mergedOptions, mLineDS } from '../shared/charts.js';
import { descriptor, labelOf } from '../shared/algorithms.js';
import { SIDEBAR_LIMIT, cachedExperiment, candidateSummary, fetchExperiment, fetchGroupData, jamBadge, jamKey,
         pickBestChild, repBadge, searchText, state, stripTags, syncUrl, variedCols } from './common.js';
import { renderAlgorithmMetrics, renderCharts, renderConfig, renderResources, renderSummary, renderTripDistribution,
         seriesSpecs } from './panels.js';

const SECTIONS = ['config', 'summary', 'charts', 'algorithm', 'distribution', 'resources'];

const displayText = e => `${reformatDate(e.date)} ${e.time.slice(0, 5)} · ${labelOf(e.algorithm)} · ${e.network_name || 'unknown'}`;

// ── Compare mode ─────────────────────────────────────────
export function toggleCompare() {
  if (state.compareMode) exitCompareMode(); else enterCompareMode();
}

export function enterCompareMode() {
  state.compareMode = true;
  state.compareView = 'sidebyside';
  state.compareRunIds.a = null;
  state.compareRunIds.b = null;
  const btn = byId('compare-btn');
  btn.classList.add('active');
  btn.textContent = 'Single ⊟';
  document.body.classList.add('compare-mode');

  const wrapper = document.createElement('div');
  wrapper.id = 'compare-wrapper';
  wrapper.className = 'compare-wrapper';

  const strip = document.createElement('div');
  strip.className = 'compare-header-strip';

  const groupIdSet = new Set(state.allGroups.map(g => g.group_id));
  const expByRunId = {};
  state.allExperiments.forEach(e => { expByRunId[e.run_id] = e; });

  const dropdownItems = [];
  state.allExperiments
    .filter(e => !e.group_id || !groupIdSet.has(e.group_id))
    .forEach(e => dropdownItems.push({ kind: 'exp', sortKey: e.date + ' ' + e.time, data: e }));
  state.allGroups.forEach(g => {
    const members = state.allExperiments.filter(e => e.group_id === g.group_id);
    const sortKey = members.length
      ? members.map(e => e.date + ' ' + e.time).sort().reverse()[0]
      : (g.date || '0000-00-00') + ' 00:00:00';
    dropdownItems.push({ kind: 'group', sortKey, data: g });
  });
  dropdownItems.sort((a, b) => b.sortKey.localeCompare(a.sortKey));

  const childDesc = (e) => {
    const meta = expByRunId[e.run_id];
    if (meta) return meta;
    const parts = String(e.run_id).split('_');
    const ds = parts.length >= 2 ? parts[parts.length - 2] : '';
    const ts = parts.length >= 1 ? parts[parts.length - 1] : '';
    return {
      run_id: e.run_id,
      date: ds.length === 8 ? `${ds.slice(0, 4)}-${ds.slice(4, 6)}-${ds.slice(6)}` : 'unknown',
      time: ts.length >= 4 ? `${ts.slice(0, 2)}:${ts.slice(2, 4)}` : '',
      algorithm: (e.config || {}).algorithm,
      network_name: (e.config || {}).network_name,
    };
  };

  ['a', 'b'].forEach(ns => {
    const hdr = document.createElement('div');
    hdr.className = 'compare-panel-header';

    const lbl = document.createElement('span');
    lbl.className = 'compare-panel-label';
    lbl.textContent = ns === 'a' ? 'Experiment A' : 'Experiment B';

    // Custom dropdown
    const drop = document.createElement('div');
    drop.className = 'cmp-dropdown';
    drop.id = 'exp-select-' + ns;

    const trigger = document.createElement('div');
    trigger.className = 'cmp-trigger';
    trigger.tabIndex = 0;
    const trigText = document.createElement('span');
    trigText.className = 'cmp-trigger-text';
    trigText.textContent = '— select experiment —';
    const chevron = document.createElement('span');
    chevron.className = 'cmp-chevron';
    chevron.textContent = '▾';
    trigger.appendChild(trigText);
    trigger.appendChild(chevron);

    const listbox = document.createElement('div');
    listbox.className = 'cmp-listbox';
    const searchWrap = document.createElement('div');
    searchWrap.className = 'cmp-search-wrap';
    searchWrap.innerHTML = '<input class="sidebar-search" type="search" placeholder="Search…" autocomplete="off">';
    listbox.appendChild(searchWrap);
    const searchInput = searchWrap.querySelector('input');
    searchWrap.addEventListener('click', ev => ev.stopPropagation());
    let showAllOpts = false;
    const applyDropFilter = () => {
      const q = searchInput.value.trim().toLowerCase();
      listbox.querySelectorAll('.cmp-option[data-search]').forEach(o => o.classList.toggle('cmp-hidden', !!q && !o.dataset.search.includes(q)));
      listbox.querySelectorAll('.cmp-group-head').forEach(head => {
        const kids = head._kids;
        const anyKid = kids && [...kids.querySelectorAll('.cmp-option[data-search]')].some(o => !o.classList.contains('cmp-hidden'));
        const selfMatch = !q || (head.dataset.search || '').includes(q);
        head.classList.toggle('cmp-hidden', !(selfMatch || anyKid));
        if (head._best) head._best.classList.toggle('cmp-hidden', !(selfMatch || anyKid));
        if (kids) kids.classList.toggle('open', !!q ? anyKid : kids._userOpen);
      });
      const standalone = [...listbox.querySelectorAll('.cmp-option.cmp-standalone')].filter(o => !o.classList.contains('cmp-hidden'));
      const limit = (!q && !showAllOpts) ? SIDEBAR_LIMIT : Infinity;
      standalone.forEach((o, i) => o.classList.toggle('cmp-more-hidden', i >= limit));
      const more = listbox.querySelector('.cmp-show-more');
      if (more) {
        const extra = Math.max(0, standalone.length - limit);
        more.style.display = extra > 0 ? '' : 'none';
        more.querySelector('.cmp-opt-meta').textContent = `Show ${extra} more run${extra === 1 ? '' : 's'}…`;
      }
    };
    searchInput.addEventListener('input', applyDropFilter);

    const commit = (runId, text, optEl) => {
      listbox.querySelectorAll('.cmp-option').forEach(o => o.classList.remove('active'));
      if (optEl) optEl.classList.add('active');
      trigText.textContent = text;
      trigText.classList.add('has-value');
      trigger.classList.remove('open');
      listbox.classList.remove('open');
      loadAndRender(runId, ns);
    };
    const makeOption = (e, extraClass) => {
      const opt = document.createElement('div');
      opt.className = 'cmp-option' + (extraClass ? ' ' + extraClass : '');
      opt.dataset.runId = e.run_id;
      opt.dataset.search = searchText(e);
      const badgeTxt = [jamBadge(e), repBadge(e)].map(stripTags).filter(Boolean).join(' ');
      const display = displayText(e) + (badgeTxt ? ` · ${badgeTxt}` : '');
      opt.innerHTML = `<span class="cmp-opt-name">${reformatDate(e.date)} ${e.time.slice(0, 5)} · ${escHtml(labelOf(e.algorithm))}${jamBadge(e)}${repBadge(e)}</span><span class="cmp-opt-meta">${escHtml(e.network_name || 'unknown')}</span>`;
      opt.addEventListener('click', () => commit(e.run_id, display, opt));
      return opt;
    };
    const markMismatches = () => {
      const other = state.compareRunIds[ns === 'a' ? 'b' : 'a'];
      const om = other ? (expByRunId[other] || (cachedExperiment(other) || {}).config) : null;
      const ok = om ? jamKey(om) : null;
      listbox.querySelectorAll('.cmp-option[data-run-id]').forEach(o => {
        const m = expByRunId[o.dataset.runId] || (cachedExperiment(o.dataset.runId) || {}).config;
        const k = m ? jamKey(m) : null;
        o.classList.toggle('cmp-mismatch', !!(ok && k && k !== ok));
      });
    };
    trigger.addEventListener('click', markMismatches);

    const appendGroup = (g) => {
      const head = document.createElement('div');
      head.className = 'cmp-group-head';
      head.innerHTML =
        `<span class="cmp-group-badge">⊞</span>` +
        `<div class="cmp-group-title">` +
          `<div class="cmp-group-name">${escHtml(formatGroupLabel(g))}</div>` +
          `<div class="cmp-group-sub">${escHtml(g.network_name || 'unknown')} · ${g.count} run${g.count !== 1 ? 's' : ''}</div>` +
        `</div>` +
        `<button type="button" class="cmp-group-expand" title="Show all candidates">▶</button>`;
      head.dataset.search = searchText({ ...g, label: formatGroupLabel(g) });
      listbox.appendChild(head);

      const bestSlot = document.createElement('div');
      bestSlot.innerHTML = `<div class="cmp-option" style="cursor:default"><span class="cmp-opt-meta">Loading best…</span></div>`;
      listbox.appendChild(bestSlot);

      const children = document.createElement('div');
      children.className = 'cmp-group-children';
      children._userOpen = false;
      listbox.appendChild(children);
      head._kids = children;
      head._best = bestSlot;

      const renderFrom = (data) => {
        const exps = (data.experiments || []).filter(e => e.status === 'done' || e.att);
        const { bestId, bestAtt, isSaScore } = pickBestChild(exps);

        bestSlot.innerHTML = '';
        if (bestId) {
          const be = exps.find(e => e.run_id === bestId);
          const desc = childDesc(be);
          const opt = makeOption(desc, 'cmp-best');
          opt.querySelector('.cmp-opt-name').innerHTML =
            `★ best · ${reformatDate(desc.date)} ${desc.time}`;
          opt.querySelector('.cmp-opt-meta').textContent =
            (bestAtt != null ? `${isSaScore ? 'score' : 'ATT'} ${Math.round(bestAtt)}s · ` : '') +
            (desc.network_name || 'unknown');
          bestSlot.appendChild(opt);
        } else {
          const none = document.createElement('div');
          none.className = 'cmp-option';
          none.style.cursor = 'default';
          none.innerHTML = `<span class="cmp-opt-meta">No completed run — expand to pick one</span>`;
          bestSlot.appendChild(none);
        }

        children.innerHTML = '';
        const cands = GroupView.groupByCandidate(exps);
        const cols = variedCols(exps);
        cands.forEach((cd, idx) => {
          const sm = candidateSummary(cd, idx, cols);
          if (!sm.runId) return;
          const pickRun = cd.runs.find(r => r.run_id === sm.runId) || cd.runs[0];
          const desc = childDesc(pickRun);
          const opt = makeOption(desc, 'cmp-child');
          opt.dataset.search = [sm.label, sm.score, sm.params, searchText(desc)].join(' ').toLowerCase();
          opt.querySelector('.cmp-opt-name').innerHTML =
            `${escHtml(sm.label)} ${escHtml(sm.score)}${cd.isBest ? ' ★' : ''} <span style="color:var(--text-dim);font-weight:400;">${escHtml(sm.dec)}</span>${jamBadge(pickRun.config)}`;
          opt.querySelector('.cmp-opt-meta').textContent = sm.params || `${reformatDate(desc.date)} ${desc.time}`;
          children.appendChild(opt);
          if (cd.runs.length > 1) {
            const tog = document.createElement('button');
            tog.type = 'button'; tog.className = 'cmp-cand-toggle'; tog.textContent = '▶'; tog.title = 'Show repeats';
            opt.style.flexDirection = 'row'; opt.style.flexWrap = 'wrap'; opt.style.alignItems = 'center';
            opt.querySelector('.cmp-opt-meta').style.flexBasis = '100%';
            opt.insertBefore(tog, opt.querySelector('.cmp-opt-meta'));
            const reps = document.createElement('div');
            reps.className = 'cmp-rep-list';
            cd.runs.forEach((r, i) => {
              const rd = childDesc(r);
              const ro = makeOption(rd, 'cmp-child cmp-rep');
              ro.dataset.search = opt.dataset.search + ' ' + searchText(rd);
              const att = r.att && r.att.mean != null ? `${Math.round(r.att.mean)} s` : (r.status === 'done' ? '—' : r.status);
              ro.querySelector('.cmp-opt-name').innerHTML = `${repBadge(r.config) || `run ${i + 1}`} ${escHtml(att)}${r.timed_out ? ' ⏱' : ''}`;
              ro.querySelector('.cmp-opt-meta').textContent = `${reformatDate(rd.date)} ${rd.time}`;
              reps.appendChild(ro);
            });
            children.appendChild(reps);
            tog.addEventListener('click', ev => { ev.stopPropagation(); const o = reps.classList.toggle('open'); tog.classList.toggle('open', o); });
          }
        });
        applyDropFilter();
      };

      fetchGroupData(g.group_id).then(renderFrom);

      const expandBtn = head.querySelector('.cmp-group-expand');
      head.addEventListener('click', (ev) => {
        ev.stopPropagation();  // keep dropdown open
        const open = children.classList.toggle('open');
        children._userOpen = open;
        expandBtn.classList.toggle('open', open);
      });
    };

    dropdownItems.forEach(item => {
      if (item.kind === 'exp') listbox.appendChild(makeOption(item.data, 'cmp-standalone'));
      else appendGroup(item.data);
    });
    const moreOpt = document.createElement('div');
    moreOpt.className = 'cmp-option cmp-show-more';
    moreOpt.innerHTML = '<span class="cmp-opt-meta" style="color:var(--blue);font-weight:600;"></span>';
    moreOpt.addEventListener('click', ev => { ev.stopPropagation(); showAllOpts = true; applyDropFilter(); });
    listbox.appendChild(moreOpt);
    applyDropFilter();

    function toggleDrop(e) {
      e.stopPropagation();
      const isOpen = listbox.classList.contains('open');
      // Close all compare dropdowns
      document.querySelectorAll('.cmp-listbox').forEach(lb => lb.classList.remove('open'));
      document.querySelectorAll('.cmp-trigger').forEach(t => t.classList.remove('open'));
      if (!isOpen) { listbox.classList.add('open'); trigger.classList.add('open'); }
    }
    trigger.addEventListener('click', toggleDrop);
    trigger.addEventListener('keydown', ev => { if (ev.key === 'Enter' || ev.key === ' ') toggleDrop(ev); });

    drop.appendChild(trigger);
    drop.appendChild(listbox);

    hdr.appendChild(lbl);
    hdr.appendChild(drop);
    strip.appendChild(hdr);
  });

  // Close dropdowns when clicking outside
  const cmpOutside = () => {
    document.querySelectorAll('.cmp-listbox').forEach(lb => lb.classList.remove('open'));
    document.querySelectorAll('.cmp-trigger').forEach(t => t.classList.remove('open'));
  };
  document.addEventListener('click', cmpOutside);
  wrapper._cmpOutside = cmpOutside;

  wrapper.appendChild(strip);

  const viewBar = document.createElement('div');
  viewBar.className = 'compare-viewbar';
  viewBar.innerHTML = `
    <div class="cmp-viewtoggle">
      <button type="button" class="cmp-vbtn active" data-view="sidebyside">Side-by-side</button>
      <button type="button" class="cmp-vbtn" data-view="inline">Inline diff</button>
    </div>
    <span class="cmp-viewbar-hint">Inline diff overlays both runs and shows Δ vs B on each metric.</span>`;
  viewBar.querySelectorAll('.cmp-vbtn').forEach(b =>
    b.addEventListener('click', () => setCompareView(b.dataset.view)));
  wrapper.appendChild(viewBar);

  const scroll = document.createElement('div');
  scroll.id = 'compare-scroll';
  scroll.className = 'compare-scroll';
  wrapper.appendChild(scroll);
  document.querySelector('.main-wrap').appendChild(wrapper);

  buildCompareScaffold();

  if (state.selectedRunId) {
    const dropA = byId('exp-select-a');
    if (dropA) {
      const opt = dropA.querySelector(`.cmp-option[data-run-id="${state.selectedRunId}"]`);
      if (opt) opt.click();
    }
  }
  syncUrl();
}

export function selectCompareRun(runId, ns) {
  const drop = byId('exp-select-' + ns);
  const opt = drop && drop.querySelector(`.cmp-option[data-run-id="${runId}"]`);
  if (opt) { opt.click(); return; }
  const e = state.allExperiments.find(x => x.run_id === runId);
  const text = drop && drop.querySelector('.cmp-trigger-text');
  if (text) {
    text.textContent = e ? displayText(e) : runId;
    text.classList.add('has-value');
  }
  loadAndRender(runId, ns);
}

export function exitCompareMode() {
  state.compareMode = false;
  const btn = byId('compare-btn');
  btn.classList.remove('active');
  btn.textContent = 'Compare ⊞';
  document.body.classList.remove('compare-mode');
  const wrapper = byId('compare-wrapper');
  if (wrapper) {
    if (wrapper._cmpOutside) document.removeEventListener('click', wrapper._cmpOutside);
    wrapper.remove();
  }
  syncUrl();
}

function setSections(ns, html) {
  SECTIONS.forEach(s => {
    const el = byId('compare-sec-' + ns + '-' + s);
    if (el) el.innerHTML = html;
  });
}

function loadAndRender(runId, ns) {
  if (!runId) return;
  state.compareRunIds[ns] = runId;
  syncUrl();

  const afterLoad = () => {
    if (state.compareView === 'inline') renderInlineCompare();
    else renderSideBySide(ns);
  };

  if (cachedExperiment(runId)) { afterLoad(); return; }
  const inlineHost = () => byId('inline-content');
  if (state.compareView === 'inline') {
    if (inlineHost()) inlineHost().innerHTML = '<div class="empty-state">Loading…</div>';
  } else {
    setSections(ns, '<div style="color:var(--text-dim);font-size:13px;padding:8px 0;">Loading…</div>');
  }
  fetchExperiment(runId)
    .then(afterLoad)
    .catch(() => {
      if (state.compareView === 'inline') {
        if (inlineHost()) inlineHost().innerHTML = '<div class="empty-state" style="color:var(--red);">Failed to load experiment</div>';
      } else {
        setSections(ns, '<div style="color:var(--red);font-size:13px;padding:8px 0;">Failed to load</div>');
      }
    });
}

function renderSideBySide(ns) {
  const runId = state.compareRunIds[ns];
  const data = runId ? cachedExperiment(runId) : null;
  if (!data) return;
  const cards = {
    config: renderConfig(data.config, data),
    summary: renderSummary(data),
    charts: renderCharts(data, ns),
    algorithm: renderAlgorithmMetrics(data, ns),
    distribution: renderTripDistribution(data, ns),
    resources: renderResources(data.resources, ns),
  };
  SECTIONS.forEach(s => {
    const el = byId('compare-sec-' + ns + '-' + s);
    if (!el) return;
    el.innerHTML = '';
    if (cards[s]) el.appendChild(cards[s]);
  });
}

function setCompareView(view) {
  if (view === state.compareView) return;
  state.compareView = view;
  buildCompareScaffold();
  document.querySelectorAll('.cmp-vbtn').forEach(b =>
    b.classList.toggle('active', b.dataset.view === view));
  if (view === 'inline') {
    renderInlineCompare();
  } else {
    if (state.compareRunIds.a) renderSideBySide('a');
    if (state.compareRunIds.b) renderSideBySide('b');
  }
}

function buildCompareScaffold() {
  const scroll = byId('compare-scroll');
  if (!scroll) return;
  destroyAllCharts();
  scroll.innerHTML = '';
  if (state.compareView === 'inline') {
    const host = document.createElement('div');
    host.id = 'inline-content';
    host.className = 'inline-content';
    scroll.appendChild(host);
  } else {
    SECTIONS.forEach(sec => {
      const row = document.createElement('div');
      row.className = 'compare-row';
      row.id = 'compare-row-' + sec;
      ['a', 'b'].forEach(ns => {
        const col = document.createElement('div');
        col.className = 'compare-col';
        col.id = 'compare-sec-' + ns + '-' + sec;
        row.appendChild(col);
      });
      scroll.appendChild(row);
    });
  }
}

function renderInlineCompare() {
  const host = byId('inline-content');
  if (!host) return;
  destroyAllCharts();
  host.innerHTML = '';

  const dA = state.compareRunIds.a ? cachedExperiment(state.compareRunIds.a) : null;
  const dB = state.compareRunIds.b ? cachedExperiment(state.compareRunIds.b) : null;
  if (!dA || !dB) {
    host.innerHTML = '<div class="empty-state">Select an experiment for both A and B to see inline differences.</div>';
    return;
  }

  const narrow = document.createElement('div');
  narrow.className = 'inline-narrow';
  narrow.appendChild(inlineLegend(dA, dB));
  const jamWarn = bottleneckMismatch(dA, dB);
  if (jamWarn) narrow.appendChild(jamWarn);
  narrow.appendChild(renderSummaryDiff(dA, dB));
  narrow.appendChild(renderChartsMerged(dA, dB));

  const algCard = renderAlgorithmMerged(dA, dB);
  if (algCard) narrow.appendChild(algCard);
  host.appendChild(narrow);

  const distSec = sideBySideSection(
    'Trip Distribution',
    'Histograms can’t be overlaid into one chart, A and B are shown side-by-side.',
    dA, dB, (d, ns) => renderTripDistribution(d, ns));
  if (distSec) host.appendChild(distSec);

  const resSec = sideBySideSection(
    'Resources',
    'Per-layer resource charts can’t be merged, A and B are shown side-by-side.',
    dA, dB, (d, ns) => renderResources(d.resources, ns));
  if (resSec) host.appendChild(resSec);
}

function expLabel(d) {
  return labelOf(d.config.algorithm) + ' · ' + (d.config.network_name || 'unknown');
}

function inlineLegend(dA, dB) {
  const div = document.createElement('div');
  div.className = 'inline-legend';
  const item = (tag, color, d) => `
    <div class="inline-legend-item">
      <span class="inline-legend-swatch" style="background:${color}"></span>
      <span class="inline-legend-tag" style="color:${color}">${tag}</span>
      <span class="inline-legend-meta">${escHtml(expLabel(d))} · ATT ${fmt((d.att || {}).mean, 1)} s</span>
    </div>`;
  div.innerHTML = item('A', A_COLOR, dA) + item('B', B_COLOR, dB);
  return div;
}

const _g = (obj, path) => path.split('.').reduce((o, k) => (o == null ? null : o[k]), obj);

function diffCell(label, aVal, bVal, unit, decimals, better) {
  const valStr = (aVal == null || aVal === undefined)
    ? '—'
    : (typeof aVal === 'number' ? aVal.toFixed(decimals) : String(aVal));
  let chip = '';
  if (typeof aVal === 'number' && typeof bVal === 'number') {
    const delta = aVal - bVal;
    const sign = delta > 0 ? '+' : '';
    let cls = 'neutral';
    if (better && delta !== 0) {
      const aBetter = better === 'lower' ? delta < 0 : delta > 0;
      cls = aBetter ? 'good' : 'bad';
    }
    chip = `<div class="stat-delta ${cls}">Δ ${sign}${delta.toFixed(decimals)}${unit ? ' ' + unit : ''} vs B</div>`;
  }
  return `<div class="stat-cell">
    <div class="stat-label">${label}</div>
    <div class="stat-val">${valStr}<span class="stat-unit">${unit ? ' ' + unit : ''}</span></div>
    ${chip}
  </div>`;
}

function bottleneckMismatch(dA, dB) {
  if (jamKey(dA.config) === jamKey(dB.config)) return null;
  const el = document.createElement('div');
  el.style.cssText = 'padding:8px 10px; margin-bottom:12px; border-radius:5px; font-size:12px; '
    + 'border:1px solid var(--red); background:rgba(224,49,49,0.08); color:var(--red);';
  el.textContent = `Different bottlenecks — A: ${dA.config.bottleneck}, B: ${dB.config.bottleneck}. These runs are not comparable.`;
  return el;
}

function renderSummaryDiff(dA, dB) {
  const card = document.createElement('div');
  card.className = 'card';
  card.innerHTML = `<div class="card-header"><span class="card-title">Summary — A vs B</span></div>`;
  const body = document.createElement('div');
  body.className = 'card-body';
  const sections = document.createElement('div');
  sections.className = 'stat-sections';

  // [label, path, unit, decimals, better, scale]
  const SPECS = {
    'Run Overview': [
      ['Throughput', 'throughput_vehicles_per_sim_min', 'veh/min', 2, 'higher', 1],
      ['Congestion', 'congestion_vehicle_seconds', 'veh·s', 0, 'lower', 1],
      ['Departures', 'departures', '', 0, null, 1],
      ['Sim Duration', 'sim_duration_s', 's', 1, null, 1],
    ],
    'Average Travel Time': [
      ['Mean', 'att.mean', 's', 1, 'lower', 1],
      ['Censored', 'att.censored_mean', 's', 1, 'lower', 1],
      ['Median', 'att.median', 's', 1, 'lower', 1],
      ['p95', 'att.p95', 's', 1, 'lower', 1],
      ['Count', 'att.count', '', 0, null, 1],
      ['Not arrived', 'att.not_arrived', '', 0, 'lower', 1],
    ],
    'Routing': [
      ['On Time', 'routing.on_time_pct', '%', 1, 'higher', 1],
      ['Timed Out', 'routing.timed_out_pct', '%', 1, 'lower', 1],
      ['Too Late', 'routing.too_late_pct', '%', 1, 'lower', 1],
      ['Teleports ↑', 'routing.teleports_started', '', 0, 'lower', 1],
    ],
    'Latency': [
      ['RTT Mean', 'round_trip_latency.mean', 'ms', 1, 'lower', 1000],
      ['Decision Mean', 'decision_latency.mean', 'ms', 1, 'lower', 1000],
    ],
    'Edge Travel Time': [
      ['Mean', 'edge_travel_time.mean', 's', 1, 'lower', 1],
      ['p95', 'edge_travel_time.p95', 's', 1, 'lower', 1],
    ],
    'Loops & Path Efficiency': [
      ['Hops ratio', 'loops.hops_ratio', '×', 2, 'lower', 1],
      ['Extra edges', 'loops.mean_extra_edges', '', 2, 'lower', 1],
      ['Looped', 'loops.looped_pct', '%', 1, 'lower', 1],
      ['Mean revisits', 'loops.mean_revisits', '', 2, 'lower', 1],
      ['Max revisits', 'loops.max_revisits', '', 0, 'lower', 1],
    ],
  };
  const desc = descriptor(dA.config.algorithm);
  if (desc && dA.config.algorithm === dB.config.algorithm && desc.metrics.summary.length) {
    SPECS[desc.label] = desc.metrics.summary.map(s =>
      [s.label, 'algorithm_metrics.summary.' + s.key, s.unit || '', s.decimals ?? 3, s.better || null, 1]);
  }

  Object.entries(SPECS).forEach(([secLabel, rows]) => {
    // Skip a section entirely if neither run has any value for it (e.g. Loops without capture)
    const hasAny = rows.some(([, path]) => _g(dA, path) != null || _g(dB, path) != null);
    if (!hasAny) return;
    const cells = rows.map(([label, path, unit, dec, better, scale]) => {
      let a = _g(dA, path), b = _g(dB, path);
      if (typeof a === 'number') a *= scale;
      if (typeof b === 'number') b *= scale;
      return diffCell(label, a, b, unit, dec, better);
    });
    sections.appendChild(makeStatSection(secLabel, cells));
  });

  body.appendChild(sections);
  card.appendChild(body);
  return card;
}

function renderChartsMerged(dA, dB) {
  const card = document.createElement('div');
  card.className = 'card';
  card.innerHTML = `<div class="card-header"><span class="card-title">Time Series — A vs B</span></div>`;
  const body = document.createElement('div');
  body.className = 'card-body';
  const grid = document.createElement('div');
  grid.className = 'charts-grid';

  const wrapArrivals = chartWrap('chart-arrivals-m', 'Cumulative Arrivals');
  wrapArrivals.classList.add('chart-full');
  grid.appendChild(wrapArrivals);
  grid.appendChild(chartWrap('chart-congestion-m', 'Congestion (vehicle-seconds per snapshot)'));
  grid.appendChild(chartWrap('chart-att-m', 'Travel Time per Arrival'));

  body.appendChild(grid);
  card.appendChild(body);

  const sampleScatter = (pts) => {
    let s = pts || [];
    if (s.length > 500) { const step = Math.ceil(s.length / 500); s = s.filter((_, i) => i % step === 0); }
    return s;
  };

  requestAnimationFrame(() => {
    makeChart('chart-arrivals-m', {
      type: 'line',
      data: {
        datasets: [
          mLineDS('A', (dA.trips_series || []).map(p => ({ x: p.sim_time, y: p.cumulative_arrivals })), A_COLOR),
          mLineDS('B', (dB.trips_series || []).map(p => ({ x: p.sim_time, y: p.cumulative_arrivals })), B_COLOR),
        ],
      },
      options: mergedOptions('Sim Time (s)', 'Vehicles'),
    });

    makeChart('chart-congestion-m', {
      type: 'line',
      data: {
        datasets: [
          mLineDS('A', (dA.congestion_series || []).map(p => ({ x: p.sim_time, y: p.vehicle_seconds })), A_COLOR),
          mLineDS('B', (dB.congestion_series || []).map(p => ({ x: p.sim_time, y: p.vehicle_seconds })), B_COLOR),
        ],
      },
      options: mergedOptions('Sim Time (s)', 'Vehicle-seconds'),
    });

    makeChart('chart-att-m', {
      type: 'scatter',
      data: {
        datasets: [
          { label: 'A', data: sampleScatter(dA.att_series).map(p => ({ x: p.arrival_sim_time, y: p.att })), backgroundColor: 'rgba(26,110,245,0.40)', pointRadius: 2 },
          { label: 'B', data: sampleScatter(dB.att_series).map(p => ({ x: p.arrival_sim_time, y: p.att })), backgroundColor: 'rgba(214,48,64,0.40)', pointRadius: 2 },
        ],
      },
      options: mergedOptions('Arrival Time (s)', 'Travel Time (s)'),
    });
  });

  return card;
}

function renderAlgorithmMerged(dA, dB) {
  const sA = dA.algorithm_metrics.series;
  const sB = dB.algorithm_metrics.series;
  const specs = [...seriesSpecs(dA.config.algorithm), ...seriesSpecs(dB.config.algorithm)]
    .filter((s, i, all) => all.findIndex(x => x.key === s.key) === i)
    .filter(s => sA.some(p => p[s.key] != null) || sB.some(p => p[s.key] != null));
  if (!specs.length) return null;

  const card = document.createElement('div');
  card.className = 'card';
  card.innerHTML = `<div class="card-header"><span class="card-title">Learner Metrics — A vs B</span></div>`;
  const body = document.createElement('div');
  body.className = 'card-body';
  const grid = document.createElement('div');
  grid.className = 'charts-grid';

  specs.forEach(s => grid.appendChild(chartWrap(`chart-alg-${s.key}-m`, s.label)));
  body.appendChild(grid);
  card.appendChild(body);

  requestAnimationFrame(() => {
    specs.forEach(s => {
      makeChart(`chart-alg-${s.key}-m`, {
        type: 'line',
        data: {
          datasets: [
            mLineDS('A', sA.map(p => ({ x: p.step, y: p[s.key] })), A_COLOR),
            mLineDS('B', sB.map(p => ({ x: p.step, y: p[s.key] })), B_COLOR),
          ],
        },
        options: mergedOptions('Step', s.label),
      });
    });
  });

  return card;
}

function sideBySideSection(title, note, dA, dB, renderFn) {
  const cards = [['A', dA, 'a'], ['B', dB, 'b']].map(([tag, d, ns]) => ({ tag, card: renderFn(d, ns) }));
  if (!cards.some(c => c.card)) return null;

  const wrap = document.createElement('div');
  wrap.className = 'inline-sbs-wrap';

  const noteEl = document.createElement('div');
  noteEl.className = 'inline-sbs-note';
  noteEl.innerHTML = `<strong>${escHtml(title)}</strong> — ${escHtml(note)}`;
  wrap.appendChild(noteEl);

  const grid = document.createElement('div');
  grid.className = 'inline-sbs-grid';
  cards.forEach(({ tag, card }) => {
    const col = document.createElement('div');
    col.className = 'inline-sbs-col';
    const lbl = document.createElement('div');
    lbl.className = 'inline-ab-tag';
    lbl.textContent = 'Experiment ' + tag;
    col.appendChild(lbl);
    col.appendChild(card || Object.assign(document.createElement('div'),
      { className: 'empty-state', style: 'padding:20px;', textContent: 'No data' }));
    grid.appendChild(col);
  });
  wrap.appendChild(grid);
  return wrap;
}
