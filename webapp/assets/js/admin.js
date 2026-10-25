// Administrator screens: Home, Reports (+ detail, charts), People, System.
import {
  ago, api, busy, cached, confirmBox, firstName, get, go, greeting, h, icon, invalidate, modal, openExternal,
  peek, post, session, shell, toast, today, when,
} from './core.js';
import {
  CATEGORY, empty, enterSubmits, errorBox, field, message, pageHead, priorityChip, refBox, say, segmented,
  select, skeletonList, statusChip, switchEl,
} from './ui.js';
import { sensorsCard } from './sensors.js';

const PUBLIC_SITE = 'https://aureo-clean.github.io/aureoclean/';

async function fill(container, loader, render, { skeleton = 4 } = {}) {
  if (skeleton) container.replaceChildren(skeletonList(skeleton));
  try {
    container.replaceChildren(...[].concat(render(await loader())));
  } catch (err) {
    container.replaceChildren(errorBox(err, () => fill(container, loader, render, { skeleton })));
  }
}

// ================================================================== HOME
export function homeView() {
  const pill = h('a', { class: 'status-pill', href: '#/system' }, h('span', { class: 'dot off' }), 'Checking…');
  const openCard = h('a', { class: 'card stat', href: '#/reports' }, h('div', { class: 'eyebrow' }, 'Open reports'),
    h('div', { class: 'value' }, '–'), h('div', { class: 'row wrap' }));
  const waitCard = h('a', { class: 'card stat', href: '#/people' }, h('div', { class: 'eyebrow' }, 'Waiting for you'),
    h('div', { class: 'value gold' }, '–'), h('div', { class: 'small muted' }, 'sign-up requests'));
  const todayCard = h('a', { class: 'card stack tight', href: '#/charts' }, h('div', { class: 'eyebrow' }, 'Last 24 hours'),
    h('div', { class: 'today-line' }, h('span', null, 'Reports'), h('b', { class: 'num' }, '–')),
    h('div', { class: 'today-line' }, h('span', null, 'Fixed'), h('b', { class: 'num', style: { color: 'var(--green-text)' } }, '–')),
    h('div', { class: 'today-line' }, h('span', null, 'Median fix'), h('b', { class: 'num' }, '–')),
    h('div', { class: 'small', style: { color: 'var(--blue)', marginTop: 'auto' } }, 'See charts ›'));
  todayCard.style.flex = '1';

  const sensors = sensorsCard();

  const view = h('div', { class: 'stack enter', style: { flex: '1', minHeight: '0' } },
    h('header', { class: 'page-head' },
      h('div', null, h('h1', null, `${greeting()}, ${firstName(session.user)}`), h('div', { class: 'sub' }, today())),
      h('div', { class: 'grow' }), pill),
    h('div', { class: 'home-grid' }, sensors, h('aside', { class: 'side-stats' }, openCard, waitCard, todayCard)));


  view.refresh = async () => {
    try {
      const [ov, reports] = await Promise.all([get('/admin/overview?cached=true'), cached('reports', () => get('/admin/reports'), 10000)]);
      const open = reports.filter((r) => r.status !== 'resolved');
      openCard.children[1].textContent = String(ov.openReports);
      const urgent = open.filter((r) => ['critical', 'high'].includes(r.priority)).length;
      const taken = open.filter((r) => r.status === 'in_progress').length;
      openCard.children[2].replaceChildren(
        urgent ? h('span', { class: 'chip amber' }, `${urgent} urgent`) : null,
        taken ? h('span', { class: 'chip violet' }, `${taken} being fixed`) : null,
        !open.length ? h('span', { class: 'chip green' }, 'All clear') : null);
      waitCard.children[1].textContent = String(ov.pendingRequests);
      waitCard.children[2].textContent = ov.awaitingActivation
        ? `sign-up requests · ${ov.awaitingActivation} still activating` : 'sign-up requests';
    } catch (err) { if (err.status !== 401) toast(err.message, 'error'); }
    get('/admin/reports/summary?days=1').then((d) => {
      const s = d.summary;
      const fixed = d.records.filter((r) => r.status === 'resolved').length;
      const lines = todayCard.querySelectorAll('.today-line b');
      lines[0].textContent = String(s.totalReports);
      lines[1].textContent = String(fixed);
      lines[2].textContent = s.medianMinutesToResolve === null ? '—' : `${s.medianMinutesToResolve} min`;
    }).catch(() => {});
    healthPill(pill);
  };
  view.refresh();
  return view;
}

export async function healthPill(pill) {
  try {
    const r = await cached('health', () => get('/admin/health', { timeout: 90000 }), 60000);
    const kind = r.status === 'ok' ? '' : r.status;
    pill.className = `status-pill ${kind}`;
    pill.replaceChildren(h('span', { class: `dot ${r.status}` }),
      r.status === 'ok' ? 'All systems OK' : `${r.problems} thing${r.problems === 1 ? '' : 's'} need${r.problems === 1 ? 's' : ''} attention`);
    return r;
  } catch { return null; }
}

// ================================================================== REPORTS
const REPORT_TABS = [['open', 'Open'], ['in_progress', 'In progress'], ['resolved', 'Done'], ['all', 'All']];

export function reportsView(_p, query) {
  let tab = query.get('tab') || 'open';
  let search = '';
  const list = h('div', { class: 'card flush' });
  const seg = segmented(REPORT_TABS, tab, (id) => { tab = id; history.replaceState(null, '', `#/reports?tab=${id}`); draw(); }, { label: 'Filter reports' });
  const find = h('input', { class: 'input', type: 'search', placeholder: 'Search reports', 'aria-label': 'Search reports' });
  find.style.maxWidth = '200px';
  find.style.height = '34px';
  find.addEventListener('input', () => { search = find.value.trim().toLowerCase(); draw(); });
  let rows = peek('reports');
  let seen = new Set((rows || []).map((r) => r.id));

  function draw() {
    if (!rows) { list.replaceChildren(skeletonList(5)); return; }
    const counts = { open: 0, in_progress: 0, resolved: 0, all: rows.length };
    rows.forEach((r) => { counts[r.status] = (counts[r.status] || 0) + 1; });
    seg.setCount('open', counts.open + counts.in_progress);
    seg.setCount('in_progress', counts.in_progress);
    seg.setCount('resolved', counts.resolved);
    const shown = rows.filter((r) => (tab === 'all' ? true : tab === 'open' ? r.status !== 'resolved' : r.status === tab))
      .filter((r) => !search || `${r.title} ${r.location} #${r.smsRef} ${r.details || ''}`.toLowerCase().includes(search));
    if (!shown.length) {
      list.replaceChildren(empty(search ? 'No matching reports' : tab === 'resolved' ? 'Nothing done yet' : 'Nothing here. Nice.',
        search ? 'Try another word.' : tab === 'open' ? 'New reports from the QR codes and janitors appear here.' : '', 'check'));
      return;
    }
    list.replaceChildren(h('div', { class: 'list' }, shown.slice(0, 200).map((r) => reportRow(r, !seen.has(r.id)))));
    seen = new Set(rows.map((r) => r.id));
  }

  const view = h('div', { class: 'stack enter' },
    pageHead('Reports',
      h('a', { class: 'btn sm', href: '#/charts' }, icon('chart'), 'Charts'),
      h('button', { class: 'btn primary sm', type: 'button', onclick: () => newReportDialog(() => view.refresh(true)) }, icon('plus'), 'New report')),
    h('div', { class: 'row wrap' }, seg, h('div', { class: 'grow' }), h('div', { class: 'hide-phone', style: { display: 'contents' } }, find)),
    list);
  view.refresh = async (force) => {
    if (force) invalidate('reports');
    try {
      rows = await cached('reports', () => get('/admin/reports'), force ? 0 : 5000);
      draw();
    } catch (err) { if (!rows) list.replaceChildren(errorBox(err, () => view.refresh(true))); }
  };
  draw();
  view.refresh(true);
  return view;
}

function reportRow(r, isNew) {
  const who = r.status === 'resolved'
    ? `Fixed by ${(r.resolvedBy || r.assignedTo || {}).name || (r.resolvedBy || {}).username || 'admin'}`
    : r.assignedTo ? `Taken by ${r.assignedTo.name || r.assignedTo.username}`
      : r.notifiedTo && r.notifiedTo.length ? `Texted ${r.notifiedTo.length}` : sourceLabel(r);
  return h('a', { class: `list-row${isNew ? ' is-new' : ''}`, href: `#/reports/${encodeURIComponent(r.id)}` },
    refBox(r.smsRef, r.priority),
    h('div', { class: 'grow' },
      h('div', { class: 'row' }, h('span', { class: 'row-title' }, r.title), priorityChip(r.priority)),
      h('div', { class: 'row-sub' }, [r.location || 'No place given', ago(r.reportedAt || r.createdAt)].join(' · '))),
    h('div', { class: 'row-end' }, statusChip(r.status), h('span', { class: 'small muted hide-phone' }, who)),
    h('span', { class: 'chev', 'aria-hidden': 'true' }, '›'));
}

function sourceLabel(r) {
  if (r.source === 'public') return r.channel === 'qr' ? 'QR code' : 'Report page';
  if (r.source === 'janitor') return `From ${(r.createdBy || {}).username || 'a janitor'}`;
  return 'Admin';
}

async function locationOptions() {
  const opts = await cached('report-options', () => get('/report-options'), 300000);
  return opts;
}

function newReportDialog(onSaved) {
  modal((close) => {
    const cat = select('Type', Object.entries(CATEGORY), 'facility');
    const title = field('What\'s wrong?', { placeholder: 'e.g. Leaking pipe under the sink', max: 80 });
    const place = select('Where', [['', 'Loading…']]);
    const other = field('Place', { placeholder: 'e.g. Faculty restroom' });
    other.classList.add('hidden');
    const details = field('Details (optional)', { rows: 3, max: 1000 });
    const prio = select('Priority', [['', 'Normal'], ['low', 'Can wait'], ['high', 'Urgent'], ['critical', 'Emergency']], '');
    const notify = select('Text people?', [['none', 'No texts'], ['janitors', 'Text the janitors'], ['everyone', 'Text everyone']], 'none');
    const msg = message();
    const save = h('button', { class: 'btn primary', type: 'button' }, 'Save report');
    locationOptions().then((o) => {
      place.input.replaceChildren(...o.locations.map((l) => h('option', { value: l.name }, l.name)), h('option', { value: '__other' }, 'Somewhere else…'));
    }).catch(() => place.input.replaceChildren(h('option', { value: '__other' }, 'Somewhere else…')));
    place.input.addEventListener('change', () => other.classList.toggle('hidden', place.input.value !== '__other'));
    cat.input.addEventListener('change', () => {
      if (cat.input.value === 'emergency') { prio.input.value = 'critical'; notify.input.value = 'everyone'; }
    });
    save.addEventListener('click', () => busy(save, 'Saving…', async () => {
      try {
        const r = await post('/admin/reports', {
          category: cat.input.value, title: title.input.value, details: details.input.value, priority: prio.input.value,
          location: place.input.value === '__other' ? other.input.value : place.input.value, notify: notify.input.value,
        });
        close();
        toast(r.message || 'Report saved.', 'success');
        onSaved && onSaved(r);
      } catch (err) { say(msg, err.message, 'error'); }
    }));
    return [h('h2', null, 'New report'), h('div', { class: 'grid2' }, cat, prio), title,
      h('div', { class: 'grid2' }, place, notify), other, details, msg,
      h('div', { class: 'actions' }, h('button', { class: 'btn', onclick: () => close() }, 'Cancel'), save)];
  }, { wide: true });
}

// ------------------------------------------------------------------ report detail
export function reportDetailView({ id }) {
  const body = h('div', { class: 'stack' });
  const view = h('div', { class: 'stack enter' },
    h('header', { class: 'page-head' }, h('a', { class: 'back', href: '#/reports', 'aria-label': 'Back to reports' }, '‹'),
      h('div', { class: 'sub' }, 'Report')), body);

  async function load(force) {
    if (force) invalidate('reports');
    const rows = await cached('reports', () => get('/admin/reports'), force ? 0 : 5000);
    const r = rows.find((x) => x.id === id);
    if (!r) return [empty('Report not found', 'It may have been removed.', 'search')];
    return render(r);
  }

  function render(r) {
    const notes = (r.notes || []).slice().reverse();
    const contact = r.contact && (r.contact.name || r.contact.phone);
    const facts = [
      ['Where', r.location || '—'],
      ['Reported', `${when(r.reportedAt || r.createdAt)} · ${sourceLabel(r)}`],
      r.assignedTo ? ['Taken by', `${r.assignedTo.name || r.assignedTo.username}${r.acceptedAt ? ' · ' + when(r.acceptedAt) : ''}`] : null,
      r.resolvedAt ? ['Done', when(r.resolvedAt)] : null,
      r.rating ? ['Cleanliness', `${r.rating} / 5`] : null,
      r.issues && r.issues.length ? ['Problems', String(r.issues.length)] : null,
      contact ? ['Contact', [r.contact.name, r.contact.phone].filter(Boolean).join(' · ')] : null,
      r.escalatedAt ? ['Escalated', when(r.escalatedAt)] : null,
    ].filter(Boolean);
    const noteBox = field('Add a note', { rows: 2, max: 500, placeholder: 'e.g. Plumber called, coming at 3 PM' });
    const msg = message();
    const act = (status, label, cls) => {
      const b = h('button', { class: `btn ${cls}`, type: 'button' }, label);
      b.addEventListener('click', () => busy(b, 'Saving…', async () => {
        try {
          await post(`/admin/reports/${encodeURIComponent(r.id)}/update`, { status, note: noteBox.input.value });
          toast(status === 'resolved' ? `#${r.smsRef} marked done.` : 'Saved.', 'success');
          refresh();
        } catch (err) { say(msg, err.message, 'error'); }
      }));
      return b;
    };
    const addNote = h('button', { class: 'btn', type: 'button' }, 'Add note');
    addNote.addEventListener('click', () => {
      if (!noteBox.input.value.trim()) { say(msg, 'Write a note first.', 'error'); noteBox.input.focus(); return; }
      busy(addNote, 'Saving…', async () => {
        try { await post(`/admin/reports/${encodeURIComponent(r.id)}/update`, { note: noteBox.input.value }); toast('Note added.', 'success'); refresh(); }
        catch (err) { say(msg, err.message, 'error'); }
      });
    });
    const actions = r.status === 'resolved'
      ? [act('open', 'Reopen', '')]
      : [r.status === 'open' ? act('in_progress', 'Mark in progress', '') : null, act('resolved', 'Mark done', 'success')];
    return [
      h('div', { class: 'stack tight' },
        h('div', { class: 'row wrap' }, h('span', { class: 'chip navy' }, `#${r.smsRef || '—'}`), h('span', { class: 'chip' }, CATEGORY[r.category] || r.category),
          r.category === 'emergency' ? null : priorityChip(r.priority), statusChip(r.status)),
        h('h1', { style: { fontSize: '22px', fontWeight: '700', letterSpacing: '-.01em' } }, r.title)),
      h('div', { class: 'detail-grid' },
        h('div', { class: 'stack' },
          h('section', { class: 'card', style: { padding: '4px 14px' } }, facts.map(([k, v]) => h('div', { class: 'kv' }, h('span', null, k), h('b', null, v))),
            r.details ? h('div', { class: 'kv', style: { display: 'block' } }, h('span', null, 'Details'), h('p', { style: { marginTop: '4px' } }, r.details)) : null),
          h('section', { class: 'card stack tight' }, noteBox, msg, h('div', { class: 'row wrap' }, addNote, h('div', { class: 'grow' }), actions))),
        h('section', { class: 'card stack' }, h('h2', null, 'What happened'),
          h('div', { class: 'timeline' },
            notes.map((n) => h('div', { class: 'item' }, h('span', { class: `dot ${n.status === 'resolved' ? 'ok' : n.by === 'sms' ? 'warn' : ''}` }),
              h('div', null, h('div', null, n.text), h('div', { class: 'when' }, `${when(n.at)}${n.by && n.by !== 'sms' ? ' · ' + n.by : ''}`)))),
            h('div', { class: 'item' }, h('span', { class: 'dot', style: { background: 'var(--blue)' } }),
              h('div', null, h('div', null, `Reported (${sourceLabel(r)})`), h('div', { class: 'when' }, when(r.reportedAt || r.createdAt))))))),
    ];
  }
  const refresh = () => fill(body, () => load(true), (x) => x, { skeleton: 0 });
  fill(body, () => load(false), (x) => x, { skeleton: 3 });
  view.refresh = refresh;
  return view;
}

// ================================================================== CHARTS
export function chartsView(_p, query) {
  let days = Number(query.get('days')) || 30;
  const body = h('div', { class: 'stack' });
  const excel = h('button', { class: 'btn sm', type: 'button' }, icon('download'), 'Excel');
  const pdf = h('button', { class: 'btn sm', type: 'button' }, icon('download'), 'PDF');
  const seg = segmented([['7', '7 days'], ['30', '30 days'], ['90', '90 days']], String(days), (d) => {
    days = Number(d); history.replaceState(null, '', `#/charts?days=${d}`); load();
  }, { label: 'Period' });
  excel.addEventListener('click', () => busy(excel, 'Excel…', () => downloadXlsx(days)));
  pdf.addEventListener('click', () => {
    if (shell.isPi) shell.send('pdf', { name: `AureoClean-reports-${new Date().toISOString().slice(0, 10)}.pdf` });
    else window.print();
  });
  const view = h('div', { class: 'stack enter' },
    h('header', { class: 'page-head' }, h('a', { class: 'back no-print', href: '#/reports', 'aria-label': 'Back to reports' }, '‹'),
      h('h1', null, 'Report charts'), h('div', { class: 'grow' }), h('div', { class: 'row no-print' }, seg, excel, pdf)),
    h('p', { class: 'print-only muted' }, ''), body);
  function load() {
    view.querySelector('.print-only').textContent = `AureoClean · last ${days} days · printed ${new Date().toLocaleString()}`;
    fill(body, () => get(`/admin/reports/summary?days=${days}`), ({ summary }) => charts(summary), { skeleton: 3 });
  }
  load();
  return view;
}

async function downloadXlsx(days) {
  try {
    const res = await api('GET', `/admin/reports/export.xlsx?days=${days}`, undefined, { raw: true, timeout: 60000 });
    const blob = await res.blob();
    const name = (res.headers.get('Content-Disposition') || '').match(/filename="([^"]+)"/)?.[1] || 'AureoClean-reports.xlsx';
    const url = URL.createObjectURL(blob);
    const a = h('a', { href: url, download: name });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
    if (!shell.isPi) toast(`Downloaded ${name}`, 'success');
  } catch (err) { toast(err.message, 'error'); }
}

function bars(title, items, color = '') {
  const top = Math.max(1, ...items.map(([, v]) => v));
  return h('section', { class: 'card stack tight' }, h('h2', null, title),
    items.length ? h('div', { class: 'bars' }, items.slice(0, 7).map(([name, v], i) => {
      const fill = h('div', { class: `fill ${color}` });
      fill.style.width = `${Math.max(3, Math.round((v / top) * 100))}%`;
      fill.style.animationDelay = `${i * 40}ms`;
      return h('div', { class: 'bar' }, h('div', { class: 'name', title: name }, name), h('div', { class: 'track' }, fill), h('div', { class: 'v' }, v));
    })) : h('p', { class: 'small muted' }, 'No reports in this period.'));
}

function charts(s) {
  const mins = (v) => (v === null || v === undefined ? '—' : `${v} min`);
  const kpis = [
    [s.totalReports, 'Reports'], [s.openReports, 'Still open'], [s.emergencies, 'Emergencies'],
    [mins(s.medianMinutesToAccept), 'Median time to accept'], [mins(s.medianMinutesToResolve), 'Median time to fix'],
    [s.averageCleanliness === null || s.averageCleanliness === undefined ? '—' : `${s.averageCleanliness}/5`, 'Average cleanliness'],
  ];
  const hours = Array.from({ length: 24 }, (_, hr) => Number(s.byHour[hr] ?? s.byHour[String(hr)] ?? 0));
  const peak = Math.max(...hours);
  const hourCols = h('div', { class: 'hours', role: 'img', 'aria-label': 'Reports by hour of day' }, hours.map((n, hr) => {
    const b = h('div', { class: `colbar${n && n === peak ? ' peak' : ''}` });
    b.style.height = `${peak ? Math.max(2, Math.round((n / peak) * 100)) : 0}%`;
    b.style.animationDelay = `${hr * 15}ms`;
    return h('div', { class: 'col', title: `${hr}:00 · ${n}` }, b, h('div', { class: 'lbl' }, hr % 3 === 0 ? (hr === 0 ? '12a' : hr < 12 ? `${hr}a` : hr === 12 ? '12p' : `${hr - 12}p`) : ''));
  }));
  const janitors = Object.entries(s.byJanitor || {});
  return [
    h('div', { class: 'grid3' }, kpis.map(([v, k]) => h('div', { class: 'kpi' }, h('div', { class: 'value' }, v), h('div', { class: 'name' }, k)))),
    h('div', { class: 'grid2' },
      bars('Reports by restroom', Object.entries(s.byLocation || {})),
      bars('Most common problems', Object.entries(s.byIssue || {}), 'green'),
      bars('By type', Object.entries(s.byCategory || {}).map(([k, v]) => [CATEGORY[k] || k, v]), 'navy'),
      h('section', { class: 'card stack tight' }, h('h2', null, 'Busiest hours'), hourCols)),
    h('div', { class: 'grid2' },
      h('section', { class: 'card stack tight' }, h('h2', null, 'Problems that keep coming back'),
        (s.recurringProblems || []).length
          ? (s.recurringProblems || []).slice(0, 6).map((p) => h('div', { class: 'kv' }, h('span', null, `${p.issue} · ${p.location}`), h('b', null, `${p.count}×`)))
          : h('p', { class: 'small muted' }, 'Nothing repeating yet.')),
      h('section', { class: 'card stack tight' }, h('h2', null, 'Janitors'),
        janitors.length
          ? janitors.slice(0, 6).map(([n, v]) => h('div', { class: 'kv' }, h('span', null, n),
            h('b', null, `${v.handled} job${v.handled === 1 ? '' : 's'}${v.medianMinutesToResolve !== null ? ` · ${v.medianMinutesToResolve} min` : ''}`)))
          : h('p', { class: 'small muted' }, 'Jobs taken by SMS or in the app show up here.'))),
  ];
}

// ================================================================== PEOPLE
export function peopleView(_p, query) {
  let tab = query.get('tab') || 'requests';
  const body = h('div', { class: 'stack' });
  const seg = segmented([['requests', 'Requests'], ['users', 'Users'], ['invites', 'Invites']], tab, (id) => {
    tab = id; history.replaceState(null, '', `#/people?tab=${id}`); draw();
  }, { label: 'People sections' });
  const action = h('div');
  const view = h('div', { class: 'stack enter' }, pageHead('People', action), h('div', null, seg), body);
  function draw() {
    action.replaceChildren(tab === 'invites'
      ? h('button', { class: 'btn primary sm', type: 'button', onclick: () => inviteDialog(draw) }, icon('plus'), 'Invite admin') : '');
    if (tab === 'requests') requestsPanel(body, seg);
    else if (tab === 'users') usersPanel(body);
    else invitesPanel(body);
  }
  view.refresh = () => { if (tab === 'requests') requestsPanel(body, seg, true); };
  draw();
  return view;
}

function requestsPanel(body, seg, quiet = false) {
  fill(body, () => get('/admin/requests'), (rows) => {
    const waiting = rows.filter((r) => r.status === 'pending');
    const activating = rows.filter((r) => r.status === 'approved');
    seg.setCount('requests', waiting.length || '');
    const card = (r) => {
      const approve = h('button', { class: 'btn success sm', type: 'button' }, 'Approve');
      const reject = h('button', { class: 'btn sm', type: 'button' }, 'Reject');
      approve.addEventListener('click', () => busy(approve, 'Approving…', async () => {
        try {
          const res = await post(`/admin/requests/${encodeURIComponent(r.id)}/approve`, {}, { timeout: 90000 });
          if (res.code) showCode('Approved', `Give ${r.fullName} this activation code. It works once and expires ${when(res.expiresAt)}.`, res.code, r.username);
          else toast(res.message, 'success');
          invalidate('overview');
          requestsPanel(body, seg, true);
        } catch (err) { toast(err.message, 'error'); }
      }));
      reject.addEventListener('click', () => rejectDialog(r, () => requestsPanel(body, seg, true)));
      const contact = [r.source === 'web' ? 'SMS only (website sign-up)' : r.username, r.email, r.phone].filter(Boolean).join(' · ');
      return h('div', { class: 'card row' }, h('div', { class: 'avatar' }, initialsOf(r.fullName)),
        h('div', { class: 'grow' }, h('div', { class: 'row-title' }, r.fullName), h('div', { class: 'row-sub' }, `${contact} · ${ago(r.createdAt)}`)),
        reject, approve);
    };
    const activatingCard = (r) => {
      const again = h('button', { class: 'btn sm', type: 'button' }, 'New code');
      again.addEventListener('click', () => busy(again, '…', async () => {
        try {
          const res = await post(`/admin/requests/${encodeURIComponent(r.id)}/reissue-code`);
          showCode('New activation code', `The old code no longer works. Give ${r.fullName} this one.`, res.code, r.username);
        } catch (err) { toast(err.message, 'error'); }
      }));
      const expired = r.activationExpiresAt && new Date(r.activationExpiresAt) < new Date();
      return h('div', { class: 'list-row' }, h('div', { class: 'avatar' }, initialsOf(r.fullName)),
        h('div', { class: 'grow' }, h('div', { class: 'row-title' }, r.fullName),
          h('div', { class: 'row-sub' }, `${r.username} · ${expired ? 'code expired' : 'code expires ' + when(r.activationExpiresAt)}`)),
        expired ? h('span', { class: 'chip amber' }, 'Expired') : h('span', { class: 'chip blue' }, 'Waiting to activate'), again);
    };
    return [
      h('p', { class: 'small muted' }, 'Janitors who signed up here or on the website. App accounts get an activation code; website sign-ups become SMS contacts and get a welcome text.'),
      waiting.length ? waiting.map(card) : h('div', { class: 'card' }, empty('No requests waiting', 'New sign-ups appear here.', 'people')),
      activating.length ? [h('div', { class: 'eyebrow', style: { marginTop: '6px' } }, 'Approved, not activated yet'),
        h('div', { class: 'card flush' }, h('div', { class: 'list' }, activating.map(activatingCard)))] : null,
    ];
  }, { skeleton: quiet ? 0 : 3 });
}

function initialsOf(name) {
  return (name || '?').split(/\s+/).filter(Boolean).slice(0, 2).map((p) => p[0].toUpperCase()).join('');
}

function showCode(title, text, code, username) {
  modal((close) => [h('h2', null, title), h('p', { class: 'muted small' }, text), h('div', { class: 'code-box' }, code),
    h('p', { class: 'small muted' }, `They enter it on the sign-in screen under "I have an activation code", with the username ${username}.`),
    h('div', { class: 'actions' }, h('button', { class: 'btn primary', onclick: () => close() }, 'Done'))]);
}

function rejectDialog(r, after) {
  modal((close) => {
    const reason = field('Reason (optional)', { max: 200, placeholder: 'e.g. Not on staff list' });
    const go2 = h('button', { class: 'btn danger solid', type: 'button' }, 'Reject request');
    go2.addEventListener('click', () => busy(go2, 'Rejecting…', async () => {
      try { await post(`/admin/requests/${encodeURIComponent(r.id)}/reject`, { reason: reason.input.value }); close(); toast('Request rejected.'); after(); }
      catch (err) { toast(err.message, 'error'); }
    }));
    return [h('h2', null, `Reject ${r.fullName}?`), reason, h('div', { class: 'actions' }, h('button', { class: 'btn', onclick: () => close() }, 'Cancel'), go2)];
  });
}

function usersPanel(body) {
  fill(body, () => get('/admin/users'), (users) => {
    users.sort((a, b) => (a.role === b.role ? (a.fullName || '').localeCompare(b.fullName || '') : a.role === 'admin' ? -1 : 1));
    return h('div', { class: 'card flush' },
      h('div', { class: 'table-head hide-phone' }, h('div', { class: 'grow' }, 'Name'), h('div', { style: { width: '80px' } }, 'Role'),
        h('div', { style: { width: '90px' } }, 'Last sign-in'), h('div', { style: { width: '32px' } })),
      h('div', { class: 'list' }, users.map((u) => {
        const contact = [u.smsOnly ? 'SMS only' : u.username, u.smsPhone ? maskPhone(u.smsPhone) : null].filter(Boolean).join(' · ');
        const more = h('button', { class: 'btn icon sm', type: 'button', 'aria-label': `Actions for ${u.fullName}` }, icon('more'));
        more.addEventListener('click', () => userActions(u, () => usersPanel(body)));
        return h('div', { class: 'list-row' }, h('div', { class: 'avatar' }, initialsOf(u.fullName || u.username)),
          h('div', { class: 'grow' }, h('div', { class: 'row' }, h('span', { class: 'row-title' }, u.fullName || u.username),
            u.status !== 'active' ? h('span', { class: 'chip' }, 'Disabled') : null, u.uid === session.user.uid ? h('span', { class: 'chip blue' }, 'You') : null),
          h('div', { class: 'row-sub' }, contact)),
          h('div', { style: { width: '80px' } }, h('span', { class: `chip ${u.role === 'admin' ? 'navy' : 'green'}` }, u.role === 'admin' ? 'Admin' : 'Janitor')),
          h('div', { class: 'small muted hide-phone', style: { width: '90px' } }, u.smsOnly ? 'by SMS' : u.lastLoginAt ? ago(u.lastLoginAt) : 'never'),
          more);
      })));
  });
}

function maskPhone(p) { return p ? `${p.slice(0, -7).replace('+63', '0')}•••${p.slice(-4)}` : ''; }

function userActions(u, after) {
  const me = u.uid === session.user.uid;
  modal((close) => {
    const msg = message();
    const items = [];
    if (!me) {
      const toggle = h('button', { class: 'btn block', type: 'button' }, u.status === 'active' ? 'Disable account' : 'Enable account');
      toggle.addEventListener('click', () => busy(toggle, 'Saving…', async () => {
        try { await post(`/admin/users/${encodeURIComponent(u.uid)}/status`, { active: u.status !== 'active' }); close(); toast('Saved.', 'success'); after(); }
        catch (err) { say(msg, err.message, 'error'); }
      }));
      items.push(toggle);
    }
    if (u.smsPhone) {
      const test = h('button', { class: 'btn block', type: 'button' }, icon('sms'), 'Send a test text');
      test.addEventListener('click', () => busy(test, 'Sending…', async () => {
        try { const r = await post(`/admin/users/${encodeURIComponent(u.uid)}/test-sms`, {}, { timeout: 90000 }); say(msg, r.message, 'success'); }
        catch (err) { say(msg, err.message, 'error'); }
      }));
      items.push(test);
    }
    if (!me) items.push(h('button', { class: 'btn danger block', type: 'button', onclick: () => { close(); deleteDialog(u, after); } }, 'Delete account…'));
    return [h('div', { class: 'row' }, h('div', { class: 'avatar' }, initialsOf(u.fullName || u.username)),
      h('div', { class: 'grow' }, h('h2', null, u.fullName || u.username), h('div', { class: 'small muted' }, [u.username, u.email].filter(Boolean).join(' · ')))),
    items.length ? h('div', { class: 'stack tight' }, items) : h('p', { class: 'muted small' }, 'Change your own account in Account.'), msg,
    h('div', { class: 'actions' }, h('button', { class: 'btn', onclick: () => close() }, 'Close'))];
  });
}

function deleteDialog(u, after) {
  modal((close) => {
    const confirmF = field(`Type ${u.username} to confirm`, { autocomplete: 'off' });
    const msg = message();
    const del = h('button', { class: 'btn danger solid', type: 'button' }, 'Delete for good');
    del.addEventListener('click', () => busy(del, 'Deleting…', async () => {
      try { const r = await post(`/admin/users/${encodeURIComponent(u.uid)}/delete`, { confirmUsername: confirmF.input.value }); close(); toast(r.message, 'success'); after(); }
      catch (err) { say(msg, err.message, 'error'); }
    }));
    enterSubmits([confirmF], del);
    return [h('h2', null, `Delete ${u.fullName || u.username}?`),
      h('p', { class: 'small muted' }, 'This removes the account, its sign-in and saved sessions. Their past reports stay. This can\'t be undone.'),
      confirmF, msg, h('div', { class: 'actions' }, h('button', { class: 'btn', onclick: () => close() }, 'Cancel'), del)];
  });
}

function invitesPanel(body) {
  fill(body, () => get('/admin/invites'), (rows) => [
    h('p', { class: 'small muted' }, 'Invite a new administrator: they get a one-time code by email (and text, if you add their number) and register on the sign-in screen under "Admin invite code".'),
    rows.length ? h('div', { class: 'card flush' }, h('div', { class: 'list' }, rows.map((r) => {
      const revoke = r.status === 'active' ? h('button', { class: 'btn danger sm', type: 'button' }, 'Cancel') : null;
      if (revoke) {
        revoke.addEventListener('click', async () => {
          if (!(await confirmBox('Cancel this invite?', 'The code will stop working.', { ok: 'Cancel invite', danger: true }))) return;
          try { await post(`/admin/invites/${encodeURIComponent(r.id)}/revoke`); toast('Invite cancelled.'); invitesPanel(body); }
          catch (err) { toast(err.message, 'error'); }
        });
      }
      const chip = { active: ['Active', 'green'], used: ['Used', 'blue'], revoked: ['Cancelled', ''], expired: ['Expired', 'amber'] }[r.status] || [r.status, ''];
      return h('div', { class: 'list-row' }, h('div', { class: 'avatar', style: { background: '#EAF0FA', color: 'var(--navy)' } }, icon('shield')),
        h('div', { class: 'grow' }, h('div', { class: 'row-title' }, r.email || 'Anyone with the code'),
          h('div', { class: 'row-sub' }, `by ${(r.createdBy || {}).username || 'admin'} · ${r.status === 'active' ? 'expires ' + when(r.expiresAt) : when(r.createdAt)}`)),
        h('span', { class: `chip ${chip[1]}` }, chip[0]), revoke);
    }))) : h('div', { class: 'card' }, empty('No invites yet', 'Create one to add another administrator.', 'shield')),
  ]);
}

function inviteDialog(after) {
  modal((close) => {
    const email = field('Their email', { type: 'email', placeholder: 'name@gmail.com', hint: 'The code is emailed to them, and only this email can use it.' });
    const phone = field('Their mobile (optional)', { type: 'tel', placeholder: '0917 123 4567', inputmode: 'tel', hint: 'Also text them the code.' });
    const hours = select('Expires after', [['24', '1 day'], ['48', '2 days'], ['72', '3 days'], ['168', '1 week']], '48');
    const msg = message();
    const make = h('button', { class: 'btn primary', type: 'button' }, 'Send invite');
    cached('mail', () => get('/admin/mail'), 30000).then((m) => {
      if (!m.configured) say(msg, 'Email isn\'t set up yet, so the code won\'t be emailed: you\'ll see it here to pass on (or add a mobile number to text it). Set up email in System → SMS & email.', 'warn');
    }).catch(() => {});
    make.addEventListener('click', () => busy(make, 'Sending…', async () => {
      try {
        const r = await post('/admin/invites', { email: email.input.value, phone: phone.input.value, hours: Number(hours.input.value) }, { timeout: 90000 });
        close();
        const lines = [];
        if (r.emailed) lines.push(h('div', { class: 'msg success' }, `Emailed to ${r.email}. Ask them to check Spam too.`));
        else if (r.email && r.emailError) lines.push(h('div', { class: 'msg warn' }, r.emailError));
        if (r.texted) lines.push(h('div', { class: 'msg success' }, 'Texted to their mobile.'));
        else if (r.smsError) lines.push(h('div', { class: 'msg warn' }, `Couldn't text it: ${r.smsError}`));
        modal((c2) => [h('h2', null, r.emailed ? 'Invite sent' : 'Invite code'), ...lines,
          h('p', { class: 'small muted' }, `${r.emailed || r.texted ? 'Keep this in case they need it again' : 'Give this to the new administrator'} — it's shown only now and works once, until ${when(r.expiresAt)}.`),
          h('div', { class: 'code-box' }, r.code),
          h('p', { class: 'small muted' }, 'They register on the sign-in screen under "Admin invite code" (on the Pi, or on the Wi-Fi website).'),
          h('div', { class: 'actions' }, h('button', { class: 'btn primary', onclick: () => c2() }, 'Done'))]);
        after();
      } catch (err) { say(msg, err.message, 'error'); }
    }));
    enterSubmits([email, phone], make);
    return [h('h2', null, 'Invite an administrator'), email, phone, hours, msg,
      h('div', { class: 'actions' }, h('button', { class: 'btn', onclick: () => close() }, 'Cancel'), make)];
  });
}

function mailCard(st, after) {
  const msg = message();
  if (!st.configured) {
    return h('section', { class: 'card stack tight' }, h('h2', null, 'Email (admin invites)'),
      h('p', { class: 'small muted' }, 'Send admin invites by email from a Gmail account. You need a Gmail "app password" (Google Account → Security → 2-Step Verification → App passwords).'),
      h('div', null, h('button', { class: 'btn primary sm', type: 'button', onclick: () => mailDialog(after) }, 'Set up email')), msg);
  }
  const test = h('button', { class: 'btn sm', type: 'button' }, 'Send me a test');
  test.addEventListener('click', () => busy(test, 'Sending…', async () => {
    try { const r = await post('/admin/mail/test', {}, { timeout: 60000 }); say(msg, r.message, 'success'); }
    catch (err) { say(msg, err.message, 'error'); }
  }));
  const remove = h('button', { class: 'btn danger sm', type: 'button' }, 'Remove');
  remove.addEventListener('click', async () => {
    if (!(await confirmBox('Stop sending email?', 'Invites will show the code on screen only.', { ok: 'Remove', danger: true }))) return;
    try { await post('/admin/mail/clear'); invalidate('mail'); after(); } catch (err) { say(msg, err.message, 'error'); }
  });
  return h('section', { class: 'card stack tight' }, h('h2', null, 'Email (admin invites)'),
    h('div', { class: 'row small' }, h('span', { class: 'dot ok' }), `Invites are sent from ${st.user}`),
    h('div', { class: 'row wrap' }, test, h('button', { class: 'btn sm', type: 'button', onclick: () => mailDialog(after, st.user) }, 'Change'), remove), msg);
}

function mailDialog(after, user = '') {
  modal((close) => {
    const addr = field('Gmail address', { type: 'email', value: user, placeholder: 'aureoclean.sti@gmail.com', autocomplete: 'off' });
    const pass = field('App password', { type: 'password', placeholder: 'abcd efgh ijkl mnop', autocomplete: 'off',
      hint: '16 letters from Google Account → Security → 2-Step Verification → App passwords. Not your normal Gmail password.' });
    const msg = message();
    const save = h('button', { class: 'btn primary', type: 'button' }, 'Save & send a test');
    save.addEventListener('click', () => busy(save, 'Checking…', async () => {
      try {
        await post('/admin/mail', { user: addr.input.value, password: pass.input.value });
        invalidate('mail');
        try {
          const t = await post('/admin/mail/test', {}, { timeout: 60000 });
          close(); toast(t.message, 'success'); after();
        } catch (err) { say(msg, `Saved, but the test didn't go through: ${err.message}`, 'error'); after(); }
      } catch (err) { say(msg, err.message, 'error'); }
    }));
    enterSubmits([addr, pass], save);
    return [h('h2', null, 'Set up email'),
      h('p', { class: 'small muted' }, 'AureoClean sends admin invites from this Gmail account. The app password stays on the Pi only.'),
      addr, pass, msg, h('div', { class: 'actions' }, h('button', { class: 'btn', onclick: () => close() }, 'Cancel'), save)];
  });
}

// ================================================================== SYSTEM
export function systemView(_p, query) {
  let tab = query.get('tab') || 'health';
  const body = h('div', { class: 'stack' });
  const seg = segmented([['health', 'Health'], ['sms', 'SMS & email'], ['kiosk', shell.isPi ? 'Kiosk & web' : 'Website'], ['log', 'Activity log']], tab, (id) => {
    tab = id; history.replaceState(null, '', `#/system?tab=${id}`); draw();
  }, { label: 'System sections' });
  const view = h('div', { class: 'stack enter' }, pageHead('System'), h('div', null, seg), body);
  const draw = () => ({ health: healthPanel, sms: smsPanel, kiosk: kioskPanel, log: logPanel })[tab](body);
  draw();
  return view;
}

function healthPanel(body, force = false, balance = false) {
  if (force) invalidate('health');
  const loader = () => (balance ? get('/admin/health?balance=true', { timeout: 90000 }) : cached('health', () => get('/admin/health', { timeout: 90000 }), force ? 0 : 20000));
  fill(body, loader, (r) => {
    const again = h('button', { class: 'btn sm', type: 'button', onclick: () => healthPanel(body, true) }, icon('reload'), 'Check again');
    const hasBalance = r.checks.some((c) => c.key === 'balance');
    const summary = r.status === 'ok' ? 'Everything is working' : `${r.problems} thing${r.problems === 1 ? '' : 's'} need${r.problems === 1 ? 's' : ''} attention`;
    return [
      h('div', { class: 'row wrap' }, h('span', { class: `dot ${r.status}`, style: { width: '10px', height: '10px' } }), h('b', null, summary),
        h('span', { class: 'small muted' }, `· checked ${ago(r.checkedAt)}`), h('div', { class: 'grow' }),
        hasBalance ? h('button', { class: 'btn sm', type: 'button', onclick: () => healthPanel(body, true, true) }, 'Check balance') : null, again),
      h('div', { class: 'checks' }, r.checks.map((c) => h('div', { class: 'check-tile' }, h('span', { class: `dot ${c.status}` }),
        h('div', { class: 'grow' }, h('div', { class: 'name' }, c.name), h('div', { class: 'detail' }, c.detail))))),
    ];
  }, { skeleton: 4 });
}

function smsPanel(body) {
  fill(body, () => Promise.all([get('/admin/sms/settings'), get('/admin/sms/promo'), cached('mail', () => get('/admin/mail'), 0)]), ([st, promo, mail]) => {
    const onModem = st.mode === 'modem';
    const state = h('div', { class: 'row small' }, h('span', { class: `dot ${onModem ? 'ok' : 'warn'}` }), st.label);
    const out = message();
    const find = h('button', { class: 'btn primary sm', type: 'button' }, onModem ? 'Find modem again' : 'Find & use modem');
    const check = h('button', { class: 'btn sm', type: 'button' }, 'Check signal');
    const test = onModem ? h('button', { class: 'btn sm', type: 'button' }, 'Use test mode') : null;
    find.addEventListener('click', () => busy(find, 'Looking… (up to a minute)', async () => {
      try { const r = await post('/admin/sms/detect', {}, { timeout: 120000 }); say(out, r.message, 'success'); setTimeout(() => smsPanel(body), 1500); }
      catch (err) { say(out, err.message, 'error'); }
    }));
    check.addEventListener('click', () => busy(check, 'Checking…', async () => {
      try { const r = await get('/admin/sms/status', { timeout: 90000 }); say(out, `${r.ok ? '✓' : '✗'} ${r.detail}`, r.ok ? 'success' : 'error'); }
      catch (err) { say(out, err.message, 'error'); }
    }));
    if (test) {
      test.addEventListener('click', async () => {
        if (!(await confirmBox('Use test mode?', 'Texts will only be written to a log file, not sent.', { ok: 'Use test mode' }))) return;
        try { const r = await post('/admin/sms/test-mode'); toast(r.message); smsPanel(body); } catch (err) { say(out, err.message, 'error'); }
      });
    }
    const to = field('Send a test text to', { type: 'tel', placeholder: '0917 123 4567', inputmode: 'tel' });
    const send = h('button', { class: 'btn success', type: 'button' }, 'Send');
    send.addEventListener('click', () => busy(send, 'Sending…', async () => {
      try { const r = await post('/admin/sms/test', { phone: to.input.value }, { timeout: 90000 }); say(out, r.message, 'success'); }
      catch (err) { say(out, err.message, 'error'); }
    }));
    enterSubmits([to], send);
    const from = field('From number', { type: 'tel', placeholder: '0917 123 4567' });
    const text = field('Text', { placeholder: 'OK 12' });
    const tryBtn = h('button', { class: 'btn', type: 'button' }, 'Try reply');
    const labels = { accepted: 'took the job', done: 'marked it done', taken: 'was told it\'s already taken', already_done: 'was told it\'s already done',
      not_found: 'was told that report isn\'t open', help: 'got the how-to-reply text', ignored: 'was ignored' };
    tryBtn.addEventListener('click', () => busy(tryBtn, 'Trying…', async () => {
      try {
        const r = await post('/admin/sms/simulate-reply', { phone: from.input.value, text: text.input.value }, { timeout: 90000 });
        say(out, `The sender ${labels[r.action] || r.action}${r.ref ? ` (#${r.ref})` : r.reason ? `: ${r.reason}` : ''}.`, 'success');
        invalidate('reports');
      } catch (err) { say(out, err.message, 'error'); }
    }));
    enterSubmits([from, text], tryBtn);

    return [
      h('div', { class: 'grid2' },
        h('section', { class: 'card stack tight' }, h('h2', null, 'GSM modem'), state,
          h('div', { class: 'row wrap' }, find, check, test), h('div', { class: 'row', style: { alignItems: 'flex-end' } }, h('div', { class: 'grow' }, to), send)),
        promoCard(promo, () => smsPanel(body))),
      out,
      mailCard(mail, () => smsPanel(body)),
      h('section', { class: 'card stack tight' }, h('h2', null, 'Try a reply'),
        h('p', { class: 'small muted' }, 'Pretend a janitor texted back, e.g. OK 12 or DONE 12. Real replies are read from the SIM every 20 seconds.'),
        h('div', { class: 'row wrap', style: { alignItems: 'flex-end' } }, h('div', { class: 'grow' }, from), h('div', { style: { width: '140px' } }, text), tryBtn)),
    ];
  }, { skeleton: 3 });
}

function promoCard(p, after) {
  const edit = h('button', { class: 'btn sm', type: 'button', onclick: () => promoDialog(p, after) }, p.set ? 'Update promo' : 'Set promo');
  if (!p.set) {
    return h('section', { class: 'card stack tight' }, h('h2', null, 'SIM promo'),
      h('p', { class: 'small muted' }, 'Enter the SIM\'s promo (e.g. unli texts for 15 days). You\'ll be warned, and the admins get a text, before it ends.'), h('div', null, edit));
  }
  const total = p.registered ? Math.max(1, Math.round((new Date(p.expires) - new Date(p.registered)) / 86400000)) : 15;
  const kind = p.daysLeft < 0 ? 'fail' : p.daysLeft <= 2 ? 'warn' : '';
  const bar = h('div', { class: kind });
  bar.style.width = `${Math.max(0, Math.min(100, (p.daysLeft / total) * 100))}%`;
  const left = p.daysLeft < 0 ? 'ended' : p.daysLeft === 0 ? 'ends today' : p.daysLeft === 1 ? 'day left' : 'days left';
  return h('section', { class: 'card stack tight' }, h('h2', null, 'SIM promo'),
    h('div', { class: 'row', style: { alignItems: 'baseline' } }, p.daysLeft > 0 ? h('span', { class: 'stat' }, h('span', { class: 'value navy' }, p.daysLeft)) : null,
      h('span', { class: `small ${kind ? '' : 'muted'}`, style: kind ? { color: 'var(--amber-text)', fontWeight: '600' } : {} }, left)),
    h('div', { class: 'progress' }, bar),
    h('div', { class: 'small muted' }, `${p.name} · ends ${new Date(p.expires + 'T00:00').toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })}`),
    h('div', null, edit));
}

function promoDialog(p, after) {
  modal((close) => {
    const name = field('Promo name', { placeholder: 'Globe unli calls & texts', value: p.set ? p.name : '', max: 60 });
    const days = field('Lasts (days)', { type: 'number', value: String(p.set && p.registered ? Math.round((new Date(p.expires) - new Date(p.registered)) / 86400000) : 15) });
    const start = field('Registered on', { type: 'date', value: p.set && p.registered ? p.registered : new Date().toISOString().slice(0, 10) });
    const ends = h('p', { class: 'small muted' });
    const upd = () => {
      const d = new Date(start.input.value + 'T00:00');
      d.setDate(d.getDate() + Number(days.input.value || 0));
      ends.textContent = isNaN(d) ? '' : `Ends on ${d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' })}.`;
    };
    days.input.addEventListener('input', upd);
    start.input.addEventListener('input', upd);
    upd();
    const msg = message();
    const save = h('button', { class: 'btn primary', type: 'button' }, 'Save');
    save.addEventListener('click', () => busy(save, 'Saving…', async () => {
      try {
        await post('/admin/sms/promo', { name: name.input.value, days: Number(days.input.value), registered: start.input.value });
        close(); toast('SIM promo saved. You\'ll get a text the day before it ends.', 'success'); invalidate('health'); after();
      } catch (err) { say(msg, err.message, 'error'); }
    }));
    const remove = p.set ? h('button', { class: 'btn danger left', type: 'button' }, 'Remove') : null;
    if (remove) {
      remove.addEventListener('click', () => busy(remove, '…', async () => {
        try { await post('/admin/sms/promo/clear'); close(); toast('SIM promo removed.'); invalidate('health'); after(); } catch (err) { say(msg, err.message, 'error'); }
      }));
    }
    return [h('h2', null, 'SIM promo'), h('p', { class: 'small muted' }, 'AureoClean warns in System health and texts the admins the day before it ends.'),
      name, h('div', { class: 'grid2 keep' }, days, start), ends, msg,
      h('div', { class: 'actions' }, remove, h('button', { class: 'btn', onclick: () => close() }, 'Cancel'), save)];
  });
}

function testRow(msg) {
  const btn = h('button', { class: 'btn sm', type: 'button' }, 'Test connection');
  btn.addEventListener('click', () => busy(btn, 'Testing…', async () => {
    try {
      const r = await get('/admin/web/test', { timeout: 30000 });
      say(msg, r.verdict, r.ok ? 'success' : 'error');
    } catch (err) { say(msg, err.message, 'error'); }
  }));
  return h('div', { class: 'row wrap' }, btn, h('span', { class: 'small muted' }, 'Checks the website from this Pi and explains what to do.'));
}

function kioskPanel(body) {
  fill(body, () => Promise.all([get('/admin/web'), cached('links', () => get('/links'), 60000)]), ([web, links]) => {
    const cards = [];
    if (shell.isPi) {
      const sw = switchEl(shell.kiosk, (on) => shell.send('kiosk', { on }), 'Kiosk mode');
      const label = h('b', null, shell.kiosk ? 'Kiosk mode is on' : 'Kiosk mode is off');
      const sync = (m) => { if (m.event === 'kiosk') { sw.set(m.on); label.textContent = m.on ? 'Kiosk mode is on' : 'Kiosk mode is off'; } };
      shell.listeners.add(sync);
      cards.push(h('section', { class: 'card stack tight' }, h('h2', null, 'Kiosk mode'),
        h('div', { class: 'row' }, sw, label),
        h('p', { class: 'small muted' }, 'Full screen, can\'t be closed by accident, and locks after a few minutes without a touch. Turn it off here, or on the sign-in screen with an admin password.')));
    }
    const msg = message();
    const sw = switchEl(web.enabled, (on, el) => {
      el.disabled = true;
      post('/admin/web', { enabled: on }, { timeout: 30000 }).then(() => kioskPanel(body))
        .catch((err) => { el.set(!on); el.disabled = false; say(msg, err.message, 'error'); });
    }, 'Website on Wi-Fi');
    if (web.available === false) sw.disabled = true;
    const qr = h('img', { class: 'qr hidden', alt: 'QR code for the website address' });
    if (web.enabled && web.running) {
      api('GET', '/admin/web/qr.svg', undefined, { raw: true }).then((r) => r.blob()).then((b) => { qr.src = URL.createObjectURL(b); qr.classList.remove('hidden'); }).catch(() => {});
    }
    const stateLine = web.enabled
      ? (web.running ? h('div', { class: 'row small' }, h('span', { class: 'dot ok' }), 'On. Open one of these on a phone or laptop on the same Wi-Fi:')
        : h('div', { class: 'msg error' }, web.error || 'Starting…'))
      : h('p', { class: 'small muted' }, 'Lets janitors and admins open AureoClean on their phones and laptops — only on the same Wi-Fi as the Pi, never from the internet.');
    cards.push(h('section', { class: 'card stack tight' }, h('h2', null, 'Website on Wi-Fi'),
      h('div', { class: 'row' }, sw, h('b', null, web.enabled ? 'On' : 'Off')), stateLine,
      web.enabled && web.running ? h('div', { class: 'row', style: { alignItems: 'flex-start', gap: '12px' } }, h('div', { class: 'grow stack tight' },
        web.urls.slice(0, 3).map((u, i) => h('div', { class: 'url-box', style: i ? { fontSize: '12px', fontWeight: '500' } : {} }, u)),
        h('p', { class: 'small muted' }, 'Type the first address exactly (or scan the QR code) on a phone connected to the same Wi-Fi as the Pi — not mobile data.')), qr) : null,
      web.enabled ? testRow(msg) : null, msg));
    const linkRow = (text, url) => h('button', { class: 'linkrow', type: 'button', onclick: () => openExternal(url) }, text, icon('external'));
    cards.push(h('section', { class: 'card' }, h('h2', { style: { marginBottom: '4px' } }, 'Websites'),
      links.filter((l) => l.key !== 'dashboard').map((l) => linkRow(l.title, l.url)),
      linkRow('Public report page (QR codes)', `${PUBLIC_SITE}report/`),
      linkRow('Janitor sign-up page', `${PUBLIC_SITE}login/`)));
    return h('div', { class: 'grid2' }, cards);
  }, { skeleton: 2 });
}

const ACTIONS = {
  login: 'Signed in', janitor_request_submitted: 'Janitor request submitted', janitor_request_approved: 'Request approved',
  janitor_request_rejected: 'Request rejected', activation_code_reissued: 'New activation code issued', janitor_activated: 'Janitor account activated',
  admin_invite_created: 'Admin invite created', admin_invite_revoked: 'Admin invite cancelled', admin_registered: 'Admin registered with invite',
  admin_bootstrapped: 'First admin created', admin_linked_existing: 'Existing account linked as admin', user_enabled: 'User enabled',
  user_disabled: 'User disabled', user_deleted: 'Account deleted', sms_reply_received: 'SMS reply received', sms_reply_simulated: 'SMS reply tried',
  sms_from_unknown: 'SMS from unknown number', report_escalated: 'Report escalated (no reply)', password_changed: 'Password changed',
  sms_code_sent: 'SMS verification code sent', sms_phone_verified: 'Mobile number verified', sms_phone_removed: 'Mobile number removed',
  sms_alerts_on: 'SMS alerts on', sms_alerts_off: 'SMS alerts off', sms_test_sent: 'Test SMS sent', sms_modem_enabled: 'SMS switched to GSM modem',
  sms_test_mode: 'SMS switched to test mode', web_signup_received: 'Website sign-up received', web_signup_rejected: 'Website sign-up rejected',
  janitor_contact_approved: 'Janitor added as SMS contact', report_created: 'Report created', report_updated: 'Report updated',
  public_report_dropped: 'Public report dropped (too many)', admin_email_verified: 'Admin email verified', verification_email_sent: 'Verification email sent',
  lock_pin_set: 'Screen-lock PIN set', lock_pin_removed: 'Screen-lock PIN removed', unlock_locked_out: 'Signed out after wrong PINs',
  kiosk_exit: 'Left kiosk mode', admin_invite_emailed: 'Admin invite emailed', mail_set_up: 'Email set up', mail_removed: 'Email removed', sim_promo_set: 'SIM promo updated', sim_promo_cleared: 'SIM promo removed',
  wifi_website_on: 'Wi-Fi website turned on', wifi_website_off: 'Wi-Fi website turned off',
};

function logPanel(body) {
  fill(body, () => get('/admin/audit?limit=200'), (rows) => {
    let shown = 60;
    const list = h('div', { class: 'list' });
    const more = h('button', { class: 'btn sm', type: 'button' }, 'Show more');
    const draw = () => {
      list.replaceChildren(...rows.slice(0, shown).map((a) => {
        const d = a.details || {};
        const extra = d.title || d.username || d.status || d.text || d.phone || d.reason || '';
        return h('div', { class: 'list-row' }, h('span', { class: 'small muted', style: { width: '74px', flexShrink: '0' } }, when(a.at)),
          h('div', { class: 'grow' }, h('div', { class: 'row-title', style: { fontWeight: '500' } }, ACTIONS[a.action] || a.action.replace(/_/g, ' ')),
            extra ? h('div', { class: 'row-sub' }, String(extra)) : null),
          h('span', { class: 'small muted' }, (a.actor || {}).username || ''));
      }));
      more.classList.toggle('hidden', shown >= rows.length);
    };
    more.addEventListener('click', () => { shown += 60; draw(); });
    draw();
    return [rows.length ? h('div', { class: 'card flush' }, list) : h('div', { class: 'card' }, empty('Nothing yet', '', 'inbox')), h('div', null, more)];
  });
}

export { go };
