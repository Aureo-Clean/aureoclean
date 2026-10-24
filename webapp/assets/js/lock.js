// Kiosk mode lock screen: after a few minutes without a touch the Pi screen locks.
// The PIN (or the password, if no PIN is set) brings you back where you were;
// five wrong tries sign the session out (enforced by the service).
import { closeAllModals, h, post, session, shell } from './core.js';

let lastActivity = Date.now();
let lockEl = null;
let timer = null;
let signOut = () => {};

export function initLock(onSignOut) {
  signOut = onSignOut;
  const touch = () => { lastActivity = Date.now(); };
  ['pointerdown', 'keydown', 'touchstart', 'wheel'].forEach((ev) => window.addEventListener(ev, touch, { passive: true, capture: true }));
  if (!timer) timer = setInterval(check, 10000);
  shell.listeners.add((m) => { if (m.event === 'kiosk') touch(); if (m.event === 'lock') lockNow(); });
}

function check() {
  const minutes = Number((session.user || {}).idleLockMinutes || 0);
  if (!shell.isPi || !shell.kiosk || !session.user || !session.token || !minutes || lockEl) return;
  if (Date.now() - lastActivity >= minutes * 60000) lockNow(minutes);
}

export function isLocked() { return !!lockEl; }

export function lockNow(minutes = 0) {
  if (lockEl || !session.user) return;
  closeAllModals();
  const user = session.user;
  const usePin = !!user.hasLockPin;
  let entry = '';
  const clock = h('div', { class: 'clock' });
  const date = h('div', { class: 'date' });
  const tick = () => {
    const now = new Date();
    clock.textContent = now.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
    date.textContent = now.toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' });
  };
  tick();
  const clockTimer = setInterval(tick, 10000);
  const err = h('div', { class: 'err', role: 'alert' });
  const pins = h('div', { class: 'pins', 'aria-hidden': 'true' });
  const drawPins = () => pins.replaceChildren(...Array.from({ length: Math.max(4, entry.length) }, (_, i) => h('span', { class: i < entry.length ? 'on' : '' })));
  const pw = h('input', { class: 'pw', type: 'password', placeholder: 'Password', 'aria-label': 'Password', autocomplete: 'current-password' });

  async function unlock() {
    const secret = usePin ? entry : pw.value;
    if (!secret) return;
    entry = ''; pw.value = ''; if (usePin) drawPins();
    try {
      await post('/auth/unlock', { secret }, { timeout: 60000 });
      close();
    } catch (e) {
      err.textContent = e.message;
      lockEl.classList.remove('shake'); void lockEl.offsetWidth; lockEl.classList.add('shake');
      if (e.status === 401) { close(); signOut(e.message); }
    }
  }
  function close() {
    clearInterval(clockTimer);
    document.removeEventListener('keydown', onKey, true);
    lockEl && lockEl.remove();
    lockEl = null;
    lastActivity = Date.now();
  }
  function press(d) { if (entry.length < 6) { entry += d; drawPins(); err.textContent = ''; } }
  const onKey = (e) => {
    if (!usePin) return;
    if (/^[0-9]$/.test(e.key)) { press(e.key); e.preventDefault(); }
    else if (e.key === 'Backspace') { entry = entry.slice(0, -1); drawPins(); e.preventDefault(); }
    else if (e.key === 'Enter') { unlock(); e.preventDefault(); }
  };
  document.addEventListener('keydown', onKey, true);

  const key = (label, fn, cls = '') => h('button', { class: `key ${cls}`, type: 'button', 'aria-label': label, onclick: fn }, label);
  const pad = usePin
    ? [h('div', { class: 't' }, 'Enter your PIN'), pins,
      h('div', { class: 'keys' }, ['1', '2', '3', '4', '5', '6', '7', '8', '9'].map((d) => key(d, () => press(d))),
        h('button', { class: 'key ghost', type: 'button', 'aria-label': 'Delete', onclick: () => { entry = entry.slice(0, -1); drawPins(); } }, '⌫'),
        key('0', () => press('0')), h('button', { class: 'key go', type: 'button', onclick: unlock }, 'Unlock'))]
    : [h('div', { class: 't' }, 'Enter your password'), pw, h('button', { class: 'key go', type: 'button', onclick: unlock }, 'Unlock'),
      h('div', { class: 'small', style: { color: 'var(--nav-group)', textAlign: 'center' } }, 'Tip: set a PIN in My account for faster unlocking.')];
  pw.addEventListener('keydown', (e) => { if (e.key === 'Enter') unlock(); });
  if (usePin) drawPins();

  lockEl = h('div', { class: 'lock', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Screen locked' },
    h('div', { class: 'left' }, h('img', { class: 'logo', src: '/assets/logo.svg', alt: 'AureoClean' }), clock, date,
      h('div', { class: 'who' }, `Locked · ${user.fullName || user.username}`),
      h('div', { class: 'note' }, minutes ? `Locked after ${minutes} min without use. Alerts and texts keep running.` : 'Alerts and texts keep running while locked.')),
    h('div', { class: 'pad' }, pad, err,
      h('button', { class: 'out', type: 'button', onclick: () => { close(); signOut(); } }, 'Sign out instead')));
  document.body.append(lockEl);
  if (!usePin) setTimeout(() => pw.focus(), 50);
}
