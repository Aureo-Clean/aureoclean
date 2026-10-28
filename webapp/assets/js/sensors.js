// Live sensors: your Arduino Cloud dashboards, drawn in AureoClean's style.
// Each Arduino dashboard is a page with the same name ("Urinal", "Page 2", …) and the
// same widgets (gauges, percentages, LEDs/status, charts, values). Read by the Pi with an
// API key, so nobody signs in to Arduino. Values update in place every few seconds with
// gentle animations; small charts show the last few hours.
import { ago, busy, get, go, h, icon, modal, openExternal, post, session, shell, toast } from './core.js';
import { field, message, say, switchEl } from './ui.js';
import { STATE, alertEditor, canAlert } from './sensor-alerts.js';

const REFRESH_MS = 5000;
const HISTORY_MS = 180000;
const STALE_MS = 15 * 60000;
const ALERT_WORDS = /(smoke|fire|leak|flood|alarm|emergency|gas|overflow|full|vandal|tamper)/i;
const SVG_NS = 'http://www.w3.org/2000/svg';
// Motion is kept small: numbers count up briefly on phones and laptops; on the Pi (and with
// "reduce motion") values just change. No looping animations — they slowed the Pi down.
const motion = () => !document.documentElement.classList.contains('on-pi')
  && !(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
let uid = 0;

// Shared between the Home card and the full view, so switching is instant.
const store = { data: null, series: {}, historyAt: 0, error: '', at: 0, pending: {} };

function s(tag, attrs = {}, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null) el.setAttribute(k, String(v));
  for (const c of children.flat()) if (c) el.append(c);
  return el;
}
const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const remember = (k, v) => { try { localStorage.setItem(k, v); } catch { /* private mode */ } };
const recall = (k) => { try { return localStorage.getItem(k); } catch { return null; } };

// ------------------------------------------------------------------ numbers
function decimalsFor(v) {
  if (!isNum(v) || Math.abs(v) >= 100 || Number.isInteger(v)) return 0;
  return Math.abs(v) >= 10 ? 1 : Math.min(2, (String(v).split('.')[1] || '').length);
}
function fmt(v, d) { return isNum(v) ? v.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }) : '—'; }
const ease = (t) => 1 - Math.pow(1 - t, 3);

// Count a number smoothly from its last value to the new one.
function tween(el, to, d = decimalsFor(to)) {
  const from = isNum(el._v) ? el._v : null;
  el._v = to;
  if (!isNum(to)) { el.textContent = '—'; return; }
  if (from === null || !motion() || from === to || !el.isConnected) { el.textContent = fmt(to, d); return; }
  const start = performance.now();
  const dur = 350;
  cancelAnimationFrame(el._raf);
  const step = (now) => {
    const t = Math.min(1, (now - start) / dur);
    el.textContent = fmt(from + (to - from) * ease(t), d);
    if (t < 1) el._raf = requestAnimationFrame(step);
  };
  el._raf = requestAnimationFrame(step);
}

function setPath(path, d) {
  if (path.getAttribute('d') !== d) path.setAttribute('d', d);
}

// Smooth line through points (x, y in 0..W, 0..H).
function linePath(pts) {
  if (!pts.length) return '';
  if (pts.length === 1) return `M${pts[0][0]} ${pts[0][1]}`;
  let d = `M${pts[0][0].toFixed(1)} ${pts[0][1].toFixed(1)}`;
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[i - 1] || pts[i]; const p1 = pts[i]; const p2 = pts[i + 1]; const p3 = pts[i + 2] || p2;
    const c1x = p1[0] + (p2[0] - p0[0]) / 6; const c1y = p1[1] + (p2[1] - p0[1]) / 6;
    const c2x = p2[0] - (p3[0] - p1[0]) / 6; const c2y = p2[1] - (p3[1] - p1[1]) / 6;
    d += ` C${c1x.toFixed(1)} ${c1y.toFixed(1)} ${c2x.toFixed(1)} ${c2y.toFixed(1)} ${p2[0].toFixed(1)} ${p2[1].toFixed(1)}`;
  }
  return d;
}

// ------------------------------------------------------------------ history
// Server history (last hours) + the live values seen since, newest last.
function seriesFor(v) { return (store.series[v.id] || []).slice(-60); }
function noteLive(data) {
  // Live values move the newest point; a new point starts every 6 minutes, like the history.
  for (const t of data.things || []) {
    for (const v of t.properties) {
      const list = store.series[v.id];
      if (!isNum(v.value) || !list || !list.length) continue;
      const at = Math.round(new Date(v.updatedAt || Date.now()).getTime() / 1000);
      const last = list[list.length - 1];
      if (at < last[0]) continue;
      const prev = list[list.length - 2];
      if (list.tail && prev && at - prev[0] < 360) list[list.length - 1] = [at, v.value];
      else { list.push([at, v.value]); list.tail = true; if (list.length > 61) list.shift(); }
    }
  }
}
function trend(series) {
  if (series.length < 6) return null;
  const now = series[series.length - 1][1];
  const before = series[Math.max(0, series.length - 6)][1];
  const vals = series.map((p) => p[1]);
  const range = Math.max(...vals) - Math.min(...vals) || Math.abs(now) || 1;
  const delta = now - before;
  if (Math.abs(delta) < range * 0.08) return { dir: 'steady', text: 'steady' };
  return { dir: delta > 0 ? 'up' : 'down', text: `${delta > 0 ? '+' : '−'}${fmt(Math.abs(delta), decimalsFor(Math.abs(delta)) || 1)} in 30 min` };
}

// ------------------------------------------------------------------ widgets
function kindOf(w) {
  const t = w.type || '';
  const v = (w.values[0] || {});
  if (/chart/.test(t)) return 'chart';
  if (/gauge/.test(t)) return 'gauge';
  if (/percent/.test(t) || (!t && /PERCENT/i.test(v.type))) return 'ring';
  if (/sticky|note/.test(t)) return 'note';
  if (/messenger/.test(t)) return 'message';
  if (/map|location/.test(t)) return 'map';
  if (/image|webview|weblink|link/.test(t)) return 'other';
  if (typeof v.value === 'boolean' || /led|status|switch|button|light/.test(t)) return 'state';
  if (/slider|stepper|selector|dropdown/.test(t)) return 'level';
  if (isNum(v.value)) return 'value';
  return 'text';
}

function isStale(v) { return v && v.updatedAt && Date.now() - new Date(v.updatedAt).getTime() > STALE_MS; }
function whenText(v) {
  if (!v || !v.updatedAt) return 'no value yet';
  return isStale(v) ? `last seen ${ago(v.updatedAt)}` : ago(v.updatedAt);
}
function bump() { /* (the old "glow on change" forced a re-layout on every change; removed) */ }

// ------------------------------------------------------------------ controls (switches, sliders …)
// A change shows at once; it's kept for a few seconds so the next refresh doesn't snap it back
// before the device confirms. If Arduino refuses, it springs back with the reason.
const HOLD_MS = 8000;
const BOOL_TYPES = /^(BOOL|STATUS|HOME_SWITCH)$/i;
function isBool(v) { return typeof v.value === 'boolean' || BOOL_TYPES.test(v.type || ''); }
function isText(v) { return typeof v.value === 'string' || /STRING/i.test(v.type || ''); }
function canChange(v) {
  return !!(v && v.writable && store.data && store.data.canControl && (isBool(v) || isNum(v.value) || isText(v) || v.value == null));
}
function withPending(v) {
  const p = store.pending[v.id];
  if (!p) return v;
  if (Date.now() > p.until || (p.sent && v.value === p.value)) { delete store.pending[v.id]; return v; }
  return { ...v, value: p.value, pending: !p.sent };
}
function notify() { listeners.forEach((fn) => fn()); }
async function sendValue(v, value, name = '') {
  const prev = v.value;
  store.pending[v.id] = { value, until: Date.now() + HOLD_MS, sent: false };
  notify();
  try {
    const r = await post('/sensors/control', { thingId: v.thingId, propertyId: v.id, value }, { timeout: 20000 });
    store.pending[v.id] = { value: r.value, until: Date.now() + HOLD_MS, sent: true };
    for (const t of (store.data && store.data.things) || []) for (const x of t.properties) if (x.id === v.id) x.value = r.value;
    toast(name ? `${name} → ${r.shown}` : r.message, 'success');
  } catch (err) {
    delete store.pending[v.id];
    for (const t of (store.data && store.data.things) || []) for (const x of t.properties) if (x.id === v.id) x.value = prev;
    toast(err.message, 'error');
  }
  notify();
}
const noBubble = (e) => e.stopPropagation();   // a control's tap doesn't open the details

function frame(w, kind, ...content) {
  const title = h('div', { class: 'wname', title: w.name }, w.name);
  const foot = h('div', { class: 'wwhen' });
  const el = h('article', { class: `widget w-${kind}` }, h('div', { class: 'whead' }, title), ...content, foot);
  return { el, title, foot };
}

function makeValue(w) {
  const num = h('span', { class: 'wnum num' });
  const unit = h('span', { class: 'wunit' });
  const tr = h('span', { class: 'wtrend' });
  const spark = sparkline();
  const change = h('button', { class: 'wchange hidden', type: 'button' }, 'Change…');
  const f = frame(w, 'value', h('div', { class: 'wvalue' }, num, unit), h('div', { class: 'wmeta' }, tr, change), spark.el);
  change.addEventListener('click', (e) => { noBubble(e); openDetail(f.el._w || w); });
  return {
    el: f.el,
    update(w2) {
      const v = w2.values[0] || {};
      change.classList.toggle('hidden', !canChange(v));
      if (num._v !== undefined && num._v !== v.value) bump(f.el);
      tween(num, isNum(v.value) ? v.value : null);
      unit.textContent = v.unit || '';
      const series = seriesFor(v);
      spark.draw(series);
      const t = trend(series);
      tr.textContent = t ? t.text : '';
      tr.className = `wtrend ${t ? t.dir : ''}`;
      f.foot.textContent = whenText(v);
      f.el.classList.toggle('stale', !!isStale(v));
    },
  };
}

function sparkline({ height = 34, axis = false } = {}) {
  const id = `sg${++uid}`;
  const area = s('path', { class: 'sarea', fill: `url(#${id})` });
  const line = s('path', { class: 'sline' });
  const dot = s('circle', { class: 'sdot', r: 2.6 });
  const svg = s('svg', { viewBox: `0 0 300 ${height}`, preserveAspectRatio: 'none', 'aria-hidden': 'true', class: 'spark' },
    s('defs', {}, s('linearGradient', { id, x1: 0, y1: 0, x2: 0, y2: 1 },
      s('stop', { offset: '0%', 'stop-color': 'currentColor', 'stop-opacity': '.22' }),
      s('stop', { offset: '100%', 'stop-color': 'currentColor', 'stop-opacity': '0' }))),
    area, line, dot);
  const lims = s('g', { class: 'slimits' });
  if (axis) svg.append(lims);
  const lo = h('span'); const hi = h('span');
  const guide = h('div', { class: 'sguide hidden' });
  const mark = h('div', { class: 'smark hidden' });
  const tip = h('div', { class: 'stip hidden' });
  const el = axis ? h('div', { class: 'chartbox' }, svg, guide, mark, tip, h('div', { class: 'yaxis' }, hi, lo)) : h('div', { class: 'sparkbox' }, svg);
  let last = null; let hideTimer = null;
  if (axis) {
    // Touch or move along the chart to read the value at that time.
    const scrub = (e) => {
      if (!last || last.series.length < 2) return;
      const rect = svg.getBoundingClientRect();
      const fx = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
      let i = 0; let best = Infinity;
      last.pts.forEach((p, k) => { const d = Math.abs(p[0] / 300 - fx); if (d < best) { best = d; i = k; } });
      const [t, v] = last.series[i];
      const x = (last.pts[i][0] / 300) * rect.width; const y = (last.pts[i][1] / height) * rect.height;
      guide.style.left = `${x}px`; mark.style.left = `${x}px`; mark.style.top = `${y}px`;
      const d = new Date(t * 1000);
      const same = d.toDateString() === new Date().toDateString();
      tip.textContent = `${fmt(v, decimalsFor(Math.abs(v) < 10 ? Math.round(v * 100) / 100 : Math.round(v)))}${last.unit ? ` ${last.unit}` : ''} · ${
        d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })}${same ? '' : `, ${d.toLocaleDateString(undefined, { weekday: 'short' })}`}`;
      tip.style.left = `${Math.max(4, Math.min(rect.width - tip.offsetWidth - 4, x - tip.offsetWidth / 2))}px`;
      [guide, mark, tip].forEach((n) => n.classList.remove('hidden'));
      clearTimeout(hideTimer);
    };
    const hide = () => { clearTimeout(hideTimer); hideTimer = setTimeout(() => [guide, mark, tip].forEach((n) => n.classList.add('hidden')), 1400); };
    el.addEventListener('pointerdown', (e) => { noBubble(e); scrub(e); });
    el.addEventListener('pointermove', scrub);
    el.addEventListener('pointerleave', hide);
    el.addEventListener('pointerup', hide);
    el.addEventListener('click', noBubble);
  }
  return {
    el,
    draw(series, unit = '', limits = []) {
      el.classList.toggle('empty', series.length < 2);
      lims.replaceChildren();
      if (series.length < 2) { last = null; return; }
      const vals = series.map((p) => p[1]).concat(limits.filter(isNum));
      let min = Math.min(...vals); let max = Math.max(...vals);
      if (max - min < 1e-9) { min -= 1; max += 1; }
      const pad = (max - min) * 0.12;
      min -= pad; max += pad;
      const t0 = series[0][0]; const t1 = series[series.length - 1][0] || t0 + 1;
      const pts = series.map(([t, v]) => [((t - t0) / Math.max(1, t1 - t0)) * 300, height - 2 - ((v - min) / (max - min)) * (height - 6)]);
      const d = linePath(pts);
      setPath(line, d);
      setPath(area, `${d} L300 ${height} L0 ${height} Z`);
      last = { series, pts, unit };
      const end = pts[pts.length - 1];
      dot.setAttribute('cx', end[0]); dot.setAttribute('cy', end[1]);
      for (const L of limits.filter(isNum)) {   // alert limits: a dashed red line
        const y = height - 2 - ((L - min) / (max - min)) * (height - 6);
        lims.append(s('line', { x1: 0, x2: 300, y1: y, y2: y, class: 'slimit' }));
      }
      const dd = decimalsFor(max - min < 10 ? 0.1 : 1);
      hi.textContent = fmt(max - pad, dd); lo.textContent = fmt(min + pad, dd);
    },
  };
}

function makeChart(w) {
  const num = h('span', { class: 'wnum num' });
  const unit = h('span', { class: 'wunit' });
  const chart = sparkline({ height: 90, axis: true });
  const span = h('div', { class: 'xaxis' }, h('span', { class: 'from' }), h('span', null, 'now'));
  const f = frame(w, 'chart', chart.el, span);
  f.el.querySelector('.whead').append(h('div', { class: 'wvalue sm' }, num, unit));
  return {
    el: f.el,
    update(w2) {
      const v = w2.values[0] || {};
      tween(num, isNum(v.value) ? v.value : null);
      unit.textContent = v.unit || '';
      const series = seriesFor(v);
      chart.draw(series, v.unit || '');
      span.firstChild.textContent = series.length > 1 ? ago(new Date(series[0][0] * 1000).toISOString()) : '';
      f.foot.textContent = series.length > 1 ? whenText(v) : 'Collecting history…';
    },
  };
}

function arc(cx, cy, r, a0, a1) {
  const p = (a) => [cx + r * Math.cos((a * Math.PI) / 180), cy + r * Math.sin((a * Math.PI) / 180)];
  const [x0, y0] = p(a0); const [x1, y1] = p(a1);
  return `M${x0.toFixed(2)} ${y0.toFixed(2)} A${r} ${r} 0 ${a1 - a0 > 180 ? 1 : 0} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`;
}

function makeGauge(w) {
  const d = arc(60, 60, 48, 150, 390);
  const track = s('path', { d, class: 'gtrack', pathLength: 100 });
  const bar = s('path', { d, class: 'gbar', pathLength: 100, 'stroke-dasharray': '100 100', 'stroke-dashoffset': 100 });
  const num = h('span', { class: 'wnum num' });
  const unit = h('span', { class: 'wunit' });
  const lo = h('span'); const hi = h('span');
  const f = frame(w, 'gauge', h('div', { class: 'gauge' }, s('svg', { viewBox: '0 0 120 104', 'aria-hidden': 'true' }, track, bar),
    h('div', { class: 'gcenter' }, h('div', { class: 'wvalue' }, num, unit)), h('div', { class: 'grange' }, lo, hi)));
  return {
    el: f.el,
    update(w2) {
      const v = w2.values[0] || {};
      const min = isNum(w2.options.min) ? w2.options.min : 0;
      const max = isNum(w2.options.max) && w2.options.max > min ? w2.options.max : (isNum(v.value) && v.value > 100 ? Math.ceil(v.value / 100) * 100 : 100);
      const frac = isNum(v.value) ? Math.max(0, Math.min(1, (v.value - min) / (max - min))) : 0;
      if (num._v !== undefined && num._v !== v.value) bump(f.el);
      tween(num, isNum(v.value) ? v.value : null);
      unit.textContent = v.unit || '';
      bar.style.strokeDashoffset = String(100 - frac * 100);
      lo.textContent = fmt(min, decimalsFor(min)); hi.textContent = fmt(max, decimalsFor(max));
      f.foot.textContent = whenText(v);
      f.el.classList.toggle('stale', !!isStale(v));
    },
  };
}

function makeRing(w) {
  const track = s('circle', { cx: 40, cy: 40, r: 32, class: 'rtrack', pathLength: 100 });
  const bar = s('circle', { cx: 40, cy: 40, r: 32, class: 'rbar', pathLength: 100, 'stroke-dasharray': '100 100', 'stroke-dashoffset': 100,
    transform: 'rotate(-90 40 40)' });
  const num = h('span', { class: 'wnum num' });
  const f = frame(w, 'ring', h('div', { class: 'ring' }, s('svg', { viewBox: '0 0 80 80', 'aria-hidden': 'true' }, track, bar),
    h('div', { class: 'rcenter' }, num, h('span', { class: 'wunit' }, '%'))));
  return {
    el: f.el,
    update(w2) {
      const v = w2.values[0] || {};
      const pct = isNum(v.value) ? Math.max(0, Math.min(100, v.value)) : 0;
      if (num._v !== undefined && num._v !== v.value) bump(f.el);
      tween(num, isNum(v.value) ? Math.round(v.value) : null, 0);
      bar.style.strokeDashoffset = String(100 - pct);
      f.el.classList.toggle('low', isNum(v.value) && pct <= 20);
      f.foot.textContent = whenText(v);
      f.el.classList.toggle('stale', !!isStale(v));
    },
  };
}

function makeState(w) {
  const light = h('span', { class: 'light' });
  const word = h('span', { class: 'sword' });
  const momentary = /button/.test(w.type || '') && !/switch/.test(w.type || '');
  const toggle = h('button', { class: 'wswitch hidden', type: 'button', role: 'switch', 'aria-label': `${w.name}: on or off` }, h('i'));
  const hold = h('button', { class: 'whold hidden', type: 'button' }, 'Press');
  let cur = null;
  toggle.addEventListener('click', (e) => { noBubble(e); if (cur) sendValue(cur.v, !cur.on, cur.name); });
  let down = false;
  hold.addEventListener('pointerdown', (e) => { noBubble(e); e.preventDefault(); if (!cur || down) return; down = true; hold.classList.add('down'); sendValue(cur.v, true, cur.name); });
  const release = (e) => { noBubble(e); if (!down) return; down = false; hold.classList.remove('down'); sendValue(cur.v, false, cur.name); };
  ['pointerup', 'pointerleave', 'pointercancel'].forEach((ev) => hold.addEventListener(ev, release));
  hold.addEventListener('click', noBubble);
  const f = frame(w, 'state', h('div', { class: 'state' }, light, word, h('div', { class: 'grow' }), toggle), hold);
  return {
    el: f.el,
    update(w2) {
      const v = w2.values[0] || {};
      const on = v.value === true || v.value === 1 || v.value === 'true' || v.value === 'on';
      const alertish = ALERT_WORDS.test(`${w2.name} ${v.name}`);
      const text = v.value === null || v.value === undefined ? '—' : on ? (alertish ? 'Yes' : 'On') : (alertish ? 'No' : 'Off');
      if (word.textContent && word.textContent !== text) bump(f.el);
      word.textContent = text;
      cur = { v, on, name: w2.name };
      const ctl = canChange(v) && isBool(v);
      toggle.classList.toggle('hidden', !ctl || momentary);
      hold.classList.toggle('hidden', !ctl || !momentary);
      toggle.setAttribute('aria-checked', String(on));
      toggle.classList.toggle('busy', !!v.pending);
      f.el.classList.toggle('controllable', ctl);
      f.el.classList.toggle('on', on && !alertish);
      f.el.classList.toggle('alert', on && alertish);
      f.el.classList.toggle('calm', !on && alertish);
      f.foot.textContent = ctl ? (momentary ? 'Hold to press · ' : 'Tap the switch · ') + whenText(v) : whenText(v);
      f.el.classList.toggle('stale', !!isStale(v));
    },
  };
}

function rangeOf(w, v) {
  const min = isNum(w.options.min) ? w.options.min : 0;
  const max = isNum(w.options.max) && w.options.max > min ? w.options.max : Math.max(100, isNum(v.value) ? Math.ceil(v.value) : 100);
  const step = isNum(w.options.step) && w.options.step > 0 ? w.options.step : (Number.isInteger(min) && Number.isInteger(max) && max - min >= 10 ? 1 : (max - min) / 100);
  return { min, max, step };
}

function makeLevel(w) {
  const num = h('span', { class: 'wnum num' });
  const unit = h('span', { class: 'wunit' });
  const fill = h('div', { class: 'lfill' });
  const lo = h('span'); const hi = h('span');
  const stepper = /stepper/.test(w.type || '');
  const range = h('input', { class: 'wrange hidden', type: 'range', 'aria-label': w.name });
  const minus = h('button', { class: 'wstep hidden', type: 'button', 'aria-label': 'Less' }, '−');
  const plus = h('button', { class: 'wstep hidden', type: 'button', 'aria-label': 'More' }, '+');
  let cur = null; let dragging = false; let stepTimer = null; let local = null;
  const show = (val, r) => {
    num.textContent = fmt(val, decimalsFor(r.step < 1 ? r.step : val)); num._v = val;
    fill.style.transform = `scaleX(${Math.max(0, Math.min(1, (val - r.min) / (r.max - r.min)))})`;
  };
  ['pointerdown', 'click', 'keydown'].forEach((ev) => [range, minus, plus].forEach((b) => b.addEventListener(ev, noBubble)));
  range.addEventListener('input', () => { dragging = true; show(Number(range.value), cur.r); });
  range.addEventListener('change', () => { dragging = false; sendValue(cur.v, Number(range.value), cur.name); });
  const nudge = (dir) => {
    const r = cur.r;
    local = Math.max(r.min, Math.min(r.max, +((local ?? cur.v.value ?? r.min) + dir * r.step).toFixed(6)));
    show(local, r);
    clearTimeout(stepTimer);
    stepTimer = setTimeout(() => { const val = local; local = null; sendValue(cur.v, val, cur.name); }, 700);
  };
  minus.addEventListener('click', () => nudge(-1));
  plus.addEventListener('click', () => nudge(1));
  const f = frame(w, 'level', h('div', { class: 'wvalue' }, minus, num, unit, plus), h('div', { class: 'level' }, fill), range, h('div', { class: 'grange' }, lo, hi));
  return {
    el: f.el,
    update(w2) {
      const v = w2.values[0] || {};
      const r = rangeOf(w2, v);
      cur = { v, r, name: w2.name };
      const ctl = canChange(v) && !isBool(v) && !isText(v);
      range.classList.toggle('hidden', !ctl || stepper);
      minus.classList.toggle('hidden', !ctl || !stepper);
      plus.classList.toggle('hidden', !ctl || !stepper);
      f.el.classList.toggle('controllable', ctl);
      range.min = r.min; range.max = r.max; range.step = r.step;
      if (!dragging && local === null) {
        if (isNum(v.value)) range.value = v.value;
        if (num._v !== undefined && num._v !== v.value) bump(f.el);
        tween(num, isNum(v.value) ? v.value : null);
        fill.style.transform = `scaleX(${isNum(v.value) ? Math.max(0, Math.min(1, (v.value - r.min) / (r.max - r.min))) : 0})`;
      }
      unit.textContent = v.unit || '';
      lo.textContent = fmt(r.min, decimalsFor(r.min)); hi.textContent = fmt(r.max, decimalsFor(r.max));
      f.foot.textContent = ctl ? (stepper ? 'Tap − / + · ' : 'Drag to change · ') + whenText(v) : whenText(v);
      f.el.classList.toggle('stale', !!isStale(v));
    },
  };
}

function makeText(w, kind) {
  const body = h('div', { class: 'wtext' });
  const change = h('button', { class: 'wchange hidden', type: 'button' }, kind === 'message' ? 'Send a message…' : 'Change…');
  const f = frame(w, kind, body, change);
  change.addEventListener('click', (e) => { noBubble(e); openDetail(f.el._w || w); });
  return {
    el: f.el,
    update(w2) {
      const v = w2.values[0] || {};
      change.classList.toggle('hidden', !(canChange(v) && isText(v)) || kind === 'note' || kind === 'other');
      let text;
      if (kind === 'note') text = w2.options.text || w2.options.content || w2.name;
      else if (kind === 'map' && v.value && typeof v.value === 'object') text = `${fmt(v.value.lat, 5)}, ${fmt(v.value.lon, 5)}`;
      else if (kind === 'other') text = 'Shown in Arduino\'s own dashboard';
      else if (v.value === null || v.value === undefined) text = '—';
      else text = typeof v.value === 'object' ? JSON.stringify(v.value) : String(v.value);
      if (body.textContent && body.textContent !== text) bump(f.el);
      body.textContent = String(text).slice(0, 240);
      f.foot.textContent = kind === 'note' || kind === 'other' ? '' : whenText(v);
    },
  };
}

function makeWidget(w) {
  const kind = kindOf(w);
  const maker = { value: makeValue, chart: makeChart, gauge: makeGauge, ring: makeRing, state: makeState, level: makeLevel }[kind];
  const wd = maker ? maker(w) : makeText(w, kind);
  if (kind === 'chart' || (kind === 'note' && (w.w || 1) >= 3)) wd.el.classList.add('wide');
  if (kind === 'gauge') wd.el.classList.add('tall');
  wd.kind = kind;
  if (kind !== 'note' && kind !== 'other' && w.values.length) {
    wd.el.setAttribute('tabindex', '0');
    wd.el.setAttribute('role', 'button');
    wd.el.setAttribute('aria-label', `${w.name}: details`);
    wd.el.addEventListener('click', () => openDetail(wd.el._w || w));
    wd.el.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openDetail(wd.el._w || w); } });
  }
  const update = wd.update;
  const badge = h('span', { class: 'walert hidden' });
  wd.el.querySelector('.whead').append(badge);
  wd.update = (w2) => { wd.el._w = w2; update(w2); markAlerts(wd.el, badge, w2); };
  return wd;
}

// A value over its alert limit: amber while waiting, red once the alert went out (with the report number).
function alertsFor(id) { return ((store.data && store.data.alerts) || {})[id] || []; }
const SMART_VAR = { temperature: 'temperature', iaq: 'iaq', gasresistance: 'gas', totalflushes: 'flushes', totaltofusers: 'uses', totaluses: 'uses' };
function smartFor(v) {
  const tier = (((store.data && store.data.smart) || {})[v.thingId] || {})[SMART_VAR[(v.name || '').toLowerCase()]];
  return tier ? [{ state: tier >= 1 ? 'alert' : 'over', text: 'Smart alert', ref: null, smart: true }] : [];
}
function worstAlert(w) {
  const all = w.values.flatMap((v) => alertsFor(v.id).concat(smartFor(v)));
  return all.find((a) => a.state === 'alert') || all.find((a) => a.state === 'over') || null;
}
function markAlerts(el, badge, w) {
  const a = worstAlert(w);
  el.classList.toggle('sensor-alert', !!a && a.state === 'alert');
  el.classList.toggle('sensor-over', !!a && a.state === 'over');
  badge.classList.toggle('hidden', !a);
  badge.replaceChildren(...(a ? [icon('bell'), a.state === 'alert' && a.ref ? `#${a.ref}` : 'Over limit'] : []));
  badge.title = a ? a.text : '';
}

// ------------------------------------------------------------------ details (tap a tile)
function currentValue(id) {
  for (const t of (store.data && store.data.things) || []) for (const v of t.properties) if (v.id === id) return withPending(v);
  return null;
}

function controlFor(w, v, after) {
  if (!canChange(v)) return null;
  if (isBool(v)) {
    const momentary = /button/.test(w.type || '') && !/switch/.test(w.type || '');
    if (momentary) {
      const b = h('button', { class: 'btn primary lg block', type: 'button' }, 'Hold to press');
      let down = false;
      b.addEventListener('pointerdown', (e) => { e.preventDefault(); down = true; sendValue(v, true, w.name); });
      ['pointerup', 'pointerleave', 'pointercancel'].forEach((ev) => b.addEventListener(ev, () => { if (down) { down = false; sendValue(v, false, w.name); } }));
      return b;
    }
    const on = v.value === true;
    const b = h('button', { class: `btn lg block ${on ? '' : 'primary'}`, type: 'button' }, on ? 'Turn off' : 'Turn on');
    b.addEventListener('click', () => sendValue(v, !on, w.name).then(after));
    return b;
  }
  if (isText(v)) {
    const input = h('input', { class: 'input', type: 'text', maxlength: 256, placeholder: 'Type a message', 'aria-label': 'New value' });
    const b = h('button', { class: 'btn primary', type: 'button' }, 'Send');
    const go2 = () => { if (input.value.trim()) { sendValue(v, input.value, w.name).then(after); input.value = ''; } };
    b.addEventListener('click', go2);
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') go2(); });
    return h('div', { class: 'row', style: { gap: '8px' } }, input, b);
  }
  const r = rangeOf(w, v);
  const input = h('input', { class: 'input num', type: 'number', inputmode: 'decimal', step: 'any', min: r.min, max: r.max, value: isNum(v.value) ? v.value : '', 'aria-label': 'New value', style: { width: '120px' } });
  const range = h('input', { class: 'wrange', type: 'range', min: r.min, max: r.max, step: r.step, value: isNum(v.value) ? v.value : r.min, 'aria-label': w.name });
  range.addEventListener('input', () => { input.value = range.value; });
  input.addEventListener('input', () => { range.value = input.value; });
  const b = h('button', { class: 'btn primary', type: 'button' }, 'Set');
  const go2 = () => { const n = Number(input.value); if (input.value !== '' && Number.isFinite(n)) sendValue(v, n, w.name).then(after); };
  b.addEventListener('click', go2);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') go2(); });
  return h('div', { class: 'stack tight' }, range, h('div', { class: 'row', style: { gap: '8px' } }, input, h('span', { class: 'muted' }, v.unit || ''), h('div', { class: 'grow' }), b));
}

function openDetail(w) {
  const first = w.values[0];
  if (!first) return;
  let hours = 6;
  const big = h('span', { class: 'wnum num' });
  const unit = h('span', { class: 'wunit' });
  const updated = h('span', { class: 'small muted' });
  const ctlSlot = h('div', { class: 'detail-ctl' });
  const chart = sparkline({ height: 150, axis: true });
  const xFrom = h('span');
  const stats = h('div', { class: 'detail-stats' });
  const note = h('div', { class: 'small muted' });
  const alertSlot = h('div', { class: 'detail-alerts' });
  const seg = h('div', { class: 'seg', role: 'tablist', 'aria-label': 'Time range' }, [[1, '1 h'], [6, '6 h'], [24, '24 h']].map(([n, label]) =>
    h('button', { type: 'button', role: 'tab', 'aria-selected': String(n === hours), onclick: (e) => {
      hours = n; seg.querySelectorAll('button').forEach((b) => b.setAttribute('aria-selected', String(b === e.currentTarget))); loadSeries();
    } }, label)));
  const numeric = !isText(first) && (isNum(first.value) || isBool(first) || first.value == null);

  function drawValue() {
    const v = currentValue(first.id) || first;
    if (isBool(v)) { big.textContent = v.value == null ? '—' : v.value ? 'On' : 'Off'; big._v = undefined; unit.textContent = ''; }
    else if (isNum(v.value)) { tween(big, v.value); unit.textContent = v.unit || ''; }
    else { big.textContent = v.value == null ? '—' : String(typeof v.value === 'object' ? JSON.stringify(v.value) : v.value).slice(0, 120); unit.textContent = ''; }
    updated.textContent = `${v.pending ? 'Sending… · ' : ''}${whenText(v)}`;
    ctlSlot.replaceChildren(...[controlFor(w, v, drawValue)].filter(Boolean));
    drawAlerts(v);
    note.textContent = v.writable ? (store.data && store.data.canControl ? 'You can change this. Changes are sent to the device and logged.'
      : 'Only administrators can change this.') : 'Read-only sensor value.';
  }
  function drawAlerts(v) {
    const admin = session.user && session.user.role === 'admin';
    const rules = alertsFor(first.id);
    const sig = JSON.stringify(rules) + admin;
    if (alertSlot._sig === sig) return;
    alertSlot._sig = sig;
    if (!canAlert(v) || (!rules.length && !admin)) { alertSlot.replaceChildren(); return; }
    const after = () => { refresh(); };
    const edit = async (a) => {
      try {
        const r = await get('/admin/sensor-alerts');
        const rule = r.rules.find((x) => x.id === a.id);
        if (rule) { closeSheet(); alertEditor(v, rule, after); }
      } catch (err) { toast(err.message, 'error'); }
    };
    alertSlot.replaceChildren(
      ...rules.map((a) => {
        const [word, color] = STATE[a.state] || STATE.ok;
        return h('div', { class: `alert-line ${a.state}` }, icon('bell'), h('div', { class: 'grow' }, h('b', null, a.text),
          h('div', { class: 'small muted' }, { emergency: 'Emergency · texts everyone', urgent: 'Urgent · texts the janitors', report: 'Report only' }[a.level]),
          a.why ? h('div', { class: 'small' }, a.why) : null),
        h('span', { class: `chip ${color}` }, word + (a.ref ? ` #${a.ref}` : '')),
        admin ? h('button', { class: 'btn sm', type: 'button', onclick: () => edit(a) }, 'Edit') : null);
      }),
      admin && !rules.length ? h('button', { class: 'btn block', type: 'button', onclick: () => { closeSheet(); alertEditor(v, null, after); } },
        icon('bell'), 'Set an alert… (report + text when it goes over a limit)') : null);
  }
  async function loadSeries() {
    if (!numeric) { chart.el.classList.add('hidden'); seg.classList.add('hidden'); return; }
    stats.replaceChildren(h('span', { class: 'small muted' }, 'Loading…'));
    try {
      const r = await get(`/sensors/series?thingId=${encodeURIComponent(first.thingId)}&propertyId=${encodeURIComponent(first.id)}&hours=${hours}`, { timeout: 30000 });
      const pts = r.points;
      chart.draw(pts, first.unit || '', alertsFor(first.id).map((a) => a.limit));
      if (pts.length < 2) { stats.replaceChildren(h('span', { class: 'small muted' }, 'Not enough history for this time range yet.')); xFrom.textContent = ''; return; }
      xFrom.textContent = `${hours} h ago`;
      const vals = pts.map((p) => p[1]);
      const avg = vals.reduce((a, b) => a + b, 0) / vals.length;
      const d = (x) => fmt(x, decimalsFor(Math.abs(x) < 10 ? Math.round(x * 100) / 100 : Math.round(x)));
      const u = first.unit ? ` ${first.unit}` : '';
      stats.replaceChildren(...(isBool(first)
        ? [['On', `${Math.round((vals.filter((x) => x > 0.5).length / vals.length) * 100)}% of the time`], ['Changes', String(vals.filter((x, i) => i && (x > 0.5) !== (vals[i - 1] > 0.5)).length)]]
        : [['Lowest', d(Math.min(...vals)) + u], ['Average', d(avg) + u], ['Highest', d(Math.max(...vals)) + u]])
        .map(([k, val]) => h('div', null, h('span', null, k), h('b', null, val))));
    } catch (err) { stats.replaceChildren(h('span', { class: 'small error-text' }, err.message)); }
  }
  const onData = () => drawValue();
  let closeSheet = () => {};
  closeSheet = modal((close) => [
    h('div', { class: 'detail-head' }, h('div', null, h('h2', null, w.name), h('div', { class: 'small muted' }, [first.thing, first.name].filter(Boolean).join(' · '))),
      h('button', { class: 'btn ghost icon', type: 'button', 'aria-label': 'Close', onclick: () => close() }, icon('x'))),
    h('div', { class: 'row', style: { alignItems: 'baseline', gap: '10px' } }, h('div', { class: 'wvalue detail-value' }, big, unit), updated),
    ctlSlot, alertSlot,
    numeric ? h('div', { class: 'row', style: { justifyContent: 'space-between' } }, h('b', { class: 'small' }, 'History'), seg) : null,
    chart.el,
    numeric ? h('div', { class: 'xaxis' }, xFrom, h('span', null, 'now')) : null,
    stats, note,
  ], { wide: true, onClose: () => listeners.delete(onData) });
  listeners.add(onData);
  drawValue();
  loadSeries();
}

// ------------------------------------------------------------------ data
let inflight = null;
async function refresh() {
  if (inflight) return inflight;
  inflight = (async () => {
    try {
      const d = await get('/sensors', { timeout: 30000 });
      store.data = d; store.error = ''; store.at = Date.now();
      if (d.configured && Date.now() - store.historyAt > HISTORY_MS) {
        store.historyAt = Date.now();
        get('/sensors/history', { timeout: 60000 }).then((hist) => {
          for (const [pid, pts] of Object.entries(hist.series || {})) store.series[pid] = pts.slice();
          noteLive(store.data);
          listeners.forEach((fn) => fn());
        }).catch(() => { store.historyAt = 0; });
      }
      noteLive(d);
    } catch (err) {
      store.error = err.message;
    } finally {
      inflight = null;
    }
    listeners.forEach((fn) => fn());
  })();
  return inflight;
}
const listeners = new Set();

// ------------------------------------------------------------------ the panel
export function sensorsCard({ full = false } = {}) {
  const home = session.user && session.user.role === 'admin' ? '/home' : '/sensors';
  const back = full ? h('a', { class: 'back', href: `#${home}`, 'aria-label': 'Back to Home', title: 'Back to Home' }, icon('back')) : null;
  const dot = h('span', { class: 'dot ok live' });
  const tabs = h('div', { class: 'seg pages', role: 'tablist', 'aria-label': 'Arduino dashboard pages' });
  const boards = h('div', { class: 'seg boards hidden', role: 'tablist', 'aria-label': 'Arduino dashboards' });
  const updated = h('span', { class: 'updated small muted' });
  const expand = full ? null : h('button', { class: 'btn sm ghost', type: 'button', onclick: () => go('/live'), 'aria-label': 'Open full view' },
    icon('expand'), h('span', { class: 'hide-xs' }, 'Full view'));
  const head = h('div', { class: 'shead' }, back,
    h('div', { class: 'stitle' }, dot, h(full ? 'h1' : 'b', { class: full ? 'stitle-h' : '' }, 'Live sensors')), boards, tabs,
    h('div', { class: 'sstatus' }, updated), expand);
  const banner = h('div', { class: 'sbanner hidden', role: 'status' });
  const grid = h('div', { class: 'wgrid' });
  const body = h('div', { class: 'sbody' }, banner, grid);
  const foot = h('div', { class: 'sfoot' });
  const card = h('section', { class: `card sensors${full ? ' full' : ''}`, 'aria-label': 'Live sensors' }, head, body, foot);

  let page = recall('aureo.sensorPageId');   // the Arduino page's id
  let built = { page: null, sig: '', widgets: new Map() };
  let timer = null;

  function skeleton() {
    grid.replaceChildren(...[1, 2, 3, 4].map(() => h('div', { class: 'widget skeleton', style: { minHeight: '96px' } })));
  }

  // A page has an alarm when one of its yes/no values named like smoke, leak … is on.
  const alarmOn = (pg) => pg.widgets.some((w) => w.values.some((v) => ((v.value === true || v.value === 1) && ALERT_WORDS.test(`${w.name} ${v.name}`))
    || alertsFor(v.id).some((a) => a.state === 'alert')));
  const choose = (pg) => { page = pg.id; remember('aureo.sensorPageId', page); render(true); };

  function drawTabs(pages) {
    if (!pages.some((p) => p.id === page)) page = (pages.find((p) => p.widgets.length) || pages[0] || {}).id || null;
    const current = pages.find((p) => p.id === page);
    // Several dashboards: pick the dashboard first, then its pages.
    const dashIds = [...new Set(pages.map((p) => p.dashboardId))];
    const mine = current ? pages.filter((p) => p.dashboardId === current.dashboardId) : [];
    const sig = pages.map((p) => `${p.id}=${p.name}${alarmOn(p) ? '!' : ''}`).join('|') + '#' + page;
    if (tabs._sig === sig) return;
    tabs._sig = sig;
    boards.classList.toggle('hidden', dashIds.length < 2);
    boards.replaceChildren(...dashIds.map((id) => {
      const first = pages.filter((p) => p.dashboardId === id);
      const pick = first.find((p) => p.widgets.length) || first[0];
      return h('button', { type: 'button', role: 'tab', 'aria-selected': current && current.dashboardId === id ? 'true' : 'false',
        onclick: () => choose(pick) }, pick.dashboard || pick.name, first.some(alarmOn) ? h('span', { class: 'tab-alarm', 'aria-label': 'alarm' }) : null);
    }));
    tabs.classList.toggle('hidden', mine.length < 2 && !(full && dashIds.length < 2));
    tabs.replaceChildren(...mine.map((p) => h('button', {
      type: 'button', role: 'tab', 'aria-selected': p.id === page ? 'true' : 'false', onclick: () => choose(p),
    }, p.name, alarmOn(p) ? h('span', { class: 'tab-alarm', 'aria-label': 'alarm' }) : null)));
    const on = tabs.querySelector('[aria-selected="true"]');
    if (on && (on.offsetLeft < tabs.scrollLeft || on.offsetLeft + on.offsetWidth > tabs.scrollLeft + tabs.clientWidth)) {
      tabs.scrollLeft = on.offsetLeft - 8;   // keep the chosen page's tab in view (many pages)
    }
  }

  function alertCount() {
    const all = Object.values((store.data && store.data.alerts) || {}).flat();
    const hot = all.filter((a) => a.state === 'alert').length;
    return all.length ? h('span', { class: `n${hot ? ' hot' : ''}` }, hot ? `${hot} active` : String(all.length)) : null;
  }
  function drawFoot(configured) {
    const sig = `${configured}|${JSON.stringify((store.data && store.data.alerts) || {})}`;
    if (foot._sig === sig) return;
    foot._sig = sig;
    foot.replaceChildren(
      full && shell.isPi ? h('button', { class: 'btn sm ghost', type: 'button', onclick: () => shell.send('dashboard') }, icon('external'), 'Arduino\'s own dashboard') : null,
      session.user && session.user.role === 'admin' && configured
        ? h('a', { class: 'btn sm ghost', href: '#/alerts' }, icon('bell'), 'Alerts', alertCount()) : null);
  }

  function tick() {
    if (!store.at) { updated.textContent = 'Connecting…'; return; }
    const secs = Math.round((Date.now() - store.at) / 1000);
    updated.textContent = store.error ? 'Retrying…' : secs < 3 ? 'Updated just now' : `Updated ${secs} s ago`;
  }


  function render(pageChanged = false) {
    const d = store.data;
    if (!d) { if (store.error) { banner.textContent = store.error; banner.classList.remove('hidden'); } return; }
    dot.className = `dot live ${store.error ? 'warn' : 'ok'}`;
    banner.classList.toggle('hidden', !store.error);
    banner.textContent = store.error ? `${store.error} Showing the last values.` : '';
    if (!d.configured) {
      const admin = session.user && session.user.role === 'admin';
      tabs.classList.add('hidden');
      built = { page: null, sig: '', widgets: new Map() };
      grid.replaceChildren(h('div', { class: 'empty' }, icon('sensors'), h('b', null, 'Live sensors aren\'t connected yet'),
        h('span', null, admin ? 'Connect Arduino Cloud once with an API key — then everyone sees your Arduino dashboards here, no Arduino sign-in needed.'
          : 'An administrator needs to connect Arduino Cloud first.'),
        admin ? h('button', { class: 'btn primary', type: 'button', style: { marginTop: '6px' }, onclick: () => connectDialog(() => refresh()) }, 'Connect Arduino Cloud') : null));
      drawFoot(false);
      return;
    }
    const pages = d.pages || [];
    drawTabs(pages);
    drawFoot(true);
    const current = pages.find((p) => p.id === page);
    if (!current) {
      grid.replaceChildren(h('div', { class: 'empty' }, h('b', null, 'No dashboards yet'), h('span', null, 'Make a dashboard in Arduino Cloud and it shows up here.')));
      built = { page: null, sig: '', widgets: new Map() };
      return;
    }
    const sig = current.widgets.map((w) => `${w.id}:${w.type}`).join(',') + `#${!!d.canControl}`;
    if (built.page !== current.id || built.sig !== sig || pageChanged) {
      built = { page: current.id, sig, widgets: new Map() };
      if (!current.widgets.length) {
        grid.replaceChildren(h('div', { class: 'empty' }, icon('sensors'), h('b', null, `Nothing on “${current.name}” yet`),
          h('span', null, 'Add widgets to this page in Arduino Cloud — they show up here within a minute.')));
      } else {
        const els = current.widgets.map((w) => {
          const wd = makeWidget(w);
          built.widgets.set(w.id, wd);
          return wd.el;
        });
        grid.replaceChildren(...els);
      }
    }
    for (const w of current.widgets) {
      const wd = built.widgets.get(w.id);
      if (wd) wd.update({ ...w, values: w.values.map((v) => currentValue(v.id) || withPending(v)) });
    }
  }

  const onData = () => {
    if (!card.isConnected && card._seen) { stop(); return; }
    if (card.isConnected) card._seen = true;
    render(); tick();
  };
  function stop() { clearInterval(timer); clearInterval(card._tick); listeners.delete(onData); }
  listeners.add(onData);
  skeleton();
  if (store.data) setTimeout(onData, 0);
  refresh();
  timer = setInterval(() => {
    if (!card.isConnected) { if (card._seen) stop(); return; }
    if (document.visibilityState === 'visible' && !document.querySelector('.lock')) refresh();
  }, REFRESH_MS);
  card._tick = setInterval(tick, 1000);
  card.stop = stop;
  return card;
}

// Full view: the same pages, bigger, inside the app (route #/live).
export function liveView() {
  return h('div', { class: 'stack enter live-view' }, sensorsCard({ full: true }));
}

// ------------------------------------------------------------------ connect (admin)
export function connectDialog(after, existing = false) {
  modal((close) => {
    const id = field('Client ID', { autocomplete: 'off' });
    const secret = field('Client Secret', { type: 'password', autocomplete: 'off', hint: 'Shown only once when you make the key — it\'s also in the PDF Arduino lets you download.' });
    const org = field('Organization ID (only for an organization space)', { autocomplete: 'off' });
    const msg = message();
    const save = h('button', { class: 'btn primary', type: 'button' }, 'Connect');
    save.addEventListener('click', () => busy(save, 'Connecting…', async () => {
      try {
        const r = await post('/admin/arduino', { clientId: id.input.value, clientSecret: secret.input.value, org: org.input.value }, { timeout: 60000 });
        store.historyAt = 0;
        close(); toast(r.message, 'success'); after();
      } catch (err) { say(msg, err.message, 'error'); }
    }));
    const disconnect = existing ? h('button', { class: 'btn danger left', type: 'button' }, 'Disconnect') : null;
    const janitors = existing ? switchEl(!!(store.data && store.data.janitorsControl), async (on, b) => {
      try { await post('/admin/arduino/options', { janitorsControl: on }); toast(on ? 'Janitors can now use the controls.' : 'Only admins can use the controls now.'); refresh(); }
      catch (err) { b.set(!on); toast(err.message, 'error'); }
    }, 'Janitors can use the controls') : null;
    if (disconnect) {
      disconnect.addEventListener('click', () => busy(disconnect, '…', async () => {
        try { await post('/admin/arduino/clear'); store.series = {}; close(); toast('Arduino Cloud disconnected.'); after(); } catch (err) { say(msg, err.message, 'error'); }
      }));
    }
    return [h('h2', null, existing ? 'Arduino Cloud' : 'Connect Arduino Cloud'),
      h('ol', { class: 'small muted', style: { margin: '0', paddingLeft: '18px', lineHeight: '1.6' } },
        h('li', null, 'Open ', h('a', { href: '#', onclick: (e) => { e.preventDefault(); openExternal('https://app.arduino.cc/api-keys'); } }, 'app.arduino.cc/api-keys'), ' and sign in.'),
        h('li', null, 'Press Create API key, name it "AureoClean".'),
        h('li', null, 'Copy the Client ID and Client Secret here.')),
      janitors ? h('div', { class: 'row', style: { gap: '10px' } }, janitors, h('div', { class: 'grow' }, h('b', { class: 'small' }, 'Janitors can use the controls'),
        h('div', { class: 'small muted' }, 'Switches, sliders and buttons on the dashboard. Admins always can; every change is logged.'))) : null,
      existing ? h('div', { class: 'small muted' }, 'To change the API key, paste a new one below.') : null,
      id, secret, org, msg,
      h('p', { class: 'small muted' }, 'The key stays on the Pi only. Your Arduino dashboards then show up here, page by page, with the same names.'),
      h('div', { class: 'actions' }, disconnect, h('button', { class: 'btn', onclick: () => close() }, 'Cancel'), save)];
  });
}
