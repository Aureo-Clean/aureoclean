// Janitor screens: My jobs (the simple report view), Report a problem, Live sensors.
import { ago, busy, cached, firstName, get, h, icon, peek, post, session, toast, invalidate } from './core.js';
import { aiFillBox, aiJobTip, DANGER_NOTE, tipsCard } from './insights.js';
import { sensorsCard } from './sensors.js';
import { empty, errorBox, message, pageHead, priorityChip, say, segmented } from './ui.js';

export function jobsView(_p, query) {
  let tab = query.get('tab') || 'open';
  let data = peek('jobs');
  const grid = h('div', { class: 'stack' });
  const summary = h('div', { class: 'sub' }, '');
  const seg = segmented([['open', 'Open'], ['mine', 'Mine'], ['done', 'Done today']], tab, (id) => {
    tab = id; history.replaceState(null, '', `#/jobs?tab=${id}`); draw();
  }, { large: true, label: 'Which jobs' });
  const view = h('div', { class: 'stack enter' },
    h('header', { class: 'page-head' }, h('div', null, h('h1', null, `Hi ${firstName(session.user)}`), summary), h('div', { class: 'grow' }), seg),
    tipsCard(),
    grid,
    h('p', { class: 'small muted', style: { marginTop: 'auto' } }, 'Same as texting back OK and DONE with the number — whichever you do first counts.'));

  function draw() {
    if (!data) {
      grid.replaceChildren(h('div', { class: 'jobs' }, [1, 2].map(() => h('div', { class: 'job' }, h('div', { class: 'skeleton', style: { height: '14px', width: '40%' } }),
        h('div', { class: 'skeleton', style: { height: '20px', width: '70%' } }), h('div', { class: 'skeleton', style: { height: '44px' } })))));
      return;
    }
    seg.setCount('open', data.open.length);
    seg.setCount('mine', data.mine.length);
    seg.setCount('done', data.done.length);
    summary.textContent = `${data.open.length} open · ${data.mine.length} with you`;
    const list = data[tab];
    const cards = list.map((j) => jobCard(j, tab));
    const taken = tab === 'open' && data.taken.length
      ? [h('div', { class: 'eyebrow', style: { marginTop: '4px' } }, 'Being fixed by others'),
        h('div', { class: 'card flush' }, h('div', { class: 'list' }, data.taken.map((j) => h('div', { class: 'list-row' },
          h('div', { class: 'ref' }, `#${j.ref || '—'}`), h('div', { class: 'grow' }, h('div', { class: 'row-title' }, j.title), h('div', { class: 'row-sub' }, j.location)),
          h('span', { class: 'chip violet' }, `${j.takenBy} is on it`)))))]
      : [];
    const none = {
      open: empty('All clear', 'No open jobs right now. New ones show up here and by text.', 'check'),
      mine: empty('Nothing with you', 'Tap "I\'ll take it" on an open job.', 'inbox'),
      done: empty('Nothing done yet today', '', 'inbox'),
    }[tab];
    grid.replaceChildren(cards.length ? h('div', { class: 'jobs' }, cards) : h('div', { class: 'card' }, none), ...taken);
  }

  function jobCard(j, where) {
    const urgent = j.priority === 'critical' ? 'critical' : j.priority === 'high' ? 'urgent' : '';
    const card = h('article', { class: `job ${urgent}` },
      h('div', { class: 'row' }, h('span', { style: { fontSize: '13px', fontWeight: '700', color: 'var(--navy)' } }, `#${j.ref || '—'}`),
        where === 'mine' ? h('span', { class: 'chip violet' }, 'With you') : where === 'done' ? h('span', { class: 'chip green' }, `Done by ${j.doneBy || '—'}`) : priorityChip(j.priority) || h('span', { class: 'chip blue' }, 'Open'),
        h('div', { class: 'grow' }), h('span', { class: 'small muted' }, ago(where === 'done' ? j.resolvedAt : j.createdAt))),
      h('div', { class: 'title' }, j.title),
      h('div', { class: 'where' }, icon('pin'), j.location || 'No place given'),
      j.details ? h('div', { class: 'details' }, j.details) : null,
      where !== 'done' ? aiJobTip(j.ai) : null);
    if (where === 'open') {
      const take = h('button', { class: 'btn primary lg block', type: 'button' }, 'I\'ll take it');
      take.addEventListener('click', () => busy(take, 'Taking…', async () => {
        try {
          await post(`/janitor/jobs/${encodeURIComponent(j.id)}/take`);
          card.classList.add('leaving');
          toast(`#${j.ref} is yours. Tap Done when it's finished.`, 'success');
          setTimeout(() => view.refresh(true), 220);
        } catch (err) { toast(err.message, 'error'); view.refresh(true); }
      }));
      card.append(take);
    } else if (where === 'mine') {
      const done = h('button', { class: 'btn success lg block', type: 'button' }, icon('check'), 'Done');
      done.addEventListener('click', () => busy(done, 'Saving…', async () => {
        try {
          await post(`/janitor/jobs/${encodeURIComponent(j.id)}/done`, { note: '' });
          done.replaceWith(h('div', { class: 'doneline' }, icon('check'), 'Fixed · thanks!'));
          setTimeout(() => { card.classList.add('leaving'); setTimeout(() => view.refresh(true), 220); }, 900);
        } catch (err) { toast(err.message, 'error'); view.refresh(true); }
      }));
      card.append(done);
    }
    return card;
  }

  view.refresh = async (force) => {
    if (force) invalidate('jobs');
    try { data = await cached('jobs', () => get('/janitor/jobs'), force ? 0 : 5000); draw(); }
    catch (err) { if (!data) grid.replaceChildren(errorBox(err, () => view.refresh(true))); }
  };
  draw();
  view.refresh(true);
  return view;
}

// ------------------------------------------------------------------ report a problem
const COMMON = ['emergency', 'wet_floor', 'dirty_toilet', 'dirty_floor', 'bad_odor', 'trash_full', 'no_soap', 'no_tissue', 'no_paper_towel',
  'no_water', 'clogged_toilet', 'broken_flush', 'leak', 'broken_faucet', 'broken_lock', 'broken_light', 'other'];
const SHORT = { emergency: 'Emergency', wet_floor: 'Wet floor', dirty_toilet: 'Dirty toilet', dirty_floor: 'Dirty floor', dirty_sink: 'Dirty sink',
  bad_odor: 'Bad smell', trash_full: 'Trash full', no_soap: 'No soap', no_tissue: 'No toilet paper', no_paper_towel: 'No paper towels',
  no_water: 'No water', clogged_toilet: 'Clogged toilet', broken_flush: 'Flush broken', leak: 'Leak', broken_faucet: 'Faucet broken',
  broken_lock: 'Door / lock broken', broken_light: 'Light out', other: 'Something else' };

export function reportProblemView() {
  const body = h('div', { class: 'stack' });
  const view = h('div', { class: 'stack enter' }, pageHead('Report a problem'), body);
  let place = null;
  const picked = new Set();

  function form(opts) {
    const tiles = h('div', { class: 'tiles' }, opts.locations.map((l) => {
      const b = h('button', { class: 'tile', type: 'button', 'aria-pressed': 'false' }, l.name.replace(' Floor', '').replace('Ground', 'Ground'));
      b.addEventListener('click', () => { place = l; tiles.querySelectorAll('.tile').forEach((t) => t.setAttribute('aria-pressed', String(t === b))); update(); });
      return b;
    }));
    const known = new Set(opts.issues.map((i) => i.code));
    const chips = h('div', { class: 'chips' }, COMMON.filter((c) => known.has(c)).map((code) => {
      const b = h('button', { class: `chipbtn${code === 'emergency' ? ' danger' : ''}`, type: 'button', 'aria-pressed': 'false' }, SHORT[code] || code);
      b.addEventListener('click', () => {
        picked.has(code) ? picked.delete(code) : picked.add(code);
        b.setAttribute('aria-pressed', String(picked.has(code)));
        update();
      });
      return b;
    }));
    const note = h('input', { class: 'input', id: 'rp-note', placeholder: 'e.g. second stall from the door', maxlength: 500 });
    const msg = message();
    const send = h('button', { class: 'btn primary lg', type: 'button', disabled: true }, 'Send report');
    const update = () => {
      send.disabled = !(place && picked.size);
      if (picked.has('emergency')) say(msg, 'Emergency: everyone with SMS alerts gets a text right away.', 'warn');
      else say(msg, '');
    };
    send.addEventListener('click', () => busy(send, 'Sending…', async () => {
      try {
        const r = await post('/reports/problem', { location: place.name, locationCode: place.code, issues: [...picked], details: note.value });
        invalidate('jobs', 'reports');
        body.replaceChildren(h('div', { class: 'card', style: { textAlign: 'center' } }, h('div', { class: 'stack', style: { alignItems: 'center', padding: '20px 0' } },
          h('div', { class: 'success-mark' }, icon('check')), h('h2', { style: { fontSize: '18px' } }, r.message),
          h('p', { class: 'small muted' }, 'It\'s in the list now. Thank you!'),
          h('div', { class: 'row' }, h('a', { class: 'btn', href: '#/jobs' }, 'See jobs'),
            h('button', { class: 'btn primary', type: 'button', onclick: () => { place = null; picked.clear(); body.replaceChildren(form(opts)); } }, 'Report another')))));
      } catch (err) { say(msg, err.message, 'error'); }
    }));
    note.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !send.disabled) send.click(); });
    const aiBox = aiFillBox((r) => {
      if (r.location) {
        const spot = opts.locations.find((l) => l.code === r.location.code);
        if (spot) {
          place = spot;
          tiles.querySelectorAll('.tile').forEach((t, i) => t.setAttribute('aria-pressed', String(opts.locations[i] === spot)));
        }
      }
      const shown = COMMON.filter((c) => known.has(c));
      if (r.issues.length) {
        picked.clear();
        r.issues.forEach((c) => picked.add(shown.includes(c) ? c : 'other'));
        chips.querySelectorAll('.chipbtn').forEach((b, i) => b.setAttribute('aria-pressed', String(picked.has(shown[i]))));
      }
      if (r.details) note.value = r.details;
      update();
      if (r.possibleEmergency) say(msg, DANGER_NOTE, 'warn');
    });
    return [
      aiBox,
      h('div', { class: 'eyebrow' }, '1 · Where?'), tiles,
      h('div', { class: 'eyebrow', style: { marginTop: '4px' } }, '2 · What\'s wrong? ', h('span', { style: { textTransform: 'none', letterSpacing: '0', fontWeight: '400' } }, '(tap all that apply)')),
      chips,
      h('div', { class: 'row wrap', style: { alignItems: 'flex-end', marginTop: '4px' } },
        h('div', { class: 'field grow', style: { minWidth: '240px' } }, h('label', { for: 'rp-note' }, '3 · Anything to add? (optional)'), note), send),
      msg,
    ];
  }

  cached('report-options', () => get('/report-options'), 300000)
    .then((opts) => body.replaceChildren(...form(opts)))
    .catch((err) => body.replaceChildren(errorBox(err)));
  return view;
}

// ------------------------------------------------------------------ home: live dashboard + my jobs
export function sensorsView() {
  const openCard = h('a', { class: 'card stat', href: '#/jobs' }, h('div', { class: 'eyebrow' }, 'Open jobs'),
    h('div', { class: 'value' }, '–'), h('div', { class: 'small muted' }, 'tap to see them'));
  const mineCard = h('a', { class: 'card stat', href: '#/jobs?tab=mine' }, h('div', { class: 'eyebrow' }, 'With you'),
    h('div', { class: 'value navy' }, '–'), h('div', { class: 'small muted' }, 'tap Done when finished'));
  const report = h('a', { class: 'btn primary block', href: '#/report' }, icon('plus'), 'Report a problem');
  const view = h('div', { class: 'stack enter', style: { flex: '1', minHeight: '0' } },
    h('header', { class: 'page-head' }, h('div', null, h('h1', null, `Hi ${firstName(session.user)}`), h('div', { class: 'sub' }, new Date().toLocaleDateString(undefined, { weekday: 'long', month: 'short', day: 'numeric' })))),
    h('div', { class: 'home-grid' }, sensorsCard(), h('aside', { class: 'side-stats' }, openCard, mineCard, report)));
  view.refresh = async () => {
    try {
      invalidate('jobs');
      const jobs = await cached('jobs', () => get('/janitor/jobs'), 0);
      openCard.children[1].textContent = String(jobs.open.length);
      mineCard.children[1].textContent = String(jobs.mine.length);
    } catch { /* shown on My jobs */ }
  };
  view.refresh();
  return view;
}
