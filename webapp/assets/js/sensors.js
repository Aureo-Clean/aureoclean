// Live sensors: your Arduino Cloud dashboards, drawn in AureoClean's style.
// Each Arduino dashboard is a page with the same name ("Urinal", "Page 2", …) and the
// same widgets (gauges, percentages, LEDs/status, charts, values). Read by the Pi with an
// API key, so nobody signs in to Arduino. Values update in place every few seconds with
// gentle animations; small charts show the last few hours.
import { ago, busy, get, go, h, icon, modal, openExternal, post, session, shell, toast } from './core.js';
import { field, message, say } from './ui.js';

const REFRESH_MS = 5000;
const HISTORY_MS = 180000;
const STALE_MS = 15 * 60000;
const ALERT_WORDS = /(smoke|fire|leak|flood|alarm|emergency|gas|overflow|full|vandal|tamper)/i;
const SVG_NS = 'http://www.w3.org/2000/svg';
const MOTION = !(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
const CSS_D = !!(window.CSS && CSS.supports && CSS.supports('d', 'path("M0 0")'));
let uid = 0;

// Shared between the Home card and the full view, so switching is instant.
const store = { data: null, series: {}, historyAt: 0, error: '', at: 0 };

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
  if (from === null || !MOTION || from === to) { el.textContent = fmt(to, d); return; }
  const start = performance.now();
  const dur = 650;
  cancelAnimationFrame(el._raf);
  const step = (now) => {
    const t = Math.min(1, (now - start) / dur);
    el.textContent = fmt(from + (to - from) * ease(t), d);
    if (t < 1) el._raf = requestAnimationFrame(step);
  };
  el._raf = requestAnimationFrame(step);
}

function setPath(path, d) {
  if (CSS_D && MOTION) path.style.d = `path("${d}")`;
  else path.setAttribute('d', d);
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
function bump(el) {
  if (!MOTION) return;
  el.classList.remove('changed'); void el.offsetWidth; el.classList.add('changed');
}

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
  const f = frame(w, 'value', h('div', { class: 'wvalue' }, num, unit), h('div', { class: 'wmeta' }, tr), spark.el);
  return {
    el: f.el,
    update(w2) {
      const v = w2.values[0] || {};
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
  const lo = h('span'); const hi = h('span');
  const el = axis ? h('div', { class: 'chartbox' }, svg, h('div', { class: 'yaxis' }, hi, lo)) : h('div', { class: 'sparkbox' }, svg);
  return {
    el,
    draw(series) {
      el.classList.toggle('empty', series.length < 2);
      if (series.length < 2) return;
      const vals = series.map((p) => p[1]);
      let min = Math.min(...vals); let max = Math.max(...vals);
      if (max - min < 1e-9) { min -= 1; max += 1; }
      const pad = (max - min) * 0.12;
      min -= pad; max += pad;
      const t0 = series[0][0]; const t1 = series[series.length - 1][0] || t0 + 1;
      const pts = series.map(([t, v]) => [((t - t0) / Math.max(1, t1 - t0)) * 300, height - 2 - ((v - min) / (max - min)) * (height - 6)]);
      const d = linePath(pts);
      setPath(line, d);
      setPath(area, `${d} L300 ${height} L0 ${height} Z`);
      const last = pts[pts.length - 1];
      dot.setAttribute('cx', last[0]); dot.setAttribute('cy', last[1]);
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
      chart.draw(series);
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
  const f = frame(w, 'state', h('div', { class: 'state' }, light, word));
  return {
    el: f.el,
    update(w2) {
      const v = w2.values[0] || {};
      const on = v.value === true || v.value === 1 || v.value === 'true' || v.value === 'on';
      const alertish = ALERT_WORDS.test(`${w2.name} ${v.name}`);
      const text = v.value === null || v.value === undefined ? '—' : on ? (alertish ? 'Yes' : 'On') : (alertish ? 'No' : 'Off');
      if (word.textContent && word.textContent !== text) bump(f.el);
      word.textContent = text;
      f.el.classList.toggle('on', on && !alertish);
      f.el.classList.toggle('alert', on && alertish);
      f.el.classList.toggle('calm', !on && alertish);
      f.foot.textContent = whenText(v);
      f.el.classList.toggle('stale', !!isStale(v));
    },
  };
}

function makeLevel(w) {
  const num = h('span', { class: 'wnum num' });
  const unit = h('span', { class: 'wunit' });
  const fill = h('div', { class: 'lfill' });
  const lo = h('span'); const hi = h('span');
  const f = frame(w, 'level', h('div', { class: 'wvalue' }, num, unit), h('div', { class: 'level' }, fill), h('div', { class: 'grange' }, lo, hi));
  return {
    el: f.el,
    update(w2) {
      const v = w2.values[0] || {};
      const min = isNum(w2.options.min) ? w2.options.min : 0;
      const max = isNum(w2.options.max) && w2.options.max > min ? w2.options.max : 100;
      const frac = isNum(v.value) ? Math.max(0, Math.min(1, (v.value - min) / (max - min))) : 0;
      if (num._v !== undefined && num._v !== v.value) bump(f.el);
      tween(num, isNum(v.value) ? v.value : null);
      unit.textContent = v.unit || '';
      fill.style.transform = `scaleX(${frac})`;
      lo.textContent = fmt(min, decimalsFor(min)); hi.textContent = fmt(max, decimalsFor(max));
      f.foot.textContent = whenText(v);
      f.el.classList.toggle('stale', !!isStale(v));
    },
  };
}

function makeText(w, kind) {
  const body = h('div', { class: 'wtext' });
  const f = frame(w, kind, body);
  return {
    el: f.el,
    update(w2) {
      const v = w2.values[0] || {};
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
  return wd;
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
  const ringFill = s('circle', { cx: 9, cy: 9, r: 7, class: 'cd', pathLength: 100 });
  const countdown = s('svg', { viewBox: '0 0 18 18', class: 'countdown', 'aria-hidden': 'true' }, s('circle', { cx: 9, cy: 9, r: 7, class: 'cdt' }), ringFill);
  const updated = h('span', { class: 'updated small muted' });
  const expand = full ? null : h('button', { class: 'btn sm ghost', type: 'button', onclick: () => go('/live'), 'aria-label': 'Open full view' },
    icon('expand'), h('span', { class: 'hide-xs' }, 'Full view'));
  const head = h('div', { class: 'shead' }, back,
    h('div', { class: 'stitle' }, dot, h(full ? 'h1' : 'b', { class: full ? 'stitle-h' : '' }, 'Live sensors')), boards, tabs,
    h('div', { class: 'sstatus' }, countdown, updated), expand);
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
  const alarmOn = (pg) => pg.widgets.some((w) => w.values.some((v) => (v.value === true || v.value === 1) && ALERT_WORDS.test(`${w.name} ${v.name}`)));
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

  function drawFoot(configured) {
    foot.replaceChildren(
      full && shell.isPi ? h('button', { class: 'btn sm ghost', type: 'button', onclick: () => shell.send('dashboard') }, icon('external'), 'Arduino\'s own dashboard') : null,
      session.user && session.user.role === 'admin' && configured && full
        ? h('button', { class: 'btn sm ghost', type: 'button', onclick: () => connectDialog(() => refresh(), true) }, 'Arduino settings') : null);
  }

  function tick() {
    if (!store.at) { updated.textContent = 'Connecting…'; return; }
    const secs = Math.round((Date.now() - store.at) / 1000);
    updated.textContent = store.error ? 'Retrying…' : secs < 3 ? 'Updated just now' : `Updated ${secs} s ago`;
  }

  function restartCountdown() {
    ringFill.classList.remove('run'); void ringFill.getBoundingClientRect(); ringFill.classList.add('run');
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
    const sig = current.widgets.map((w) => `${w.id}:${w.type}`).join(',');
    if (built.page !== current.id || built.sig !== sig || pageChanged) {
      built = { page: current.id, sig, widgets: new Map() };
      if (!current.widgets.length) {
        grid.replaceChildren(h('div', { class: 'empty' }, icon('sensors'), h('b', null, `Nothing on “${current.name}” yet`),
          h('span', null, 'Add widgets to this page in Arduino Cloud — they show up here within a minute.')));
      } else {
        const els = current.widgets.map((w, i) => {
          const wd = makeWidget(w);
          built.widgets.set(w.id, wd);
          wd.el.style.animationDelay = `${Math.min(i, 8) * 30}ms`;
          return wd.el;
        });
        grid.replaceChildren(...els);
        grid.classList.remove('swap'); void grid.offsetWidth; grid.classList.add('swap');
      }
    }
    for (const w of current.widgets) {
      const wd = built.widgets.get(w.id);
      if (wd) wd.update(w);
    }
  }

  const onData = () => {
    if (!card.isConnected && card._seen) { stop(); return; }
    if (card.isConnected) card._seen = true;
    render(); tick(); restartCountdown();
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
function connectDialog(after, existing = false) {
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
      id, secret, org, msg,
      h('p', { class: 'small muted' }, 'The key stays on the Pi only. Your Arduino dashboards then show up here, page by page, with the same names.'),
      h('div', { class: 'actions' }, disconnect, h('button', { class: 'btn', onclick: () => close() }, 'Cancel'), save)];
  });
}
