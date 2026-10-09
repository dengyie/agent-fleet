import {accountRequest} from './api/accounts.js';
import {mountAccount} from './views/account.js';
/* Composition only: authenticate before mounting any feature or stream. */
import * as api from './api/client.js';
import * as platform from './api/platform.js';
import { FleetStore } from './state/store.js';
import { SseClient } from './realtime/sse.js';
import { resolveRoute, mountNavigation } from './shell/navigation.js';
import { mountFleet } from './views/fleet.js';
import { mountMachine } from './views/machine.js';
import { mountTask } from './views/task.js';
import { mountSession } from './views/session.js';
import { mountAssistant } from './views/assistant.js';
import { mountMonitoring } from './views/monitoring.js';
import { mountLogin, redirectToLogin } from './shell/operator.js';
import './ui/ChatPromptInput.js';
import './ui/StreamMarkdown.js';
import './ui/ToolCallBadge.js';
import './ui/AutoScrollAnchor.js';

function protectConsole() {
  const client = {...api, ...platform};
  const store = new FleetStore();
  let route = resolveRoute();
  const sse = new SseClient(store, {client});
  const shell = document.querySelector('.console-layout');
  const gate = document.getElementById('access-state');
  const retry = gate.querySelector('button');
  let teardown, disposeNavigation, mounted = false, checking = false, redirecting = false, account = null;
  let lastSessionCheckAt = 0;
  function disposeRoute() {
    if (typeof teardown === 'function') teardown();
    teardown = undefined;
    document.getElementById('route-view').replaceChildren();
  }
  function disposeView() {
    shell.hidden = true; sse.stop();
    if (disposeNavigation) disposeNavigation();
    disposeNavigation = undefined;
    disposeRoute();
    for (const id of ['route-view', 'sidebar-conversations', 'sidebar-nodes']) {
      document.getElementById(id).replaceChildren();
    }
  }
  function requireLogin() {
    if (redirecting) return;
    redirecting = true; disposeView();
    redirectToLogin();
  }
  function mountRoute() {
    const target = document.getElementById('route-view');
    target.replaceChildren();
    const mountEntity = {machine: mountMachine, task: mountTask, session: mountSession};
    if (route.page === 'account') teardown = mountAccount(target);
    else if (route.page === 'fleet') teardown = mountFleet(target, store, client);
    else if (route.page === 'assistant') teardown = mountAssistant(target, {conversationId: route.id, account});
    else if (route.page === 'monitoring') teardown = mountMonitoring(target, client);
    else if (mountEntity[route.page] && route.id) teardown = mountEntity[route.page](target, route.id, store, client);
    else target.textContent = '页面不存在或缺少标识，请从导航重新进入。';
  }
  function renderRoute(nextRoute) {
    if (typeof teardown === 'function') teardown();
    teardown = undefined;
    route = nextRoute;
    if (disposeNavigation && typeof disposeNavigation.updateRoute === 'function') {
      disposeNavigation.updateRoute(route);
    }
    mountRoute();
  }
  function navigateTo(url, {replace = false, fromHistory = false} = {}) {
    let target;
    try { target = new URL(url, window.location.href); } catch (_) { return false; }
    if (target.origin !== window.location.origin) return false;
    let nextRoute = resolveRoute(target);
    if (nextRoute.page === 'invalid') return false;
    const forbiddenForAccount = account && account.role !== 'admin' && !['assistant', 'account'].includes(nextRoute.page);
    if (forbiddenForAccount) {
      target = new URL('/assistant', window.location.origin);
      nextRoute = resolveRoute(target);
      replace = true;
    }
    const nextUrl = target.pathname + target.search + target.hash;
    if ((!fromHistory || forbiddenForAccount) && nextUrl !== window.location.pathname + window.location.search + window.location.hash) {
      window.history[replace ? 'replaceState' : 'pushState'](null, '', nextUrl);
    }
    if (mounted && (route.page !== nextRoute.page || route.id !== nextRoute.id || fromHistory)) {
      renderRoute(nextRoute);
    } else {
      route = nextRoute;
    }
    return true;
  }
  function onNavigationClick(event) {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const anchor = event.target instanceof Element ? event.target.closest('a[href]') : null;
    if (!anchor || anchor.hasAttribute('download') || (anchor.target && anchor.target !== '_self')) return;
    let target;
    try { target = new URL(anchor.href, window.location.href); } catch (_) { return; }
    if (target.origin !== window.location.origin || (target.pathname === window.location.pathname &&
        target.search === window.location.search)) return;
    if (resolveRoute(target).page === 'invalid') return;
    event.preventDefault();
    navigateTo(target);
  }
  function onFleetNavigate(event) {
    const detail = event.detail || {};
    if (typeof detail.path === 'string') navigateTo(detail.path, {replace: detail.replace === true});
  }
  function onPopState() { navigateTo(window.location.href, {fromHistory: true}); }
  document.addEventListener('click', onNavigationClick);
  window.addEventListener('fleet:navigate', onFleetNavigate);
  window.addEventListener('popstate', onPopState);
  function mount() {
    disposeNavigation = mountNavigation(route, store);
    mountRoute();
    if ((!account || account.role === 'admin') && route.page !== 'fleet') client.getStatus().then(status => store.setStatus(status)).catch(() => {
      document.getElementById('sidebar-nodes').textContent = '节点列表暂时不可用';
    });
    mounted = true;
  }
  async function verifyAccess({force = false} = {}) {
    const now = Date.now();
    if (checking || redirecting || (!force && now - lastSessionCheckAt < 60000)) return;
    checking = true;
    lastSessionCheckAt = now;
    try {
      const session = await api.getOperatorSession();
      if (redirecting) return;
      const nextAccount = session.user || null;
      if (mounted && (account?.id !== nextAccount?.id || account?.role !== nextAccount?.role)) {
        // A session may have changed in another tab. The old view, async work,
        // navigation and store belong to that identity; recreate the document.
        redirecting = true;
        disposeView();
        window.location.reload();
        return;
      }
      account = nextAccount;
      document.querySelector('[data-nav="account"]').hidden = !account;
      if (account && account.role !== "admin") {
        document.querySelector('[data-nav="fleet"]').hidden = true;
        document.querySelector('[data-nav="monitoring"]').hidden = true;
        document.getElementById("sidebar-nodes").textContent = "节点由管理员分配";
        if (!["assistant", "account"].includes(route.page)) {
          window.history.replaceState(null, '', '/assistant');
          route = resolveRoute();
        }
      }
      if (redirecting) return;
      if (!mounted) mount();
      gate.hidden = true; shell.hidden = false; if (!account || account.role === "admin") sse.start();
    } catch (error) {
      if (error.status === 401) requireLogin();
      else if (!mounted) document.getElementById('route-view').textContent =
        '工作空间暂时无法连接，稍后会自动重试。';
    } finally { checking = false; }
  }
  window.addEventListener('fleet-auth-required', requireLogin);
  document.getElementById('operator-logout').addEventListener('click', async () => {
    try { if (account) await accountRequest('logout', {}); requireLogin(); }
    catch (error) { gate.hidden = false; gate.querySelector('p').textContent = '退出登录失败，请重试'; }
  });
  document.getElementById('refresh-page').addEventListener('click', () => window.location.reload());
  retry.addEventListener('click', () => verifyAccess({force: true}));
  window.addEventListener('focus', () => verifyAccess());
  const sessionTimer = window.setInterval(async () => {
    if (document.hidden || checking || redirecting) return;
    await verifyAccess();
  }, 60000);
  window.addEventListener('pagehide', event => {
    shell.hidden = true; sse.stop();
    if (!event.persisted) { window.clearInterval(sessionTimer); disposeView(); }
  });
  window.addEventListener('pageshow', event => { if (event.persisted) verifyAccess(); });
  gate.hidden = true;
  shell.hidden = false;
  verifyAccess({force: true});
}

if (window.location.pathname === '/login') mountLogin();
else protectConsole();
