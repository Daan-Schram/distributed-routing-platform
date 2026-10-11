import { escHtml, fmtParam } from '../shared/util.js';

const isNumeric = p => p.type === 'int' || p.type === 'float';

function parseValue(p, raw) {
  if (p.type === 'bool') return typeof raw === 'boolean' ? raw : raw === 'true' ? true : raw === 'false' ? false : undefined;
  if (p.type === 'choice') return p.choices.includes(raw) ? raw : undefined;
  const v = p.type === 'int' ? parseInt(raw, 10) : parseFloat(raw);
  return Number.isFinite(v) ? v : undefined;
}

function fieldHtml(p, value) {
  const id = 'hp-' + p.name;
  const attrs = `id="${id}" data-param="${p.name}"`;
  if (p.type === 'choice') {
    return `<select ${attrs}>${p.choices.map(c => `<option value="${escHtml(c)}"${c === value ? ' selected' : ''}>${escHtml(c)}</option>`).join('')}</select>`;
  }
  if (p.type === 'float' && p.scale === 'log') return `<input type="text" ${attrs} value="${fmtParam(value)}">`;
  const b = p.bounds ? ` min="${p.bounds[0]}" max="${p.bounds[1]}"` : '';
  return `<input type="number" ${attrs} value="${value}" step="${p.type === 'int' ? 1 : 'any'}"${b}>`;
}

function rowHtml(p, value) {
  const help = p.help ? escHtml(p.help) : '';
  if (p.type === 'bool') {
    return `<div class="check-field" data-param-row="${p.name}" style="grid-column:1/-1">
      <input type="checkbox" id="hp-${p.name}" data-param="${p.name}"${value ? ' checked' : ''}>
      <label for="hp-${p.name}">${escHtml(p.label)}${help ? ` <span style="color:var(--text-dim)">— ${help}</span>` : ''}</label></div>`;
  }
  return `<div class="form-group" data-param-row="${p.name}">
    <label for="hp-${p.name}" title="${help}">${escHtml(p.label)}</label>${fieldHtml(p, value)}</div>`;
}

function readRaw(host, params) {
  const out = {};
  params.forEach(p => {
    const el = host.querySelector(`[data-param="${p.name}"]`);
    if (!el) return;
    const v = parseValue(p, p.type === 'bool' ? el.checked : el.value);
    if (v !== undefined) out[p.name] = v;
  });
  return out;
}

function visible(p, values) {
  return !p.visible_if || Object.entries(p.visible_if).every(([k, v]) => values[k] === v);
}

export function applyVisibility(host, params) {
  const values = readRaw(host, params);
  params.forEach(p => {
    const row = host.querySelector(`[data-param-row="${p.name}"]`);
    if (row) row.style.display = visible(p, values) ? '' : 'none';
  });
}

export function renderCustom(host, params, values, onChange) {
  let html = '', group = null;
  params.forEach(p => {
    if (p.group && p.group !== group) { group = p.group; html += `<div class="param-group-label">${escHtml(group)}</div>`; }
    html += rowHtml(p, values[p.name] !== undefined ? values[p.name] : p.default);
  });
  host.innerHTML = html;
  applyVisibility(host, params);
  const changed = () => { applyVisibility(host, params); onChange(); };
  host.oninput = changed;
  host.onchange = changed;
}

export function readCustom(host, params) {
  const values = readRaw(host, params);
  return Object.fromEntries(Object.entries(values).filter(([k]) => visible(params.find(p => p.name === k), values)));
}

export function defaultsLine(params) {
  return 'ℹ Defaults: ' + params.map(p => `${p.name}=${fmtParam(p.default)}`).join(', ');
}

function gridPlaceholder(p) {
  if (p.type === 'bool') return 'e.g. true, false';
  if (p.type === 'choice') return 'e.g. ' + p.choices.join(', ');
  if (p.search) return `e.g. ${fmtParam(p.search[0])}, ${fmtParam(p.default)}, ${fmtParam(p.search[1])}`;
  return 'e.g. ' + fmtParam(p.default);
}

function gridValues(p, str) {
  return str.split(',').map(s => parseValue(p, s.trim())).filter(v => v !== undefined);
}

export function renderGrid(host, params, onChange) {
  host.innerHTML = params.map(p => `
    <label class="gs-param-row-label"><input type="checkbox" data-key="${p.name}"> ${escHtml(p.label)}</label>
    <input type="text" class="gs-param-input" data-val="${p.name}" placeholder="${escHtml(gridPlaceholder(p))}" disabled>`).join('');
  host.onchange = e => {
    if (e.target.dataset.key) {
      const inp = host.querySelector(`[data-val="${e.target.dataset.key}"]`);
      inp.disabled = !e.target.checked;
      if (e.target.checked) inp.focus();
    }
    onChange();
  };
  host.oninput = onChange;
}

export function readGrid(host, params) {
  const keys = [], values = [];
  host.querySelectorAll('input[type=checkbox]:checked').forEach(cb => {
    const p = params.find(x => x.name === cb.dataset.key);
    const vals = gridValues(p, host.querySelector(`[data-val="${p.name}"]`).value);
    if (vals.length) { keys.push(p.name); values.push(vals); }
  });
  return { keys, values };
}

function saStart(p, mn, mx) {
  let v = p.default;
  if (!(mn <= v && v <= mx)) v = p.scale === 'log' && mn > 0 ? Math.sqrt(mn * mx) : (mn + mx) / 2;
  return p.type === 'int' ? Math.round(v) : v;
}

function saRowHtml(p, s) {
  const checked = s.checked !== undefined ? s.checked : p.search_default;
  const tag = isNumeric(p) ? (p.scale === 'log' ? `log ${p.type}` : p.type) : p.type === 'bool' ? 'true/false' : 'choice';
  let mid, right;
  if (isNumeric(p)) {
    mid = `<div style="display:flex; align-items:center; gap:6px;">
      <span class="form-hint">min</span><input type="text" class="gs-param-input" data-sa-min="${p.name}" style="width:80px" value="${escHtml(s.min ?? fmtParam(p.search[0]))}">
      <span class="form-hint">max</span><input type="text" class="gs-param-input" data-sa-max="${p.name}" style="width:80px" value="${escHtml(s.max ?? fmtParam(p.search[1]))}"></div>`;
    right = `<div class="sa-start" data-sa-start="${p.name}"></div>`;
  } else {
    const opts = p.type === 'bool' ? [true, false] : p.choices;
    const fixed = s.fixed !== undefined ? s.fixed : String(p.default);
    mid = `<div style="display:flex; align-items:center; gap:6px;" class="form-hint">fixed when off: <select class="gs-param-input" data-sa-fixed="${p.name}" style="width:auto;">`
      + opts.map(o => `<option value="${escHtml(String(o))}"${String(o) === fixed ? ' selected' : ''}>${escHtml(String(o))}</option>`).join('') + `</select></div>`;
    right = `<div class="sa-start">searches all</div>`;
  }
  return `<label class="gs-param-row-label"><input type="checkbox" data-sa-key="${p.name}"${checked ? ' checked' : ''}> ${escHtml(p.label)} <span class="sa-scale">(${tag})</span></label>${mid}${right}`;
}

function updateSaRows(host, params) {
  params.forEach(p => {
    const cb = host.querySelector(`[data-sa-key="${p.name}"]`);
    if (!isNumeric(p)) { host.querySelector(`[data-sa-fixed="${p.name}"]`).disabled = cb.checked; return; }  // fixed value only matters when not searched
    const minEl = host.querySelector(`[data-sa-min="${p.name}"]`), maxEl = host.querySelector(`[data-sa-max="${p.name}"]`);
    minEl.disabled = maxEl.disabled = !cb.checked;
    const mn = parseFloat(minEl.value), mx = parseFloat(maxEl.value);
    host.querySelector(`[data-sa-start="${p.name}"]`).textContent =
      Number.isFinite(mn) && Number.isFinite(mx) ? 'start ' + fmtParam(saStart(p, mn, mx)) : '';
  });
}

export function renderSa(host, params, saved, onChange) {
  host.innerHTML = params.map(p => saRowHtml(p, (saved || {})[p.name] || {})).join('');
  updateSaRows(host, params);
  const changed = () => { updateSaRows(host, params); onChange(); };
  host.oninput = changed;
  host.onchange = changed;
}

export function readSa(host, params) {
  const param_space = [], fixed = {}, state = {};
  params.forEach(p => {
    const checked = host.querySelector(`[data-sa-key="${p.name}"]`).checked;
    if (isNumeric(p)) {
      const min = host.querySelector(`[data-sa-min="${p.name}"]`).value;
      const max = host.querySelector(`[data-sa-max="${p.name}"]`).value;
      state[p.name] = { checked, min, max };
      if (checked && Number.isFinite(parseFloat(min)) && Number.isFinite(parseFloat(max))) {
        param_space.push({ key: p.name, min: parseFloat(min), max: parseFloat(max) });
      }
    } else {
      const f = host.querySelector(`[data-sa-fixed="${p.name}"]`).value;
      state[p.name] = { checked, fixed: f };
      if (checked) param_space.push({ key: p.name });
      else fixed[p.name] = parseValue(p, f);
    }
  });
  return { param_space, fixed, state };
}
