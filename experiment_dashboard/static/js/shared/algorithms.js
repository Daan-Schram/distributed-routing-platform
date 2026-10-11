const reg = { list: [], platform: [], byName: {}, plugins: {} };

export async function loadAlgorithms() {
  const d = await fetch('/api/algorithms').then(r => r.json());
  reg.list = d.algorithms;
  reg.platform = d.platform_tunables;
  reg.byName = Object.fromEntries(d.algorithms.map(a => [a.name, a]));
  await Promise.all(d.algorithms.filter(a => a.frontend).map(async a => {
    reg.plugins[a.name] = await import('/static/js/' + a.frontend);
  }));
  return reg;
}

export const algorithms = () => reg.list;
export const platformTunables = () => reg.platform;
export const descriptor = name => reg.byName[name] || null;
export const labelOf = name => (reg.byName[name] || {}).label || name || '?';
export const pluginOf = name => reg.plugins[name] || {};
export const referenceAlgorithms = () => reg.list.filter(a => a.capabilities.reference);

export function paramOf(algorithm, name) {
  const d = descriptor(algorithm);
  return (d && d.params.find(p => p.name === name)) || reg.platform.find(p => p.name === name) || null;
}
