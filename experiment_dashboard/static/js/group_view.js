// Group view for the Analyze page
// SA searches and grid searches as one row per candidate seen in an overview, repeats as sub rows
(function () {
  'use strict';

  const _expanded = new Set();
  const _sortMode = {};
  const _refAlg = {};

  // ── Grouping ───────────────────────────────────────────────
  function candidateKey(e) {
    const c = e.config || {};
    return c.queue_item_id || e.run_id;
  }

  function paramsOf(cfg) {
    cfg = cfg || {};
    return cfg.speed_ema_alpha != null ? { ...cfg.hyperparams, speed_ema_alpha: cfg.speed_ema_alpha } : (cfg.hyperparams || {});
  }

  function groupByCandidate(experiments) {
    const map = new Map();
    const isSa = experiments.some(e => e.sa);
    experiments.forEach(e => {
      const k = candidateKey(e);
      if (!map.has(k)) map.set(k, { key: k, runs: [] });
      map.get(k).runs.push(e);
    });
    const cands = [...map.values()];
    cands.forEach(c => {
      c.runs.sort((a, b) => ((a.config || {}).repeat_index || 0) - ((b.config || {}).repeat_index || 0)
                          || String(a.run_id).localeCompare(String(b.run_id)));
      c.sa = c.runs.map(r => r.sa).find(Boolean) || null;
      c.iteration = c.sa ? c.sa.iteration : null;
      c.confirm = !!(c.sa && c.sa.confirm);
      c.params = paramsOf(c.runs[0].config);
      c.repeats = (c.runs[0].config || {}).repeats || c.runs.length;
      c.timedOut = c.runs.filter(r => r.timed_out).length;
      c.status = c.runs.some(r => r.status === 'error') ? 'error'
               : c.runs.some(r => r.status === 'running') ? 'running'
               : c.runs.some(r => r.status === 'queued') ? 'queued'
               : c.runs.length < c.repeats ? 'partial' : 'done';
      const done = c.runs.filter(r => r.status === 'done' && !r.timed_out && r.att && r.att.mean != null);
      if (c.sa || isSa) {
        c.score = c.sa ? c.sa.att : null;
      } else {
        c.score = done.length ? done.reduce((s, r) => s + r.att.mean, 0) / done.length : null;
      }
      c.pick = done.length ? done.reduce((a, b) => (a.att.mean <= b.att.mean ? a : b))
             : c.runs.filter(r => r.status === 'done').slice(-1)[0] || c.runs[c.runs.length - 1] || null;
      c.firstId = String(c.runs[0].run_id);
    });
    cands.sort((a, b) => {
      if (a.iteration != null && b.iteration != null && a.iteration !== b.iteration) return a.iteration - b.iteration;
      if (a.confirm !== b.confirm) return a.confirm ? 1 : -1;
      return a.firstId.localeCompare(b.firstId);
    });
    // best-so-far over the candidates in run order
    let best = null;
    cands.forEach(c => {
      c.isNewBest = !c.confirm && c.score != null && (best == null || c.score < best);
      if (c.isNewBest) best = c.score;
      c.bestSoFar = best;
    });
    const bestCand = cands.filter(c => !c.confirm && c.score != null && (c.sa || !c.timedOut)).sort((a, b) => a.score - b.score)[0] || null;
    cands.forEach(c => { c.isBest = !!bestCand && c.key === bestCand.key; });
    return cands;
  }

  // ── Small helpers ───────────────────────────────────────────
  const num = (v, d = 1) => (v == null || isNaN(v)) ? '—' : Number(v).toFixed(d);
  const tfmt = v => v == null ? '—' : (v >= 100 ? v.toFixed(0) : v.toFixed(1));
  const pct = v => v == null ? '—' : Number(v).toFixed(1) + '%';

  function statusHtml(h, st, err) {
    if (st === 'timed_out') return '<span style="color:var(--amber);font-weight:600;">⏱ timed out</span>';
    if (st === 'done')      return '<span style="color:var(--green);font-weight:600;">✓ done</span>';
    if (st === 'running')   return '<span style="color:var(--blue);font-weight:600;">▶ running</span>';
    if (st === 'error')     return `<span style="color:var(--red);font-weight:600;" title="${h.escHtml(err || '')}">✗ error</span>`;
    if (st === 'partial')   return '<span style="color:var(--text-dim);">○ partial</span>';
    return '<span style="color:var(--text-dim);">○ queued</span>';
  }

  function paramChips(h, cols, params) {
    return cols.map(col => {
      const raw = params[col];
      const val = raw != null ? h.escHtml(h.fmtHyperval(raw)) : '—';
      return `<span style="display:inline-flex;align-items:baseline;gap:4px;white-space:nowrap;"><span style="color:var(--text-dim);text-transform:uppercase;letter-spacing:0.5px;font-size:9px;font-weight:700;">${h.escHtml(col.replace(/_/g, ' '))}</span><span style="font-family:var(--mono);font-size:11px;">${val}</span></span>`;
    }).join('<span style="color:var(--border2);margin:0 4px;">·</span>');
  }

  // Latest reference run on the same network with the same bottleneck, for the ratio column
  function referenceAtt(h, run, ref) {
    const cfg = run.config || {};
    const key = h.jamKey(cfg);
    const cands = h.allExperiments().filter(e => e.algorithm === ref && e.att_mean != null && !e.timed_out
      && e.network_name === cfg.network_name && h.jamKey(e) === key);
    return cands.length ? cands[0].att_mean : null;
  }

  // ── Cards ───────────────────────────────────────────────────
  function card(title, badge) {
    const c = document.createElement('div');
    c.className = 'card';
    c.innerHTML = `<div class="card-header">${badge ? `<span style="font-size:10px;font-weight:700;letter-spacing:0.5px;background:var(--blue);color:#fff;padding:1px 6px;border-radius:4px;margin-right:8px;">${badge}</span>` : ''}<span class="card-title">${title}</span></div><div class="card-body"></div>`;
    return c;
  }

  function headerCard(h, data, cands) {
    const sa = data.sa_summary;
    const iters = cands.filter(c => !c.confirm);
    const valid = iters.filter(c => c.score != null);
    const accepted = valid.filter(c => c.sa && c.sa.accepted).length;
    const first = data.experiments[0] || {};
    const cfg = first.config || {};
    const c = card(sa ? 'Simulated Annealing Search' : 'Grid Search', sa ? 'SA' : 'GRID');
    const body = c.querySelector('.card-body');
    const cells = [];
    const best = cands.find(x => x.isBest);
    cells.push(h.statCell(sa ? 'Best score' : 'Best ATT', best ? num(best.score, 1) : '—', 's', best ? 'green' : ''));
    if (sa) {
      const nIter = sa.n_iter ? sa.n_iter + 1 : sa.max_iteration + 1 || iters.length;
      const k = iters.length;
      const t = sa.t_init != null && sa.alpha != null ? sa.t_init * Math.pow(sa.alpha, Math.max(0, k - 1)) : null;
      cells.push(h.statCell('Iterations', `${k} / ${nIter}`));
      cells.push(h.statCell('Temperature', t != null ? tfmt(t) : '—', '', ''));
      cells.push(h.statCell('Accepted', valid.length ? `${Math.round(100 * accepted / valid.length)}%` : '—', valid.length ? `${accepted}/${valid.length}` : ''));
      if (sa.confirm) cells.push(h.statCell('Confirm run', sa.confirm.att != null ? num(sa.confirm.att, 1) : '—', 's'));
    } else {
      cells.push(h.statCell('Candidates', String(iters.length)));
    }
    const timeouts = data.experiments.filter(e => e.timed_out).length;
    const errors = data.experiments.filter(e => e.status === 'error').length;
    cells.push(h.statCell('Runs', String(data.experiments.filter(e => e.status === 'done').length)));
    cells.push(h.statCell('Timed out', String(timeouts), '', timeouts ? 'amber' : ''));
    if (errors) cells.push(h.statCell('Errors', String(errors), '', 'red'));
    body.appendChild(h.makeStatSection('', cells));

    const s = (sa && sa.settings) || {};
    const bits = [];
    if (cfg.network_name) bits.push(h.escHtml(cfg.network_name));
    const jb = h.jamBadge(cfg); if (jb) bits.push(jb);
    if (s.repeats || cfg.repeats) bits.push(`${s.repeats || cfg.repeats} repeat${(s.repeats || cfg.repeats) > 1 ? 's' : ''}`);
    if (s.warm_start) bits.push('warm start' + ((cfg.hyperparams || {}).epsilon_reset_on_load ? ' · ε re-annealed' : ''));
    if (s.score_mode) bits.push(`score: ${h.escHtml(s.score_mode)}`);
    if (sa && sa.t_init != null) bits.push(`T₀ ${tfmt(sa.t_init)} · α ${sa.alpha}`);
    const line = document.createElement('div');
    line.style.cssText = 'margin-top:10px;font-size:11px;color:var(--text-mid);display:flex;flex-wrap:wrap;gap:6px;align-items:center;';
    line.innerHTML = bits.join('<span style="color:var(--border2);">·</span>');
    body.appendChild(line);
    return c;
  }

  function curveCard(h, cands) {
    const iters = cands.filter(c => !c.confirm && c.iteration != null);
    if (iters.length < 2) return null;
    const c = card('Search curve');
    const body = c.querySelector('.card-body');
    body.appendChild(h.chartWrap('chart-sa-curve', 'Score per iteration (censored ATT), best-so-far and temperature'));
    const acc = [], rej = [], inv = [], best = [], temp = [];
    const maxScore = Math.max(...iters.filter(x => x.score != null).map(x => x.score), 0);
    iters.forEach(x => {
      const i = x.iteration + 1;
      if (x.score == null) inv.push({ x: i, y: maxScore * 1.05 });
      else if (x.sa && x.sa.accepted) acc.push({ x: i, y: x.score });
      else rej.push({ x: i, y: x.score });
      if (x.bestSoFar != null) best.push({ x: i, y: x.bestSoFar });
      if (x.sa && x.sa.temperature != null) temp.push({ x: i, y: x.sa.temperature });
    });
    requestAnimationFrame(() => {
      const o = h.legendChartOptions('Iteration', 'Score (s)');
      o.scales.x.type = 'linear';
      o.scales.x.ticks.stepSize = 1;
      o.scales.y1 = { position: 'right', title: { display: true, text: 'Temperature', font: { size: 10 }, color: '#8a9aae' },
                      ticks: { font: { size: 10 }, color: '#8a9aae' }, grid: { drawOnChartArea: false } };
      o.plugins.tooltip = { callbacks: { label: ctx => `${ctx.dataset.label}: ${Number(ctx.parsed.y).toFixed(1)}` } };
      h.makeChart('chart-sa-curve', {
        type: 'scatter',
        data: { datasets: [
          { label: 'accepted', data: acc, backgroundColor: '#1a9e50', pointRadius: 4 },
          { label: 'rejected', data: rej, backgroundColor: '#d63040', pointStyle: 'crossRot', pointRadius: 5, borderColor: '#d63040', borderWidth: 1.5 },
          { label: 'invalid', data: inv, backgroundColor: '#b8c4d4', pointStyle: 'triangle', pointRadius: 4 },
          { label: 'best so far', data: best, type: 'line', borderColor: '#1a6ef5', borderWidth: 2, pointRadius: 0, stepped: 'before', fill: false },
          { label: 'temperature', data: temp, type: 'line', borderColor: '#b8c4d4', borderDash: [4, 3], borderWidth: 1, pointRadius: 0, yAxisID: 'y1', fill: false },
        ] },
        options: o,
      });
    });
    return c;
  }

  function candidateTable(h, data, cands, groupId) {
    const sa = !!data.sa_summary;
    const exps = data.experiments;
    const variedCols = h.getVariedCols(exps);
    const mode = _sortMode[groupId] || 'order';
    const ref = _refAlg[groupId] || h.referenceAlgorithms()[0].name;
    const c = card(sa ? 'Candidates' : 'Runs');
    const hdr = c.querySelector('.card-header');
    hdr.insertAdjacentHTML('beforeend', `
      <span style="margin-left:auto;display:flex;gap:8px;align-items:center;font-size:11px;color:var(--text-dim);">
        <label>Order <select id="gv-sort" style="font-size:11px;padding:2px 4px;">
          <option value="order"${mode === 'order' ? ' selected' : ''}>as run</option>
          <option value="best"${mode === 'best' ? ' selected' : ''}>best first</option>
        </select></label>
        <label>vs <select id="gv-ref" style="font-size:11px;padding:2px 4px;">${h.referenceAlgorithms().map(a =>
          `<option value="${a.name}"${a.name === ref ? ' selected' : ''}>${h.escHtml(a.label)}</option>`).join('')}</select></label>
        <button id="gv-expand-all" class="btn-ghost" style="font-size:11px;padding:2px 8px;border:1px solid var(--border);border-radius:4px;background:var(--surface);cursor:pointer;">expand all</button>
        <button id="gv-copy" style="font-size:11px;padding:2px 8px;border:1px solid var(--border);border-radius:4px;background:var(--surface);cursor:pointer;">copy as TSV</button>
      </span>`);
    const body = c.querySelector('.card-body');
    body.style.padding = '0';
    body.style.overflowX = 'auto';

    let rows = cands.slice();
    if (mode === 'best') rows.sort((a, b) => a.confirm - b.confirm || (a.score == null) - (b.score == null) || (a.score ?? 0) - (b.score ?? 0));

    const cols = ['#'];
    if (sa) cols.push('Score', 'Δ', 'P', 'T', 'Decision'); else cols.push('Best ATT');
    if (variedCols.length) cols.push('Parameters');
    cols.push('Repeats', 'Status', '');
    let html = `<table class="group-result-table"><thead><tr>${cols.map(x => `<th>${x}</th>`).join('')}</tr></thead><tbody>`;
    const nCols = cols.length;

    rows.forEach(cd => {
      const open = _expanded.has(groupId + '|' + cd.key);
      const cls = cd.isBest ? 'group-best-row' : '';
      const iterLabel = cd.confirm ? 'confirm' : (cd.iteration != null ? cd.iteration + 1 : rows.indexOf(cd) + 1);
      html += `<tr class="gv-cand ${cls}" data-key="${h.escHtml(cd.key)}" style="cursor:pointer;">`;
      html += `<td style="font-family:var(--mono);font-weight:700;color:var(--text-mid);"><span style="display:inline-block;width:10px;color:var(--text-dim);">${open ? '▾' : '▸'}</span>${iterLabel}</td>`;
      if (sa) {
        const s = cd.sa || {};
        html += `<td style="font-family:var(--mono);">${cd.score != null ? num(cd.score, 1) : `<span style="color:var(--text-dim)">${h.escHtml({too_few: '<20 veh', no_result: 'no data', last_missing: 'last: no ATT', error: 'run failed', stopped: 'stopped', timed_out: 'timed out'}[s.reason] || (cd.status === 'done' ? 'invalid' : '…'))}</span>`}${cd.isBest ? ' ★' : ''}</td>`;
        html += `<td style="font-family:var(--mono);">${s.delta != null ? (s.delta > 0 ? '+' : '') + num(s.delta, 0) : '—'}</td>`;
        html += `<td style="font-family:var(--mono);">${s.prob != null ? Number(s.prob).toFixed(2) : '—'}</td>`;
        html += `<td style="font-family:var(--mono);">${s.temperature != null ? tfmt(s.temperature) : '—'}</td>`;
        const dec = cd.confirm ? '<span style="color:var(--blue);">↻ confirm</span>'
          : cd.score == null ? '<span style="color:var(--text-dim);">✗ invalid</span>'
          : s.accepted ? '<span style="color:var(--green);">✓ accept</span>' : '<span style="color:var(--red);">✗ reject</span>';
        html += `<td>${dec}${cd.isNewBest && !cd.confirm ? ' <span style="color:var(--green);font-weight:700;" title="new best so far">↓</span>' : ''}</td>`;
      } else {
        html += `<td style="font-family:var(--mono);">${cd.score != null ? num(cd.score, 1) : '—'}${cd.isBest ? ' ★' : ''}</td>`;
      }
      if (variedCols.length) html += `<td style="white-space:normal;">${paramChips(h, variedCols, cd.params)}</td>`;
      const done = cd.runs.filter(r => r.status === 'done').length;
      html += `<td style="font-family:var(--mono);">${done}/${cd.repeats}${cd.timedOut ? ` <span style="color:var(--amber)" title="timed out">⏱${cd.timedOut}</span>` : ''}</td>`;
      html += `<td>${statusHtml(h, cd.status, (cd.runs.find(r => r.error) || {}).error)}</td>`;
      html += `<td style="width:32px;"></td></tr>`;

      if (open) {
        html += `<tr class="gv-sub"><td colspan="${nCols}" style="padding:0 0 0 22px;background:var(--surface2);">
          <table class="group-result-table" style="font-size:12px;"><thead><tr>
            <th>Repeat</th><th>ATT</th><th>Censored</th><th>vs ${h.escHtml(h.labelOf(ref))}</th><th>Arrived</th><th>Hops ratio</th><th>Looped</th><th>Steps</th><th>Wall</th><th>Status</th><th></th>
          </tr></thead><tbody>`;
        cd.runs.forEach((r, i) => {
          const isDone = r.status === 'done';
          const att = r.att || {}, loops = r.loops || {};
          const refAtt = isDone && att.mean != null ? referenceAtt(h, r, ref) : null;
          const arrived = att.count != null && r.departures ? 100 * att.count / r.departures : null;
          const prev = i > 0 ? { ...(cd.runs[i - 1].config || {}), timed_out: cd.runs[i - 1].timed_out } : null;
          html += `<tr data-run-id="${h.escHtml(r.run_id)}" style="cursor:${isDone ? 'pointer' : 'default'};">`;
          html += `<td>${h.repeatChip(r.config, prev) || `<span style="color:var(--text-dim);">run ${i + 1}</span>`}</td>`;
          html += `<td style="font-family:var(--mono);">${num(att.mean, 1)}</td>`;
          html += `<td style="font-family:var(--mono);">${num(att.censored_mean, 1)}</td>`;
          html += `<td style="font-family:var(--mono);">${refAtt ? (att.mean / refAtt).toFixed(2) + '×' : '—'}</td>`;
          html += `<td style="font-family:var(--mono);">${pct(arrived)}</td>`;
          html += `<td style="font-family:var(--mono);">${loops.hops_ratio != null ? num(loops.hops_ratio, 2) + '×' : '—'}</td>`;
          html += `<td style="font-family:var(--mono);">${pct(loops.looped_pct)}</td>`;
          html += `<td style="font-family:var(--mono);">${r.sim_steps != null ? Number(r.sim_steps).toLocaleString() : '—'}</td>`;
          html += `<td style="font-family:var(--mono);">${r.wall_duration_s != null ? Math.round(r.wall_duration_s) + ' s' : '—'}</td>`;
          html += `<td>${statusHtml(h, r.timed_out ? 'timed_out' : r.status, r.error)}</td>`;
          html += `<td style="width:32px;text-align:center;">${isDone ? '<span style="color:var(--text-dim);font-size:14px;" title="View experiment">&#128065;</span>' : ''}</td></tr>`;
        });
        html += `</tbody></table></td></tr>`;
      }
    });
    html += '</tbody></table>';
    body.innerHTML = html;

    body.querySelectorAll('tr.gv-cand').forEach(tr => {
      tr.addEventListener('click', () => {
        const k = groupId + '|' + tr.dataset.key;
        if (_expanded.has(k)) _expanded.delete(k); else _expanded.add(k);
        h.rerender();
      });
    });
    body.querySelectorAll('tr.gv-sub tr[data-run-id]').forEach(tr => {
      if (tr.style.cursor !== 'pointer') return;
      tr.addEventListener('click', ev => { ev.stopPropagation(); h.openRun(tr.dataset.runId); });
    });
    c.querySelector('#gv-sort').addEventListener('change', ev => { _sortMode[groupId] = ev.target.value; h.rerender(); });
    c.querySelector('#gv-ref').addEventListener('change', ev => { _refAlg[groupId] = ev.target.value; h.rerender(); });
    c.querySelector('#gv-expand-all').addEventListener('click', () => {
      const all = cands.every(cd => _expanded.has(groupId + '|' + cd.key));
      cands.forEach(cd => { const k = groupId + '|' + cd.key; if (all) _expanded.delete(k); else _expanded.add(k); });
      h.rerender();
    });
    c.querySelector('#gv-copy').addEventListener('click', ev => {
      const tsv = toTsv(h, data, rows, variedCols, ref);
      navigator.clipboard.writeText(tsv).then(() => { ev.target.textContent = 'copied'; setTimeout(() => { ev.target.textContent = 'copy as TSV'; }, 1500); });
    });
    return c;
  }

  function toTsv(h, data, cands, variedCols, ref) {
    const sa = !!data.sa_summary;
    const r3 = v => (typeof v === 'number' && isFinite(v)) ? +v.toFixed(3) : (v ?? '');
    const head = ['candidate', ...(sa ? ['score', 'delta', 'prob', 'temperature', 'decision'] : ['best_att']), ...variedCols,
                  'repeat', 'run_id', 'att_mean', 'att_censored', 'vs_reference', 'arrived_pct', 'hops_ratio', 'looped_pct', 'sim_steps', 'wall_s', 'warm_start', 'loaded_checkpoint', 'status'];
    const lines = [head.join('\t')];
    cands.forEach(cd => {
      const s = cd.sa || {};
      cd.runs.forEach(r => {
        const att = r.att || {}, loops = r.loops || {}, cfg = r.config || {};
        const refAtt = att.mean != null ? referenceAtt(h, r, ref) : null;
        const row = [cd.confirm ? 'confirm' : (cd.iteration != null ? cd.iteration + 1 : cd.key)];
        if (sa) row.push(r3(cd.score), r3(s.delta), r3(s.prob), r3(s.temperature), cd.confirm ? 'confirm' : cd.score == null ? 'invalid' : s.accepted ? 'accept' : 'reject');
        else row.push(r3(cd.score));
        variedCols.forEach(col => row.push(cd.params[col] ?? ''));
        row.push(cfg.repeat_index ?? '', r.run_id, r3(att.mean), r3(att.censored_mean), refAtt ? (att.mean / refAtt).toFixed(3) : '',
                 att.count != null && r.departures ? (100 * att.count / r.departures).toFixed(1) : '', r3(loops.hops_ratio), r3(loops.looped_pct),
                 r.sim_steps ?? '', r3(r.wall_duration_s), cfg.warm_start ?? '', cfg.loaded_checkpoint ?? '', r.timed_out ? 'timed_out' : r.status);
        lines.push(row.join('\t'));
      });
    });
    return lines.join('\n');
  }

  function solutionCard(h, data, cands) {
    const sa = data.sa_summary;
    const best = sa && sa.best;
    if (!best || !best.solution) return null;
    const start = (sa.iterations || []).find(it => it.iteration === 0);
    const c = card('Best hyperparameters');
    const body = c.querySelector('.card-body');
    body.style.padding = '0';
    const keys = Object.keys(best.solution);
    const bestCand = cands.find(x => x.isBest);
    const cfg = bestCand ? (bestCand.runs[0].config || {}) : {};
    const render = () => {
      let html = `<table class="group-result-table"><thead><tr><th>Parameter</th><th>Learner default</th><th>Start (iter 1)</th><th>Best</th></tr></thead><tbody>`;
      keys.forEach(k => {
        const p = h.paramOf(cfg.algorithm, k);
        const d = p ? p.default : undefined;
        const s0 = start && start.solution ? start.solution[k] : undefined;
        const b = best.solution[k];
        const changed = d !== undefined && JSON.stringify(d) !== JSON.stringify(b);
        html += `<tr><td style="text-transform:uppercase;font-size:10px;letter-spacing:0.5px;color:var(--text-dim);font-weight:700;">${h.escHtml(k.replace(/_/g, ' '))}</td>`
          + `<td style="font-family:var(--mono);color:var(--text-mid);">${d !== undefined ? h.escHtml(h.fmtHyperval(d)) : '—'}</td>`
          + `<td style="font-family:var(--mono);color:var(--text-mid);">${s0 !== undefined ? h.escHtml(h.fmtHyperval(s0)) : '—'}</td>`
          + `<td style="font-family:var(--mono);${changed ? 'color:var(--blue);font-weight:700;' : ''}">${h.escHtml(h.fmtHyperval(b))}</td></tr>`;
      });
      html += `</tbody></table>`;
      body.innerHTML = html;
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;justify-content:flex-end;padding:10px 12px;border-top:1px solid var(--border);';
      const btn = document.createElement('button');
      btn.style.cssText = 'padding:5px 14px;border-radius:5px;border:1px solid var(--border);background:var(--surface);color:var(--blue);font-size:12px;font-weight:600;cursor:pointer;letter-spacing:0.5px;';
      btn.textContent = 'Use these hyperparameters';
      btn.addEventListener('click', () => {
        const hp = { ...(cfg.hyperparams || {}) };
        const platform = cfg.speed_ema_alpha != null ? { speed_ema_alpha: cfg.speed_ema_alpha } : {};
        keys.forEach(k => { if (k in platform) platform[k] = best.solution[k]; else hp[k] = best.solution[k]; });
        h.writePrefill({ algorithm: cfg.algorithm, network_name: cfg.network_name, hyperparams: hp, platform });
      });
      row.appendChild(btn);
      body.appendChild(row);
    };
    render();
    return c;
  }

  // ── Entry point ─────────────────────────────────────────────
  function render(panel, data, h) {
    h.destroyChart('chart-sa-curve');
    panel.innerHTML = '';
    const groupId = data.meta.group_id;
    const cands = groupByCandidate(data.experiments);
    panel.appendChild(headerCard(h, data, cands));
    if (data.sa_summary) { const cc = curveCard(h, cands); if (cc) panel.appendChild(cc); }
    panel.appendChild(candidateTable(h, data, cands, groupId));
    if (data.sa_summary) { const sc = solutionCard(h, data, cands); if (sc) panel.appendChild(sc); }

    const delRow = document.createElement('div');
    delRow.style.cssText = 'display:flex;justify-content:flex-end;margin-top:4px;';
    const doneCount = data.experiments.filter(e => e.status === 'done').length;
    delRow.innerHTML = `<button id="delete-group-btn" class="text-btn danger" data-group-id="${h.escHtml(groupId)}" data-done-count="${doneCount}">Delete group</button>`;
    panel.appendChild(delRow);
  }

  window.GroupView = { render, groupByCandidate, paramsOf };
})();
