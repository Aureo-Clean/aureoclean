// Sensor alerts (admins): "Temperature above 35 °C for 5 min → report + text the janitors".
// The Pi checks the limits every 15 seconds with fixed rules (no AI) and makes a report
// and texts through the same rules as every other report.
import { busy, confirmBox, get, h, icon, modal, post, toast } from './core.js';
import { field, message, say, select, switchEl } from './ui.js';

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

/** Make or change the alert on one sensor value. v = the live value ({id, thingId, label, unit, value, type, thing}). */
export async function alertEditor(v, existing = null, after = () => {}) {
  let info; let tip = null;
  try {
    [info, tip] = await Promise.all([get('/admin/sensor-alerts'),
      existing ? null : get(`/admin/sensor-alerts/suggest?thingId=${encodeURIComponent(v.thingId)}&propertyId=${encodeURIComponent(v.id)}`, { timeout: 30000 })]);
  } catch (err) { toast(err.message, 'error'); return; }
  const rule = existing || { op: tip.op, limit: tip.limit, level: tip.level, minutes: tip.minutes, location: tip.location, label: tip.label, on: true };
  const bool = isBoolValue(v);
  modal((close) => {
    let op = rule.op; let level = rule.level; let on = rule.on !== false;
    const unit = v.unit || '';
    const name = field('Name in reports and texts', { value: rule.label || v.label, max: 60,
      hint: bool ? '' : 'e.g. "Air quality" for a BME680 gas reading.' });
    const opSeg = h('div', { class: 'seg', role: 'tablist', 'aria-label': 'Alert when' }, (bool ? ['on', 'off'] : ['above', 'below']).map((o) => {
      const b = h('button', { type: 'button', role: 'tab', 'aria-selected': String(o === op) }, bool ? (o === 'on' ? 'Turns on' : 'Turns off') : (o === 'above' ? 'Goes above' : 'Goes below'));
      b.addEventListener('click', () => { op = o; opSeg.querySelectorAll('button').forEach((x) => x.setAttribute('aria-selected', String(x === b))); preview(); });
      return b;
    }));
    const limit = h('input', { class: 'input num', type: 'number', inputmode: 'decimal', step: 'any', value: isNum(rule.limit) ? rule.limit : '',
      'aria-label': 'Limit', style: { width: '140px' } });
    const minutes = select('For at least', MINUTES.map(([m, t]) => [String(m), t]), String(rule.minutes ?? 2));
    const where = select('Restroom', [['', 'Not set'], ...info.locations.map((l) => [l.name, l.name])], rule.location || '');
    const cards = h('div', { class: 'alert-levels', role: 'radiogroup', 'aria-label': 'What happens' }, LEVELS.map(([code, title, about, color]) => {
      const b = h('button', { type: 'button', role: 'radio', class: `alert-level ${color}`, 'aria-checked': String(code === level) },
        h('b', null, title), h('span', null, about));
      b.addEventListener('click', () => { level = code; cards.querySelectorAll('.alert-level').forEach((x) => x.setAttribute('aria-checked', String(x === b))); preview(); });
      return b;
    }));
    const onSwitch = switchEl(on, (x) => { on = x; }, 'Alert is on');
    const sample = h('div', { class: 'sms-preview' });
    const msg = message();
    function preview() {
      sample.textContent = level === 'report' ? 'No text for "Report only" — the report shows in Reports and My jobs.'
        : previewText(name.input.value.trim() || v.label, op, Number(limit.value), unit, level, where.input.value);
      limitRow.classList.toggle('hidden', bool);
    }
    [name.input, limit].forEach((x) => x.addEventListener('input', preview));
    where.input.addEventListener('change', preview);
    const limitRow = h('div', { class: 'field' }, h('label', null, 'Limit'),
      h('div', { class: 'row', style: { gap: '8px' } }, limit, h('span', { class: 'muted' }, unit),
        h('div', { class: 'grow' }), h('span', { class: 'small muted' }, `Now: ${show(v.value, unit)}`)));
    const save = h('button', { class: 'btn primary', type: 'button' }, existing ? 'Save' : 'Turn on alert');
    save.addEventListener('click', () => busy(save, 'Saving…', async () => {
      if (!bool && (limit.value === '' || !Number.isFinite(Number(limit.value)))) { say(msg, 'Type the limit as a number.', 'error'); limit.focus(); return; }
      try {
        const r = await post('/admin/sensor-alerts', {
          id: existing ? existing.id : '', thingId: v.thingId, propertyId: v.id, label: name.input.value.trim(),
          op, limit: bool ? null : Number(limit.value), level, minutes: Number(minutes.input.value), location: where.input.value, on,
        });
        close(); toast(r.message, 'success'); after();
      } catch (err) { say(msg, err.message, 'error'); }
    }));
    const del = existing ? h('button', { class: 'btn danger left', type: 'button' }, 'Delete') : null;
    const test = existing ? h('button', { class: 'btn ghost', type: 'button', title: 'Make a TEST report and texts now' }, 'Send test alert') : null;
    if (test) test.addEventListener('click', () => testAlert({ ...existing, text: existing.text || v.label }, test));
    if (del) {
      del.addEventListener('click', async () => {
        if (!(await confirmBox('Delete this alert?', 'The sensor stays on the dashboard; it just won\'t make reports or texts any more.', { ok: 'Delete', danger: true }))) return;
        try { await post(`/admin/sensor-alerts/${encodeURIComponent(existing.id)}/delete`); close(); toast('Alert deleted.'); after(); }
        catch (err) { say(msg, err.message, 'error'); }
      });
    }
    setTimeout(preview, 0);
    return [
      h('div', { class: 'detail-head' }, h('div', null, h('h2', null, `Alert: ${v.label}`),
        h('div', { class: 'small muted' }, [v.thing, `now ${show(v.value, unit)}`].filter(Boolean).join(' · '))),
      h('button', { class: 'btn ghost icon', type: 'button', 'aria-label': 'Close', onclick: () => close() }, icon('x'))),
      tip && tip.why ? h('div', { class: 'msg', style: { display: 'block' } }, h('b', null, 'Suggested limit. '), tip.why) : null,
      h('div', { class: 'field' }, h('label', null, 'Alert when the value'), opSeg),
      limitRow, minutes,
      h('div', { class: 'field' }, h('label', null, 'Then'), cards),
      where, name,
      h('div', { class: 'field' }, h('label', null, 'Text that goes out'), sample),
      h('div', { class: 'row', style: { gap: '10px' } }, onSwitch, h('div', { class: 'grow' }, h('b', { class: 'small' }, 'Alert is on'),
        h('div', { class: 'small muted' }, 'Checked every 15 seconds on the Pi, even when nobody has the app open.'))),
      msg,
      existing && existing.why ? h('div', { class: 'small muted' }, h('b', null, 'Now: '), existing.why) : null,
      h('div', { class: 'actions' }, del, test, h('button', { class: 'btn', type: 'button', onclick: () => close() }, 'Cancel'), save),
    ];
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

/** Every alert, with its state, from the Live sensors view (admins). */
export function alertsDialog(data, after = () => {}) {
  const values = [];
  for (const t of (data && data.things) || []) for (const v of t.properties) if (canAlert(v)) values.push(v);
  const byId = new Map(values.map((v) => [v.id, v]));
  let timer = null;
  modal((close) => {
    const list = h('div', { class: 'list alert-list' }, h('div', { class: 'small muted', style: { padding: '12px' } }, 'Loading…'));
    const status = h('div');
    const reopen = () => { after(); alertsDialog(data, after); };
    async function load() {
      try {
        const r = await get('/admin/sensor-alerts');
        status.replaceChildren(...[r.rules.some((x) => x.on) ? checkerLine(r.checker) : null].filter(Boolean));
        if (!r.rules.length) {
          list.replaceChildren(h('div', { class: 'empty' }, icon('bell'), h('b', null, 'No sensor alerts yet'),
            h('span', null, 'Add one below, or tap a value on the dashboard and choose Set an alert.')));
          return;
        }
        if (list.contains(document.activeElement)) return;   // don't redraw under the finger
        list.replaceChildren(...r.rules.map((rule) => {
          const v = byId.get(rule.propertyId);
          const [stateText, color] = STATE[rule.state] || STATE.ok;
          const sw = switchEl(rule.on, async (on, b) => {
            try { await post('/admin/sensor-alerts', { ...rule, on, limit: rule.limit }); toast(on ? 'Alert on.' : 'Alert off.'); after(); load(); }
            catch (err) { b.set(!on); toast(err.message, 'error'); }
          }, `${rule.text}: on or off`);
          const edit = h('button', { class: 'btn sm', type: 'button', disabled: !v }, 'Edit');
          edit.addEventListener('click', () => { close(); alertEditor(v, rule, reopen); });
          const test = h('button', { class: 'btn sm ghost', type: 'button' }, 'Test');
          test.addEventListener('click', () => testAlert(rule, test).then(load));
          return h('div', { class: 'list-row' },
            h('span', { class: `dot ${rule.state === 'alert' ? 'fail' : rule.state === 'over' ? 'warn' : rule.on ? 'ok' : 'off'}` }),
            h('div', { class: 'grow' },
              h('div', { class: 'row-title' }, rule.text),
              h('div', { class: 'row-sub' }, [rule.location || 'No restroom set', rule.levelText].join(' · ')),
              h('div', { class: 'row-why' }, h('span', { class: `chip ${color}` }, stateText + (rule.active && rule.active.ref ? ` #${rule.active.ref}` : '')),
                ' ', rule.why)),
            h('div', { class: 'alert-btns' }, sw, h('div', { class: 'row', style: { gap: '4px' } }, test, edit)));
        }));
      } catch (err) { list.replaceChildren(h('div', { class: 'msg error', style: { margin: '12px' } }, err.message)); }
    }
    const pick = select('Sensor value', [['', 'Choose…'], ...values.map((v) => [v.id, `${v.thing} · ${v.label} (now ${show(v.value, v.unit)})`])], '');
    const add = h('button', { class: 'btn primary', type: 'button' }, icon('plus'), 'Add alert');
    add.addEventListener('click', () => {
      const v = byId.get(pick.input.value);
      if (!v) { pick.input.focus(); return; }
      close(); alertEditor(v, null, reopen);
    });
    load();
    timer = setInterval(() => { if (document.visibilityState === 'visible') load(); }, 5000);
    return [
      h('div', { class: 'detail-head' }, h('div', null, h('h2', null, 'Sensor alerts'),
        h('div', { class: 'small muted' }, 'When a value crosses its limit, the Pi makes a report and texts people — fixed rules, no AI.')),
      h('button', { class: 'btn ghost icon', type: 'button', 'aria-label': 'Close', onclick: () => close() }, icon('x'))),
      status,
      h('div', { class: 'card flush' }, list),
      values.length ? h('div', { class: 'row wrap', style: { gap: '8px', alignItems: 'flex-end' } }, h('div', { class: 'grow' }, pick), add)
        : h('p', { class: 'small muted' }, 'No number or on/off values on Arduino Cloud yet.'),
    ];
  }, { wide: true, onClose: () => clearInterval(timer) });
}
