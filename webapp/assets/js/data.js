// Admins: the Pi's local data (System → Data) and the deleted bin for reports (#/bin).
// The local copy is encrypted on the Pi; opening it here needs the admin's password again,
// lasts a few minutes, and is written to the activity log. Codes and password hashes never show.
import { ago, busy, confirmBox, get, h, icon, invalidate, modal, post, toast, when } from './core.js';
import { empty, errorBox, field, message, say, segmented } from './ui.js';

const LABELS = { reports: 'Reports', deletedReports: 'Deleted', users: 'Users', janitorRequests: 'Sign-ups', adminInvites: 'Invites',
  auditLog: 'Activity', localCredentials: 'Offline accounts', counters: 'Counters' };

function summary(collection, d) {
  if (collection === 'reports' || collection === 'deletedReports') return [`#${d.smsRef ?? '—'} ${d.title || ''}`, [d.location, d.status].filter(Boolean).join(' · ')];
  if (collection === 'users') return [`${d.fullName || d.username || ''} (${d.role || ''})`, [d.username, d.email, d.status].filter(Boolean).join(' · ')];
  if (collection === 'janitorRequests') return [d.fullName || d.username || '', [d.status, d.source].filter(Boolean).join(' · ')];
  if (collection === 'auditLog') return [d.action || '', [(d.actor || {}).username, d.target].filter(Boolean).join(' · ')];
  if (collection === 'localCredentials') return [d.email || d.uid, `Firebase Auth: ${d.firebaseAuth || '?'}`];
  return [Object.keys(d).filter((k) => k !== '_meta').slice(0, 3).map((k) => `${k}: ${JSON.stringify(d[k])}`).join(' · '), ''];
}

/** System → Data */
export function dataPanel(body) {
  let token = null; let lockTimer = null;
  const status = h('div', { class: 'card stack tight' });
  const viewer = h('div', { class: 'card stack' });
  const lock = () => { token = null; clearTimeout(lockTimer); drawLocked(); };

  async function drawStatus() {
    try {
      const s = await get('/admin/data/status');
      const sync = h('button', { class: 'btn sm', type: 'button' }, icon('reload'), 'Sync now');
      sync.addEventListener('click', () => busy(sync, 'Syncing…', async () => {
        try { const r = await post('/admin/data/sync', {}, { timeout: 120000 }); toast(r.message, r.online ? 'success' : ''); drawStatus(); } catch (err) { toast(err.message, 'error'); }
      }));
      const firebase = s.mode.startsWith('Firebase');
      status.replaceChildren(
        h('div', { class: 'row wrap' }, h('span', { class: `dot ${firebase ? (s.online ? (s.pending ? 'warn' : 'ok') : 'warn') : 'off'}` }),
          h('b', null, firebase ? (s.online ? (s.pending ? `Online — ${s.pending} change(s) sending` : 'Online — in sync with Firebase') : `Offline — ${s.pending} change(s) waiting`) : 'Local only'),
          h('div', { class: 'grow' }), firebase ? sync : null),
        h('div', { class: 'kv' }, h('span', null, 'Storage'), h('b', null, s.mode)),
        h('div', { class: 'kv' }, h('span', null, 'On this Pi'), h('b', null, s.encrypted ? 'Encrypted (AES-256)' : 'Not encrypted')),
        firebase ? h('div', { class: 'kv' }, h('span', null, 'Last sent / received'), h('b', null, `${s.lastPush ? ago(s.lastPush) : '—'} / ${s.lastPull ? ago(s.lastPull) : '—'}`)) : null,
        s.error ? h('div', { class: 'msg warn', style: { display: 'block' } }, s.error) : null,
        h('p', { class: 'small muted' }, 'The app always works on the Pi\'s local copy, so it keeps going without Wi-Fi. Changes sync with Firebase automatically when it\'s back. Accounts made offline are marked "created locally".'));
      status._data = s;
    } catch (err) { status.replaceChildren(errorBox(err, drawStatus)); }
  }

  function drawLocked() {
    const pw = field('Your password', { type: 'password', autocomplete: 'current-password' });
    const msg = message();
    const open = h('button', { class: 'btn primary', type: 'button' }, icon('shield'), 'Open local data');
    const go2 = () => busy(open, 'Checking…', async () => {
      try {
        const r = await post('/admin/data/unlock', { password: pw.input.value });
        token = r.token; clearTimeout(lockTimer); lockTimer = setTimeout(() => { lock(); toast('Local data locked again.'); }, r.minutes * 60000);
        drawBrowser('reports');
      } catch (err) { say(msg, err.message, 'error'); pw.input.select(); }
    });
    open.addEventListener('click', go2);
    pw.input.addEventListener('keydown', (e) => { if (e.key === 'Enter') go2(); });
    viewer.replaceChildren(h('div', { class: 'ins-title' }, icon('shield'), h('b', null, 'Local data (admins only)')),
      h('p', { class: 'small muted' }, 'Enter your password to look at what is stored on this Pi. It stays open for 5 minutes, and each look is written to the activity log. Codes and password hashes are always hidden.'),
      pw, msg, h('div', { class: 'actions' }, open));
  }

  async function drawBrowser(collection) {
    const counts = Object.fromEntries(((status._data || {}).collections || []).map((c) => [c.key, c.count]));
    const seg = segmented(Object.keys(LABELS).map((k) => [k, LABELS[k], counts[k]]), collection, (id) => drawBrowser(id), { label: 'Data' });
    const list = h('div', { class: 'list data-list' }, h('div', { class: 'small muted', style: { padding: '12px' } }, 'Loading…'));
    viewer.replaceChildren(h('div', { class: 'row' }, h('div', { class: 'ins-title grow' }, icon('shield'), h('b', null, 'Local data')),
      h('button', { class: 'btn sm', type: 'button', onclick: lock }, 'Lock')), h('div', { class: 'seg-scroll' }, seg), list);
    try {
      const r = await get(`/admin/data/browse?collection=${encodeURIComponent(collection)}`, { headers: { 'X-Data-Token': token } });
      if (!r.rows.length) { list.replaceChildren(empty('Nothing here', '', 'inbox')); return; }
      list.replaceChildren(...r.rows.map(({ id, data }) => {
        const [title, sub] = summary(collection, data);
        const meta = data._meta || {};
        const b = h('button', { class: 'list-row', type: 'button' },
          h('div', { class: 'grow' }, h('div', { class: 'row-title' }, title || id),
            h('div', { class: 'row-sub' }, [sub, meta.updatedAt ? `changed ${ago(meta.updatedAt)}` : ''].filter(Boolean).join(' · '))),
          data.createdLocally || collection === 'localCredentials' ? h('span', { class: 'chip violet' }, 'Created locally') : null,
          meta.offline ? h('span', { class: 'chip amber' }, 'Made offline') : null);
        b.addEventListener('click', () => modal((close) => [h('div', { class: 'detail-head' }, h('h2', null, title || id),
          h('button', { class: 'btn ghost icon', type: 'button', 'aria-label': 'Close', onclick: () => close() }, icon('x'))),
        h('div', { class: 'small muted' }, `${LABELS[collection]} · ${id}`), h('pre', { class: 'json' }, JSON.stringify(data, null, 2))], { wide: true }));
        return b;
      }), r.total > r.rows.length ? h('div', { class: 'small muted', style: { padding: '8px 12px' } }, `Showing the newest ${r.rows.length} of ${r.total}.`) : null);
    } catch (err) {
      if (err.status === 403) { lock(); toast(err.message, 'error'); return; }
      list.replaceChildren(errorBox(err));
    }
  }

  body.replaceChildren(status, viewer);
  drawStatus();
  drawLocked();
}

/** Report detail → Delete (moves it to the bin). */
export function deleteReportDialog(r, after) {
  modal((close) => {
    const why = field('Why delete it?', { placeholder: 'e.g. duplicate of #12, test, prank', max: 200 });
    const msg = message();
    const del = h('button', { class: 'btn danger solid', type: 'button' }, 'Move to deleted bin');
    del.addEventListener('click', () => busy(del, 'Deleting…', async () => {
      try { const res = await post(`/admin/reports/${encodeURIComponent(r.id)}/delete`, { reason: why.input.value }); close(); invalidate('reports'); toast(res.message, 'success'); after(); }
      catch (err) { say(msg, err.message, 'error'); }
    }));
    return [h('h2', null, `Delete report #${r.smsRef ?? ''}?`),
      h('p', { class: 'muted' }, `“${r.title}” goes to the deleted bin for 30 days — you can restore it from there. After 30 days it's removed from this Pi and from Firebase.`),
      why, msg, h('div', { class: 'actions' }, h('button', { class: 'btn', type: 'button', onclick: () => close() }, 'Cancel'), del)];
  });
}

/** #/bin — deleted reports */
export function binView() {
  const list = h('div', { class: 'card flush' });
  const view = h('div', { class: 'stack enter' },
    h('header', { class: 'page-head' }, h('a', { class: 'back', href: '#/reports', 'aria-label': 'Back to reports' }, '‹'),
      h('h1', null, 'Deleted reports')),
    h('p', { class: 'small muted' }, 'Restorable for 30 days, then removed from this Pi and from Firebase.'), list);
  async function load() {
    try {
      const rows = await get('/admin/reports/deleted');
      if (!rows.length) { list.replaceChildren(empty('The bin is empty', '', 'check')); return; }
      list.replaceChildren(h('div', { class: 'list' }, rows.map((r) => {
        const restore = h('button', { class: 'btn sm', type: 'button' }, 'Restore');
        restore.addEventListener('click', () => busy(restore, '…', async () => {
          try { const res = await post(`/admin/reports/${encodeURIComponent(r.id)}/restore`); invalidate('reports'); toast(res.message, 'success'); load(); } catch (err) { toast(err.message, 'error'); }
        }));
        const purge = h('button', { class: 'btn sm danger', type: 'button' }, 'Delete forever');
        purge.addEventListener('click', () => busy(purge, '…', async () => {
          if (!(await confirmBox('Delete forever?', `Report #${r.smsRef} is removed from this Pi and from Firebase. This can't be undone.`, { ok: 'Delete forever', danger: true }))) return;
          try { const res = await post(`/admin/reports/${encodeURIComponent(r.id)}/purge`); toast(res.message); load(); } catch (err) { toast(err.message, 'error'); }
        }));
        return h('div', { class: 'list-row' },
          h('div', { class: 'grow' }, h('div', { class: 'row-title' }, `#${r.smsRef ?? '—'} ${r.title}`),
            h('div', { class: 'row-sub' }, `Deleted ${when(r.deletedAt)} by ${(r.deletedBy || {}).username || '?'} — “${r.deleteReason || ''}” · ${r.daysLeft ?? '?'} days left`)),
          h('div', { class: 'row', style: { gap: '4px' } }, restore, purge));
      })));
    } catch (err) { list.replaceChildren(errorBox(err, load)); }
  }
  load();
  view.refresh = load;
  return view;
}

