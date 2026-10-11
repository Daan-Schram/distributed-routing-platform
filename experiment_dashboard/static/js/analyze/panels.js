import { escHtml, fmt, makeStatSection, statCell, writePrefill } from '../shared/util.js';
import { chartOptions, chartWrap, lineDS, makeChart } from '../shared/charts.js';
import { descriptor, labelOf, pluginOf } from '../shared/algorithms.js';
import { prevRepeatOf, state, warmStartInfo } from './common.js';

const SERIES_COLORS = ['rgb(26,110,245)', 'rgb(208,80,0)', 'rgb(26,158,80)', 'rgb(140,0,200)', 'rgb(214,40,110)', 'rgb(0,150,160)'];

export function seriesSpecs(algorithm) {
  const d = descriptor(algorithm);
  return (d ? d.metrics.series : []).map((s, i) => ({ color: SERIES_COLORS[i % SERIES_COLORS.length], ...s }));
}

// ── Config card ──────────────────────────────────────────
export function renderConfig(cfg, d) {
  const card = document.createElement('div');
  card.className = 'card';
  card.innerHTML = `<div class="card-header"><span class="card-title">Config</span></div>`;
  const body = document.createElement('div');
  body.className = 'card-body';

  // Bottleneck banner
  const jam = cfg.bottleneck !== 'none' ? cfg : null;
  const banner = document.createElement('div');
  banner.style.cssText = 'display:flex; align-items:center; gap:8px; padding:8px 10px; '
    + 'border-radius:5px; margin-bottom:12px; font-size:12px; border:1px solid '
    + (jam ? 'var(--amber)' : 'var(--border)')
    + '; background:' + (jam ? 'rgba(208,128,0,0.08)' : 'var(--surface2)') + ';';
  if (jam) {
    const share = cfg.bottleneck_path_share != null
      ? ` &middot; ${(cfg.bottleneck_path_share * 100).toFixed(1)}% of shortest paths` : '';
    const j = cfg.jam || {};
    const hit = [];
    if (j.veh_time_share != null) hit.push(`${(j.veh_time_share * 100).toFixed(1)}% of vehicle-time on jammed edges`);
    if (j.arrivals_touching_pct != null) hit.push(`${j.arrivals_touching_pct}% of arrivals touched one`);
    banner.innerHTML = `<span style="font-weight:700; letter-spacing:0.5px; text-transform:uppercase;">Bottleneck</span>`
      + `<span>${escHtml(cfg.bottleneck)}${share}</span>`
      + (hit.length ? `<span style="color:var(--text-mid);">· ${escHtml(hit.join(' · '))}</span>` : '')
      + (cfg.bottleneck_edges ? `<span style="color:var(--text-dim); font-family:monospace;">${escHtml(cfg.bottleneck_edges)}</span>` : '');
  } else {
    banner.innerHTML = `<span style="font-weight:700; letter-spacing:0.5px; text-transform:uppercase; color:var(--text-dim);">Bottleneck</span>`
      + `<span style="color:var(--text-dim);">none &mdash; unmodified network</span>`;
  }
  body.appendChild(banner);

  if (cfg.repeat_index || cfg.warm_start != null || cfg.loaded_checkpoint != null) {
    const hp = cfg.hyperparams || {};
    let est = null;
    if (cfg.warm_start && cfg.loaded_checkpoint == null && cfg.train_steps_at_start == null
        && d.algorithm_metrics.series.length && hp.broadcast_every != null) {
      const v = d.algorithm_metrics.series[0].step - hp.broadcast_every;
      if (v > 0) est = v;
    }
    const ws = warmStartInfo(est ? {...cfg, train_steps_at_start: est} : cfg, prevRepeatOf(cfg, state.allExperiments));
    const bits = [];
    if (cfg.repeat_index) bits.push(`repeat ${cfg.repeat_index} of ${cfg.repeats || '?'}`);
    if (ws) bits.push(est ? ws.text.replace(' steps', ' steps (estimated from the first training step)').replace(' (unconfirmed)', '') : ws.text);
    if (cfg.warm_start && hp.epsilon_reset_on_load) bits.push('ε re-annealed on load');
    if (cfg.checkpoint_exists_before === false && cfg.warm_start) bits.push('no checkpoint file before the run');
    else if (cfg.checkpoint_train_steps_before != null && cfg.train_steps_at_start == null) bits.push(`checkpoint before the run: ${Number(cfg.checkpoint_train_steps_before).toLocaleString()} steps`);
    const bad = ws && ws.cls === 'bad';
    const run = document.createElement('div');
    run.style.cssText = 'display:flex; align-items:center; gap:8px; padding:8px 10px; '
      + 'border-radius:5px; margin-bottom:12px; font-size:12px; border:1px solid '
      + (bad ? 'var(--red)' : 'var(--border)') + '; background:' + (bad ? 'rgba(224,49,49,0.08)' : 'var(--surface2)') + ';';
    run.title = cfg.queue_item_id ? 'queue item ' + cfg.queue_item_id : '';
    run.innerHTML = `<span style="font-weight:700; letter-spacing:0.5px; text-transform:uppercase; color:${bad ? 'var(--red)' : 'var(--text-dim)'};">Run</span>`
      + `<span style="color:${bad ? 'var(--red)' : 'var(--text)'};">${escHtml(bits.join(' · '))}</span>`;
    body.appendChild(run);
  }

  const RUN_KEYS = new Set(['algorithm', 'queue_item_id', 'repeat_index', 'repeats', 'warm_start', 'loaded_checkpoint', 'train_steps_at_start',
                            'checkpoint_exists_before', 'checkpoint_train_steps_before', 'jam_requested', 'jam_factor']);

  const grid = document.createElement('div');
  grid.className = 'config-grid';

  function addConfigItem(k, v) {
    const item = document.createElement('div');
    item.innerHTML = `<div class="config-key">${escHtml(k)}</div><div class="config-val">${v === null ? '—' : escHtml(String(v))}</div>`;
    grid.appendChild(item);
  }

  addConfigItem('algorithm', labelOf(cfg.algorithm));
  Object.entries(cfg).forEach(([k, v]) => {
    if (typeof v === 'object' && v !== null) return;
    if (k.startsWith('bottleneck') || RUN_KEYS.has(k)) return;  // shown in the banners above
    addConfigItem(k, v);
  });

  const hp = cfg.hyperparams || {};
  if (Object.keys(hp).length) {
    const sep = document.createElement('div');
    sep.style.cssText = 'grid-column:1/-1; padding-top:10px; margin-top:6px; border-top:1px solid var(--border); font-size:10px; font-weight:700; letter-spacing:1px; text-transform:uppercase; color:var(--text-dim);';
    sep.textContent = 'Hyperparameters';
    grid.appendChild(sep);
    Object.entries(hp).forEach(([k, v]) => addConfigItem(k, v));
  }

  body.appendChild(grid);

  if (Object.keys(hp).length) {
    const btnRow = document.createElement('div');
    btnRow.style.cssText = 'display:flex; justify-content:flex-end; padding-top:10px; border-top:1px solid var(--border); margin-top:10px;';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'text-btn primary';
    btn.textContent = 'Use these hyperparameters';
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      writePrefill({ algorithm: cfg.algorithm, network_name: cfg.network_name, hyperparams: hp,
                     platform: cfg.speed_ema_alpha != null ? { speed_ema_alpha: cfg.speed_ema_alpha } : {} });
    });
    btnRow.appendChild(btn);
    body.appendChild(btnRow);
  }

  card.appendChild(body);
  return card;
}

// ── Summary card ─────────────────────────────────────────
export function renderSummary(d) {
  const card = document.createElement('div');
  card.className = 'card';
  card.innerHTML = `<div class="card-header"><span class="card-title">Summary</span></div>`;
  const body = document.createElement('div');
  body.className = 'card-body';

  const sections = document.createElement('div');
  sections.className = 'stat-sections';

  // Run overview
  sections.appendChild(makeStatSection('Run Overview', [
    statCell('Sim Steps',    fmt(d.sim_steps, 0)),
    statCell('Sim Duration', fmt(d.sim_duration_s, 1), 's'),
    statCell('Wall Time',    fmt(d.wall_duration_s, 1), 's'),
    statCell('Departures',   fmt(d.departures, 0)),
    statCell('Throughput',   fmt(d.throughput_vehicles_per_sim_min, 2), 'veh/min'),
    statCell('Congestion',   fmt(d.congestion_vehicle_seconds, 0), 'veh·s'),
  ]));

  // ATT
  const att = d.att || {};
  sections.appendChild(makeStatSection('Average Travel Time', [
    statCell('Mean',   fmt(att.mean,   1), 's'),
    statCell('Censored', fmt(att.censored_mean, 1), 's', att.not_arrived > 0 ? 'amber' : ''),
    statCell('Median', fmt(att.median, 1), 's'),
    statCell('p95',    fmt(att.p95,    1), 's'),
    statCell('Count',  fmt(att.count,  0)),
    statCell('Not arrived', fmt(att.not_arrived, 0), '', att.not_arrived > 0 ? 'red' : ''),
  ]));

  // Routing
  const r = d.routing || {};
  sections.appendChild(makeStatSection('Routing', [
    statCell('On Time',    fmt(r.on_time_pct,    1), '%', 'green'),
    statCell('Timed Out',  fmt(r.timed_out_pct,  1), '%', r.timed_out_pct > 0 ? 'amber' : ''),
    statCell('Too Late',   fmt(r.too_late_pct,   1), '%', r.too_late_pct > 0  ? 'red'   : ''),
    statCell('Total',      fmt(r.total,          0)),
    statCell('Exceptions', fmt(r.exceptions,     0), '', r.exceptions > 0 ? 'amber' : ''),
    statCell('Duplicates', fmt(r.duplicate_requests, 0)),
    statCell('Unaccounted',fmt(r.truly_unaccounted, 0)),
    statCell('Teleports ↑', fmt(r.teleports_started, 0), '', r.teleports_started > 0 ? 'amber' : ''),
    statCell('Teleports ↓', fmt(r.teleports_ended,   0)),
  ]));

  // Latency
  const rtl = d.round_trip_latency || {};
  const dl  = d.decision_latency   || {};
  sections.appendChild(makeStatSection('Latency', [
    statCell('RTT Mean', fmt(rtl.mean != null ? rtl.mean * 1000 : null, 1), 'ms'),
    statCell('RTT p95',  fmt(rtl.p95  != null ? rtl.p95  * 1000 : null, 1), 'ms'),
    statCell('Decision Mean', fmt(dl.mean != null ? dl.mean * 1000 : null, 1), 'ms'),
    statCell('Decision p95',  fmt(dl.p95  != null ? dl.p95  * 1000 : null, 1), 'ms'),
  ]));

  // Edge travel time
  const ett = d.edge_travel_time || {};
  sections.appendChild(makeStatSection('Edge Travel Time', [
    statCell('Mean',  fmt(ett.mean,  1), 's'),
    statCell('p95',   fmt(ett.p95,   1), 's'),
    statCell('Count', fmt(ett.count, 0)),
  ]));

  const desc = descriptor(d.config.algorithm);
  const summary = d.algorithm_metrics.summary;
  if (desc && summary && desc.metrics.summary.length) {
    sections.appendChild(makeStatSection(desc.label, desc.metrics.summary.map(s =>
      statCell(s.label, fmt(summary[s.key], s.decimals ?? 3), s.unit || ''))));
  }

  // Loops & path efficiency (only when trajectory capture was enabled)
  if (d.loops) {
    const L = d.loops;
    // hops_ratio: mean of (edges driven / edges on the Dijkstra path). 1.00 = optimal
    // length, higher = wandering. Baseline-free, so it needs no comparison run.
    const hr = L.hops_ratio;
    const hrTone = hr == null ? '' : (hr <= 1.15 ? 'green' : hr <= 1.5 ? 'amber' : 'red');
    sections.appendChild(makeStatSection('Loops & Path Efficiency', [
      statCell('Hops ratio', fmt(hr, 2), '×', hrTone),
      statCell('Extra edges', fmt(L.mean_extra_edges, 2)),
      statCell('Looped', fmt(L.looped_pct, 1), '%', L.looped_pct > 0 ? 'amber' : 'green'),
      statCell('Vehicles w/ loops', fmt(L.vehicles_with_loops, 0)),
      statCell('Mean revisits', fmt(L.mean_revisits, 2)),
      statCell('Max revisits', fmt(L.max_revisits, 0)),
      statCell('Total vehicles', fmt(L.vehicles_total, 0)),
    ]));
  }

  const plugin = pluginOf(d.config.algorithm);
  if (plugin.summarySections) plugin.summarySections(d).forEach(s => sections.appendChild(s));

  body.appendChild(sections);
  card.appendChild(body);
  return card;
}

// ── Charts card ──────────────────────────────────────────
export function renderCharts(d, ns) {
  const sfx = ns ? '-' + ns : '';
  const card = document.createElement('div');
  card.className = 'card';
  card.innerHTML = `<div class="card-header"><span class="card-title">Time Series</span></div>`;
  const body = document.createElement('div');
  body.className = 'card-body';

  const grid = document.createElement('div');
  grid.className = 'charts-grid';

  // 1. Cumulative arrivals
  const wrapArrivals = chartWrap('chart-arrivals' + sfx, 'Cumulative Arrivals');
  wrapArrivals.classList.add('chart-full');
  grid.appendChild(wrapArrivals);

  // 2. Congestion
  grid.appendChild(chartWrap('chart-congestion' + sfx, 'Congestion (vehicle-seconds per snapshot)'));

  // 3. ATT scatter
  grid.appendChild(chartWrap('chart-att' + sfx, 'Travel Time per Arrival'));

  body.appendChild(grid);
  card.appendChild(body);

  // Defer chart creation until DOM is attached
  requestAnimationFrame(() => {
    // Arrivals
    const trips = d.trips_series || [];
    makeChart('chart-arrivals' + sfx, {
      type: 'line',
      data: {
        labels: trips.map(p => p.sim_time),
        datasets: [{
          label: 'Cumulative Arrivals',
          data: trips.map(p => p.cumulative_arrivals),
          borderColor: '#1a6ef5', backgroundColor: 'rgba(26,110,245,0.08)',
          tension: 0.3, fill: true, pointRadius: 3,
        }]
      },
      options: chartOptions('Sim Time (s)', 'Vehicles'),
    });

    // Congestion
    const cong = d.congestion_series || [];
    makeChart('chart-congestion' + sfx, {
      type: 'bar',
      data: {
        labels: cong.map(p => p.sim_time),
        datasets: [{
          label: 'Vehicle-seconds',
          data: cong.map(p => p.vehicle_seconds),
          backgroundColor: 'rgba(208,112,0,0.65)',
          borderColor: '#d07000', borderWidth: 1,
        }]
      },
      options: chartOptions('Sim Time (s)', 'Vehicle-seconds'),
    });

    // ATT scatter (sample if large)
    let attPts = d.att_series || [];
    if (attPts.length > 500) {
      const step = Math.ceil(attPts.length / 500);
      attPts = attPts.filter((_, i) => i % step === 0);
    }
    makeChart('chart-att' + sfx, {
      type: 'scatter',
      data: {
        datasets: [{
          label: 'Travel Time (s)',
          data: attPts.map(p => ({ x: p.arrival_sim_time, y: p.att })),
          backgroundColor: 'rgba(26,158,80,0.45)',
          pointRadius: 2,
        }]
      },
      options: chartOptions('Arrival Time (s)', 'Travel Time (s)'),
    });
  });

  return card;
}

export function renderAlgorithmMetrics(d, ns) {
  const series = d.algorithm_metrics.series;
  const specs = seriesSpecs(d.config.algorithm);
  const plugin = pluginOf(d.config.algorithm);
  if (!series.length || !specs.length) return null;

  const sfx = ns ? '-' + ns : '';
  const card = document.createElement('div');
  card.className = 'card';
  card.innerHTML = `<div class="card-header"><span class="card-title">Learner Metrics (${series.length} points)</span></div>`;
  const body = document.createElement('div');
  body.className = 'card-body';
  const grid = document.createElement('div');
  grid.className = 'charts-grid';

  specs.forEach(s => grid.appendChild(chartWrap(`chart-alg-${s.key}${sfx}`, s.label)));
  if (plugin.metricCharts) plugin.metricCharts(d, sfx).forEach(el => grid.appendChild(el));

  body.appendChild(grid);
  card.appendChild(body);

  requestAnimationFrame(() => {
    const steps = series.map(p => p.step);
    specs.forEach(s => makeChart(`chart-alg-${s.key}${sfx}`, {
      type: 'line',
      data: { labels: steps, datasets: [lineDS(s.label, series.map(p => p[s.key]), s.color)] },
      options: chartOptions('Step', s.label),
    }));
  });

  return card;
}

// ── Trip Distribution card ───────────────────────────────
export function renderTripDistribution(d, ns) {
  const dist = d.trip_distribution;
  if (!dist || !dist.count) return null;

  const sfx = ns ? '-' + ns : '';
  const card = document.createElement('div');
  card.className = 'card';
  card.innerHTML = `<div class="card-header"><span class="card-title">Trip Distribution (${dist.count} trips)</span></div>`;
  const body = document.createElement('div');
  body.className = 'card-body';

  const grid = document.createElement('div');
  grid.className = 'charts-grid';
  grid.appendChild(chartWrap('chart-dist-time' + sfx, 'Travel Time Distribution'));
  grid.appendChild(chartWrap('chart-dist-dist' + sfx, 'Distance Distribution'));

  body.appendChild(grid);
  card.appendChild(body);

  requestAnimationFrame(() => {
    function binChart(canvasId, bins, unit, color) {
      function fmtLabel(v) {
        if (unit === 'km') return (v / 1000).toFixed(1) + ' km';
        if (unit === 'min') return (v / 60).toFixed(1) + ' min';
        return v.toFixed(0) + ' ' + unit;
      }
      const labels = bins.map(b => fmtLabel(b.lo) + '–' + fmtLabel(b.hi));
      makeChart(canvasId, {
        type: 'bar',
        data: {
          labels,
          datasets: [{ label: 'Trips', data: bins.map(b => b.count), backgroundColor: color, borderWidth: 0 }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            x: { ticks: { font: { size: 9 }, color: '#8a9aae', maxRotation: 35, minRotation: 35 }, grid: { display: false } },
            y: { title: { display: true, text: 'Trips', font: { size: 10 }, color: '#8a9aae' }, ticks: { font: { size: 10 }, color: '#8a9aae' }, grid: { color: '#e8edf4' }, beginAtZero: true }
          }
        }
      });
    }

    const maxDist = (dist.distance_m.bins || []).reduce((m, b) => Math.max(m, b.hi), 0);
    const distUnit = maxDist >= 5000 ? 'km' : 'm';
    const maxTime = (dist.travel_time_s.bins || []).reduce((m, b) => Math.max(m, b.hi), 0);
    const timeUnit = maxTime >= 300 ? 'min' : 's';

    binChart('chart-dist-time' + sfx, dist.travel_time_s.bins || [], timeUnit, 'rgba(26,110,245,0.7)');
    binChart('chart-dist-dist' + sfx, dist.distance_m.bins  || [], distUnit,  'rgba(26,158,80,0.7)');
  });

  return card;
}

// ── Resources card ───────────────────────────────────────
function hasData(pts, key) {
  return pts.some(p => p[key] !== null && p[key] !== undefined && p[key] !== 0);
}

export function renderResources(resources, ns) {
  if (!resources) return null;
  const sfx = ns ? '-' + ns : '';

  const layers = Object.keys(resources).filter(k => resources[k] && resources[k].length > 0);
  if (!layers.length) return null;

  const card = document.createElement('div');
  card.className = 'card';
  card.innerHTML = `<div class="card-header"><span class="card-title">Resources</span></div>`;
  const body = document.createElement('div');
  body.className = 'card-body';

  const grid = document.createElement('div');
  grid.className = 'resources-grid';

  layers.sort().forEach(layer => {
    const pts = resources[layer];

    // Determine which charts to show
    const charts = [];
    charts.push({ id: `res-${layer}-cpu${sfx}`, title: 'CPU %',    unit: '%',  color: '#1a6ef5', bg: 'rgba(26,110,245,0.08)', data: pts.map(p => p.cpu_pct) });
    charts.push({ id: `res-${layer}-ram${sfx}`, title: 'RAM (MB)', unit: 'MB', color: '#1a9e50', bg: 'rgba(26,158,80,0.08)',  data: pts.map(p => p.ram_mb) });
    if (hasData(pts, 'gpu_pct'))    charts.push({ id: `res-${layer}-gpu${sfx}`,    title: 'GPU %',       unit: '%',  color: '#9b59b6', bg: 'rgba(155,89,182,0.08)', data: pts.map(p => p.gpu_pct) });
    if (hasData(pts, 'gpu_mem_mb')) charts.push({ id: `res-${layer}-gpumem${sfx}`, title: 'GPU Mem (MB)', unit: 'MB', color: '#8e44ad', bg: 'rgba(142,68,173,0.08)', data: pts.map(p => p.gpu_mem_mb) });

    // Network gets a combined chart (in + out on one canvas)
    const hasNet = hasData(pts, 'net_in_mb') || hasData(pts, 'net_out_mb');

    const labels = pts.map(p => new Date(p.timestamp * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }));

    // Build HTML for chart slots
    let chartsHtml = charts.map(c => `<div class="res-chart-wrap"><canvas id="${c.id}"></canvas></div>`).join('');
    if (hasNet) chartsHtml += `<div class="res-chart-wrap"><canvas id="res-${layer}-net${sfx}"></canvas></div>`;

    const layerCard = document.createElement('div');
    layerCard.className = 'res-layer-card';
    layerCard.innerHTML = `<div class="res-layer-header">${layer}</div>
      <div class="res-charts">${chartsHtml}</div>`;
    grid.appendChild(layerCard);

    requestAnimationFrame(() => {
      charts.forEach(c => {
        makeChart(c.id, {
          type: 'line',
          data: {
            labels,
            datasets: [{ label: c.title, data: c.data, borderColor: c.color, backgroundColor: c.bg, tension: 0.3, fill: true, pointRadius: 0 }]
          },
          options: resChartOptions(c.title, c.unit),
        });
      });

      if (hasNet) {
        makeChart(`res-${layer}-net${sfx}`, {
          type: 'line',
          data: {
            labels,
            datasets: [
              { label: '↓ In',  data: pts.map(p => p.net_in_mb),  borderColor: '#e67e22', backgroundColor: 'rgba(230,126,34,0.08)',  tension: 0.3, fill: false, pointRadius: 0 },
              { label: '↑ Out', data: pts.map(p => p.net_out_mb), borderColor: '#c0392b', backgroundColor: 'rgba(192,57,43,0.08)', tension: 0.3, fill: false, pointRadius: 0 },
            ]
          },
          options: resChartOptions('Network (MB)', 'MB', true),
        });
      }
    });
  });

  body.appendChild(grid);
  card.appendChild(body);
  return card;
}

function resChartOptions(title, unit, showLegend = false) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: { display: showLegend, labels: { font: { size: 9 }, color: '#8a9aae', boxWidth: 12 } },
      title: { display: true, text: title, font: { size: 10 }, color: '#8a9aae', padding: { bottom: 4 } },
    },
    scales: {
      x: { display: false },
      y: {
        min: 0,
        ticks: { font: { size: 9 }, color: '#8a9aae', callback: v => v + ' ' + unit },
        grid: { color: '#e8edf4' },
      }
    }
  };
}

export function renderBottleneckMap(d) {
  const jam = d.config.jam;
  if (!jam || !jam.edges || !jam.edges.length) return null;

  const card = document.createElement('div');
  card.className = 'card';
  card.innerHTML = `<div class="card-header"><span class="card-title">Bottleneck</span></div>`;
  const body = document.createElement('div');
  body.className = 'card-body';
  const canvas = document.createElement('canvas');
  canvas.style.cssText = 'display:block; width:100%; height:380px;';
  body.appendChild(canvas);
  card.appendChild(body);

  NetMap.geometry(d.config.network_name)
    .then(geo => NetMap.drawJam(canvas, geo, jam.usage || {}, jam.edges, 380))
    .catch(() => { body.innerHTML = '<div class="empty-state" style="padding:18px;">Network geometry unavailable.</div>'; });
  return card;
}
