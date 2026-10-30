// Alerts (admins): everything that can raise a job or a text, on one page.
//   1. Happening now   — open smart alerts and your alerts that are over their limit
//   2. Automatic       — the built-in smart alerts (fire/heat, bad air, cleaning, flush, faults)
//   3. Suggested       — ideas from the sensor trends (and the AI), added with one tap
//   4. Your alerts     — limits you set yourself
//   5. Connection      — Arduino Cloud and the SMS modem
// The alerts themselves are fixed rules on the Pi; the AI only suggests.
import { ago, busy, get, go, h, icon, post, toast } from './core.js';
import { errorBox, pageHead, switchEl } from './ui.js';
import { STATE, alertEditor, canAlert, show, testAlert } from './sensor-alerts.js';
import { connectDialog } from './sensors.js';
import { smartSettingsDialog } from './smart.js';

const TIER = { 0: ['Watching', ''], 1: ['Job', 'blue'], 2: ['Texted janitors', 'amber'], 3: ['Emergency', 'red'] };
const LEVEL = { report: 'Job only', urgent: 'Texts the janitors', emergency: 'Emergency' };

function section(title, sub, right, ...body) {
  return h('section', { class: 'card al-section' },
    h('header', { class: 'al-head' }, h('div', { class: 'grow' }, h('h2', null, title), sub ? h('p', { class: 'small muted' }, sub) : null), right),
    ...body);
}

export function alertsView() {
  const status = h('div', { class: 'al-status small muted' });
  const body = h('div', { class: 'stack al-page' }, h('div', { class: 'card' }, h('div', { class: 'skeleton', style: { height: '80px' } })));
  const view = h('div', { class: 'stack enter' }, pageHead('Alerts', status), body);
  let values = [];
  let busyNow = false;

  async function load() {
    if (busyNow) return;
    busyNow = true;
    try {
      const [page, sensors] = await Promise.all([get('/admin/alerts', { timeout: 30000 }), get('/sensors', { timeout: 30000 }).catch(() => null)]);
      values = [];
      for (const t of (sensors && sensors.things) || []) for (const v of t.properties) if (canAlert(v)) values.push(v);
      const typing = body.contains(document.activeElement) && /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName);
      if (!typing) draw(page);    // don't redraw under someone who is typing or choosing
    } catch (err) { body.replaceChildren(errorBox(err, load)); } finally { busyNow = false; }
  }

  function draw(p) {
    const c = p.checker;
    status.replaceChildren(h('span', { class: `dot ${c.error ? 'fail' : c.running ? 'ok' : 'warn'}` }),
      c.error ? ` ${c.error}` : c.running ? ` Checking every ${c.every} s` : ' Not checking — restart AureoClean',
      p.quiet ? h('span', { class: 'chip violet', style: { marginLeft: '8px' } }, `Quiet hours ${p.quietHours}`) : null);
    if (!p.configured) {
      body.replaceChildren(section('Connect the sensors', 'Alerts need Arduino Cloud.', null,
        h('button', { class: 'btn primary', type: 'button', onclick: () => connectDialog(load) }, 'Connect Arduino Cloud')));
      return;
    }
    body.replaceChildren(nowSection(p), automaticSection(p), ideasSection(p), yoursSection(p), connectionSection(p));
  }

  // ---------------------------------------------------------------- 1. happening now
  function nowSection(p) {
    const rows = p.now.map((x) => {
      const [word, color] = x.source === 'yours' ? [x.tierText, x.tier ? 'red' : 'amber'] : TIER[x.tier];
      return h(x.reportId ? 'a' : 'div', { class: 'list-row', href: x.reportId ? `#/reports/${encodeURIComponent(x.reportId)}` : undefined },
        h('span', { class: `dot ${x.tier >= 2 ? 'fail' : x.tier === 1 ? 'warn' : 'off'}` }),
        h('div', { class: 'grow' }, h('div', { class: 'row-title' }, x.title),
          h('div', { class: 'row-sub' }, [x.where, x.source === 'yours' ? 'your alert' : 'automatic', x.active ? x.for : 'back to normal, job still open'].filter(Boolean).join(' · '))),
        h('span', { class: `chip ${color}` }, `${word}${x.ref ? ` #${x.ref}` : ''}`));
    });
    return section('Happening now', null, null,
      rows.length ? h('div', { class: 'list' }, rows) : h('div', { class: 'al-clear' }, icon('check'), h('b', null, 'All clear'),
        h('span', { class: 'small muted' }, `${p.units.length} unit${p.units.length === 1 ? '' : 's'} watched`)));
  }

  // ---------------------------------------------------------------- 2. automatic
  function automaticSection(p) {
    const missing = p.units.filter((u) => !u.location);
    const rows = p.automatic.map((r) => {
      const sw = r.canTurnOff ? switchEl(r.on, async (on, b) => {
        const off = p.automatic.filter((x) => (x.kind === r.kind ? !on : !x.on)).map((x) => x.kind);
        try { await post('/admin/smart/settings', { kindsOff: off }); r.on = on; toast(`${r.title}: ${on ? 'on' : 'off'}`); }
        catch (err) { b.set(!on); toast(err.message, 'error'); }
      }, `${r.title} on or off`) : h('span', { class: 'chip red', title: 'Always on' }, 'Always on');
      return h('div', { class: 'al-rule' }, h('span', { class: `al-bar ${r.color}` }),
        h('div', { class: 'grow' }, h('div', { class: 'row-title' }, r.title),
          h('div', { class: 'al-when' }, h('span', null, 'When '), r.when), h('div', { class: 'al-then' }, h('span', null, 'Then '), r.then)), sw);
    });
    return section('Automatic alerts', `Built in — no setup needed. Quiet hours ${p.quietHours}: only emergencies text at night; other texts go out in the morning.`,
      h('button', { class: 'btn sm', type: 'button', onclick: () => smartSettingsDialog().then(() => setTimeout(load, 600)) }, icon('settings'), 'Limits & units'),
      missing.length ? h('div', { class: 'msg warn', style: { display: 'block' } }, `No restroom set for ${missing.map((u) => u.name).join(', ')} — reports and texts will only name the unit. `,
        h('a', { href: '#', onclick: (e) => { e.preventDefault(); smartSettingsDialog(); } }, 'Set it')) : null,
      h('div', { class: 'al-rules' }, rows));
  }

  // ---------------------------------------------------------------- 3. suggested
  function ideasSection(p) {
    const ideas = p.ideas;
    const find = h('button', { class: 'btn sm', type: 'button' }, icon('sparkle'), ideas.createdAt ? 'Look again' : 'Find suggestions');
    find.addEventListener('click', () => busy(find, ideas.ai ? 'The AI is reading the trends…' : 'Reading the trends…', async () => {
      try { await post('/admin/alerts/ideas/refresh', {}, { timeout: 620000 }); await load(); } catch (err) { toast(err.message, 'error'); }
    }));
    const rows = ideas.ideas.map((i) => {
      const r = i.rule;
      const what = r.op === 'on' || r.op === 'off' ? `${r.label} is ${r.op}` : `${r.label} ${r.op} ${show(r.limit, i.unit)}`;
      const add = h('button', { class: 'btn sm primary', type: 'button' }, icon('plus'), 'Add');
      add.addEventListener('click', () => busy(add, 'Adding…', async () => {
        try { const res = await post(`/admin/alerts/ideas/${encodeURIComponent(i.id)}/add`); toast(res.message, 'success'); load(); }
        catch (err) { toast(err.message, 'error'); }
      }));
      const no = h('button', { class: 'btn sm ghost', type: 'button', 'aria-label': 'Dismiss suggestion' }, 'Dismiss');
      no.addEventListener('click', async () => { try { await post(`/admin/alerts/ideas/${encodeURIComponent(i.id)}/dismiss`); load(); } catch (err) { toast(err.message, 'error'); } });
      return h('div', { class: 'al-idea' },
        h('div', { class: 'grow' },
          h('div', { class: 'row-title' }, i.title, h('span', { class: `chip ${i.source === 'ai' ? 'violet' : 'navy'}`, style: { marginLeft: '6px' } }, i.source === 'ai' ? 'AI' : 'Trend')),
          h('div', { class: 'small muted' }, i.why),
          h('div', { class: 'al-chips' }, h('span', { class: 'chip' }, `${what}${r.minutes ? ` for ${r.minutes} min` : ''}`), h('span', { class: 'chip' }, LEVEL[r.level]))),
        h('div', { class: 'al-btns' }, add, no));
    });
    return section('Suggested alerts', 'From the last 3 days of readings' + (ideas.ai ? ', read by the AI.' : '.') + ' Add one and it becomes one of your alerts.', find,
      rows.length ? h('div', { class: 'stack tight' }, rows)
        : h('p', { class: 'small muted' }, ideas.createdAt ? `Nothing new to suggest (${ago(ideas.createdAt)}).` : 'Tap Find suggestions. New ones also come every morning.'),
      ideas.note ? h('p', { class: 'small muted' }, ideas.note) : null);
  }

  // ---------------------------------------------------------------- 4. yours
  function yoursSection(p) {
    const byId = new Map(values.map((v) => [v.id, v]));
    const rows = p.custom.rules.map((rule) => {
      const v = byId.get(rule.propertyId);
      const [word, color] = STATE[rule.state] || STATE.ok;
      const sw = switchEl(rule.on, async (on, b) => {
        try { await post('/admin/sensor-alerts', { ...rule, on }); toast(on ? 'Alert on.' : 'Alert off.'); load(); }
        catch (err) { b.set(!on); toast(err.message, 'error'); }
      }, `${rule.text}: on or off`);
      const edit = h('button', { class: 'btn sm', type: 'button', disabled: !v }, 'Edit');
      edit.addEventListener('click', () => alertEditor(v, rule, load));
      const test = h('button', { class: 'btn sm ghost', type: 'button' }, 'Test');
      test.addEventListener('click', () => testAlert(rule, test).then(load));
      return h('div', { class: 'list-row' },
        h('span', { class: `dot ${rule.state === 'alert' ? 'fail' : rule.state === 'over' ? 'warn' : rule.on ? 'ok' : 'off'}` }),
        h('div', { class: 'grow' }, h('div', { class: 'row-title' }, rule.text),
          h('div', { class: 'row-sub' }, [rule.thing, rule.location || 'no restroom', LEVEL[rule.level]].filter(Boolean).join(' · ')),
          h('div', { class: 'row-why' }, h('span', { class: `chip ${color}` }, word), ' ', v ? rule.why : 'This value is gone from Arduino Cloud.')),
        h('div', { class: 'alert-btns' }, sw, h('div', { class: 'row', style: { gap: '4px' } }, test, edit)));
    });
    const add = h('button', { class: 'btn primary', type: 'button', disabled: !values.length }, icon('plus'), 'New alert');
    add.addEventListener('click', () => alertEditor(null, null, load, values));
    return section('Your alerts', 'Start from a template (too hot, bad smell, damp floor, someone inside too long…) or any sensor value.', add,
      rows.length ? h('div', { class: 'list' }, rows) : h('p', { class: 'small muted' }, 'None yet — tap New alert.'));
  }

  // ---------------------------------------------------------------- 5. connection
  function connectionSection(p) {
    return section('Connection', null, null,
      h('div', { class: 'al-conn' },
        h('div', { class: 'grow' }, h('b', null, 'Arduino Cloud'), h('div', { class: 'small muted' }, 'Connected with an API key. Change the key or let janitors use the controls.')),
        h('button', { class: 'btn sm', type: 'button', onclick: () => connectDialog(load, true) }, 'Arduino settings')),
      h('div', { class: 'al-conn' },
        h('div', { class: 'grow' }, h('b', null, 'SMS'), h('div', { class: `small ${p.smsMode === 'modem' ? 'muted' : 'error-text'}` },
          p.smsMode === 'modem' ? 'GSM modem on — texts are sent.' : 'Test mode — texts are NOT sent, only written to the log.')),
        h('button', { class: 'btn sm', type: 'button', onclick: () => go('/system?tab=sms') }, 'SMS settings')));
  }

  load();
  view.refresh = load;
  return view;
}
