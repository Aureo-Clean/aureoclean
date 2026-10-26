// Core helpers for the AureoClean web app: DOM building (text only, never raw HTML, so nothing
// typed by a user can ever run as code), the API client, routing, toasts, modals,
// and the bridge to the Pi app window.

// ------------------------------------------------------------------ DOM
// Screens are built from pieces that may be "nothing" (null/false) when a feature
// doesn't apply; never show those as the words "null"/"false".
for (const name of ['append', 'prepend', 'replaceChildren']) {
  const original = Element.prototype[name];
  Element.prototype[name] = function (...nodes) {
    return original.apply(this, nodes.flat(Infinity).filter((n) => n !== null && n !== undefined && n !== false && n !== ''));
  };
}

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
      else if (k === 'dataset') Object.assign(el.dataset, v);
      else if (v === true) el.setAttribute(k, '');
      else el.setAttribute(k, String(v));
    }
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

// Put the cursor in a box once the screen is up — unless the user already tapped into something.
export function focusSoon(el, ms = 50) {
  setTimeout(() => {
    const a = document.activeElement;
    if (el && el.isConnected && (!a || a === document.body || a === document.documentElement)) el.focus();
  }, ms);
}

export function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }

const SVG_NS = 'http://www.w3.org/2000/svg';
const ICONS = {
  home: ['path', { d: 'M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z' }],
  reports: [['rect', { x: 6, y: 4, width: 12, height: 17, rx: 2 }], ['path', { d: 'M9 4h6v3H9zM9 12h6M9 16h4' }]],
  people: [['circle', { cx: 9, cy: 8, r: 3.5 }], ['path', { d: 'M2.5 20c.8-3.5 3.4-5.5 6.5-5.5s5.7 2 6.5 5.5' }],
    ['circle', { cx: 17, cy: 9, r: 2.5 }], ['path', { d: 'M16 14.6c2.6.2 4.6 2 5.3 4.9' }]],
  pulse: ['path', { d: 'M3 12h4l2.5-6 5 12 2.5-6H21' }],
  user: [['circle', { cx: 12, cy: 8, r: 4 }], ['path', { d: 'M4 21c1-4 4.3-6 8-6s7 2 8 6' }]],
  lock: [['rect', { x: 5, y: 11, width: 14, height: 10, rx: 2 }], ['path', { d: 'M8 11V8a4 4 0 0 1 8 0v3' }]],
  logout: ['path', { d: 'M15 4h4a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1h-4M10 17l5-5-5-5M15 12H4' }],
  chart: ['path', { d: 'M4 20V10M10 20V4M16 20v-7M22 20H2' }],
  plus: ['path', { d: 'M12 5v14M5 12h14' }],
  check: ['path', { d: 'm5 12 5 5 9-10' }],
  pin: [['path', { d: 'M12 21s-7-6.2-7-11a7 7 0 0 1 14 0c0 4.8-7 11-7 11z' }], ['circle', { cx: 12, cy: 10, r: 2.5 }]],
  reload: ['path', { d: 'M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7' }],
  sensors: [['path', { d: 'M12 13a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3z' }], ['path', { d: 'M8.5 15.5a5 5 0 0 1 0-7M15.5 8.5a5 5 0 0 1 0 7M5.6 18.4a9 9 0 0 1 0-12.8M18.4 5.6a9 9 0 0 1 0 12.8' }]],
  external: ['path', { d: 'M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5' }],
  wifi: ['path', { d: 'M2 9a15 15 0 0 1 20 0M5 12.5a10 10 0 0 1 14 0M8.5 16a5 5 0 0 1 7 0M12 19.5h.01' }],
  sms: ['path', { d: 'M4 5h16v11H8l-4 4z' }],
  download: ['path', { d: 'M12 4v11M7 10l5 5 5-5M5 20h14' }],
  inbox: [['path', { d: 'M3 13h5l2 3h4l2-3h5' }], ['path', { d: 'M5 5h14l2 8v6a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-6z' }]],
  search: [['circle', { cx: 11, cy: 11, r: 7 }], ['path', { d: 'm20 20-3.5-3.5' }]],
  more: ['path', { d: 'M5 12h.01M12 12h.01M19 12h.01' }],
  x: ['path', { d: 'M6 6l12 12M18 6 6 18' }],
  expand: ['path', { d: 'M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5' }],
  back: ['path', { d: 'm15 6-6 6 6 6' }],
  keyboard: [['rect', { x: 2.5, y: 6, width: 19, height: 12, rx: 2 }], ['path', { d: 'M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10' }]],
  shift: ['path', { d: 'M12 4 4 12h4.5v7h7v-7H20z' }],
  bksp: [['path', { d: 'M9 5h11a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H9l-6-7z' }], ['path', { d: 'm12 9 6 6M18 9l-6 6' }]],
  kbdown: [['rect', { x: 3, y: 3, width: 18, height: 11, rx: 2 }], ['path', { d: 'M7 7h.01M11 7h.01M15 7h.01M8 10.5h8M9 18l3 3 3-3' }]],
  sparkle: [['path', { d: 'M12 3.5l1.9 5.1 5.1 1.9-5.1 1.9L12 17.5l-1.9-5.1-5.1-1.9 5.1-1.9z' }], ['path', { d: 'M19 15.5l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z' }]],
  settings: [['circle', { cx: 12, cy: 12, r: 3 }], ['path', { d: 'M12 2.5v3M12 18.5v3M4.2 6.5l2.6 1.5M17.2 16l2.6 1.5M4.2 17.5l2.6-1.5M17.2 8l2.6-1.5' }]],
  shield: ['path', { d: 'M12 3 4 6v6c0 4.5 3.4 8.3 8 9 4.6-.7 8-4.5 8-9V6z' }],
};

export function icon(name, cls = 'icon-svg') {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.8');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('class', cls);
  let parts = ICONS[name] || ICONS.more;
  if (typeof parts[0] === 'string') parts = [parts];
  for (const [tag, attrs] of parts) {
    const el = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    svg.append(el);
  }
  return svg;
}

// ------------------------------------------------------------------ API
export class ApiError extends Error {
  constructor(message, status = 0) { super(message); this.status = status; }
}

const TOKEN_KEY = 'aureo.token';
let token = null;
try { token = sessionStorage.getItem(TOKEN_KEY); } catch { /* private mode */ }

export const session = {
  get token() { return token; },
  set(t) { token = t; try { t ? sessionStorage.setItem(TOKEN_KEY, t) : sessionStorage.removeItem(TOKEN_KEY); } catch { /* ignore */ } },
  user: null,
};

const SLOW = 'The AureoClean service is taking too long — the Pi\'s internet may be slow. Try again.';

export async function api(method, path, body, { timeout = 50000, raw = false } = {}) {
  const attempts = method === 'GET' ? 2 : 1;
  for (let i = 1; i <= attempts; i++) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeout);
    try {
      const res = await fetch(path, {
        method,
        headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: ctrl.signal,
        cache: 'no-store',
      });
      if (!res.ok) {
        let message = `Request failed (${res.status}).`;
        try {
          const data = await res.json();
          if (typeof data.detail === 'string') message = data.detail;
          else if (Array.isArray(data.detail)) message = 'Please check the form: ' + data.detail.map((d) => `${(d.loc || []).slice(-1)[0]} ${d.msg}`).join('; ');
        } catch { /* not JSON */ }
        const err = new ApiError(message, res.status);
        if (res.status === 401 && token) onSessionEnded(message);
        throw err;
      }
      if (raw) return res;
      const text = await res.text();
      return text ? JSON.parse(text) : null;
    } catch (err) {
      if (err instanceof ApiError) throw err;
      if (err.name === 'AbortError') { if (i === attempts) throw new ApiError(SLOW); continue; }
      throw new ApiError('Can\'t reach the AureoClean service. Is the Pi on and connected?');
    } finally {
      clearTimeout(timer);
    }
  }
  throw new ApiError(SLOW);
}
export const get = (path, opts) => api('GET', path, undefined, opts);
export const post = (path, body = {}, opts) => api('POST', path, body, opts);

let sessionEndedHandler = () => {};
export function onSessionEnded(message) { sessionEndedHandler(message); }
export function setSessionEndedHandler(fn) { sessionEndedHandler = fn; }

// ------------------------------------------------------------------ routing
const routes = [];
export function route(pattern, view) { routes.push({ pattern, view }); }
export function go(path, { replace = false } = {}) {
  const hash = '#' + path;
  if (location.hash === hash) { window.dispatchEvent(new HashChangeEvent('hashchange')); return; }
  replace ? location.replace(hash) : (location.hash = hash);
}
export function currentPath() { return location.hash.replace(/^#/, '') || '/'; }
export function matchRoute(path) {
  const [pathname, qs] = path.split('?');
  for (const r of routes) {
    const names = [];
    const re = new RegExp('^' + r.pattern.replace(/:[a-z]+/gi, (m) => { names.push(m.slice(1)); return '([^/]+)'; }) + '$');
    const m = pathname.match(re);
    if (m) {
      const params = Object.fromEntries(names.map((n, i) => [n, decodeURIComponent(m[i + 1])]));
      return { view: r.view, params, query: new URLSearchParams(qs || '') };
    }
  }
  return null;
}

// ------------------------------------------------------------------ toasts
export function toast(text, kind = '') {
  const box = document.getElementById('toasts');
  const el = h('div', { class: `toast ${kind}` }, text);
  box.append(el);
  while (box.children.length > 2) box.firstElementChild.remove(); // newest two only
  setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 250); }, kind === 'error' ? 6000 : 3500);
}

// ------------------------------------------------------------------ modals
export function modal(build, { wide = false, onClose } = {}) {
  const prevFocus = document.activeElement;
  const box = h('div', { class: `modal${wide ? ' wide' : ''}`, role: 'dialog', 'aria-modal': 'true' });
  const overlay = h('div', { class: 'overlay' }, box);
  const close = (value) => {
    overlay.remove();
    document.removeEventListener('keydown', onKey);
    if (prevFocus && prevFocus.focus) prevFocus.focus();
    onClose && onClose(value);
  };
  const onKey = (e) => { if (e.key === 'Escape') close(); };
  overlay.addEventListener('mousedown', (e) => { if (e.target === overlay) close(); });
  document.addEventListener('keydown', onKey);
  append(box, [build(close)]);
  document.body.append(overlay);
  const first = box.querySelector('input, select, textarea, button.primary, button');
  // Focus the first field — unless the user already tapped into the dialog.
  if (first) setTimeout(() => { if (!box.contains(document.activeElement)) first.focus(); }, 30);  // (dialogs: focus moves inside)
  return close;
}
export function closeAllModals() { document.querySelectorAll('.overlay').forEach((o) => o.remove()); }

export function confirmBox(title, text, { ok = 'OK', danger = false } = {}) {
  return new Promise((resolve) => {
    let answered = false;
    modal((close) => [
      h('h2', null, title),
      h('p', { class: 'muted' }, text),
      h('div', { class: 'actions' },
        h('button', { class: 'btn', onclick: () => close() }, 'Cancel'),
        h('button', { class: `btn ${danger ? 'danger solid' : 'primary'}`, onclick: () => { answered = true; close(); resolve(true); } }, ok)),
    ], { onClose: () => { if (!answered) resolve(false); } });
  });
}

// ------------------------------------------------------------------ busy buttons
export async function busy(btn, label, fn) {
  const old = Array.from(btn.childNodes);
  btn.disabled = true;
  clear(btn);
  append(btn, [h('span', { class: 'spinner', 'aria-hidden': 'true' }), label]);
  try { return await fn(); } finally {
    btn.disabled = false;
    clear(btn);
    append(btn, old);
  }
}

// ------------------------------------------------------------------ formatting
export function ago(iso) {
  if (!iso) return '';
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 172800) return 'yesterday';
  return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}
export function when(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  const today = new Date();
  const time = d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
  if (d.toDateString() === today.toDateString()) return time;
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) + ', ' + time;
}
export function initials(name) {
  return (name || '?').split(/\s+/).filter(Boolean).slice(0, 2).map((p) => p[0].toUpperCase()).join('') || '?';
}
export function firstName(user) { return ((user && (user.fullName || user.username)) || '').split(/\s+/)[0]; }
export function greeting() {
  const hr = new Date().getHours();
  return hr < 12 ? 'Good morning' : hr < 18 ? 'Good afternoon' : 'Good evening';
}
export function today() {
  return new Date().toLocaleDateString(undefined, { weekday: 'long', month: 'short', day: 'numeric' });
}

// ------------------------------------------------------------------ the Pi app window
// The Pi app (desktop/shell.py) loads this page with ?shell=pi and listens for
// console messages starting with "AUREO:" (a simple, CSP-safe bridge).
export const shell = {
  isPi: new URLSearchParams(location.search).get('shell') === 'pi',
  kiosk: false,
  send(action, data = {}) { if (this.isPi) console.info('AUREO:' + JSON.stringify({ action, ...data })); },
  listeners: new Set(),
};
window.AureoShell = {
  // Called by the Pi app, e.g. AureoShell.receive({event: "kiosk", on: true})
  receive(msg) { for (const fn of shell.listeners) fn(msg); },
};
shell.listeners.add((msg) => { if (msg.event === 'kiosk') shell.kiosk = !!msg.on; });
shell.listeners.add((msg) => { if (msg.event === 'toast') toast(msg.text, msg.kind || ''); });

export function openExternal(url) {
  if (shell.isPi) shell.send('open', { url });
  else window.open(url, '_blank', 'noopener');
}

// ------------------------------------------------------------------ data cache
// Screens open straight away with the last data, then refresh in the background.
const cache = new Map();
export async function cached(key, loader, maxAge = 15000) {
  const hit = cache.get(key);
  if (hit && Date.now() - hit.at < maxAge) return hit.value;
  const value = await loader();
  cache.set(key, { at: Date.now(), value });
  return value;
}
export function peek(key) { const hit = cache.get(key); return hit ? hit.value : undefined; }
export function invalidate(...keys) { keys.length ? keys.forEach((k) => cache.delete(k)) : cache.clear(); }
