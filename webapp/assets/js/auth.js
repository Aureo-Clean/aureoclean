// Signed-out screens: sign in, request janitor access, activation code, admin
// invite, request status. Same layout on the Pi (side panel) and phones (stacked).
import { busy, focusSoon, get, go, h, icon, modal, post, session, shell } from './core.js';
import { enterSubmits, field, message, say } from './ui.js';

const PASSWORD_MAX = 16;

function authLayout(card, { joins = true } = {}) {
  const side = h('aside', { class: 'auth-side' },
    h('div', { class: 'logo' },
      h('div', { class: 'mark' }, h('img', { src: '/assets/logo.svg', alt: 'AureoClean logo' })),
      h('div', null, h('div', { class: 'wordmark' }, h('span', { class: 'gold-text' }, 'Aureo'), 'Clean'),
        h('div', { class: 'tag' }, 'Restroom hygiene monitoring'))),
    h('div', { class: 'grow' }),
    joins ? [h('div', { class: 'eyebrow', style: { color: '#9FB3DA' } }, 'New here?'), joinLinks('joins')] : null,
    h('div', { class: 'small', style: { color: '#9FB3DA' } }, 'STI College Caloocan'));
  const phoneBrand = h('div', { class: 'auth-brand-phone' },
    h('div', { class: 'mark' }, h('img', { src: '/assets/logo.svg', alt: 'AureoClean logo' })),
    h('div', { class: 'wordmark' }, h('span', null, 'Aureo'), 'Clean'));
  return h('div', { class: 'auth' }, side,
    h('div', { class: 'grow', style: { display: 'flex', flexDirection: 'column' } }, phoneBrand,
      h('main', { class: 'auth-main' }, h('div', { style: { width: '100%', maxWidth: card.classList.contains('wide') ? '520px' : '360px' } },
        card, joins ? h('div', { class: 'phone-only' }, joinLinks('joins-phone')) : null))));
}

function joinLinks(cls) {
  const link = (path, text) => h('a', { href: `#${path}` }, text, h('span', { 'aria-hidden': 'true' }, '›'));
  return h('nav', { class: cls, 'aria-label': 'Join AureoClean' },
    link('/join', 'Request janitor access'), link('/activate', 'I have an activation code'),
    link('/admin-invite', 'Admin invite code'), link('/status', 'Check my request'));
}

function backLink() { return h('a', { href: '#/login', class: 'small' }, '‹ Back to sign in'); }

// ------------------------------------------------------------------ sign in
export function loginView(_params, query) {
  const ident = field('Username or email', { placeholder: 'e.g. markagot', autocomplete: 'username', value: query.get('u') || '' });
  const pass = field('Password', { type: 'password', placeholder: 'Press Enter to sign in', autocomplete: 'current-password', max: PASSWORD_MAX });
  const msg = message();
  const submit = h('button', { class: 'btn primary lg block', type: 'button' }, 'Sign in');
  const resend = h('button', { class: 'btn ghost sm hidden', type: 'button' }, 'Resend verification email');
  const status = h('div', { class: 'service-line' }, h('span', { class: 'dot off' }), 'Checking the AureoClean service…');
  const exitKiosk = h('button', { class: 'btn sm hidden', type: 'button', onclick: () => kioskExitDialog() }, icon('lock'), 'Exit kiosk mode…');
  if (query.get('note')) say(msg, query.get('note'), 'success');
  if (query.get('ended')) say(msg, query.get('ended'), 'warn');

  submit.addEventListener('click', () => {
    say(msg, '');
    if (!ident.input.value.trim() || !pass.input.value) { say(msg, 'Enter your username (or email) and password.', 'error'); return; }
    busy(submit, 'Signing in…', async () => {
      try {
        const res = await post('/auth/login', { identifier: ident.input.value.trim(), password: pass.input.value }, { timeout: 60000 });
        session.set(res.token);
        session.user = res.user;
        pass.input.value = '';
        go(res.user.role === 'admin' ? '/home' : '/sensors', { replace: true });
      } catch (err) {
        const verify = /verify your email/i.test(err.message);
        resend.classList.toggle('hidden', !verify);
        if (!verify) pass.input.value = '';
        say(msg, err.message, 'error');
        pass.input.focus();
      }
    });
  });
  resend.addEventListener('click', () => busy(resend, 'Sending…', async () => {
    try {
      const r = await post('/auth/resend-verification', { identifier: ident.input.value.trim(), password: pass.input.value }, { timeout: 60000 });
      say(msg, r.message, 'success');
    } catch (err) { say(msg, err.message, 'error'); }
  }));
  enterSubmits([ident, pass], submit);

  get('/health?recheck=true', { timeout: 15000 }).then((r) => {
    const online = r.internet === 'online' || r.internet === 'unknown';
    let dot = 'ok'; let text;
    if (r.mode === 'local') { dot = 'warn'; text = 'Not connected to Firebase — data is saved on this Pi only'; }
    else if (r.firebase) text = 'System online';
    else if (online) { dot = 'warn'; text = 'Online, but Firebase isn\'t reachable — saving on this Pi, syncs when it is'; }
    else { dot = 'warn'; text = 'Offline — saving on this Pi, syncs when the Wi-Fi is back'; }
    status.replaceChildren(h('span', { class: `dot ${dot}` }), text);
  }).catch((err) => status.replaceChildren(h('span', { class: 'dot fail' }), err.message));

  const syncKiosk = () => exitKiosk.classList.toggle('hidden', !(shell.isPi && shell.kiosk));
  syncKiosk();
  shell.listeners.add(syncKiosk);

  const card = h('section', { class: 'auth-card' },
    h('div', { class: 'stack tight' }, h('h1', null, 'Welcome back'), h('p', { class: 'lead' }, 'Sign in to look after the restrooms.')),
    ident, pass, msg, submit, resend, status, exitKiosk);
  focusSoon(ident.input.value ? pass.input : ident.input);
  const view = authLayout(card);
  view.cleanup = () => shell.listeners.delete(syncKiosk);
  return view;
}

function kioskExitDialog() {
  modal((close) => {
    const ident = field('Admin username or email', { autocomplete: 'username' });
    const pass = field('Password', { type: 'password', autocomplete: 'current-password' });
    const msg = message();
    const ok = h('button', { class: 'btn primary', type: 'button' }, 'Exit kiosk mode');
    ok.addEventListener('click', () => busy(ok, 'Checking…', async () => {
      try {
        await post('/auth/verify-admin', { identifier: ident.input.value.trim(), password: pass.input.value }, { timeout: 60000 });
        shell.send('kiosk', { on: false });
        close();
      } catch (err) { pass.input.value = ''; say(msg, err.message, 'error'); }
    }));
    enterSubmits([ident, pass], ok);
    return [h('h2', null, 'Exit kiosk mode'), h('p', { class: 'muted small' }, 'An administrator\'s sign-in is needed, so someone passing by can\'t close the screen.'),
      ident, pass, msg, h('div', { class: 'actions' }, h('button', { class: 'btn', onclick: () => close() }, 'Cancel'), ok)];
  });
}

function done(title, text, { button = 'Back to sign in', to = '/login' } = {}) {
  return h('section', { class: 'auth-card', style: { alignItems: 'center', textAlign: 'center' } },
    h('div', { class: 'success-mark' }, icon('check')), h('h1', null, title), h('p', { class: 'lead' }, text),
    h('a', { class: 'btn primary block', href: `#${to}` }, button));
}

// ------------------------------------------------------------------ request access
export function joinView() {
  const name = field('Full name', { autocomplete: 'name', max: 80 });
  const user = field('Username', { autocomplete: 'username', hint: '3–24 letters, numbers, dots or underscores.', max: 24 });
  const email = field('Email', { type: 'email', autocomplete: 'email' });
  const phone = field('Mobile number (for SMS alerts)', { type: 'tel', placeholder: '0917 123 4567', inputmode: 'tel' });
  const msg = message();
  const submit = h('button', { class: 'btn primary block', type: 'button' }, 'Send request');
  const holder = h('div');
  submit.addEventListener('click', () => busy(submit, 'Sending…', async () => {
    say(msg, '');
    try {
      const r = await post('/register/janitor', { fullName: name.input.value, username: user.input.value, email: email.input.value, phone: phone.input.value });
      holder.replaceChildren(done('Request sent', r.message || 'An administrator will review it and give you an activation code.'));
    } catch (err) { say(msg, err.message, 'error'); }
  }));
  enterSubmits([name, user, email, phone], submit);
  holder.append(h('section', { class: 'auth-card wide' }, backLink(),
    h('div', { class: 'stack tight' }, h('h1', null, 'Request janitor access'), h('p', { class: 'lead' }, 'An administrator approves your request and gives you a one-time activation code.')),
    h('div', { class: 'grid2' }, name, user, email, phone), msg, submit));
  focusSoon(name.input);
  const card = h('div', { class: 'wide' }, holder);
  card.classList.add('wide');
  return authLayout(card, { joins: false });
}

// ------------------------------------------------------------------ activation code
export function activateView(_p, query) {
  const user = field('Username', { autocomplete: 'username', value: query.get('u') || '' });
  const code = field('Activation code', { placeholder: 'XXXXX-XXXXX', autocomplete: 'one-time-code' });
  const pass = field('Choose a password', { type: 'password', hint: '8–16 characters with a letter and a number.', max: PASSWORD_MAX, autocomplete: 'new-password' });
  const again = field('Type it again', { type: 'password', max: PASSWORD_MAX, autocomplete: 'new-password' });
  const msg = message();
  const submit = h('button', { class: 'btn primary block', type: 'button' }, 'Activate account');
  const holder = h('div');
  submit.addEventListener('click', () => {
    say(msg, '');
    if (pass.input.value !== again.input.value) { say(msg, 'The two passwords don\'t match.', 'error'); again.input.focus(); return; }
    busy(submit, 'Activating…', async () => {
      try {
        const r = await post('/register/activate', { username: user.input.value, code: code.input.value, password: pass.input.value });
        holder.replaceChildren(done('Account activated', r.message, { button: 'Sign in now', to: `/login?u=${encodeURIComponent(r.username || user.input.value)}` }));
      } catch (err) { say(msg, err.message, 'error'); }
    });
  });
  enterSubmits([user, code, pass, again], submit);
  holder.append(h('section', { class: 'auth-card wide' }, backLink(),
    h('div', { class: 'stack tight' }, h('h1', null, 'Activate your account'), h('p', { class: 'lead' }, 'Use the code an administrator gave you. It works once.')),
    h('div', { class: 'grid2' }, user, code, pass, again), msg, submit));
  focusSoon(user.input.value ? code.input : user.input);
  const card = h('div', { class: 'wide' }, holder);
  return authLayout(card, { joins: false });
}

// ------------------------------------------------------------------ admin invite
export function adminInviteView() {
  const code = field('Invite code', { placeholder: 'XXXXX-XXXXX-XXXXX' });
  const name = field('Full name', { autocomplete: 'name', max: 80 });
  const user = field('Username', { autocomplete: 'username', max: 24 });
  const email = field('Email', { type: 'email', autocomplete: 'email' });
  const pass = field('Password', { type: 'password', hint: '8–16 characters with a letter and a number.', max: PASSWORD_MAX, autocomplete: 'new-password' });
  const again = field('Type it again', { type: 'password', max: PASSWORD_MAX, autocomplete: 'new-password' });
  const msg = message();
  const submit = h('button', { class: 'btn primary block', type: 'button' }, 'Create admin account');
  const holder = h('div');
  submit.addEventListener('click', () => {
    say(msg, '');
    if (pass.input.value !== again.input.value) { say(msg, 'The two passwords don\'t match.', 'error'); return; }
    busy(submit, 'Creating…', async () => {
      try {
        const r = await post('/register/admin', { inviteCode: code.input.value, fullName: name.input.value, username: user.input.value,
          email: email.input.value, password: pass.input.value }, { timeout: 60000 });
        holder.replaceChildren(done('Admin account created', r.message, { button: 'Sign in', to: `/login?u=${encodeURIComponent(r.username || '')}` }));
      } catch (err) { say(msg, err.message, 'error'); }
    });
  });
  enterSubmits([code, name, user, email, pass, again], submit);
  holder.append(h('section', { class: 'auth-card wide' }, backLink(),
    h('div', { class: 'stack tight' }, h('h1', null, 'Register as administrator'), h('p', { class: 'lead' }, 'You need an invite code from an existing administrator.')),
    code, h('div', { class: 'grid2' }, name, user, email, h('div'), pass, again), msg, submit));
  focusSoon(code.input);
  return authLayout(h('div', { class: 'wide' }, holder), { joins: false });
}

// ------------------------------------------------------------------ request status
export function statusView() {
  const user = field('Username', { autocomplete: 'username' });
  const email = field('Email', { type: 'email', autocomplete: 'email' });
  const msg = message();
  const submit = h('button', { class: 'btn primary block', type: 'button' }, 'Check');
  submit.addEventListener('click', () => busy(submit, 'Checking…', async () => {
    try {
      const r = await post('/register/status', { username: user.input.value, email: email.input.value });
      say(msg, r.message, r.status === 'rejected' ? 'error' : r.status === 'activated' || r.status === 'approved' ? 'success' : '');
    } catch (err) { say(msg, err.message, 'error'); }
  }));
  enterSubmits([user, email], submit);
  focusSoon(user.input);
  return authLayout(h('section', { class: 'auth-card' }, backLink(),
    h('div', { class: 'stack tight' }, h('h1', null, 'Check my request'), h('p', { class: 'lead' }, 'See whether an administrator has approved you yet.')),
    user, email, msg, submit), { joins: false });
}
