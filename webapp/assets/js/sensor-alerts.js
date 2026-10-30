// Sensor alerts (admins): "Temperature above 35 °C for 5 min → report + text the janitors".
// The Pi checks the limits every 15 seconds with fixed rules (no AI) and makes a report
// and texts through the same rules as every other report.
import { busy, confirmBox, get, h, icon, modal, post, toast } from './core.js';
import { message, say, switchEl } from './ui.js';

const MINUTES = [[0, 'Right away'], [1, '1 minute'], [2, '2 minutes'], [3, '3 minutes'], [5, '5 minutes'], [10, '10 minutes'],
  [15, '15 minutes'], [30, '30 minutes'], [60, '1 hour']];
const LEVELS = [
  ['emergency', 'Emergency', 'Report + text everyone at once', 'red'],
  ['urgent', 'Urgent', 'Report + text the janitors · admins after 10 min without a reply', 'amber'],
  ['report', 'Report only', 'Shows in Reports and My jobs, no text', 'blue'],
];
const OPS = { above: 'above', below: 'below', on: 'is on', off: 'is off' };
export const STATE = { alert: ['Alert sent', 'red'], over: ['Over the limit', 'amber'], ok: ['Normal', 'green'], off: ['Off', ''] };

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
const BOOL_TYPES = /^(BOOL|STATUS|HOME_SWITCH|HOME_CONTACT_SENSOR|HOME_MOTION_SENSOR)$/i;
export const isBoolValue = (v) => typeof v.value === 'boolean' || BOOL_TYPES.test(v.type || '');
export const canAlert = (v) => !!v && (isBoolValue(v) || isNum(v.value));

export function show(value, unit = '') {
  if (typeof value === 'boolean' || value == null) return value ? 'on' : 'off';
  if (!isNum(value)) return String(value);
  const a = Math.abs(value);
  const d = a >= 100 || Number.isInteger(value) ? 0 : a >= 10 ? 1 : 2;
  const text = value.toLocaleString(undefined, { maximumFractionDigits: d });
  return unit ? `${text} ${unit}` : text;
}

/** The SMS the rule would send (same shape as the Pi's alert_text, for the preview). */
function previewText(label, op, limit, unit, level, location) {
  const ref = 'N';
  const sample = op === 'above' ? (isNum(limit) ? limit * 1.05 : 0) : op === 'below' ? (isNum(limit) ? limit * 0.9 : 0) : null;
  const title = op === 'above' ? `${label} too high: ${show(+sample.toPrecision(3), unit)}` : op === 'below' ? `${label} too low: ${show(+sample.toPrecision(3), unit)}`
    : op === 'on' ? `${label} detected` : `${label} is off`;
  const tag = level === 'emergency' ? 'EMERGENCY' : 'SENSOR ALERT';
  const lim = op === 'above' || op === 'below' ? `. Limit ${OPS[op]} ${show(limit, unit)}` : '';
  return `AureoClean ${tag} #${ref}: ${title}${location ? ` at ${location}` : ''}${lim}. Reply OK ${ref} or DONE ${ref}`.replace(/°/g, '').replace(/·/g, '-');
}

// ------------------------------------------------------------------ quick templates
// What admins usually want to watch, matched to the board's variables by name.
export const TEMPLATES = [
  { id: 'hot', icon: 'pulse', title: 'Too hot', about: 'Fan or aircon problem, or something burning', match: /temp/i, op: 'above', limit: 35, minutes: 5, level: 'urgent' },
  { id: 'air', icon: 'sparkle', title: 'Bad smell / air', about: 'Air quality index stays bad', match: /^iaq$/i, op: 'above', limit: 150, minutes: 10, level: 'urgent' },
  { id: 'gas', icon: 'sparkle', title: 'Bad smell (raw gas)', about: 'Gas reading drops far below normal', match: /gasresistance/i, op: 'below', limit: null, minutes: 10, level: 'urgent' },
  { id: 'damp', icon: 'sensors', title: 'Damp / wet floor', about: 'Humidity stays very high', match: /humid/i, op: 'above', limit: 85, minutes: 15, level: 'report' },
  { id: 'inside', icon: 'people', title: 'Someone inside too long', about: 'May need help (fainted, injured)', match: /occupied/i, op: 'on', limit: null, minutes: 20, level: 'urgent' },
  { id: 'blocked', icon: 'x', title: 'Sensor blocked', about: 'ToF distance stuck very low — uses not counted', match: /tof|distance/i, op: 'below', limit: 3, minutes: 30, level: 'report' },
];
const WHO = [['report', 'Make a job', 'No text'], ['urgent', 'Text the janitors', 'Admins after 10 min'], ['emergency', 'Emergency', 'Text everyone now']];
const SVGNS = 'http://www.w3.org/2000/svg';
const sv = (tag, attrs = {}) => { const el = document.createElementNS(SVGNS, tag); for (const [k, x] of Object.entries(attrs)) el.setAttribute(k, String(x)); return el; };

/** How often a rule would have fired on these points ([[unix s, value], …]). */
export function wouldHaveFired(points, op, limit, minutes) {
  if (points.length < 2) return null;
  const over = (x) => (op === 'above' ? x > limit : op === 'below' ? x < limit : op === 'on' ? x > 0.5 : x <= 0.5);
  const step = (points[points.length - 1][0] - points[0][0]) / (points.length - 1);
  let runs = 0; let longest = 0; let start = null;
  const close = (end) => { const d = end - start + step; if (d >= minutes * 60) { runs += 1; longest = Math.max(longest, d); } start = null; };
  for (const [t, x] of points) { if (over(x)) { if (start === null) start = t; } else if (start !== null) close(t - step); }
  if (start !== null) close(points[points.length - 1][0]);
  return { times: runs, longestMin: Math.round(longest / 60) };
}

/** The alert builder: pick a template (or a value), then fill in one sentence while the chart shows
 *  where the limit sits. v = a live value, or null to start from the templates.
 *  values = every value that can have an alert (for the templates' unit picker). */
export async function alertEditor(v, existing = null, after = () => {}, values = null) {
  let info;
  try { info = await get('/admin/sensor-alerts'); } catch (err) { toast(err.message, 'error'); return; }
  modal((close) => {
    const body = h('div', { class: 'ab' });
    const pickStep = () => {
      const all = values || (v ? [v] : []);
      const cards = TEMPLATES.map((tp) => {
        const fits = all.filter((x) => tp.match.test(x.name || ''));
        const b = h('button', { class: 'ab-tpl', type: 'button', disabled: !fits.length },
          icon(tp.icon), h('div', { class: 'grow' }, h('b', null, tp.title), h('span', null, fits.length ? tp.about : 'No matching sensor')));
        b.addEventListener('click', () => (fits.length === 1 ? build(fits[0], null, tp) : unitStep(tp, fits)));
        return b;
      });
      const custom = h('select', { class: 'input', 'aria-label': 'Any sensor value' }, h('option', { value: '' }, 'Or any sensor value…'),
        all.map((x) => h('option', { value: x.id }, `${x.thing} · ${x.label} (now ${show(x.value, x.unit)})`)));
      custom.addEventListener('change', () => { const x = all.find((y) => y.id === custom.value); if (x) build(x, null, null); });
      body.replaceChildren(h('h2', null, 'New alert'), h('p', { class: 'small muted' }, 'What should the Pi watch for?'),
        h('div', { class: 'ab-tpls' }, cards), custom,
        h('div', { class: 'actions' }, h('button', { class: 'btn', type: 'button', onclick: () => close() }, 'Cancel')));
    };
    const unitStep = (tp, fits) => {
      body.replaceChildren(h('h2', null, tp.title), h('p', { class: 'small muted' }, 'Which unit?'),
        h('div', { class: 'ab-units' }, fits.map((x) => h('button', { class: 'ab-unit', type: 'button', onclick: () => build(x, null, tp) },
          h('b', null, x.thing), h('span', null, `${x.label} · now ${show(x.value, x.unit)}`)))),
        h('div', { class: 'actions' }, h('button', { class: 'btn', type: 'button', onclick: pickStep }, icon('back'), 'Back')));
    };
    const build = async (val, rule, tp) => {
      body.replaceChildren(h('div', { class: 'small muted' }, 'Loading the last 24 hours…'));
      let tip = null; let series = [];
      try {
        [tip, series] = await Promise.all([
          rule ? null : get(`/admin/sensor-alerts/suggest?thingId=${encodeURIComponent(val.thingId)}&propertyId=${encodeURIComponent(val.id)}`, { timeout: 30000 }),
          get(`/sensors/series?thingId=${encodeURIComponent(val.thingId)}&propertyId=${encodeURIComponent(val.id)}&hours=24`, { timeout: 30000 }).then((r) => r.points).catch(() => [])]);
      } catch (err) { body.replaceChildren(h('div', { class: 'msg error' }, err.message)); return; }
      builder(val, rule || {
        op: tp ? tp.op : tip.op, limit: tp && tp.limit != null ? tp.limit : tip.limit, minutes: tp ? tp.minutes : tip.minutes,
        level: tp ? tp.level : tip.level, location: tip.location, label: tip.label || val.label, on: true,
      }, series, rule ? null : tip, !!rule);
    };

    function builder(val, rule, series, tip, isEdit) {
      const bool = isBoolValue(val);
      const unit = val.unit || '';
      let op = rule.op; let limit = isNum(rule.limit) ? rule.limit : (isNum(val.value) ? val.value : 0);
      let minutes = rule.minutes ?? 5; let level = rule.level || 'urgent'; let on = rule.on !== false;
      // ---- the sentence
      const opSel = h('select', { class: 'ab-chip', 'aria-label': 'When' }, (bool ? [['on', 'turns on'], ['off', 'turns off']] : [['above', 'goes above'], ['below', 'drops below']])
        .map(([k, l]) => h('option', { value: k }, l)));
      opSel.value = op;
      const limIn = h('input', { class: 'ab-chip num', type: 'number', step: 'any', inputmode: 'decimal', value: limit, 'aria-label': 'Limit', style: { width: '96px' } });
      const minSel = h('select', { class: 'ab-chip', 'aria-label': 'For how long' }, MINUTES.map(([m, l]) => h('option', { value: m }, m ? `for ${l}` : 'right away')));
      minSel.value = String(minutes);
      if (!MINUTES.some(([m]) => m === minutes)) minSel.append(h('option', { value: minutes, selected: true }, `for ${minutes} min`));
      const whoBtns = h('div', { class: 'ab-who', role: 'radiogroup', 'aria-label': 'Then' }, WHO.map(([k, l, s2]) => {
        const b = h('button', { type: 'button', role: 'radio', class: `ab-whoopt ${k}`, 'aria-checked': String(k === level) }, h('b', null, l), h('span', null, s2));
        b.addEventListener('click', () => { level = k; whoBtns.querySelectorAll('button').forEach((x) => x.setAttribute('aria-checked', String(x === b))); update(); });
        return b;
      }));
      const where = h('select', { class: 'ab-chip', 'aria-label': 'Restroom' }, h('option', { value: '' }, 'no restroom set'),
        info.locations.map((l) => h('option', { value: l.name }, l.name)));
      where.value = rule.location || '';
      const sentence = h('div', { class: 'ab-sentence' },
        h('span', null, 'When'), h('b', { class: 'ab-what' }, `${val.thing} · ${val.label}`), opSel,
        bool ? null : h('span', { class: 'ab-lim' }, limIn, unit ? h('span', { class: 'muted' }, unit) : null), minSel,
        h('span', null, 'at'), where);
      // ---- chart with a draggable limit line
      const W = 600; const H = 150;
      const svg = sv('svg', { viewBox: `0 0 ${W} ${H}`, class: 'ab-chart', preserveAspectRatio: 'none' });
      const zone = sv('rect', { class: 'ab-zone', x: 0, width: W });
      const line = sv('polyline', { class: 'ab-line' });
      const lim = sv('line', { class: 'ab-limit', x1: 0, x2: W });
      const handle = sv('rect', { class: 'ab-handle', x: W - 70, width: 70, height: 22, rx: 11 });
      const htxt = sv('text', { class: 'ab-htxt', x: W - 35, 'text-anchor': 'middle' });
      svg.append(zone, line, lim, handle, htxt);
      const vals = series.map((p) => p[1]).concat(isNum(val.value) ? [val.value] : []);
      let lo = vals.length ? Math.min(...vals) : 0; let hi = vals.length ? Math.max(...vals) : 1;
      if (!bool) { lo = Math.min(lo, limit); hi = Math.max(hi, limit); }
      if (bool) { lo = 0; hi = 1; }
      const pad = (hi - lo || 1) * 0.15; lo -= pad; hi += pad;
      const yOf = (x) => H - 4 - ((x - lo) / (hi - lo)) * (H - 8);
      const xOfI = (i) => (series.length > 1 ? (i / (series.length - 1)) * W : 0);
      line.setAttribute('points', series.map((p, i) => `${xOfI(i).toFixed(1)},${yOf(p[1]).toFixed(1)}`).join(' '));
      const step = (() => { const r = hi - lo; const e = 10 ** Math.floor(Math.log10(r / 50 || 1)); return e; })();
      const slider = h('input', { class: 'ab-slider', type: 'range', min: lo, max: hi, step, value: limit, 'aria-label': 'Drag the limit' });
      const setLimit = (x) => { limit = +(+x).toFixed(Math.max(0, -Math.floor(Math.log10(step)))); limIn.value = limit; slider.value = limit; update(); };
      if (!bool) {
        let drag = false;
        const fromEvent = (e) => { const r = svg.getBoundingClientRect(); const y = ((e.clientY - r.top) / r.height) * H; setLimit(lo + ((H - 4 - y) / (H - 8)) * (hi - lo)); };
        svg.addEventListener('pointerdown', (e) => { drag = true; svg.setPointerCapture(e.pointerId); fromEvent(e); });
        svg.addEventListener('pointermove', (e) => { if (drag) fromEvent(e); });
        svg.addEventListener('pointerup', () => { drag = false; });
        slider.addEventListener('input', () => setLimit(slider.value));
        limIn.addEventListener('input', () => { if (limIn.value !== '' && isNum(+limIn.value)) { limit = +limIn.value; slider.value = limit; update(false); } });
      }
      opSel.addEventListener('change', () => { op = opSel.value; update(); });
      minSel.addEventListener('change', () => { minutes = Number(minSel.value); update(); });
      where.addEventListener('change', () => update());
      // ---- verdicts
      const nowLine = h('div', { class: 'ab-now' });
      const pastLine = h('div', { class: 'small muted' });
      const name = h('input', { class: 'input', maxlength: 60, value: rule.label || val.label, 'aria-label': 'Name in reports and texts' });
      const sample = h('div', { class: 'sms-preview' });
      name.addEventListener('input', () => update(false));
      const onSw = switchEl(on, (x) => { on = x; }, 'Alert is on');
      function update() {
        const y = yOf(bool ? 0.5 : limit);
        lim.setAttribute('y1', y); lim.setAttribute('y2', y);
        handle.setAttribute('y', y - 11); htxt.setAttribute('y', y + 4); htxt.textContent = bool ? '' : show(limit, unit);
        handle.classList.toggle('hidden', bool); 
        const above = op === 'above' || op === 'on';
        zone.setAttribute('y', above ? 0 : y); zone.setAttribute('height', above ? y : H - y);
        const cur = val.value;
        const isOver = bool ? (op === 'on' ? cur === true || cur === 1 : !(cur === true || cur === 1)) : (op === 'above' ? cur > limit : cur < limit);
        nowLine.className = `ab-now ${isOver ? 'over' : 'ok'}`;
        nowLine.textContent = `Right now: ${show(cur, unit)} — ${isOver ? (minutes ? `over the limit; it would alert if it stays like this for ${minutes} min` : 'over the limit; it would alert at once') : 'fine, no alert'}.`;
        const past = wouldHaveFired(series, op, limit, minutes);
        pastLine.textContent = past == null ? 'No history yet to test this on.'
          : past.times ? `In the last 24 hours this would have alerted ${past.times} time${past.times === 1 ? '' : 's'} (longest ${past.longestMin} min).`
            : 'In the last 24 hours this would not have alerted.';
        sample.textContent = level === 'report' ? 'No text — a job appears in Reports and My jobs.'
          : previewText(name.value.trim() || val.label, op, limit, unit, level, where.value);
      }
      const msg = message();
      const save = h('button', { class: 'btn primary', type: 'button' }, icon('check'), isEdit ? 'Save' : 'Create alert');
      save.addEventListener('click', () => busy(save, 'Saving…', async () => {
        if (!bool && !isNum(+limIn.value)) { say(msg, 'Type the limit as a number.', 'error'); return; }
        try {
          const r = await post('/admin/sensor-alerts', { id: isEdit ? rule.id : '', thingId: val.thingId, propertyId: val.id, label: name.value.trim(),
            op, limit: bool ? null : Number(limIn.value), level, minutes, location: where.value, on });
          close(); toast(r.message, 'success'); after();
        } catch (err) { say(msg, err.message, 'error'); }
      }));
      const del = isEdit ? h('button', { class: 'btn danger left', type: 'button' }, 'Delete') : null;
      if (del) {
        del.addEventListener('click', async () => {
          if (!(await confirmBox('Delete this alert?', 'The sensor stays on the dashboard; it just won\'t make jobs or texts any more.', { ok: 'Delete', danger: true }))) return;
          try { await post(`/admin/sensor-alerts/${encodeURIComponent(rule.id)}/delete`); close(); toast('Alert deleted.'); after(); } catch (err) { say(msg, err.message, 'error'); }
        });
      }
      const test = isEdit ? h('button', { class: 'btn ghost', type: 'button' }, 'Send test') : null;
      if (test) test.addEventListener('click', () => testAlert({ ...rule, text: rule.text || val.label }, test));
      const back = !isEdit && values ? h('button', { class: 'btn ghost', type: 'button', onclick: pickStep }, icon('back'), 'Back') : null;
      body.replaceChildren(
        h('div', { class: 'detail-head' }, h('div', null, h('h2', null, isEdit ? 'Edit alert' : 'New alert'),
          h('div', { class: 'small muted' }, 'Fill in the sentence, or drag the red line on the chart.')),
        h('button', { class: 'btn ghost icon', type: 'button', 'aria-label': 'Close', onclick: () => close() }, icon('x'))),
        sentence,
        h('div', { class: 'ab-chartbox' }, svg, h('div', { class: 'ab-axis' }, h('span', null, '24 h ago'), h('span', null, 'now'))),
        bool ? null : slider,
        nowLine, pastLine,
        tip && tip.why ? h('div', { class: 'small muted' }, h('b', null, 'Why this limit: '), tip.why) : null,
        h('div', { class: 'field' }, h('label', null, 'Then'), whoBtns),
        h('details', { class: 'ab-more' }, h('summary', null, 'Name, text preview, on/off'),
          h('div', { class: 'stack tight' }, h('div', { class: 'field' }, h('label', null, 'Name in reports and texts'), name),
            h('div', { class: 'field' }, h('label', null, 'Text that goes out'), sample),
            h('div', { class: 'row' }, onSw, h('span', { class: 'small' }, 'Alert is on')))),
        msg,
        h('div', { class: 'actions' }, del || back, test, h('button', { class: 'btn', type: 'button', onclick: () => close() }, 'Cancel'), save));
      update();
    }

    if (v) build(v, existing, null); else pickStep();
    return [body];
  }, { wide: true });
}

/** Send a TEST report and texts for one alert (checks reports + SMS end to end). */
export async function testAlert(rule, btn) {
  const go = async () => {
    try { const r = await post(`/admin/sensor-alerts/${encodeURIComponent(rule.id)}/test`, {}, { timeout: 30000 }); toast(r.message, 'success'); }
    catch (err) { toast(err.message, 'error'); }
  };
  if (!(await confirmBox('Send a test alert?', `This makes a report marked TEST for “${rule.text}” and texts people the same way a real alert would ` +
    `(${rule.level === 'report' ? 'no text for Report only' : rule.level === 'emergency' ? 'everyone' : 'the janitors'}).`, { ok: 'Send test' }))) return;
  if (btn) await busy(btn, 'Sending…', go); else await go();
}

function checkerLine(c) {
  if (!c) return null;
  if (c.error) return h('div', { class: 'msg error', style: { display: 'block' } }, h('b', null, 'Alerts aren\'t working right now. '), c.error);
  if (!c.running) return h('div', { class: 'msg error', style: { display: 'block' } }, h('b', null, 'The alert checker isn\'t running. '),
    'Close AureoClean and open it again (or run Update AureoClean).');
  if (c.ago === null || c.ago === undefined) return h('div', { class: 'small muted' }, 'Starting… the first check runs within a few seconds.');
  if (c.ago > c.every * 6) return h('div', { class: 'msg warn', style: { display: 'block' } }, `Last checked ${Math.round(c.ago / 60)} min ago — the checker seems stuck. Restart AureoClean.`);
  return h('div', { class: 'small muted checker' }, h('span', { class: 'dot ok live' }), ` Checking every ${c.every} s · last check ${c.ago} s ago`);
}
