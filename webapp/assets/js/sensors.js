// Live sensors: the latest value of every Arduino Cloud variable, as simple tiles.
// Read by the Pi with an API key, so nobody signs in to Arduino. Refreshes every 10 s.
import { ago, busy, get, h, icon, modal, openExternal, post, session, shell, toast } from './core.js';
import { field, message, say } from './ui.js';

const ALERT_WORDS = /(smoke|fire|leak|flood|alarm|emergency|gas|overflow|full)/i;

function formatValue(p) {
  const v = p.value;
  if (v === null || v === undefined) return { text: '—', kind: '' };
  if (typeof v === 'boolean') {
    const alert = ALERT_WORDS.test(p.name);
    return { text: v ? (alert ? 'Yes' : 'On') : (alert ? 'No' : 'Off'), kind: v ? (alert ? 'alert' : 'on') : 'off', bool: true };
  }
  if (typeof v === 'number') {
    const n = Math.abs(v) >= 100 ? Math.round(v) : Math.round(v * 10) / 10;
    return { text: String(n), kind: '' };
  }
  if (typeof v === 'object') return { text: JSON.stringify(v).slice(0, 40), kind: '' };
  return { text: String(v).slice(0, 40), kind: '' };
}

function tile(p) {
  const f = formatValue(p);
  const stale = p.updatedAt && Date.now() - new Date(p.updatedAt).getTime() > 15 * 60000;
  return h('div', { class: `sensor${stale ? ' stale' : ''}${f.kind === 'alert' ? ' alert' : ''}` },
    h('div', { class: 'sname', title: p.name }, p.label),
    f.bool ? h('div', { class: `sbool ${f.kind}` }, f.text)
      : h('div', { class: 'sval num' }, f.text, p.unit ? h('span', { class: 'sunit' }, p.unit) : null),
    h('div', { class: 'swhen' }, p.updatedAt ? (stale ? `last seen ${ago(p.updatedAt)}` : ago(p.updatedAt)) : 'no value yet'));
}

export function sensorsPanel({ compact = false } = {}) {
  const body = h('div', { class: 'sensors-body' });
  const foot = h('div', { class: 'sensors-foot' });
  const panel = h('div', { class: 'sensors-panel' }, body, foot);
  let timer = null;
  let first = true;

  function extraButtons(configured) {
    foot.replaceChildren(
      shell.isPi ? h('button', { class: 'btn sm', type: 'button', onclick: () => shell.send('dashboard') }, icon('sensors'), 'Full Arduino dashboard') : null,
      session.user && session.user.role === 'admin' && configured
        ? h('button', { class: 'btn ghost sm', type: 'button', onclick: () => connectDialog(load, true) }, 'Arduino settings') : null);
  }

  async function load() {
    if (!panel.isConnected && !first) { clearInterval(timer); return; }
    if (first) body.replaceChildren(h('div', { class: 'sensor-grid' }, [1, 2, 3, 4].map(() => h('div', { class: 'sensor skeleton', style: { minHeight: '74px' } }))));
    first = false;
    try {
      const d = await get('/sensors', { timeout: 30000 });
      if (!d.configured) {
        const admin = session.user && session.user.role === 'admin';
        body.replaceChildren(h('div', { class: 'empty' }, icon('sensors'), h('b', null, 'Live sensors aren\'t connected yet'),
          h('span', null, admin ? 'Connect Arduino Cloud once with an API key — then everyone sees the readings here, no Arduino sign-in needed.'
            : 'An administrator needs to connect Arduino Cloud first.'),
          admin ? h('button', { class: 'btn primary', type: 'button', style: { marginTop: '6px' }, onclick: () => connectDialog(load) }, 'Connect Arduino Cloud') : null));
        extraButtons(false);
        return;
      }
      const things = d.things.filter((t) => t.properties.length);
      if (!things.length) {
        body.replaceChildren(h('div', { class: 'empty' }, h('b', null, 'No sensor values yet'), h('span', null, 'Add variables to your Arduino Cloud thing.')));
      } else {
        body.replaceChildren(...things.map((t) => h('section', { class: 'thing' },
          things.length > 1 || !compact ? h('div', { class: 'thing-head' }, h('b', null, t.name), t.device ? h('span', { class: 'small muted' }, t.device) : null) : null,
          h('div', { class: 'sensor-grid' }, t.properties.map(tile)))));
      }
      extraButtons(true);
    } catch (err) {
      body.replaceChildren(h('div', { class: 'msg error' }, err.message));
      extraButtons(true);
    }
  }

  load();
  timer = setInterval(() => { if (document.visibilityState === 'visible') load(); }, 10000);
  panel.stop = () => clearInterval(timer);
  return panel;
}

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
        close(); toast(r.message, 'success'); after();
      } catch (err) { say(msg, err.message, 'error'); }
    }));
    const disconnect = existing ? h('button', { class: 'btn danger left', type: 'button' }, 'Disconnect') : null;
    if (disconnect) {
      disconnect.addEventListener('click', () => busy(disconnect, '…', async () => {
        try { await post('/admin/arduino/clear'); close(); toast('Arduino Cloud disconnected.'); after(); } catch (err) { say(msg, err.message, 'error'); }
      }));
    }
    return [h('h2', null, existing ? 'Arduino Cloud' : 'Connect Arduino Cloud'),
      h('ol', { class: 'small muted', style: { margin: '0', paddingLeft: '18px', lineHeight: '1.6' } },
        h('li', null, 'Open ', h('a', { href: '#', onclick: (e) => { e.preventDefault(); openExternal('https://app.arduino.cc/api-keys'); } }, 'app.arduino.cc/api-keys'), ' and sign in.'),
        h('li', null, 'Press Create API key, name it "AureoClean".'),
        h('li', null, 'Copy the Client ID and Client Secret here.')),
      id, secret, org, msg,
      h('p', { class: 'small muted' }, 'The key stays on the Pi only. Everyone then sees the readings in AureoClean without signing in to Arduino.'),
      h('div', { class: 'actions' }, disconnect, h('button', { class: 'btn', onclick: () => close() }, 'Cancel'), save)];
  });
}
