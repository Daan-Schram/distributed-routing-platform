// ── Custom network dropdown ───────────────────────────────────────────────
let _netData = [];

function metaLine(n) {
  if (n.phases && n.phases.length) {
    const totalVeh = n.phases.reduce((s, p) => s + (p.period > 0 ? Math.round(p.duration / p.period) : 0), 0);
    const phaseStr = n.phases.map((p, i) => `phase ${i + 1}: ${p.duration}s @ ${p.period}s`).join(', ');
    return `${n.duration} s total · ~${totalVeh} vehicles · ${phaseStr}`;
  }
  if (n.duration == null && n.period == null) return null;
  const dur = n.duration != null ? `${n.duration} s` : '';
  const per = n.period   != null ? `period ${n.period} s` : '';
  const veh = (n.duration != null && n.period != null && n.period > 0) ? `~${Math.round(n.duration / n.period)} vehicles` : '';
  return [dur, per, veh].filter(Boolean).join(' · ');
}

export function setNetwork(name) {
  const hidden = document.getElementById('q-network');
  if (!_netData.some(n => n.name === name)) name = _netData.length ? _netData[0].name : name;
  hidden.value = name;
  const entry = _netData.find(n => n.name === name);
  const meta = entry ? metaLine(entry) : null;
  const inner = document.getElementById('net-trigger-inner');
  inner.outerHTML = entry
    ? `<span class="net-trigger-label" id="net-trigger-inner"><span class="net-trigger-name">${entry.name}</span>`
      + (meta ? `<span class="net-trigger-meta">${meta}</span>` : '') + `</span>`
    : `<span class="net-trigger-placeholder" id="net-trigger-inner">${name || 'Select network…'}</span>`;
  document.querySelectorAll('#net-listbox .net-option').forEach(opt => {
    opt.setAttribute('aria-selected', opt.dataset.value === name ? 'true' : 'false');
  });
  hidden.dispatchEvent(new Event('change'));
}

function close() {
  document.getElementById('net-trigger').classList.remove('open');
  document.getElementById('net-listbox').classList.remove('open');
  document.getElementById('net-dropdown').setAttribute('aria-expanded', 'false');
}

function open() {
  document.getElementById('net-trigger').classList.add('open');
  document.getElementById('net-listbox').classList.add('open');
  document.getElementById('net-dropdown').setAttribute('aria-expanded', 'true');
  const sel = document.querySelector('#net-listbox [aria-selected="true"]') || document.querySelector('#net-listbox .net-option');
  if (sel) sel.focus();
}

export function initNetworkPicker(nets) {
  _netData = nets;
  const listbox = document.getElementById('net-listbox');
  const trigger = document.getElementById('net-trigger');
  listbox.innerHTML = '';
  nets.forEach(n => {
    const meta = metaLine(n);
    const opt = document.createElement('div');
    opt.className = 'net-option';
    opt.setAttribute('role', 'option');
    opt.setAttribute('aria-selected', 'false');
    opt.setAttribute('tabindex', '-1');
    opt.dataset.value = n.name;
    opt.innerHTML = `<span class="net-opt-name">${n.name}</span>` + (meta ? `<span class="net-opt-meta">${meta}</span>` : '');
    opt.addEventListener('click', () => { setNetwork(n.name); close(); });
    opt.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault(); setNetwork(n.name); close(); trigger.focus();
      } else if (e.key === 'ArrowDown') {
        e.preventDefault(); if (opt.nextElementSibling) opt.nextElementSibling.focus();
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        if (opt.previousElementSibling) opt.previousElementSibling.focus(); else trigger.focus();
      } else if (e.key === 'Escape') {
        close(); trigger.focus();
      }
    });
    listbox.appendChild(opt);
  });
  trigger.addEventListener('click', () => (listbox.classList.contains('open') ? close() : open()));
  trigger.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ' || e.key === 'ArrowDown') { e.preventDefault(); open(); }
    else if (e.key === 'Escape') close();
  });
  document.addEventListener('click', e => {
    if (!document.getElementById('net-dropdown').contains(e.target)) close();
  });
}
