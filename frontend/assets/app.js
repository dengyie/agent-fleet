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
  const routeViews = new Map();
  const MAX_CACHED_ROUTES = 5;
  let activeRouteView = null, disposeNavigation, mounted = false, checking = false, redirecting = false, account = null;
  let lastSessionCheckAt = 0;
  function routeKey(value) {
    return value.page + ':' + (value.id || '');
  }
  function suspendRouteView(view) {
    if (!view || view.suspended) return;
    view.suspended = true;
    view.host.hidden = true;
    view.host.inert = true;
    if (view.teardown && typeof view.teardown.suspend === 'function') view.teardown.suspend();
    view.host.remove();
  }
  function resumeRouteView(view) {
    if (!view || !view.suspended) return;
    view.suspended = false;
    view.host.hidden = false;
    view.host.inert = false;
    document.getElementById('route-view').appendChild(view.host);
    if (view.teardown && typeof view.teardown.resume === 'function') view.teardown.resume();
  }
  function destroyRouteView(view) {
    if (!view) return;
    if (typeof view.teardown === 'function') view.teardown();
    view.host.remove();
    routeViews.delete(view.key);
    if (activeRouteView === view) activeRouteView = null;
  }
  function disposeView() {
    shell.hidden = true; sse.stop();
    if (disposeNavigation) disposeNavigation();
    disposeNavigation = undefined;
    for (const view of routeViews.values()) {
      if (typeof view.teardown === 'function') view.teardown();
      view.host.remove();
    }
    routeViews.clear();
    activeRouteView = null;
    mounted = false;
    for (const id of ['route-view', 'sidebar-conversations', 'sidebar-nodes']) {
      document.getElementById(id).replaceChildren();
    }
  }
  function requireLogin() {
    if (redirecting) return;
    redirecting = true; disposeView();
    redirectToLogin();
  }
  function mountRoute(view) {
    const target = view.host;
    const mountEntity = {machine: mountMachine, task: mountTask, session: mountSession};
    if (route.page === 'account') view.teardown = mountAccount(target);
    else if (route.page === 'fleet') view.teardown = mountFleet(target, store, client);
    else if (route.page === 'assistant') view.teardown = mountAssistant(target, {conversationId: route.id, account});
    else if (route.page === 'monitoring') view.teardown = mountMonitoring(target, client);
    else if (mountEntity[route.page] && route.id) view.teardown = mountEntity[route.page](target, route.id, store, client);
    else target.textContent = '页面不存在或缺少标识，请从导航重新进入。';
  }
  function renderRoute(nextRoute) {
    const key = routeKey(nextRoute);
    if (activeRouteView && activeRouteView.key === key) {
      route = nextRoute;
      if (disposeNavigation && typeof disposeNavigation.updateRoute === 'function') disposeNavigation.updateRoute(route);
      return;
    }
    suspendRouteView(activeRouteView);
    route = nextRoute;
    if (disposeNavigation && typeof disposeNavigation.updateRoute === 'function') {
      disposeNavigation.updateRoute(route);
    }
    let view = routeViews.get(key);
    if (view) {
      routeViews.delete(key);
      routeViews.set(key, view);
      resumeRouteView(view);
    } else {
      const host = document.createElement('div');
      host.className = 'route-view-entry';
      host.dataset.routeCacheEntry = key;
      view = {key, host, teardown: undefined, suspended: false};
      const routeHost = document.getElementById('route-view');
      if (!routeViews.size) routeHost.replaceChildren();
      routeHost.appendChild(host);
      routeViews.set(key, view);
      mountRoute(view);
    }
    activeRouteView = view;
    while (routeViews.size > MAX_CACHED_ROUTES) {
      destroyRouteView(routeViews.values().next().value);
    }
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
    const currentUrl = new URL(window.location.href);
    const nextUrl = target.pathname + target.search + target.hash;
    if ((!fromHistory || forbiddenForAccount) && nextUrl !== currentUrl.pathname + currentUrl.search + currentUrl.hash) {
      window.history[replace ? 'replaceState' : 'pushState'](null, '', nextUrl);
    }
    if (mounted && routeKey(route) !== routeKey(nextRoute)) {
      renderRoute(nextRoute);
    } else {
      route = nextRoute;
      if (disposeNavigation && typeof disposeNavigation.updateRoute === 'function') disposeNavigation.updateRoute(route);
    }
    return true;
  }
  function onNavigationClick(event) {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const anchor = event.target instanceof Element ? event.target.closest('a[href]') : null;
    if (!anchor || anchor.hasAttribute('download') || (anchor.target && anchor.target !== '_self')) return;
    let target;
    try { target = new URL(anchor.href, window.location.href); } catch (_) { return; }
    if (target.origin !== window.location.origin) return;
    const current = new URL(window.location.href);
    if (target.pathname === current.pathname && target.search === current.search) {
      if (target.hash === current.hash) event.preventDefault();
      return;
    }
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
    renderRoute(route);
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
      else resumeRouteView(activeRouteView);
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
    sse.stop();
    if (event.persisted) suspendRouteView(activeRouteView);
    else { shell.hidden = true; window.clearInterval(sessionTimer); disposeView(); }
  });
  window.addEventListener('pageshow', event => {
    if (!event.persisted) return;
    resumeRouteView(activeRouteView);
    gate.hidden = true; shell.hidden = false;
    if (!account || account.role === 'admin') sse.start();
    verifyAccess();
  });
  gate.hidden = true;
  shell.hidden = false;
  verifyAccess({force: true});
}

if (window.location.pathname === '/login') mountLogin();
else protectConsole();
