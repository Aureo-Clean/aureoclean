// Reusable pieces from the design framework: fields, segmented tabs, chips, empty
// and loading states, page headers.
import { h, icon } from './core.js';

let uid = 0;
export function field(label, { type = 'text', placeholder = '', value = '', hint = '', max = 0, autocomplete, inputmode,
  required = false, name, rows } = {}) {
  const id = `f${++uid}`;
  const input = rows
    ? h('textarea', { id, class: 'input', placeholder, rows, maxlength: max || undefined, name })
    : h('input', { id, class: 'input', type, placeholder, autocomplete, inputmode, maxlength: max || undefined, required, name });
  input.value = value;
  const head = h('div', { class: 'row' }, h('label', { for: id }, label));
  if (max && type !== 'password') {
    const count = h('span', { class: 'count' }, `${value.length}/${max}`);
    input.addEventListener('input', () => { count.textContent = `${input.value.length}/${max}`; });
    head.append(count);
  }
  const wrap = h('div', { class: 'field' }, head, input, hint ? h('div', { class: 'hint' }, hint) : null);
  wrap.input = input;
  return wrap;
}

export function select(label, options, value = '') {
  const id = `f${++uid}`;
  const sel = h('select', { id, class: 'input' }, options.map(([v, t]) => h('option', { value: v }, t)));
  sel.value = value;
  const wrap = h('div', { class: 'field' }, h('label', { for: id }, label), sel);
  wrap.input = sel;
  return wrap;
}

/** Enter in a box moves to the next empty one, or presses the main button. */
export function enterSubmits(fields, button) {
  const inputs = fields.map((f) => f.input || f);
  inputs.forEach((inp, i) => inp.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' || inp.tagName === 'TEXTAREA') return;
    e.preventDefault();
    const next = inputs[i + 1];
    if (next && !next.value && inp.value) next.focus();
    else button.click();
  }));
}

export function message() { return h('div', { class: 'msg', role: 'alert' }); }
export function say(box, text, kind = '') { box.className = `msg ${kind}`; box.textContent = text || ''; }

export function segmented(items, selected, onPick, { large = false, label = 'Sections' } = {}) {
  const wrap = h('div', { class: `seg${large ? ' lg' : ''}`, role: 'tablist', 'aria-label': label });
  const buttons = items.map(([id, text, n]) => {
    const b = h('button', { role: 'tab', type: 'button', 'aria-selected': String(id === selected), dataset: { id } },
      text, n !== undefined && n !== null ? h('span', { class: 'n' }, n) : null);
    b.addEventListener('click', () => {
      buttons.forEach((x) => x.setAttribute('aria-selected', String(x === b)));
      onPick(id);
    });
    return b;
  });
  wrap.append(...buttons);
  wrap.setCount = (id, n) => {
    const b = buttons.find((x) => x.dataset.id === id);
    if (!b) return;
    let span = b.querySelector('.n');
    if (!span) { span = h('span', { class: 'n' }); b.append(span); }
    span.textContent = n;
  };
  return wrap;
}

export function switchEl(on, onChange, label) {
  const b = h('button', { class: 'switch', role: 'switch', type: 'button', 'aria-checked': String(!!on), 'aria-label': label });
  b.addEventListener('click', () => {
    const next = b.getAttribute('aria-checked') !== 'true';
    b.setAttribute('aria-checked', String(next));
    onChange(next, b);
  });
  b.set = (v) => b.setAttribute('aria-checked', String(!!v));
  return b;
}

export function pageHead(title, ...right) {
  return h('header', { class: 'page-head' }, h('h1', null, title), h('div', { class: 'grow' }), ...right);
}

export function skeletonList(n = 4) {
  return h('div', { class: 'card flush', 'aria-busy': 'true' },
    Array.from({ length: n }, () => h('div', { class: 'sk-row' },
      h('div', { class: 'skeleton a' }), h('div', { class: 'skeleton b' }))));
}

export function empty(title, text, iconName = 'inbox') {
  return h('div', { class: 'empty' }, icon(iconName), h('b', null, title), text ? h('span', null, text) : null);
}

export function errorBox(err, retry) {
  return h('div', { class: 'card' }, h('div', { class: 'stack tight' },
    h('div', { class: 'msg error' }, err.message || String(err)),
    retry ? h('button', { class: 'btn sm', onclick: retry }, icon('reload'), 'Try again') : null));
}

export const CATEGORY = { emergency: 'Emergency', facility: 'Broken facility', supplies: 'Supplies', cleanliness: 'Cleanliness',
  system: 'System problem', other: 'Other' };
export const STATUS = { open: ['Open', 'blue'], in_progress: ['In progress', 'violet'], resolved: ['Done', 'green'] };
export const PRIORITY = { critical: ['Emergency', 'red'], high: ['Urgent', 'amber'], normal: null, low: ['Can wait', ''] };

export function statusChip(status) {
  const [text, color] = STATUS[status] || [status, ''];
  return h('span', { class: `chip ${color}` }, text);
}
export function priorityChip(priority) {
  const p = PRIORITY[priority];
  return p ? h('span', { class: `chip ${p[1]}` }, p[0]) : null;
}
export function refBox(ref, priority) {
  const cls = priority === 'critical' ? 'red' : priority === 'high' ? 'amber' : '';
  return h('div', { class: `ref ${cls}` }, ref ? `#${ref}` : '—');
}
