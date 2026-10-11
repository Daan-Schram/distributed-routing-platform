import { fmt } from '../shared/util.js';

const ROUTE_COLOR = '#1a6ef5';
const LOOP_COLOR = '#d63040';

// ── Route / loop map (single-experiment view) ───────────
function _revisits(path) { return path.length - new Set(path).size; }

export function renderRoutes(d) {
  const card = document.createElement('div');
  card.className = 'card';
  card.innerHTML = `<div class="card-header"><span class="card-title">Routes &amp; Loops</span></div>`;
  const body = document.createElement('div');
  body.className = 'card-body';
  body.innerHTML = '<div style="color:var(--text-dim);font-size:13px;padding:6px 0;">Loading trajectories…</div>';
  card.appendChild(body);

  fetch('/api/trajectories/' + d.run_id)
    .then(r => (r.ok ? r.json() : Promise.reject(r.status)))
    .then(traj => buildRouteMap(body, d, traj, d.config.network_name))
    .catch(() => {
      body.innerHTML = `<div class="empty-state" style="padding:18px;text-align:left;">
        No trajectories were captured for this run.<br>
        <span style="font-size:12px;color:var(--text-dim);">Tick “Capture vehicle trajectories” on the Control page before starting a run to enable the route/loop map.</span>
      </div>`;
    });
  return card;
}

function buildRouteMap(body, d, traj, netName) {
  NetMap.geometry(netName)
    .then(geo => {
      const vehicles = traj.vehicles || {};
      const meta = traj.meta || {};
      const ids = Object.keys(vehicles);
      if (!ids.length) {
        body.innerHTML = '<div class="empty-state" style="padding:18px;">Trajectory file is empty.</div>';
        return;
      }
      const _extra = id => { const m = meta[id]; return m && m.extra_edges != null ? m.extra_edges : null; };
      const _dijk = id => { const m = meta[id]; return m && m.dijkstra_edges != null ? m.dijkstra_edges : null; };
      const hasMeta = ids.some(id => _extra(id) != null);

      body.innerHTML = '';

      const L = d.loops || traj.loops;
      if (L) {
        const sum = document.createElement('div');
        sum.className = 'route-loopsum';
        sum.innerHTML =
          `<span>Looped: <b>${fmt(L.looped_pct, 1)}%</b></span>` +
          `<span>Vehicles w/ loops: <b>${fmt(L.vehicles_with_loops, 0)}</b> / ${fmt(L.vehicles_total, 0)}</span>` +
          `<span>Mean revisits: <b>${fmt(L.mean_revisits, 2)}</b></span>` +
          `<span>Max revisits: <b>${fmt(L.max_revisits, 0)}</b></span>`;
        body.appendChild(sum);
      }

      const visitCounts = new Map();   // edge -> total visits across all vehicles
      const loopCounts = new Map();    // edge -> total revisits (occurrences-1 per vehicle)
      ids.forEach(id => {
        const local = {};
        vehicles[id].forEach(e => { local[e] = (local[e] || 0) + 1; });
        Object.keys(local).forEach(e => {
          visitCounts.set(e, (visitCounts.get(e) || 0) + local[e]);
          if (local[e] > 1) loopCounts.set(e, (loopCounts.get(e) || 0) + (local[e] - 1));
        });
      });

      const edgeMap = new Map((geo.edges || []).map(e => [e.id, e.points]));

      const modeSwitch = document.createElement('div');
      modeSwitch.className = 'route-modeswitch';
      [['single', 'Single route'], ['visit', 'Visit heatmap'], ['loops', 'Loop hotspots']].forEach(([key, label]) => {
        const mb = document.createElement('button');
        mb.textContent = label;
        mb.dataset.mode = key;
        modeSwitch.appendChild(mb);
      });
      body.appendChild(modeSwitch);

      const controls = document.createElement('div');
      controls.className = 'route-controls';

      const sel = document.createElement('select');
      sel.className = 'route-select';

      const sortSel = document.createElement('select');
      sortSel.className = 'route-sortsel';
      sortSel.innerHTML =
        `<option value="departure">Sort: departure order</option>` +
        `<option value="revisits">Sort: most revisits</option>` +
        (hasMeta ? `<option value="extra">Sort: most extra edges vs shortest path</option>` : '');

      const onlyWrap = document.createElement('label');
      onlyWrap.className = 'route-check';
      const only = document.createElement('input');
      only.type = 'checkbox';
      onlyWrap.appendChild(only);
      onlyWrap.appendChild(document.createTextNode('Only vehicles with loops'));

      controls.appendChild(sel);
      controls.appendChild(sortSel);
      controls.appendChild(onlyWrap);

      let longer = null;
      if (hasMeta) {
        const longerWrap = document.createElement('label');
        longerWrap.className = 'route-check';
        longer = document.createElement('input');
        longer.type = 'checkbox';
        longerWrap.appendChild(longer);
        longerWrap.appendChild(document.createTextNode('Only longer than the shortest path'));
        controls.appendChild(longerWrap);
      }
      body.appendChild(controls);

      const wrap = document.createElement('div');
      wrap.className = 'route-map-wrap';
      const canvas = document.createElement('canvas');
      canvas.className = 'route-canvas';
      wrap.appendChild(canvas);
      body.appendChild(wrap);

      const scrub = document.createElement('div');
      scrub.className = 'route-scrubber';
      const playBtn = document.createElement('button');
      playBtn.textContent = '▶ Play';
      const prevBtn = document.createElement('button');
      prevBtn.textContent = '‹';
      const nextBtn = document.createElement('button');
      nextBtn.textContent = '›';
      const range = document.createElement('input');
      range.type = 'range';
      range.min = '1';
      range.value = '1';
      const stepLabel = document.createElement('span');
      stepLabel.className = 'route-step-label';
      scrub.appendChild(playBtn);
      scrub.appendChild(prevBtn);
      scrub.appendChild(range);
      scrub.appendChild(nextBtn);
      scrub.appendChild(stepLabel);
      body.appendChild(scrub);

      const colorbar = document.createElement('div');
      colorbar.className = 'route-colorbar';
      body.appendChild(colorbar);

      const legend = document.createElement('div');
      legend.className = 'route-legend';
      legend.innerHTML =
        `<span class="route-legend-item"><span class="route-legend-line" style="background:#c4ccd8"></span>Network</span>` +
        `<span class="route-legend-item"><span class="route-legend-line" style="background:${ROUTE_COLOR}"></span>Route</span>` +
        `<span class="route-legend-item"><span class="route-legend-line" style="background:${LOOP_COLOR}"></span>Looped edge (visited &gt; once)</span>` +
        `<span class="route-legend-item"><span class="route-legend-line" style="background:#ff8c00"></span>Current edge</span>` +
        `<span class="route-legend-item"><span class="route-legend-dot" style="background:${'#1a9e50'}"></span>Start</span>` +
        `<span class="route-legend-item"><span class="route-legend-dot" style="background:#333"></span>End</span>`;
      body.appendChild(legend);

      let mode = 'single';
      let stepK = null;          // null => full route
      let playing = false;
      let playTimer = null;

      const curPath = () => vehicles[sel.value] || [];

      const stopPlay = () => {
        playing = false;
        if (playTimer) { clearInterval(playTimer); playTimer = null; }
        playBtn.textContent = '▶ Play';
      };

      const populate = () => {
        let list = ids.slice();
        if (only.checked) list = list.filter(id => _revisits(vehicles[id]) > 0);
        if (longer && longer.checked) list = list.filter(id => (_extra(id) || 0) > 0);

        if (sortSel.value === 'extra') {
          list.sort((a, b) => (_extra(b) ?? -Infinity) - (_extra(a) ?? -Infinity));
        } else if (sortSel.value === 'revisits') {
          list.sort((a, b) => _revisits(vehicles[b]) - _revisits(vehicles[a]));
        }

        const prev = sel.value;
        sel.innerHTML = '';
        if (!list.length) {
          const opt = document.createElement('option');
          opt.textContent = 'No matching vehicles';
          opt.value = '';
          sel.appendChild(opt);
          return;
        }
        list.forEach(id => {
          const rv = _revisits(vehicles[id]);
          const ex = _extra(id);
          const dj = _dijk(id);
          const opt = document.createElement('option');
          opt.value = id;
          let label = `${id} — ${vehicles[id].length} edges, ${rv} revisit${rv === 1 ? '' : 's'}`;
          if (ex != null) label += `, ${ex >= 0 ? '+' : ''}${ex} vs shortest path (${dj})`;
          opt.textContent = label;
          sel.appendChild(opt);
        });
        if (list.includes(prev)) sel.value = prev;
      };

      const syncRange = () => {
        const path = curPath();
        range.max = String(Math.max(1, path.length));
        range.value = String(Math.max(1, path.length));
        stepK = null;
      };

      const render = () => {
        if (mode === 'single') {
          const path = curPath();
          const k = (stepK == null) ? path.length : Math.max(0, Math.min(stepK, path.length));
          drawRouteStep(canvas, geo, edgeMap, path, k);
          stepLabel.textContent = path.length
            ? `Step ${k} / ${path.length}${k > 0 ? ' — ' + path[k - 1] : ''}`
            : 'No route';
        } else {
          const counts = mode === 'visit' ? visitCounts : loopCounts;
          drawHeatmap(canvas, geo, edgeMap, counts, mode === 'visit' ? 'visit' : 'loop');
        }
      };

      const applyMode = (m) => {
        mode = m;
        if (m !== 'single') stopPlay();
        [...modeSwitch.children].forEach(b => b.classList.toggle('active', b.dataset.mode === m));
        const single = m === 'single';
        controls.style.display = single ? 'flex' : 'none';
        scrub.style.display = single ? 'flex' : 'none';
        legend.style.display = single ? 'flex' : 'none';
        colorbar.style.display = single ? 'none' : 'flex';
        if (!single) buildColorbar(colorbar, m === 'visit' ? visitCounts : loopCounts, m === 'visit' ? 'visit' : 'loop');
        render();
      };

      modeSwitch.addEventListener('click', e => {
        const b = e.target.closest('button');
        if (b) applyMode(b.dataset.mode);
      });

      sel.addEventListener('change', () => { stopPlay(); syncRange(); render(); });
      sortSel.addEventListener('change', () => { stopPlay(); populate(); syncRange(); render(); });
      only.addEventListener('change', () => { stopPlay(); populate(); syncRange(); render(); });
      if (longer) longer.addEventListener('change', () => { stopPlay(); populate(); syncRange(); render(); });

      range.addEventListener('input', () => { stopPlay(); stepK = parseInt(range.value, 10); render(); });
      prevBtn.addEventListener('click', () => {
        stopPlay();
        const n = curPath().length;
        stepK = Math.max(1, (stepK == null ? n : stepK) - 1);
        range.value = String(stepK);
        render();
      });
      nextBtn.addEventListener('click', () => {
        stopPlay();
        const n = curPath().length;
        stepK = Math.min(n, (stepK == null ? n : stepK) + 1);
        range.value = String(stepK);
        render();
      });
      playBtn.addEventListener('click', () => {
        if (playing) { stopPlay(); return; }
        const n = curPath().length;
        if (!n) return;
        if (stepK == null || stepK >= n) stepK = 0;
        playing = true;
        playBtn.textContent = '⏸ Pause';
        playTimer = setInterval(() => {
          const len = curPath().length;
          stepK = Math.min(len, (stepK || 0) + 1);
          range.value = String(stepK);
          render();
          if (stepK >= len) stopPlay();
        }, 220);
      });

      let _rt = null;
      window.addEventListener('resize', () => { clearTimeout(_rt); _rt = setTimeout(render, 150); });

      populate();
      syncRange();
      applyMode('single');
      requestAnimationFrame(render);
    })
    .catch(() => {
      body.innerHTML = '<div class="empty-state" style="padding:18px;">Failed to load network geometry for this run.</div>';
    });
}

const _setupRouteCanvas = (canvas, geo) => NetMap.setup(canvas, geo, 540);

function drawRouteStep(canvas, geo, edgeMap, path, k) {
  const p = _setupRouteCanvas(canvas, geo);
  (geo.edges || []).forEach(e => p.stroke(e.points, '#dfe3ea', 1));
  if (!path.length) return;

  k = Math.max(0, Math.min(k, path.length));
  const prefix = path.slice(0, k);
  const counts = {};
  prefix.forEach(e => { counts[e] = (counts[e] || 0) + 1; });
  Object.keys(counts).forEach(id => { if (counts[id] === 1) p.stroke(edgeMap.get(id), ROUTE_COLOR, 2.5); });
  Object.keys(counts).forEach(id => { if (counts[id] > 1) p.stroke(edgeMap.get(id), LOOP_COLOR, 4); });

  if (k > 0) p.stroke(edgeMap.get(path[k - 1]), '#ff8c00', 5);

  const firstPts = edgeMap.get(path[0]);
  if (firstPts) p.dot(firstPts[0], '#1a9e50', 5);
  if (k > 0) {
    const curPts = edgeMap.get(path[k - 1]);
    if (curPts) p.dot(curPts[curPts.length - 1], k === path.length ? '#333' : '#ff8c00', 5);
  }
}

function heatColor(t, kind) {
  t = Math.max(0, Math.min(1, t));
  if (kind === 'loop') return `hsl(${60 * (1 - t)}, 90%, ${62 - t * 26}%)`;
  return `hsl(${210 * (1 - t)}, 80%, 50%)`;
}

function drawHeatmap(canvas, geo, edgeMap, counts, kind) {
  const p = _setupRouteCanvas(canvas, geo);
  (geo.edges || []).forEach(e => p.stroke(e.points, '#e6e9ef', 1));
  let max = 0;
  counts.forEach(v => { if (v > max) max = v; });
  if (max <= 0) return;
  [...counts.entries()].sort((a, b) => a[1] - b[1]).forEach(([id, v]) => {
    const t = Math.sqrt(v / max);
    p.stroke(edgeMap.get(id), heatColor(t, kind), 1.5 + t * 4);
  });
}

function buildColorbar(container, counts, kind) {
  let max = 0;
  counts.forEach(v => { if (v > max) max = v; });
  const stops = [];
  for (let i = 0; i <= 10; i++) stops.push(heatColor(i / 10, kind));
  const label = kind === 'loop' ? 'Revisits' : 'Visits';
  container.innerHTML =
    `<span>${label}: 1</span>` +
    `<span class="route-colorbar-grad" style="background:linear-gradient(to right, ${stops.join(',')})"></span>` +
    `<span>${max}</span>` +
    `<span style="margin-left:12px;color:var(--text-dim);">${counts.size} edge${counts.size === 1 ? '' : 's'} ${kind === 'loop' ? 'revisited' : 'visited'}</span>`;
}
