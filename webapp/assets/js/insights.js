// AI insights (admins): an AI model reads the reports and the live sensor readings and
// suggests what to do — and answers questions about them. Advice only: alerts are decided
// by fixed rules, never by the AI. Janitors see the top suggestions on My jobs.
import { ago, busy, cached, get, h, icon, invalidate, modal, openExternal, post, session, shell, toast } from './core.js';
import { enterSubmits, field, message, say, switchEl } from './ui.js';

const PRIORITY = { high: ['Do first', 'red'], medium: ['Soon', 'amber'], low: ['When you can', ''] };
const SUGGESTIONS = ['Which restroom needs attention first today?', 'When do most problems happen?',
  'Are any sensor readings unusual?', 'What supplies should we restock this week?'];

// ------------------------------------------------------------------ AI settings form
function settingsForm(st, { onSaved, inline = false } = {}) {
  let provider = st.provider || 'gemini';
  const info = () => st.providers.find((p) => p.id === provider) || st.providers[0];
  const chips = h('div', { class: 'chips', role: 'radiogroup', 'aria-label': 'AI provider' });
  const note = h('div', { class: 'small muted' });
  const keyLink = h('a', { href: '#', class: 'small' });
  keyLink.addEventListener('click', (e) => { e.preventDefault(); if (info().keyUrl) openExternal(info().keyUrl); });
  const key = field('API key', { type: 'password', autocomplete: 'off',
    placeholder: st.configured && st.keyHint ? `Saved (${st.keyHint}) — leave empty to keep` : 'Paste the key here' });
  const model = field('Model', { autocomplete: 'off', value: st.model || info().model || '',
    hint: 'Leave empty to use the newest model. If the provider retires a model, AureoClean switches by itself.' });
  const url = field('Address (Ollama on this Pi, Groq, OpenRouter …)', { autocomplete: 'off', value: st.url || '' });
  const msg = message();
  const save = h('button', { class: 'btn primary', type: 'button' }, st.configured ? 'Save & test' : 'Connect & test');

  function draw() {
    const p = info();
    chips.replaceChildren(...st.providers.map((x) => h('button', {
      type: 'button', class: 'chipbtn', role: 'radio', 'aria-pressed': String(x.id === provider), 'aria-checked': String(x.id === provider),
      onclick: () => {
        provider = x.id;
        model.input.value = x.id === st.provider ? st.model : x.model;
        url.input.value = x.id === 'openai' ? (st.provider === 'openai' ? st.url : x.url) : '';
        draw();
      },
    }, x.label)));
    note.textContent = p.note;
    keyLink.textContent = p.keyUrl ? (p.id === 'gemini' ? 'Get a free key at Google AI Studio ›' : 'Get a key ›') : '';
    key.querySelector('label').textContent = p.needsKey ? 'API key' : 'API key (leave empty for Ollama)';
    url.classList.toggle('hidden', p.id !== 'openai');
    model.querySelector('label').textContent = p.id === 'openai' ? 'Model (e.g. llama3.2:3b, qwen2.5:3b)' : 'Model';
    model.input.placeholder = p.id === 'gemini' ? 'Automatic — newest Flash model' : p.id === 'claude' ? 'claude-haiku-5-5' : 'llama3.2:3b';
    const hint = model.querySelector('.hint');
    if (hint) hint.classList.toggle('hidden', p.id === 'openai');
  }
  draw();
  enterSubmits([key, model, url], save);
  save.addEventListener('click', () => busy(save, 'Testing…', async () => {
    try {
      const r = await post('/admin/llm', { provider, apiKey: key.input.value, model: model.input.value, url: url.input.value },
        { timeout: provider === 'openai' ? 620000 : 130000 });
      toast(r.message, r.warning ? 'error' : 'success');
      onSaved && onSaved(r);
    } catch (err) { say(msg, err.message, 'error'); }
  }));
  const body = [chips, h('div', { class: 'row wrap', style: { gap: '10px' } }, note, keyLink), key, model, url, msg,
    h('p', { class: 'small muted' }, 'The key stays on this Pi only. Sent to the AI: report details (names, phone numbers and e-mails removed) and sensor readings.')];
  return { body, save, msg };
}

function settingsDialog(st, after) {
  modal((close) => {
    const form = settingsForm(st, { onSaved: () => { close(); after(); } });
    const daily = switchEl(st.daily, async (on, b) => {
      try { await post('/admin/insights/daily', { daily: on, hour: Number(hour.value) }); } catch (err) { b.set(!on); toast(err.message, 'error'); }
    }, 'Make insights every morning');
    const hour = h('select', { class: 'input', style: { width: '116px', height: '34px' }, 'aria-label': 'Hour' },
      Array.from({ length: 24 }, (_, i) => h('option', { value: String(i), selected: i === st.dailyHour }, `${i % 12 || 12}:00 ${i < 12 ? 'AM' : 'PM'}`)));
    hour.addEventListener('change', () => post('/admin/insights/daily', { daily: daily.getAttribute('aria-checked') === 'true', hour: Number(hour.value) }).catch((e) => toast(e.message, 'error')));
    const remove = h('button', { class: 'btn danger left', type: 'button' }, 'Remove AI');
    remove.addEventListener('click', () => busy(remove, '…', async () => {
      try { await post('/admin/llm/clear'); close(); toast('AI removed from this Pi.'); after(); } catch (err) { say(form.msg, err.message, 'error'); }
    }));
    const tips = switchEl(st.autoTips !== false, async (on, b) => {
      try { await post('/admin/insights/daily', { autoTips: on }); } catch (err) { b.set(!on); toast(err.message, 'error'); }
    }, 'AI tips on new reports');
    return [h('h2', null, 'AI settings'),
      h('div', { class: 'row', style: { gap: '10px' } }, daily, h('div', { class: 'grow' }, h('b', { class: 'small' }, 'Every morning'),
        h('div', { class: 'small muted' }, 'Make fresh insights once a day')), hour),
      h('div', { class: 'row', style: { gap: '10px' } }, tips, h('div', { class: 'grow' }, h('b', { class: 'small' }, 'AI tips on new reports'),
        h('div', { class: 'small muted' }, 'Steps and what to bring, for the janitors (up to 25 a day)'))),
      ...form.body,
      h('div', { class: 'actions' }, remove, h('button', { class: 'btn', type: 'button', onclick: () => close() }, 'Close'), form.save)];
  }, { wide: true });
}

// ------------------------------------------------------------------ result pieces
function section(title, iconName, items, row) {
  if (!items || !items.length) return null;
  return h('section', { class: 'card ins-card' }, h('div', { class: 'ins-title' }, icon(iconName), h('b', null, title), h('span', { class: 'n' }, items.length)),
    h('div', { class: 'ins-list' }, items.map(row)));
}
const line = (main, sub, extra) => h('div', { class: 'ins-row' }, h('div', { class: 'grow' }, h('div', { class: 'ins-main' }, main),
  sub ? h('div', { class: 'ins-sub' }, sub) : null), extra || null);
const joinBits = (...bits) => bits.filter(Boolean).join(' · ');

function todo(d) {
  const p = (r) => PRIORITY[r.priority] || PRIORITY.medium;
  return section('What to do', 'check', d.recommendations, (r) => h('div', { class: `ins-row rec ${r.priority}` },
    h('span', { class: `chip ${p(r)[1]}` }, p(r)[0]),
    h('div', { class: 'grow' }, h('div', { class: 'ins-main' }, r.action), h('div', { class: 'ins-sub' }, joinBits(r.where, r.when)),
      r.reason ? h('div', { class: 'ins-why' }, r.reason) : null)));
}

function results(d) {
  return [
    h('div', { class: 'ins-grid' },
      section('Patterns', 'chart', d.patterns, (x) => line(x.finding, joinBits(x.evidence, (x.locations || []).join(', ')))),
      section('Sensors', 'sensors', d.sensorNotes, (x) => line(x.finding, joinBits(x.where, x.suggestion))),
      section('Supplies', 'inbox', d.supplyPlan, (x) => line(`${x.item}${x.where ? ` — ${x.where}` : ''}`, x.suggestion)),
      section('Repairs', 'pin', d.facilityRepairs, (x) => line(`${x.problem}${x.where ? ` — ${x.where}` : ''}`, x.suggestion))),
    d.dataQuality && d.dataQuality.length
      ? h('details', { class: 'ins-dq' }, h('summary', null, 'Notes about the data'), h('ul', null, d.dataQuality.map((t) => h('li', null, t)))) : null,
  ];
}

function askBox() {
  const input = h('input', { class: 'input', type: 'text', maxlength: 300, placeholder: 'Ask about your restrooms…', 'aria-label': 'Ask the AI a question' });
  const btn = h('button', { class: 'btn primary', type: 'button' }, icon('sparkle'), 'Ask');
  const out = h('div', { class: 'ins-answers' });
  const chips = h('div', { class: 'chips ins-sugg' }, SUGGESTIONS.map((q) => h('button', { class: 'chipbtn sm', type: 'button',
    onclick: () => { input.value = q; ask(); } }, q)));
  async function ask() {
    const q = input.value.trim();
    if (!q) { input.focus(); return; }
    const card = h('div', { class: 'ins-answer thinking' }, h('div', { class: 'q' }, q), h('div', { class: 'a' }, h('span', { class: 'typing' }, h('i'), h('i'), h('i'))));
    out.prepend(card);
    while (out.children.length > 3) out.lastElementChild.remove();
    input.value = '';
    await busy(btn, 'Thinking…', async () => {
      try {
        const r = await post('/admin/insights/ask', { question: q }, { timeout: 620000 });
        card.classList.remove('thinking');
        card.lastElementChild.replaceWith(h('div', { class: 'a' }, h('p', null, r.answer),
          r.points.length ? h('ul', null, r.points.map((t) => h('li', null, t))) : null,
          h('div', { class: 'ins-meta' }, `AI answer · from ${r.basedOn === 'both' ? 'reports and sensors' : r.basedOn || 'your data'} · check before acting`)));
      } catch (err) {
        card.classList.remove('thinking');
        card.lastElementChild.replaceWith(h('div', { class: 'a msg error' }, err.message));
      }
    });
  }
  btn.addEventListener('click', ask);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); ask(); } });
  return h('section', { class: 'card ins-ask' }, h('div', { class: 'row', style: { gap: '8px' } }, input, btn), chips, out);
}

// ------------------------------------------------------------------ the page
export function insightsView() {
  const run = h('button', { class: 'btn primary sm', type: 'button' }, icon('sparkle'), 'Make new insights');
  const gear = h('button', { class: 'btn sm ghost', type: 'button', 'aria-label': 'AI settings' }, icon('settings'), h('span', { class: 'hide-xs' }, 'AI settings'));
  const pdf = h('button', { class: 'btn sm ghost no-print', type: 'button', 'aria-label': 'Save as PDF' }, icon('download'), h('span', { class: 'hide-xs' }, 'PDF'));
  const body = h('div', { class: 'stack' });
  const view = h('div', { class: 'stack enter insights' },
    h('header', { class: 'page-head' }, h('a', { class: 'back no-print', href: '#/reports', 'aria-label': 'Back to reports' }, '‹'),
      h('div', null, h('h1', null, 'AI insights'), h('div', { class: 'sub' }, 'Suggestions from your reports and sensors')),
      h('div', { class: 'grow' }), h('div', { class: 'row no-print', style: { gap: '6px' } }, gear, pdf, run)),
    body);
  let st = null;
  let poll = null;
  let askEl = null;

  pdf.addEventListener('click', () => {
    if (shell.isPi) shell.send('pdf', { name: `AureoClean-insights-${new Date().toISOString().slice(0, 10)}.pdf` });
    else window.print();
  });
  gear.addEventListener('click', () => st && settingsDialog(st.ai, load));
  run.addEventListener('click', () => busy(run, 'Starting…', async () => {
    try { await post('/admin/insights/run'); await load(); } catch (err) { toast(err.message, 'error'); }
  }));

  function progress() {
    const secs = st.startedAt ? Math.max(0, Math.round((Date.now() - new Date(st.startedAt).getTime()) / 1000)) : 0;
    return h('section', { class: 'card ins-progress', role: 'status' }, h('div', { class: 'ins-orb' }, icon('sparkle')),
      h('div', { class: 'grow' }, h('b', null, 'Reading your reports and sensors…'),
        h('div', { class: 'small muted' }, `${secs} s · ${st.ai.providerLabel} · usually under a minute${st.ai.provider === 'openai' ? ' (longer on the Pi itself)' : ''}`),
        h('div', { class: 'ins-bar' }, h('i'))));
  }

  function draw() {
    const ai = st.ai;
    run.classList.toggle('hidden', !ai.configured);
    gear.classList.toggle('hidden', !ai.configured);
    pdf.classList.toggle('hidden', !st.latest);
    run.disabled = st.running;
    if (!ai.configured) {
      const form = settingsForm(ai, { onSaved: () => load(), inline: true });
      body.replaceChildren(h('section', { class: 'card ins-setup stack' },
        h('div', { class: 'row', style: { gap: '12px', alignItems: 'flex-start' } }, h('div', { class: 'ins-orb' }, icon('sparkle')),
          h('div', null, h('h2', { style: { margin: '0', fontSize: '17px' } }, 'Set up the AI once'),
            h('p', { class: 'small muted', style: { margin: '2px 0 0' } }, 'It reads the reports and sensor readings and suggests what the team should do. Google Gemini has a free key — no card needed.'),
            h('p', { class: 'small', style: { margin: '6px 0 0', color: 'var(--blue-dark)' } }, 'Tip: it\'s easiest from your phone — open AureoClean on the Wi-Fi website, sign in as admin, and paste the key here. You only do this once.'))),
        ...form.body, h('div', { class: 'actions' }, form.save)));
      return;
    }
    const d = st.latest;
    const parts = [];
    if (st.running) parts.push(progress());
    else if (st.lastError) parts.push(h('div', { class: 'msg error' }, st.lastError));
    if (d) {
      parts.push(h('section', { class: 'card ins-hero' },
        h('div', { class: 'ins-hero-top' }, icon('sparkle'), h('span', null, 'AI summary')),
        h('p', { class: 'ins-headline' }, d.headline),
        h('div', { class: 'ins-meta' }, joinBits(`Made ${ago(d.createdAt)}${d.reason === 'daily' ? ' (daily)' : ''}`,
          `${d.reports} report${d.reports === 1 ? '' : 's'} (${d.days} days)`,
          d.sensorsConnected ? `${d.sensorValues} sensor values` : 'sensors not connected', `${d.model}`))));
    } else if (!st.running) {
      parts.push(h('section', { class: 'card ins-empty' }, h('div', { class: 'ins-orb' }, icon('sparkle')), h('b', null, 'No insights yet'),
        h('span', { class: 'small muted' }, 'Press “Make new insights” — the AI reads the last 30 days of reports and the live sensor readings.')));
    }
    askEl = askEl || askBox();
    if (d) parts.push(todo(d));
    parts.push(askEl);
    if (d) parts.push(...results(d));
    parts.push(h('p', { class: 'small muted ins-foot' }, icon('shield'),
      'AI suggestions can be wrong — check before acting. Emergency alerts always come from the fixed alert rules, never from the AI.'));
    body.replaceChildren(...parts.filter(Boolean));
  }

  async function load() {
    try {
      st = await get('/admin/insights', { timeout: 30000 });
      draw();
      clearTimeout(poll);
      if (st.running) poll = setTimeout(() => { if (view.isConnected) load(); }, 2000);
    } catch (err) {
      body.replaceChildren(h('div', { class: 'msg error' }, err.message));
    }
  }
  body.replaceChildren(h('div', { class: 'card' }, h('div', { class: 'skeleton', style: { height: '18px', width: '60%' } }),
    h('div', { class: 'skeleton', style: { height: '14px', width: '85%', marginTop: '10px' } })));
  load();
  view.cleanup = () => clearTimeout(poll);
  return view;
}

// ------------------------------------------------------------------ janitors: My jobs
export function tipsCard() {
  const card = h('section', { class: 'card ins-tips hidden', 'aria-label': 'Suggested today' });
  get('/insights/today', { timeout: 20000 }).then((t) => {
    if (!t.available) return;
    card.replaceChildren(h('div', { class: 'ins-title' }, icon('sparkle'), h('b', null, 'Suggested today'), h('span', { class: 'small muted' }, `AI · ${ago(t.createdAt)}`)),
      h('div', { class: 'ins-list' }, t.tips.map((x) => h('div', { class: 'ins-row' },
        h('span', { class: `dot ${x.priority === 'high' ? 'fail' : 'warn'}` }),
        h('div', { class: 'grow' }, h('div', { class: 'ins-main' }, x.action), h('div', { class: 'ins-sub' }, joinBits(x.where, x.when)))))));
    card.classList.remove('hidden');
  }).catch(() => {});
  return card;
}

// ================================================================== AI inside reporting
const HELPER = { maintenance: 'Maintenance', plumber: 'a plumber', electrician: 'an electrician', security: 'Security',
  'nurse/clinic': 'the clinic', supplier: 'the supplier' };

/** The AI tip on a report (report page). `r` is the report; `after()` reloads it. */
export function aiTipCard(r, after) {
  const t = r.ai;
  const btn = h('button', { class: `btn sm ${t ? 'ghost' : 'primary'}`, type: 'button' }, icon(t ? 'reload' : 'sparkle'), t ? 'New tip' : 'Get an AI tip');
  const msg = h('div', { class: 'msg hidden' });
  btn.addEventListener('click', () => busy(btn, 'Thinking…', async () => {
    try { await post(`/reports/${encodeURIComponent(r.id)}/ai`, {}, { timeout: 620000 }); invalidate('reports', 'jobs'); after(); }
    catch (err) { msg.textContent = err.message; msg.className = 'msg error'; }
  }));
  const head = h('div', { class: 'ins-title' }, icon('sparkle'), h('b', null, 'AI tip'), h('div', { class: 'grow' }), btn);
  if (!t) {
    return h('section', { class: 'card ins-card ai-tip' }, head,
      h('p', { class: 'small muted', style: { margin: '0' } }, 'Likely cause, steps, what to bring and how long it takes — from this report, similar past reports and the sensors.'), msg);
  }
  return h('section', { class: 'card ins-card ai-tip' }, head,
    t.summary ? h('p', { class: 'ai-sum' }, t.summary) : null,
    t.safety ? h('div', { class: 'ai-safety' }, icon('shield'), t.safety) : null,
    h('div', { class: 'ai-facts' },
      t.likelyCause ? h('div', null, h('span', null, 'Likely cause'), h('b', null, t.likelyCause)) : null,
      t.minutes ? h('div', null, h('span', null, 'Takes about'), h('b', null, `${t.minutes} min`)) : null,
      t.helper ? h('div', null, h('span', null, 'May need'), h('b', null, HELPER[t.helper] || t.helper)) : null),
    t.bring && t.bring.length ? h('div', { class: 'ai-bring' }, h('span', { class: 'small muted' }, 'Bring'), t.bring.map((x) => h('span', { class: 'chip navy' }, x))) : null,
    t.steps && t.steps.length ? h('ol', { class: 'ai-steps' }, t.steps.map((x) => h('li', null, x))) : null,
    t.pattern ? h('div', { class: 'ai-pattern small' }, icon('chart'), t.pattern) : null,
    h('div', { class: 'ins-meta' }, `AI suggestion · ${ago(t.at)} · check before acting`), msg);
}

/** Compact AI tip on a janitor's job card. */
export function aiJobTip(ai) {
  if (!ai || (!ai.summary && !(ai.steps || []).length)) return null;
  const bring = (ai.bring || []).slice(0, 5).join(', ');
  return h('details', { class: 'ai-job' },
    h('summary', null, icon('sparkle'), h('span', null, bring ? `Bring: ${bring}` : 'AI tip'), ai.minutes ? h('span', { class: 'muted' }, ` · ~${ai.minutes} min`) : null),
    ai.safety ? h('div', { class: 'ai-safety' }, icon('shield'), ai.safety) : null,
    (ai.steps || []).length ? h('ol', { class: 'ai-steps' }, ai.steps.map((x) => h('li', null, x))) : null,
    h('div', { class: 'ins-meta' }, 'AI suggestion — check before acting'));
}

/** "Say it in your own words" — the AI fills the report form; the person checks and sends. */
export function aiFillBox(onResult) {
  const input = h('input', { class: 'input', type: 'text', maxlength: 400, 'aria-label': 'Describe the problem',
    placeholder: 'e.g. baradong inidoro sa 2nd floor male, 2nd stall' });
  const btn = h('button', { class: 'btn', type: 'button' }, icon('sparkle'), 'Fill in for me');
  const note = h('div', { class: 'small muted' }, 'Type it like a text message (English or Tagalog). The AI fills in the form below — check it, then send.');
  const run = () => busy(btn, 'Reading…', async () => {
    try {
      const r = await post('/reports/ai-fill', { text: input.value }, { timeout: 620000 });
      onResult(r, input.value);
      note.textContent = r.possibleEmergency ? DANGER_NOTE : 'Filled in by AI — check the restroom and problems below, then send.';
      note.className = r.possibleEmergency ? 'msg warn' : 'small ai-filled';
    } catch (err) { note.textContent = err.message; note.className = 'small error-text'; }
  });
  btn.addEventListener('click', run);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); run(); } });
  return h('section', { class: 'ai-fill' }, h('div', { class: 'ai-fill-head' }, icon('sparkle'), h('b', null, 'Say it in your own words')),
    h('div', { class: 'row', style: { gap: '8px' } }, input, btn), note);
}

export const DANGER_NOTE = 'This sounds like it could be dangerous. If it is, choose Emergency yourself — that texts everyone right away.';

/** The written AI summary for the Charts period (and its PDF). */
export function periodSummaryCard(days) {
  const card = h('section', { class: 'card ins-card ai-period' });
  function draw(d) {
    const s = d.summary;
    const btn = h('button', { class: `btn sm no-print ${s ? 'ghost' : 'primary'}`, type: 'button' }, icon(s ? 'reload' : 'sparkle'), s ? 'Rewrite' : 'Write AI summary');
    btn.addEventListener('click', () => busy(btn, 'Writing…', async () => {
      try { draw({ summary: await post('/admin/reports/summary/ai', { days, force: !!s }, { timeout: 620000 }), aiReady: true }); }
      catch (err) { toast(err.message, 'error'); }
    }));
    const head = h('div', { class: 'ins-title' }, icon('sparkle'), h('b', null, s && s.title ? s.title : `AI summary · last ${days} days`), h('div', { class: 'grow' }),
      d.aiReady ? btn : h('a', { class: 'small no-print', href: '#/insights' }, 'Set up the AI ›'));
    if (!s) {
      card.replaceChildren(head, h('p', { class: 'small muted', style: { margin: '0' } },
        d.aiReady ? 'A short written report of this period — what went well, what to watch, what to do next. Included in the PDF.'
          : 'Set up AI insights to get a written report of this period.'));
      card.classList.add('no-print');
      return;
    }
    card.classList.remove('no-print');
    const list = (title, items) => (items && items.length ? h('div', { class: 'ai-col' }, h('div', { class: 'eyebrow' }, title), h('ul', null, items.map((x) => h('li', null, x)))) : null);
    card.replaceChildren(head, h('p', { class: 'ai-overview' }, s.overview),
      h('div', { class: 'ai-cols' }, list('Went well', s.highlights), list('Watch', s.concerns), list('Next steps', s.nextSteps)),
      h('div', { class: 'ins-meta' }, `Written by AI ${ago(s.createdAt)} from ${s.reports} reports · check before sharing`));
  }
  card.replaceChildren(h('div', { class: 'skeleton', style: { height: '16px', width: '50%' } }));
  get(`/admin/reports/summary/ai?days=${days}`).then(draw).catch(() => card.replaceChildren());
  return card;
}

/** One line on the Reports page: the latest AI summary, linking to AI insights. */
export function aiHeadline() {
  const a = h('a', { class: 'ai-headline hidden', href: '#/insights' });
  if (!session.user || session.user.role !== 'admin') return a;
  cached('insights-head', () => get('/admin/insights'), 120000).then((st) => {
    const d = st.latest;
    if (!d || Date.now() - new Date(d.createdAt).getTime() > 36 * 3600000) return;
    a.replaceChildren(icon('sparkle'), h('span', { class: 'grow' }, h('b', null, 'AI: '), d.headline), h('span', { class: 'small' }, 'Insights ›'));
    a.classList.remove('hidden');
  }).catch(() => {});
  return a;
}
