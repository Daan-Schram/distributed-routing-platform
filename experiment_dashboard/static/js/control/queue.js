import { byId, escHtml } from '../shared/util.js';
import { labelOf } from '../shared/algorithms.js';
import { onSaStatus, runsLeft, saActive } from './search.js';

const post = url => fetch(url, {method: 'POST'}).then(refreshQueue);

// ── Render queue table ────────────────────────────────────────────────────
function statusPill(status) {
  const map = {queued: 'pill-queued', running: 'pill-running', done: 'pill-done', error: 'pill-error'};
  const icons = {queued: '○', running: '▶', done: '✓', error: '!'};
  return `<span class="pill ${map[status] || 'pill-queued'}">${icons[status] || ''} ${status}</span>`;
}

// ── Kill running experiment ───────────────────────────────────────────────
function updateRunningStatus(qs) {
  const btn = byId('btn-kill');
  const text = byId('running-status-text');
  if (qs.experiment_running && qs.config && Object.keys(qs.config).length) {
    const c = qs.config;
    const rep = c.repeat_index && c.repeats ? ` · repeat ${c.repeat_index}/${c.repeats}` + (c.warm_start ? ' · warm' : '') : '';
    text.textContent = labelOf(c.algorithm) + ' on ' + (c.network_name || '?') + rep;
    text.style.color = 'var(--green)';
    btn.style.display = '';
  } else {
    text.textContent = 'No experiment running';
    text.style.color = 'var(--text-dim)';
    btn.style.display = 'none';
  }
}

function rowsHtml(items, sa) {
  const saGid = saActive(sa) ? sa.group_id : null;
  const groupCounts = {};
  items.forEach(item => { const g = item.group_id || ''; groupCounts[g] = (groupCounts[g] || 0) + 1; });
  const groupColor = {};
  let colorIdx = 0;
  items.forEach(item => {
    const g = item.group_id || '';
    if (groupCounts[g] > 1 && !(g in groupColor)) groupColor[g] = colorIdx++ % 5;
  });

  const rows = [];
  const shown = new Set();
  items.forEach((item, idx) => {
    const gid = item.group_id || '';
    const isMulti = groupCounts[gid] > 1;
    const isSa = gid.startsWith('sa_');
    if (isSa && !shown.has(gid)) {
      shown.add(gid);
      const active = gid === saGid;
      const iterStr = active ? `&nbsp;·&nbsp; ${sa.confirming ? 'confirming best' : `iteration ${(sa.iteration ?? 0) + 1} / ${sa.n_iter ?? '?'}`}` : '';
      const leftStr = active ? runsLeft(sa) : 'candidates spawn one at a time';
      rows.push(`<tr class="group-header-row"><td colspan="9">
        <span style="display:flex; align-items:center; gap:6px;">
          <span class="badge">SA</span> Simulated Annealing ${iterStr}
          &nbsp;·&nbsp; <span style="color:var(--text-dim)">${leftStr}</span>
          <button class="btn btn-ghost btn-icon" style="margin-left:auto;" data-action="delete-sa" data-group="${escHtml(gid)}" title="Delete entire SA search">✕</button>
        </span>
      </td></tr>`);
    } else if (isMulti && !shown.has(gid)) {
      shown.add(gid);
      const count = st => items.filter(i => i.group_id === gid && i.status === st).length;
      rows.push(`<tr class="group-header-row"><td colspan="9">${escHtml(gid)} &nbsp;·&nbsp; ${groupCounts[gid]} items
        &nbsp; <span style="color:var(--green)">✓${count('done')}</span>
        ${count('running') > 0 ? `<span style="color:var(--blue)"> ▶${count('running')}</span>` : ''}
        ${count('queued') > 0 ? `<span style="color:var(--text-dim)"> ○${count('queued')}</span>` : ''}
      </td></tr>`);
    }

    const rowCls = [item.status === 'running' ? 'row-running' : item.status === 'done' ? 'row-done' : '',
                    isMulti ? `row-group-${groupColor[gid]}` : ''].filter(Boolean).join(' ');
    const repeatStr = item.status === 'running' ? `${item.repeat_done + 1} / ${item.repeats}` : `${item.repeat_done} / ${item.repeats}`;
    const cfg = item.config || {};
    const flags = [];
    if (item.load_checkpoint_first) flags.push('<span style="color:var(--blue)" title="Load checkpoint before repeat 1">L</span>');
    if (item.save_checkpoint)        flags.push('<span style="color:var(--green)" title="Save continuously; repeats 2+ continue from it">S</span>');
    if (item.reset_checkpoint_first) flags.push('<span style="color:var(--red)" title="Wipe checkpoint before repeat 1">W</span>');
    if (cfg.jam_top_edges)           flags.push(`<span style="color:var(--amber)" title="Bottleneck">J${cfg.jam_top_edges}×${cfg.jam_factor || 12}</span>`);
    const none = `<span style="color:var(--text-dim)">—</span>`;
    const removeBtn = item.status !== 'running' && !isSa
      ? `<button class="btn btn-ghost btn-icon" data-action="remove" data-id="${item.id}" title="Remove">✕</button>` : '';
    rows.push(`<tr class="${rowCls}">
      <td>${idx + 1}</td>
      <td>${escHtml(labelOf(cfg.algorithm))}</td>
      <td>${escHtml(cfg.network_name || '?')}</td>
      <td>${cfg.num_workers || '?'}</td>
      <td>${repeatStr}</td>
      <td>${cfg.max_runtime_s ? `${Math.round(cfg.max_runtime_s / 60)}m` : none}</td>
      <td>${flags.length ? flags.join(' ') : none}</td>
      <td>${statusPill(item.status)}</td>
      <td>${removeBtn}</td>
    </tr>`);
  });
  return rows.join('');
}

export function refreshQueue() {
  return Promise.all([
    fetch('/api/queue').then(r => r.json()),
    fetch('/api/queue/status').then(r => r.json()),
    fetch('/api/quick-status').then(r => r.json()),
    fetch('/api/sa/status').then(r => r.json()).catch(() => ({})),
  ]).then(([items, qs, quickStatus, sa]) => {
    byId('queue-heading').textContent = `Queue (${items.length} item${items.length !== 1 ? 's' : ''})`;
    byId('btn-start').style.display = qs.active ? 'none' : '';
    byId('btn-stop').style.display = qs.active ? '' : 'none';
    byId('status-bar').innerHTML = qs.active
      ? `<strong style="color:var(--blue)">Running</strong><span class="sep">|</span>Done: ${qs.done}&nbsp;&nbsp;Running: ${qs.running}&nbsp;&nbsp;Queued: ${qs.queued}`
      : `Queue inactive — press Start to begin&nbsp;&nbsp;<span class="sep">|</span>&nbsp;&nbsp;Done: ${qs.done}&nbsp;&nbsp;Queued: ${qs.queued}`;
    byId('queue-table').style.display = items.length ? '' : 'none';
    byId('empty-msg').style.display = items.length ? 'none' : '';
    byId('queue-tbody').innerHTML = items.length ? rowsHtml(items, sa) : '';
    updateRunningStatus(quickStatus);
    onSaStatus(sa);
  }).catch(() => {});
}

// ── Start / stop / clear ──────────────────────────────────────────────────
export function initQueue() {
  byId('btn-start').addEventListener('click', () => post('/api/queue/start'));
  byId('btn-stop').addEventListener('click', () => post('/api/queue/stop'));
  byId('btn-clear').addEventListener('click', () => post('/api/queue/clear'));
  byId('btn-kill').addEventListener('click', () => post('/stop'));
  byId('queue-tbody').addEventListener('click', async e => {
    const btn = e.target.closest('[data-action]');
    if (!btn) return;
    if (btn.dataset.action === 'remove') {
      await fetch('/api/queue/' + btn.dataset.id, {method: 'DELETE'});
    } else if (btn.dataset.action === 'delete-sa') {
      if (!confirm('Delete the entire Simulated Annealing search? This cancels the search, removes its queued runs, and stops any running run.')) return;
      await fetch('/api/sa/discard', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                      body: JSON.stringify({group_id: btn.dataset.group})}).catch(() => {});
    }
    refreshQueue();
  });
  refreshQueue();
  setInterval(refreshQueue, 2000);
}
