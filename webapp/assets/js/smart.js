// Smart monitoring on Home: today's advice (AI, or fixed rules without it) and each ESP32 unit
// at a glance — air, temperature, uses since cleaning, open smart alerts. Tap a unit for details
// and the Cleaned button. Alerts themselves are fixed rules on the Pi (backend/smart.py).
import { ago, busy, get, go, h, icon, modal, post, session, toast } from './core.js';
import { message, say, select, switchEl } from './ui.js';

const TIER = { 0: ['Watching', ''], 1: ['Job', 'blue'], 2: ['Texted janitors', 'amber'], 3: ['Emergency', 'red'] };
const PRI = { high: 'red', medium: 'amber', low: '' };
const isAdmin = () => session.user && session.user.role === 'admin';
const reportLink = (p) => (isAdmin() ? `#/reports/${encodeURIComponent(p.reportId)}` : '#/jobs');

function airClass(u) {
  if (!u.online) return 'off';
  if (u.calibrating || u.iaq == null) return '';
  return u.iaq > 200 ? 'fail' : u.iaq > 150 ? 'warn' : 'ok';
}

// ------------------------------------------------------------------ the strip on Home
export function smartStrip() {
  const advice = h('button', { class: 'smart-advice', type: 'button', 'aria-label': 'Today\'s plan' });
  const units = h('div', { class: 'smart-units', role: 'list', 'aria-label': 'Smart units' });
  const wrap = h('div', { class: 'smart-strip hidden' }, advice, units);
  let data = null; let plan = null; let timer = null;

  function drawAdvice() {
    if (!plan) return;
    const next = (plan.janitor || [])[0];
    advice.replaceChildren(icon('sparkle'),
      h('div', { class: 'grow' },
        h('div', { class: 'sa-top' }, h('b', null, 'Today'), h('span', { class: 'small muted' }, plan.source === 'ai' ? 'AI advice' : 'Smart advice')),
        h('div', { class: 'sa-line' }, next ? `${next.time} · ${next.task}${next.where ? ` — ${next.where}` : ''}` : (plan.headline || 'Nothing unusual today.'))),
      h('span', { class: 'sa-more' }, 'Plan ›'));
  }
  advice.addEventListener('click', () => adviceDialog(plan, async () => { plan = await get('/advice').catch(() => plan); drawAdvice(); }));

  function drawUnits() {
    if (!data || !data.units.length) { units.replaceChildren(); return; }
    units.replaceChildren(...data.units.map((u) => {
      const worst = u.problems.filter((p) => p.active).sort((a, b) => b.tier - a.tier)[0];
      const pct = u.usesSinceCleaned != null && u.cleanEvery ? Math.min(1, u.usesSinceCleaned / u.cleanEvery) : null;
      const b = h('button', { class: `unit ${worst ? `t${worst.tier}` : ''} ${u.online ? '' : 'offline'}`, type: 'button', role: 'listitem' },
        h('div', { class: 'u-head' }, h('span', { class: `dot ${u.online ? (worst ? (worst.tier >= 2 ? 'fail' : 'warn') : 'ok') : 'off'}` }),
          h('b', null, u.name), worst ? h('span', { class: `chip ${TIER[worst.tier][1]}` }, worst.ref ? `#${worst.ref}` : '!') : null),
        h('div', { class: 'u-facts' },
          h('span', { class: `air ${airClass(u)}` }, u.online ? (u.air ? `${u.air}${u.iaq != null && !u.calibrating ? ` ${Math.round(u.iaq)}` : ''}` : '—') : 'Offline'),
          u.temperature != null && u.online ? h('span', null, `${u.temperature.toFixed(1)} °C`) : null),
        pct != null ? h('div', { class: 'u-bar', title: `${u.usesSinceCleaned} of ${u.cleanEvery} uses since cleaning` },
          h('i', { style: { transform: `scaleX(${pct})` }, class: pct >= 1 ? 'due' : pct >= 0.75 ? 'soon' : '' })) : null,
        h('div', { class: 'u-sub' }, worst ? worst.title : u.usesSinceCleaned != null ? `${u.usesSinceCleaned}/${u.cleanEvery} uses since cleaning` : (u.location || '')));
      b.addEventListener('click', () => unitDialog(u, refresh));
      return b;
    }));
  }

  async function refresh() {
    if (!wrap.isConnected && wrap._seen) { clearInterval(timer); return; }
    if (wrap.isConnected) wrap._seen = true;
    try {
      data = await get('/smart', { timeout: 20000 });
      wrap.classList.toggle('hidden', !data.configured || !data.enabled);
      drawUnits();
    } catch { /* the sensors card shows connection problems */ }
  }
  const loadPlan = () => get('/advice', { timeout: 30000 }).then((p) => { plan = p; drawAdvice(); }).catch(() => {});
  loadPlan();
  refresh();
  let n = 0;
  timer = setInterval(() => {
    if (document.visibilityState !== 'visible') return;
    refresh();
    if (++n % 12 === 0) loadPlan();          // every 2 minutes
  }, 10000);
  return wrap;
}

// ------------------------------------------------------------------ advice (full)
function planList(items) {
  return h('div', { class: 'ins-list plan' }, items.map((x) => h('div', { class: 'ins-row' },
    h('span', { class: `chip ${PRI[x.priority] || ''}` }, x.time || 'Today'),
    h('div', { class: 'grow' }, h('div', { class: 'ins-main' }, x.task), h('div', { class: 'ins-sub' }, [x.where, x.why].filter(Boolean).join(' · '))))));
}

export function adviceDialog(plan, after = () => {}) {
  if (!plan) { toast('Loading the advice…'); return; }
  modal((close) => {
    const admin = isAdmin() && plan.admin;
    const body = h('div', { class: 'stack' });
    const msg = message();
    function draw(p) {
      body.replaceChildren(
        p.headline ? h('p', { class: 'ai-sum', style: { margin: '0' } }, p.headline) : null,
        h('b', { class: 'small' }, admin ? 'Janitors\' plan' : 'Your plan today'),
        (p.janitor || []).length ? planList(p.janitor) : h('p', { class: 'small muted' }, 'Nothing planned.'),
        admin && p.admin.length ? h('b', { class: 'small' }, 'For admins') : null,
        admin && p.admin.length ? h('div', { class: 'ins-list' }, p.admin.map((x) => h('div', { class: 'ins-row' },
          h('span', { class: `dot ${x.priority === 'high' ? 'fail' : x.priority === 'medium' ? 'warn' : 'ok'}` }),
          h('div', { class: 'grow' }, h('div', { class: 'ins-main' }, x.title), h('div', { class: 'ins-sub' }, x.detail))))) : null,
        admin && (p.suggestions || []).length ? h('b', { class: 'small' }, 'Suggested setting changes') : null,
        ...(admin ? (p.suggestions || []).map((s) => {
          const b = h('button', { class: 'btn sm primary', type: 'button' }, 'Apply');
          b.addEventListener('click', () => busy(b, '…', async () => {
            try { const r = await post('/admin/advice/apply', { setting: s.setting, value: s.value }); toast(r.message, 'success'); p.suggestions = p.suggestions.filter((x) => x !== s); draw(p); }
            catch (err) { say(msg, err.message, 'error'); }
          }));
          return h('div', { class: 'suggest' }, h('div', { class: 'grow' }, h('div', { class: 'ins-main' }, `${s.label}: ${s.current} → ${s.value}`),
            h('div', { class: 'ins-sub' }, s.why)), b);
        }) : []),
        h('div', { class: 'small muted' }, `${p.source === 'ai' ? 'Written by the AI' : 'Made by fixed rules from the sensor patterns'} · ${ago(p.createdAt)}${p.note ? ` · ${p.note}` : ''}. Advice only — alerts are fixed rules.`));
    }
    draw(plan);
    const refresh = admin ? h('button', { class: 'btn left', type: 'button' }, icon('reload'), plan.ai ? 'Make new AI advice' : 'Update') : null;
    if (refresh) {
      refresh.addEventListener('click', () => busy(refresh, 'Thinking…', async () => {
        try {
          await post('/admin/advice/refresh');
          for (let i = 0; i < 120; i++) {
            await new Promise((r) => setTimeout(r, 2500));
            const p = await get('/advice');
            if (!p.running) { plan = p; draw(p); after(); toast('Advice updated.', 'success'); return; }
          }
        } catch (err) { say(msg, err.message, 'error'); }
      }));
    }
    return [
      h('div', { class: 'detail-head' }, h('div', null, h('h2', null, 'Today\'s plan'), h('div', { class: 'small muted' }, new Date().toLocaleDateString(undefined, { weekday: 'long', month: 'short', day: 'numeric' }))),
        h('button', { class: 'btn ghost icon', type: 'button', 'aria-label': 'Close', onclick: () => close() }, icon('x'))),
      body, msg,
      h('div', { class: 'actions' }, refresh, h('button', { class: 'btn primary', type: 'button', onclick: () => close() }, 'OK')),
    ];
  }, { wide: true });
}

// ------------------------------------------------------------------ one unit
function unitDialog(u, after) {
  modal((close) => {
    const facts = [
      ['Restroom', u.location || 'Not set (Smart settings)'],
      ['Status', u.online ? 'Online' : `Offline — last data ${u.lastData ? ago(new Date(u.lastData * 1000).toISOString()) : 'never'}`],
      ['Air', u.iaq == null ? '—' : u.calibrating ? `IAQ ${Math.round(u.iaq)} · calibrating (accuracy ${u.accuracy ?? 0} of 3)` : `${u.air} · IAQ ${Math.round(u.iaq)}`],
      ['Temperature', u.temperature == null ? '—' : `${u.temperature.toFixed(1)} °C`],
      ['Uses today', u.usesToday == null ? '—' : String(u.usesToday)],
      ['Since cleaning', u.usesSinceCleaned == null ? '—' : `${u.usesSinceCleaned} of ${u.cleanEvery} uses${u.cleanedAt ? ` · cleaned ${ago(u.cleanedAt)}` : ''}`],
      u.sensorOk ? null : ['Air sensor', 'Error — check the BME680 wiring'],
    ].filter(Boolean);
    const cleaned = u.usesSinceCleaned != null ? h('button', { class: 'btn primary', type: 'button' }, icon('check'), 'Cleaned it') : null;
    const msg = message();
    if (cleaned) {
      cleaned.addEventListener('click', () => busy(cleaned, 'Saving…', async () => {
        try { const r = await post(`/smart/units/${encodeURIComponent(u.id)}/cleaned`); close(); toast(r.message, 'success'); after(); }
        catch (err) { say(msg, err.message, 'error'); }
      }));
    }
    return [
      h('div', { class: 'detail-head' }, h('div', null, h('h2', null, u.name), h('div', { class: 'small muted' }, `${u.kind === 'urinal' ? 'Urinal' : u.kind === 'toilet' ? 'Toilet' : 'Unit'} · smart monitoring`)),
        h('button', { class: 'btn ghost icon', type: 'button', 'aria-label': 'Close', onclick: () => close() }, icon('x'))),
      h('section', { class: 'card', style: { padding: '4px 14px' } }, facts.map(([k, v]) => h('div', { class: 'kv' }, h('span', null, k), h('b', null, v)))),
      u.problems.length ? h('div', { class: 'stack tight' }, h('b', { class: 'small' }, 'Smart alerts'),
        ...u.problems.map((p) => h('a', { class: `alert-line ${p.tier >= 2 ? 'alert' : p.active ? 'over' : ''}`, href: p.reportId ? reportLink(p) : '#', onclick: () => close() },
          icon('bell'), h('div', { class: 'grow' }, h('b', null, p.title),
            h('div', { class: 'small muted' }, p.active ? `${TIER[p.tier][0]}${p.for ? ` · ${p.for}` : ''}` : 'Back to normal · job still open')),
          p.ref ? h('span', { class: `chip ${TIER[p.tier][1]}` }, `#${p.ref}`) : null)))
        : h('p', { class: 'small muted' }, 'No smart alerts. The Pi checks this unit every 5 seconds.'),
      msg,
      h('div', { class: 'actions' },
        isAdmin() ? h('button', { class: 'btn left', type: 'button', onclick: () => { close(); smartSettingsDialog(); } }, icon('settings'), 'Smart settings') : null,
        h('button', { class: 'btn', type: 'button', onclick: () => { close(); go('/live'); } }, 'Live sensors'), cleaned),
    ];
  }, { wide: true });
}

// ------------------------------------------------------------------ settings (admin)
const GROUPS = [
  ['Emergency (texts everyone active today, any hour)', ['fireTemp', 'fireSeconds', 'riseDegrees', 'riseMinTemp']],
  ['Hot room', ['hotTemp', 'hotMinutes']],
  ['Air (BSEC IAQ)', ['iaqBad', 'iaqVeryBad', 'airJobMinutes', 'airSmsMinutes', 'gasDropPercent']],
  ['Cleaning and flush', ['cleanEvery', 'flushMissUses']],
  ['Units', ['offlineMinutes']],
];
const HOURS = Array.from({ length: 24 }, (_, hr) => [String(hr), hr === 0 ? '12 AM' : hr === 12 ? '12 PM' : hr < 12 ? `${hr} AM` : `${hr - 12} PM`]);

export async function smartSettingsDialog() {
  let done = () => {};
  const finished = new Promise((r) => { done = r; });
  let s;
  try { s = await get('/admin/smart/settings'); } catch (err) { toast(err.message, 'error'); return; }
  modal((close) => {
    const byKey = Object.fromEntries(s.settings.map((x) => [x.key, x]));
    const inputs = {};
    const numField = (k) => {
      const x = byKey[k];
      const input = h('input', { class: 'input num', type: 'number', inputmode: 'decimal', step: 'any', min: x.min, max: x.max, value: x.value, 'aria-label': x.label });
      inputs[k] = input;
      return h('div', { class: 'field set-num' }, h('label', null, x.label), input);
    };
    let enabled = s.config.enabled;
    const on = switchEl(enabled, (v) => { enabled = v; }, 'Smart monitoring on');
    const units = s.units.map((u) => {
      const where = select(`${u.name} — restroom`, [['', 'Not set'], ...s.locations.map((l) => [l.name, l.name])], u.location || '');
      const kind = select('Type', [['urinal', 'Urinal'], ['toilet', 'Toilet'], ['other', 'Other']], u.kind);
      let uon = u.on;
      const sw = switchEl(uon, (v) => { uon = v; }, `${u.name} watched`);
      return { u, where, kind, get on() { return uon; }, el: h('div', { class: 'set-unit' }, h('div', { class: 'grow' }, where), kind, h('div', { class: 'field' }, h('label', null, 'Watch'), sw)) };
    });
    const qs = select('Quiet hours from', HOURS, String(byKey.quietStart.value));
    const qe = select('until', HOURS, String(byKey.quietEnd.value));
    const msg = message();
    const save = h('button', { class: 'btn primary', type: 'button' }, 'Save');
    save.addEventListener('click', () => busy(save, 'Saving…', async () => {
      const values = { quietStart: Number(qs.input.value), quietEnd: Number(qe.input.value) };
      for (const [k, input] of Object.entries(inputs)) values[k] = Number(input.value);
      try {
        const r = await post('/admin/smart/settings', { enabled, values,
          units: Object.fromEntries(units.map((x) => [x.u.id, { location: x.where.input.value, kind: x.kind.input.value, on: x.on }])) });
        close(); toast(r.message, 'success'); done();
      } catch (err) { say(msg, err.message, 'error'); }
    }));
    return [
      h('div', { class: 'detail-head' }, h('div', null, h('h2', null, 'Smart monitoring'),
        h('div', { class: 'small muted' }, 'Trends from the ESP32 units become jobs and texts — fixed rules on the Pi, checked every 5 seconds. The AI only gives advice.')),
      h('button', { class: 'btn ghost icon', type: 'button', 'aria-label': 'Close', onclick: () => close() }, icon('x'))),
      h('div', { class: 'row', style: { gap: '10px' } }, on, h('div', { class: 'grow' }, h('b', { class: 'small' }, 'Smart monitoring is on'),
        h('div', { class: 'small muted' }, 'Off: no smart jobs or texts (your own sensor alerts still work).'))),
      h('b', { class: 'small' }, 'Units'),
      units.length ? h('div', { class: 'stack tight' }, units.map((x) => x.el)) : h('p', { class: 'small muted' }, 'No units with the AureoClean variables on Arduino Cloud yet.'),
      h('b', { class: 'small' }, 'Quiet hours — no texts except emergencies'),
      h('div', { class: 'row', style: { gap: '8px' } }, h('div', { class: 'grow' }, qs), h('div', { class: 'grow' }, qe)),
      ...GROUPS.map(([title, keys]) => h('details', { class: 'set-group' }, h('summary', null, title), h('div', { class: 'set-grid' }, keys.map(numField)))),
      msg,
      h('div', { class: 'actions' }, h('button', { class: 'btn', type: 'button', onclick: () => close() }, 'Cancel'), save),
    ];
  }, { wide: true, onClose: () => done() });
  return finished;
}
