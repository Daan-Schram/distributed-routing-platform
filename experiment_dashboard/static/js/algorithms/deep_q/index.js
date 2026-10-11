import { fmt, makeStatSection, statCell } from '../../shared/util.js';
import { chartWrap, legendChartOptions, makeChart } from '../../shared/charts.js';

export function summarySections(d) {
  // Value probe — last reported reading from the learner
  const probeRow = d.algorithm_metrics.series.filter(p => p.probe_q_span != null).pop();
  if (!probeRow) return [];
  return [makeStatSection('Value Probe (final)', [
    statCell('Q span', fmt(probeRow.probe_q_span, 2)),
    statCell('Q vs optimal', fmt(probeRow.probe_q_vs_optimal, 2), '',
             probeRow.probe_q_vs_optimal < 0 ? 'red' : ''),
    // Under congestion the greedy choice SHOULD diverge from the free-flow Dijkstra
    // direction, so this reads as "how Dijkstra-like", not "how good". No colour.
    statCell('Dijkstra-likeness', fmt(probeRow.probe_follows_pct, 1), '%'),
  ])];
}

export function metricCharts(d, sfx) {
  const series = d.algorithm_metrics.series;
  // Value probe (deep_q only, and only for runs whose learner reported it)
  if (!series.some(p => p.probe_q_span != null)) return [];

  requestAnimationFrame(() => {
    const steps = series.map(p => p.step);
    // Span and |Q| must be read together: a small span next to a large |Q| means the
    // values collapsed toward the mean, so the argmax between two candidates is a
    // coin flip. Either line on its own looks unremarkable.
    makeChart('chart-train-span' + sfx, {
      type: 'line',
      data: {
        labels: steps,
        datasets: [
          probeSet('Q span', series.map(p => p.probe_q_span), 'rgb(0,150,160)'),
          probeSet('Mean |Q|', series.map(p => p.q_mean), 'rgb(26,110,245)'),
        ]
      },
      options: legendChartOptions('Train Step', 'Q'),
    });

    // Fixed −1…+1 axis on purpose: what matters is whether the correlation falls
    // (model is evaluating a bad policy) or crosses zero (it inverted). An
    // auto-scaled axis hides both.
    const corrOpts = legendChartOptions('Train Step', 'Spearman corr');
    corrOpts.scales.y.min = -1;
    corrOpts.scales.y.max = 1;
    makeChart('chart-train-corr' + sfx, {
      type: 'line',
      data: {
        labels: steps,
        datasets: [
          probeSet('Q vs optimal', series.map(p => p.probe_q_vs_optimal), 'rgb(140,0,200)'),
          { label: 'zero', data: steps.map(() => 0), borderColor: '#b8c4d4',
            borderWidth: 1, borderDash: [4, 4], pointRadius: 0, fill: false },
        ]
      },
      options: corrOpts,
    });
  });

  return [chartWrap('chart-train-span' + sfx, 'Q spread — span vs |Q|'),
          chartWrap('chart-train-corr' + sfx, 'Q vs optimal (Spearman)')];
}

function probeSet(label, data, color) {
  return {
    label, data,
    borderColor: color,
    backgroundColor: color.replace(')', ', 0.08)').replace('rgb', 'rgba'),
    tension: 0.3, fill: false, pointRadius: 2, borderWidth: 1.5,
  };
}
