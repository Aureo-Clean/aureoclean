// ID-card (RFID) screens: "tap your card" to link it, the admin PIN step, and the sign-in screen hint.
import { busy, get, h, icon, modal, post, toast } from './core.js';
import { enterSubmits, field, message, say } from './ui.js';

// Link a card to an account: start, then wait for the tap (30 s).
// urls: { start, status, cancel } — e.g. /me/card/link or /admin/users/<uid>/card/link
export function linkCardDialog({ urls, who, after }) {
  let timer = null;
  let finished = false;
  modal((close) => {
    const msg = message();
    const left = h('div', { class: 'small muted', style: { textAlign: 'center' } });
    const art = h('div', { class: 'tap-art waiting', 'aria-hidden': 'true' }, icon('idcard'));
    const retry = h('button', { class: 'btn primary hidden', type: 'button' }, 'Try again');
    async function poll() {
      if (finished) return;
      try {
        const r = await get(urls.status, { timeout: 8000 });
        if (r.state === 'waiting') {
          left.textContent = `Waiting for the card… ${r.secondsLeft} s`;
          timer = setTimeout(poll, 600);
          return;
        }
        finished = true;
        art.classList.remove('waiting');
        left.textContent = '';
        if (r.state === 'linked') {
          art.classList.add('ok');
          say(msg, r.message, 'success');
          toast('ID card linked.', 'success');
          if (after) after();
          setTimeout(() => close(), 1400);
        } else {
          art.classList.add('bad');
          say(msg, r.message || 'No card was read. Try again.', 'error');
          retry.classList.remove('hidden');
        }
      } catch (err) { timer = setTimeout(poll, 1200); say(msg, err.message, 'warn'); }
    }
    function begin() {
      finished = false;
      retry.classList.add('hidden');
      art.className = 'tap-art waiting';
      say(msg, '');
      post(urls.start, {}, { timeout: 10000 })
        .then((r) => { left.textContent = `Waiting for the card… ${r.seconds} s`; poll(); })
        .catch((err) => { finished = true; art.className = 'tap-art bad'; say(msg, err.message, 'error'); retry.classList.remove('hidden'); });
    }
    retry.addEventListener('click', begin);
    begin();
    return [h('h2', null, who ? `Link an ID card for ${who}` : 'Link your ID card'),
      h('p', { class: 'muted small' }, 'Hold the ID card flat on the card reader beside the screen until it beeps or this screen changes.'),
      art, left, msg,
      h('div', { class: 'actions' }, h('button', { class: 'btn', type: 'button', onclick: () => close() }, 'Close'), retry)];
  }, { onClose: () => { finished = true; clearTimeout(timer); post(urls.cancel, {}).catch(() => {}); } });
}

// An admin tapped their card: PIN (or password) to finish signing in.
export function cardPinDialog(r, onDone, onCancel) {
  let ok = false;
  modal((close) => {
    const f = r.usePin
      ? field('Screen-lock PIN', { type: 'password', inputmode: 'numeric', autocomplete: 'off', max: 6, placeholder: '4–6 digits' })
      : field('Password', { type: 'password', autocomplete: 'current-password', max: 16 });
    const msg = message();
    const go = h('button', { class: 'btn primary', type: 'button' }, 'Sign in');
    go.addEventListener('click', () => busy(go, 'Checking…', async () => {
      try {
        const res = await post('/auth/card/pin', { ticket: r.ticket, secret: f.input.value }, { timeout: 30000 });
        ok = true;
        close();
        onDone(res);
      } catch (err) {
        f.input.value = '';
        say(msg, err.message, 'error');
        if (err.status === 401 || err.status === 429) setTimeout(() => close(), 1800);
        else f.input.focus();
      }
    }));
    enterSubmits([f], go);
    return [h('div', { class: 'row' }, h('div', { class: 'tap-art ok small', 'aria-hidden': 'true' }, icon('idcard')),
      h('div', { class: 'grow' }, h('h2', null, `Hi ${String(r.name || '').split(' ')[0]}`),
        h('div', { class: 'small muted' }, r.usePin ? 'Card OK. Administrators also type their PIN.' : 'Card OK. Type your password (set a PIN in My account to make this quicker).'))),
    f, msg, h('div', { class: 'actions' }, h('button', { class: 'btn', type: 'button', onclick: () => close() }, 'Cancel'), go)];
  }, { onClose: () => { if (!ok && onCancel) onCancel(); } });
}

// Sign-in screen: "Tap your ID card" while the reader is connected; polls the Pi for taps.
export function cardSignIn({ onSignedIn, msg }) {
  const hint = h('div', { class: 'card-hint hidden' }, h('div', { class: 'tap-art small waiting', 'aria-hidden': 'true' }, icon('idcard')),
    h('div', null, h('div', { class: 't' }, 'Tap your ID card'), h('div', { class: 'd' }, 'on the reader to sign in')));
  let seq = 0;
  let stopped = false;
  let paused = false;
  let timer = null;
  async function loop() {
    if (stopped || paused) return;
    try {
      const r = await post('/auth/card/poll', { after: seq }, { timeout: 8000 });
      if (typeof r.seq === 'number') seq = r.seq;
      if (r.result === 'signedIn') { stopped = true; onSignedIn(r); return; }
      if (r.result === 'needPin') {
        paused = true;
        cardPinDialog(r, (res) => { stopped = true; onSignedIn(res); }, () => { paused = false; loop(); });
        return;
      }
      if (r.result === 'unknown') say(msg, r.message, 'warn');
      if (r.result === 'error') say(msg, r.message, 'error');
    } catch { /* the Pi is busy: try again */ }
    if (!stopped) timer = setTimeout(loop, 600);
  }
  get('/auth/card/status', { timeout: 6000 }).then((st) => {
    if (!st.reader || stopped) return;
    seq = st.seq || 0;
    hint.classList.remove('hidden');
    loop();
  }).catch(() => {});
  return { hint, stop: () => { stopped = true; clearTimeout(timer); } };
}
