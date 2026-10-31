// AureoClean web app: start-up, layout (sidebar on the Pi / laptops, tab bar on
// phones), routing and background refresh.
import { accountView } from './account.js';
import { chartsView, healthPill, homeView, peopleView, reportDetailView, reportsView, systemView } from './admin.js';
import { activateView, adminInviteView, joinView, loginView, statusView } from './auth.js';
import {
  cached, closeAllModals, currentPath, get, go, h, icon, invalidate, matchRoute, post, route, session,
  setSessionEndedHandler, shell,
} from './core.js';
import { jobsView, reportProblemView, sensorsView } from './janitor.js';
import { initLock, isLocked, lockNow } from './lock.js';
import { insightsView } from './insights.js';
import { initKeyboard } from './keyboard.js';
import { liveView } from './sensors.js';
import { alertsView } from './alerts.js';
import { binView } from './data.js';

const PUBLIC = new Set(['/login', '/join', '/activate', '/admin-invite', '/status']);

route('/login', loginView);
route('/join', joinView);
route('/activate', activateView);
route('/admin-invite', adminInviteView);
route('/status', statusView);
route('/home', homeView);
route('/reports', reportsView);
route('/reports/:id', reportDetailView);
route('/charts', chartsView);
route('/insights', insightsView);
route('/people', peopleView);
route('/system', systemView);
route('/account', accountView);
route('/jobs', jobsView);
route('/report', reportProblemView);
route('/sensors', sensorsView);
route('/live', liveView);
route('/alerts', alertsView);
route('/bin', binView);

const ADMIN_NAV = [
  { path: '/home', label: 'Home', icon: 'home', tab: 'Home', also: ['/live'] },
  { group: 'Operations' },
  { path: '/reports', label: 'Reports', icon: 'reports', badge: 'reports', also: ['/charts', '/insights', '/bin'], tab: 'Reports' },
  { path: '/alerts', label: 'Alerts', icon: 'bell', badge: 'alerts', tab: 'Alerts' },
  { group: 'People' },
  { path: '/people', label: 'People', icon: 'people', badge: 'people', tab: 'People' },
  { group: 'System' },
  { path: '/system', label: 'System', icon: 'pulse', badge: 'system', tab: 'System' },
];
const JANITOR_NAV = [
  { path: '/sensors', label: 'Home', icon: 'home', tab: 'Home', also: ['/live'] },
  { path: '/jobs', label: 'My jobs', icon: 'reports', badge: 'jobs', tab: 'Jobs' },
  { path: '/report', label: 'Report a problem', icon: 'plus', tab: 'Report' },
];

const app = document.getElementById('app');
let layout = null; // {root, main, links, badges, role}
let current = null;
let pollTimer = null;

// ------------------------------------------------------------------ session
async function signOut(note) {
  const hadToken = !!session.token;
  if (hadToken) { try { await post('/auth/logout', {}, { timeout: 8000 }); } catch { /* already gone */ } }
  endSession();
  go(note ? `/login?ended=${encodeURIComponent(note)}` : '/login', { replace: true });
}
function endSession() {
  session.set(null);
  session.user = null;
  layout = null;
  invalidate();
  closeAllModals();
}
setSessionEndedHandler((message) => {
  if (!session.user && !session.token) return;
  endSession();
  go(`/login?ended=${encodeURIComponent(message || 'Please sign in again.')}`, { replace: true });
});

// ------------------------------------------------------------------ layout
function buildLayout() {
  const role = session.user.role;
  const items = role === 'admin' ? ADMIN_NAV : JANITOR_NAV;
  const links = [];
  const badges = {};
  const navLink = (it, cls) => {
    const badge = it.badge ? h('span', { class: 'badge hidden' }) : null;
    if (badge) (badges[it.badge] = badges[it.badge] || []).push(badge);
    const a = h('a', { href: `#${it.path}`, class: cls, dataset: { path: it.path, also: (it.also || []).join(',') } },
      icon(it.icon), cls === 'tab' ? it.tab : it.label, badge);
    links.push(a);
    return a;
  };
  const lockBtn = h('button', { class: 'navbtn', type: 'button', onclick: () => lockNow() }, icon('lock'), 'Lock');
  const syncKiosk = () => lockBtn.classList.toggle('hidden', !(shell.isPi && shell.kiosk));
  syncKiosk();
  shell.listeners.add(syncKiosk);

  const sidebar = h('aside', { class: 'sidebar', 'aria-label': 'Main menu' },
    h('a', { class: 'brand', href: `#${items[0].path}` }, h('div', { class: 'mark' }, h('img', { src: '/assets/logo.svg', alt: '' })),
      h('div', { class: 'name' }, h('span', null, 'Aureo'), 'Clean')),
    h('nav', { class: 'nav' }, items.map((it) => (it.group ? h('div', { class: 'group' }, it.group) : navLink(it)))),
    h('div', { class: 'spacer' }),
    h('nav', { class: 'nav' }, navLink({ path: '/account', label: 'My account', icon: 'user' })),
    h('div', { class: 'foot' }, lockBtn,
      h('button', { class: 'navbtn', type: 'button', onclick: () => signOut() }, icon('logout'), 'Sign out')));
  // Phones: the top bar shows the brand; the page itself has the big title.
  const title = h('div', { class: 'title' }, h('span', { class: 'gold-text' }, 'Aureo'), 'Clean');
  const topbar = h('header', { class: 'topbar' }, h('div', { class: 'mark' }, h('img', { src: '/assets/logo.svg', alt: '' })), title,
    h('button', { type: 'button', class: shell.isPi ? '' : 'hidden', onclick: () => lockNow(), 'aria-label': 'Lock' }, icon('lock')));
  const tabItems = items.filter((it) => it.path).concat([{ path: '/account', label: 'Me', icon: 'user', tab: 'Me' }]);
  const tabbar = h('nav', { class: 'tabbar', 'aria-label': 'Main menu' }, tabItems.map((it) => navLink(it, 'tab')));
  const main = h('main', { class: 'main', id: 'main' });
  const root = h('div', { class: 'shell' }, sidebar, h('div', { class: 'grow', style: { display: 'flex', flexDirection: 'column', minWidth: '0', height: '100%' } },
    topbar, main, tabbar));
  return { root, main, links, badges, role, title, items: tabItems };
}

function setBadge(key, value, kind = '') {
  for (const b of (layout && layout.badges[key]) || []) {
    b.textContent = value;
    b.className = `badge ${kind}${value ? '' : ' hidden'}`;
  }
}

// ------------------------------------------------------------------ routing
async function render() {
  const path = currentPath();
  const pathname = path.split('?')[0];
  if (!session.user && session.token) {
    try { session.user = await get('/auth/me'); } catch { session.set(null); }
  }
  const signedIn = !!session.user;
  if (!signedIn && !PUBLIC.has(pathname)) { go('/login', { replace: true }); return; }
  const home = signedIn ? (session.user.role === 'admin' ? '/home' : '/sensors') : '/login';
  if (signedIn && (PUBLIC.has(pathname) || pathname === '/')) { go(home, { replace: true }); return; }
  const match = matchRoute(path);
  if (!match) { go(home, { replace: true }); return; }
  if (signedIn && session.user.role !== 'admin' && ['/home', '/reports', '/charts', '/insights', '/people', '/system', '/alerts', '/bin'].some((p) => pathname.startsWith(p))) {
    go('/sensors', { replace: true }); return;
  }
  if (signedIn && session.user.role === 'admin' && ['/jobs', '/report', '/sensors'].includes(pathname)) { go('/home', { replace: true }); return; }

  if (current && current.cleanup) current.cleanup();
  closeAllModals();
  const ctx = { signOut };
  let view;
  try { view = match.view(match.params, match.query, ctx); } catch (err) { console.error(err); view = h('div', { class: 'msg error' }, err.message); }
  current = view;

  if (!signedIn) {
    layout = null;
    app.replaceChildren(view);
    stopPolling();
    return;
  }
  if (!layout || layout.role !== session.user.role) {
    layout = buildLayout();
    app.replaceChildren(layout.root);
    startPolling();
  }
  for (const a of layout.links) {
    const on = pathname === a.dataset.path || pathname.startsWith(a.dataset.path + '/') || (a.dataset.also || '').split(',').includes(pathname);
    a.toggleAttribute('aria-current', on);
    if (on) a.setAttribute('aria-current', 'page');
  }
  const active = layout.items.find((it) => pathname === it.path || pathname.startsWith(it.path + '/') || (it.also || []).includes(pathname));
  layout.main.replaceChildren(view);
  layout.main.scrollTop = 0;
  document.title = `${active ? (active.label === 'Me' ? 'My account' : active.label) : 'Home'} · AureoClean`;
}

// ------------------------------------------------------------------ background refresh
function startPolling() {
  stopPolling();
  poll();
  pollTimer = setInterval(() => { if (document.visibilityState === 'visible' && !isLocked()) poll(); }, 30000);
}
function stopPolling() { if (pollTimer) clearInterval(pollTimer); pollTimer = null; }
let ticks = 0;
async function poll() {
  if (!session.user) return;
  ticks += 1;
  try {
    if (session.user.role === 'admin') {
      const ov = await get('/admin/overview?cached=true');
      setBadge('reports', ov.openReports || '');
      setBadge('people', ov.pendingRequests || '', 'soft');
      setBadge('alerts', ov.activeAlerts || '', 'alert');
      if (ticks % 4 === 1) {
        invalidate('health');
        const r = await healthPill(h('span'));
        if (r) setBadge('system', r.status === 'ok' ? '' : '!', 'alert');
      }
    } else {
      invalidate('jobs');
      const jobs = await cached('jobs', () => get('/janitor/jobs'), 0);
      setBadge('jobs', jobs.open.length || '');
    }
    if (current && current.refresh && ticks > 1) current.refresh();
  } catch { /* shown on the screens themselves */ }
}

// ------------------------------------------------------------------ start
window.addEventListener('hashchange', render);
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible' && session.user) poll(); });
shell.listeners.add((m) => { if (m.event === 'kiosk' && current && current.refresh) current.refresh(); });
initLock((note) => signOut(note));
initKeyboard();
if (shell.isPi) {
  document.documentElement.classList.add('on-pi');
  shell.send('ready');
}
render();
