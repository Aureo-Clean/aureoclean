// My account (admins and janitors): profile, password, lock PIN, SMS alerts.
import { busy, get, h, initials, modal, post, session, toast } from './core.js';
import { enterSubmits, field, message, pageHead, say, switchEl } from './ui.js';

export function accountView(_p, _q, ctx) {
  const body = h('div', { class: 'stack' });
  const view = h('div', { class: 'stack enter' }, pageHead('My account',
    h('button', { class: 'btn sm phone-only', type: 'button', onclick: () => ctx.signOut() }, 'Sign out')), body);

  async function load() {
    try {
      const me = await get('/auth/me');
      session.user = { ...session.user, ...me };
      body.replaceChildren(...render(me));
    } catch (err) { body.replaceChildren(h('div', { class: 'msg error' }, err.message)); }
  }

  function render(me) {
    const profile = h('section', { class: 'card row', style: { gap: '14px' } },
      h('div', { class: 'avatar lg' }, initials(me.fullName || me.username)),
      h('div', { class: 'grow' }, h('div', { style: { fontSize: '16px', fontWeight: '600' } }, me.fullName || me.username),
        h('div', { class: 'small muted' }, [me.username, me.email].filter(Boolean).join(' · '))),
      h('span', { class: `chip ${me.role === 'admin' ? 'navy' : 'green'}` }, me.role === 'admin' ? 'Administrator' : 'Janitor'));

    const security = h('section', { class: 'card' }, h('div', { class: 'eyebrow', style: { marginBottom: '2px' } }, 'Sign-in & security'),
      h('div', { class: 'setting' }, h('div', { class: 'grow' }, h('div', { class: 't' }, 'Password'), h('div', { class: 'd' }, '8–16 characters')),
        h('button', { class: 'btn sm', type: 'button', onclick: passwordDialog }, 'Change')),
      h('div', { class: 'setting' }, h('div', { class: 'grow' }, h('div', { class: 't' }, 'Screen-lock PIN'),
        h('div', { class: 'd' }, me.hasLockPin ? 'Set · unlocks the Pi screen in kiosk mode' : 'Not set · the password unlocks the screen')),
      h('button', { class: 'btn sm', type: 'button', onclick: () => pinDialog(me.hasLockPin, load) }, me.hasLockPin ? 'Change' : 'Set PIN')));

    return [profile, h('div', { class: 'grid2' }, security, smsCard(me))];
  }

  function smsCard(me) {
    const msg = message();
    const card = h('section', { class: 'card stack tight' }, h('div', { class: 'eyebrow' }, 'SMS alerts'));
    if (me.smsMode === 'log') say(msg, 'SMS is in test mode on the Pi: texts are written to a log, not sent.', 'warn');
    if (me.smsPhone) {
      const sw = switchEl(me.smsAlerts, (on, el) => {
        el.disabled = true;
        post('/me/sms-alerts', { enabled: on }).then(() => { toast(on ? 'Text alerts on.' : 'Text alerts off.'); load(); })
          .catch((err) => { el.set(!on); el.disabled = false; say(msg, err.message, 'error'); });
      }, 'Text me alerts');
      const remove = h('button', { class: 'btn sm', type: 'button' }, 'Remove number');
      remove.addEventListener('click', () => busy(remove, '…', async () => {
        try { await post('/me/phone/remove'); toast('Number removed.'); load(); } catch (err) { say(msg, err.message, 'error'); }
      }));
      const test = me.role === 'admin' ? h('button', { class: 'btn sm', type: 'button' }, 'Send me a test') : null;
      if (test) {
        test.addEventListener('click', () => busy(test, 'Sending…', async () => {
          try { const r = await post(`/admin/users/${encodeURIComponent(me.uid)}/test-sms`, {}, { timeout: 90000 }); say(msg, r.message, 'success'); }
          catch (err) { say(msg, err.message, 'error'); }
        }));
      }
      card.append(
        h('div', { class: 'row' }, h('div', { class: 'grow' }, h('div', { style: { fontSize: '15px', fontWeight: '600' } }, me.smsPhone),
          h('div', { class: 'small', style: { color: 'var(--green-text)' } }, 'Verified')), sw),
        h('p', { class: 'small muted' }, me.smsAlerts
          ? (me.role === 'admin' ? 'You get emergencies, unanswered reports and system notices by text.' : 'You get restroom alerts by text. Reply OK or DONE with the number.')
          : 'Texts are off. You still see everything here.'),
        h('div', { class: 'row wrap' }, h('button', { class: 'btn sm', type: 'button', onclick: () => numberFlow(card, msg, me) }, 'Change number'), remove, test),
        msg);
    } else if (me.smsPendingPhone) {
      codeStep(card, msg, me.smsPendingPhone);
    } else {
      card.append(h('p', { class: 'small muted' }, me.role === 'admin' ? 'Add your mobile number to get emergencies and system notices by text.'
        : 'Add your mobile number to get restroom alerts by text.'));
      numberFlow(card, msg, me, true);
    }
    return card;
  }

  function numberFlow(card, msg, me, inline = false) {
    const phone = field('Mobile number', { type: 'tel', placeholder: '0917 123 4567', inputmode: 'tel', autocomplete: 'tel' });
    const send = h('button', { class: 'btn primary', type: 'button' }, 'Text me a code');
    send.addEventListener('click', () => busy(send, 'Sending…', async () => {
      try {
        const r = await post('/me/phone/start', { phone: phone.input.value }, { timeout: 90000 });
        toast(r.message, r.smsMode === 'log' ? '' : 'success');
        load();
      } catch (err) { say(msg, err.message, 'error'); }
    }));
    enterSubmits([phone], send);
    const block = h('div', { class: 'stack tight' }, h('div', { class: 'row', style: { alignItems: 'flex-end' } }, h('div', { class: 'grow' }, phone), send));
    if (inline) { card.append(block, msg); return; }
    card.replaceChildren(h('div', { class: 'eyebrow' }, 'SMS alerts'), block,
      h('button', { class: 'btn ghost sm', type: 'button', onclick: () => load(), style: { alignSelf: 'flex-start' } }, 'Cancel'), msg);
    setTimeout(() => phone.input.focus(), 30);
  }

  function codeStep(card, msg, pending) {
    const code = field(`Code we texted to ${pending}`, { inputmode: 'numeric', autocomplete: 'one-time-code', placeholder: '6 digits' });
    const ok = h('button', { class: 'btn primary', type: 'button' }, 'Verify');
    ok.addEventListener('click', () => busy(ok, 'Checking…', async () => {
      try { await post('/me/phone/confirm', { code: code.input.value }); toast('Number verified. Text alerts are on.', 'success'); load(); }
      catch (err) { say(msg, err.message, 'error'); }
    }));
    enterSubmits([code], ok);
    card.append(h('div', { class: 'row', style: { alignItems: 'flex-end' } }, h('div', { class: 'grow' }, code), ok),
      h('button', { class: 'btn ghost sm', type: 'button', style: { alignSelf: 'flex-start' }, onclick: () => numberFlow(card, msg, null) }, 'Use another number / send again'), msg);
  }

  load();
  return view;
}

function passwordDialog() {
  modal((close) => {
    const cur = field('Current password', { type: 'password', autocomplete: 'current-password' });
    const next = field('New password', { type: 'password', hint: '8–16 characters with a letter and a number.', max: 16, autocomplete: 'new-password' });
    const again = field('Type it again', { type: 'password', max: 16, autocomplete: 'new-password' });
    const msg = message();
    const save = h('button', { class: 'btn primary', type: 'button' }, 'Change password');
    save.addEventListener('click', () => {
      if (next.input.value !== again.input.value) { say(msg, 'The new passwords don\'t match.', 'error'); return; }
      busy(save, 'Saving…', async () => {
        try { await post('/auth/change-password', { currentPassword: cur.input.value, newPassword: next.input.value }, { timeout: 60000 }); close(); toast('Password changed.', 'success'); }
        catch (err) { say(msg, err.message, 'error'); }
      });
    });
    enterSubmits([cur, next, again], save);
    return [h('h2', null, 'Change password'), cur, next, again, msg,
      h('div', { class: 'actions' }, h('button', { class: 'btn', onclick: () => close() }, 'Cancel'), save)];
  });
}

function pinDialog(hasPin, after) {
  modal((close) => {
    const pw = field('Your password', { type: 'password', autocomplete: 'current-password' });
    const pin = field('New PIN (4–6 digits)', { type: 'password', inputmode: 'numeric', max: 6, autocomplete: 'off' });
    const again = field('Type the PIN again', { type: 'password', inputmode: 'numeric', max: 6, autocomplete: 'off' });
    [pin, again].forEach((f) => f.input.addEventListener('input', () => { f.input.value = f.input.value.replace(/\D/g, '').slice(0, 6); }));
    const msg = message();
    const save = h('button', { class: 'btn primary', type: 'button' }, 'Save PIN');
    const run = (remove) => {
      if (!pw.input.value) { say(msg, 'Type your password first.', 'error'); return; }
      if (!remove && pin.input.value !== again.input.value) { say(msg, 'The two PINs don\'t match.', 'error'); return; }
      busy(save, 'Saving…', async () => {
        try {
          const r = await post('/auth/lock-pin', { password: pw.input.value, pin: remove ? '' : pin.input.value }, { timeout: 60000 });
          session.user = { ...session.user, hasLockPin: r.hasLockPin };
          close(); toast(r.hasLockPin ? 'PIN saved.' : 'PIN removed: the password unlocks the screen.', 'success'); after();
        } catch (err) { say(msg, err.message, 'error'); }
      });
    };
    save.addEventListener('click', () => run(false));
    enterSubmits([pw, pin, again], save);
    return [h('h2', null, 'Screen-lock PIN'),
      h('p', { class: 'small muted' }, 'In kiosk mode the Pi screen locks itself after a few minutes without a touch. Unlock it with this PIN instead of your full password.'),
      pw, h('div', { class: 'grid2 keep' }, pin, again), msg,
      h('div', { class: 'actions' }, hasPin ? h('button', { class: 'btn danger left', type: 'button', onclick: () => run(true) }, 'Remove PIN') : null,
        h('button', { class: 'btn', onclick: () => close() }, 'Cancel'), save)];
  });
}
