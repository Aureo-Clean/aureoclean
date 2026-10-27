// On-screen keyboard for the Pi in kiosk mode.
// In kiosk mode the AureoClean window is full screen, and a full-screen window covers the
// Pi's own on-screen keyboard — so the app brings its own. It appears when a text box is
// tapped, never steals the focus, and pushes the page up so the box stays visible.
// Number boxes (PIN, phone, codes) get a number pad. A real keyboard still works: typing
// on it hides this one until the next tap.
import { h, icon, shell } from './core.js';

const LETTERS = [
  ['q', 'w', 'e', 'r', 't', 'y', 'u', 'i', 'o', 'p'],
  ['a', 's', 'd', 'f', 'g', 'h', 'j', 'k', 'l'],
  ['{shift}', 'z', 'x', 'c', 'v', 'b', 'n', 'm', '{bksp}'],
  ['{sym}', '@', '{space}', '.', '{enter}', '{hide}'],
];
const SYMBOLS = [
  ['1', '2', '3', '4', '5', '6', '7', '8', '9', '0'],
  ['-', '_', '/', ':', ';', '(', ')', '₱', '&', '#'],
  ['{more}', '!', '?', ',', '\'', '"', '+', '=', '{bksp}'],
  ['{abc}', '@', '{space}', '.', '{enter}', '{hide}'],
];
const MORE = [
  ['[', ']', '{', '}', '%', '^', '*', '<', '>', '~'],
  ['$', '|', '\\', '`', '€', '£', '¥', '•', '°', '…'],
  ['{sym}', '.com', '.ph', '?', '!', ',', '\'', '{bksp}'],
  ['{abc}', '@', '{space}', '.', '{enter}', '{hide}'],
];
const NUMBERS = [
  ['1', '2', '3', '{bksp}'],
  ['4', '5', '6', '{enter}'],
  ['7', '8', '9', '{hide}'],
  ['{abc}', '0', '.', '+'],
];
const LABELS = { '{space}': 'space', '{abc}': 'ABC', '{sym}': '?123', '{more}': '#+=', '{enter}': 'Enter' };
const TEXTISH = /^(text|password|email|search|tel|url|number)$/;

let target = null;
let panel = null;
let layer = 'letters';
let shift = 0;          // 0 off, 1 next letter, 2 caps lock
let lastShiftTap = 0;
let repeatTimer = null;

const enabled = () => shell.isPi && shell.kiosk;
function isField(el) {
  if (!el || el.disabled || el.readOnly) return false;
  if (el.tagName === 'TEXTAREA') return true;
  return el.tagName === 'INPUT' && TEXTISH.test(el.type || 'text');
}
const wantsNumbers = (el) => /^(numeric|tel|decimal)$/.test(el.getAttribute('inputmode') || '') || el.type === 'tel' || el.type === 'number';

// ------------------------------------------------------------------ typing
function fire(el) { el.dispatchEvent(new Event('input', { bubbles: true })); }

function insert(text) {
  const el = target;
  if (!el) return;
  const max = Number(el.getAttribute('maxlength') || 0);
  let start = null; let end = null;
  try { start = el.selectionStart; end = el.selectionEnd; } catch { /* email/number boxes have no cursor */ }
  if (start === null || start === undefined) {
    if (max && el.value.length + text.length > max) return;
    el.value += text;
  } else {
    const next = el.value.length - (end - start) + text.length;
    if (max && next > max) return;
    el.setRangeText(text, start, end, 'end');
  }
  fire(el);
  if (shift === 1) { shift = 0; draw(); }
}

function backspace() {
  const el = target;
  if (!el || !el.value) return;
  let start = null; let end = null;
  try { start = el.selectionStart; end = el.selectionEnd; } catch { /* no cursor */ }
  if (start === null || start === undefined) el.value = el.value.slice(0, -1);
  else if (start !== end) el.setRangeText('', start, end, 'end');
  else if (start > 0) el.setRangeText('', start - 1, start, 'end');
  fire(el);
}

function enter() {
  const el = target;
  if (!el) return;
  if (el.tagName === 'TEXTAREA') { insert('\n'); return; }
  // The screens listen for the Enter key (sign in, unlock, search…).
  const ev = new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true, cancelable: true });
  el.dispatchEvent(ev);
  if (!ev.defaultPrevented && el.form && el.form.requestSubmit) el.form.requestSubmit();
}

function press(key) {
  switch (key) {
    case '{bksp}': backspace(); break;
    case '{enter}': enter(); break;
    case '{space}': insert(' '); break;
    case '{hide}': hide(); break;
    case '{sym}': layer = 'symbols'; draw(); break;
    case '{more}': layer = 'more'; draw(); break;
    case '{abc}': layer = 'letters'; draw(); break;
    case '{shift}': {
      const now = Date.now();
      shift = shift === 0 ? (now - lastShiftTap < 350 ? 2 : 1) : (shift === 1 && now - lastShiftTap < 350 ? 2 : 0);
      lastShiftTap = now;
      draw();
      break;
    }
    default: insert(shift && key.length === 1 ? key.toUpperCase() : key);
  }
}

// ------------------------------------------------------------------ drawing
function keyButton(key) {
  if (key === '') return h('span', { class: 'kk spacer' });
  const label = key === '{shift}' ? icon('shift') : key === '{bksp}' ? icon('bksp') : key === '{hide}' ? icon('kbdown')
    : LABELS[key] || (shift && key.length === 1 ? key.toUpperCase() : key);
  const cls = ['kk'];
  if (key.startsWith('{')) cls.push('fn', key.slice(1, -1));
  if (key === '{shift}' && shift) cls.push(shift === 2 ? 'locked' : 'on');
  const aria = { '{shift}': 'Shift', '{bksp}': 'Delete', '{hide}': 'Hide keyboard', '{space}': 'Space' }[key] || LABELS[key] || key;
  const b = h('button', { class: cls.join(' '), type: 'button', tabindex: '-1', 'aria-label': aria }, label);
  b.addEventListener('pointerdown', (e) => {
    e.preventDefault();             // keep the focus (and cursor) in the text box
    b.classList.add('down');
    press(key);
    if (key === '{bksp}') {         // hold to keep deleting
      clearInterval(repeatTimer);
      repeatTimer = setTimeout(() => { repeatTimer = setInterval(backspace, 70); }, 420);
    }
  });
  const up = () => { b.classList.remove('down'); clearTimeout(repeatTimer); clearInterval(repeatTimer); };
  b.addEventListener('pointerup', up);
  b.addEventListener('pointerleave', up);
  b.addEventListener('pointercancel', up);
  return b;
}

function draw() {
  if (!panel) return;
  const rows = layer === 'numbers' ? NUMBERS : layer === 'symbols' ? SYMBOLS : layer === 'more' ? MORE : LETTERS;
  panel.className = `osk ${layer}`;
  panel.replaceChildren(...rows.map((r) => h('div', { class: 'krow' }, r.map(keyButton))));
}

function show(el) {
  target = el;
  layer = wantsNumbers(el) ? 'numbers' : 'letters';
  shift = 0;
  if (!panel) {
    panel = h('div', { class: 'osk', role: 'group', 'aria-label': 'On-screen keyboard' });
    panel.addEventListener('pointerdown', (e) => e.preventDefault());
    document.body.append(panel);
  }
  draw();
  const root = document.documentElement;
  const wasOpen = root.classList.contains('kb-open');
  root.style.setProperty('--kb', `${panel.offsetHeight}px`);
  root.classList.add('kb-open');
  // Keep the box in view above the keyboard.
  setTimeout(() => { if (target === el) el.scrollIntoView({ block: 'center', behavior: wasOpen ? 'smooth' : 'auto' }); }, wasOpen ? 0 : 60);
}

export function hide() {
  target = null;
  clearInterval(repeatTimer);
  document.documentElement.classList.remove('kb-open');
  if (panel) { panel.remove(); panel = null; }
}

export function initKeyboard() {
  document.addEventListener('focusin', (e) => { if (enabled() && isField(e.target)) show(e.target); }, true);
  // Tapping a box again brings the keyboard back after "hide".
  document.addEventListener('pointerdown', (e) => {
    if (enabled() && !panel && isField(e.target) && document.activeElement === e.target) show(e.target);
  }, true);
  document.addEventListener('focusout', () => {
    setTimeout(() => { if (panel && !isField(document.activeElement)) hide(); }, 120);
  }, true);
  // Someone typing on a real keyboard doesn't need this one.
  document.addEventListener('keydown', (e) => { if (panel && e.isTrusted && e.key.length === 1) hide(); }, true);
  shell.listeners.add((m) => { if (m.event === 'kiosk' && !m.on) hide(); });
  window.addEventListener('hashchange', hide);
}
