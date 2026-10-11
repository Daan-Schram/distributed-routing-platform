// ── Chart instance registry (destroy before re-render) ──
const _charts = {};

export const A_COLOR = '#1a6ef5';
export const B_COLOR = '#d63040';

export function destroyChart(id) {
  if (_charts[id]) { _charts[id].destroy(); delete _charts[id]; }
}

export function destroyAllCharts() {
  Object.keys(_charts).forEach(destroyChart);
}

export function makeChart(id, config) {
  destroyChart(id);
  const ctx = document.getElementById(id);
  if (!ctx) return;
  _charts[id] = new Chart(ctx, config);
}

export function chartWrap(id, title) {
  const wrap = document.createElement('div');
  wrap.className = 'chart-wrap';
  wrap.innerHTML = `<div class="chart-title">${title}</div>
    <div class="chart-canvas-wrap"><canvas id="${id}"></canvas></div>`;
  return wrap;
}

export function chartOptions(xLabel, yLabel) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    plugins: { legend: { display: false } },
    scales: {
      x: {
        title: { display: true, text: xLabel, font: { size: 10 }, color: '#8a9aae' },
        ticks: { font: { size: 10 }, color: '#8a9aae' },
        grid: { color: '#e8edf4' },
      },
      y: {
        title: { display: true, text: yLabel, font: { size: 10 }, color: '#8a9aae' },
        ticks: { font: { size: 10 }, color: '#8a9aae' },
        grid: { color: '#e8edf4' },
      }
    }
  };
}

// chartOptions hides the legend; these charts overlay two series so it has to show.
export function legendChartOptions(xLabel, yLabel) {
  const o = chartOptions(xLabel, yLabel);
  o.plugins.legend = { display: true, labels: { boxWidth: 10, font: { size: 10 }, filter: i => i.text !== 'zero' } };
  return o;
}

export function mergedOptions(xLabel, yLabel) {
  const o = chartOptions(xLabel, yLabel);
  o.plugins.legend = {
    display: true, position: 'top',
    labels: { font: { size: 11 }, color: '#8a9aae', boxWidth: 12, usePointStyle: true },
  };
  o.scales.x.type = 'linear';
  return o;
}

export function mLineDS(label, points, color, fill) {
  return {
    label, data: points,
    borderColor: color, backgroundColor: color + '22',
    tension: 0.3, fill: !!fill, pointRadius: 0, borderWidth: 2,
  };
}

export function lineDS(label, data, color, fill = true) {
  return {
    label, data,
    borderColor: color,
    backgroundColor: color.replace(')', ', 0.08)').replace('rgb', 'rgba'),
    tension: 0.3, fill, pointRadius: 2, borderWidth: 1.5,
  };
}
