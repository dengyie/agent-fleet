import {accountRequest} from './api/accounts.js';
import {mountSettings} from './views/settings.js';
import {pagePath} from './routes.js';
/* Composition only: authenticate before mounting any feature or stream. */
import * as api from './api/client.js';
import * as platform from './api/platform.js';
import { FleetStore } from './state/store.js';
import { SseClient } from './realtime/sse.js';
import { resolveRoute, mountNavigation, isPageAllowedForAccount } from './shell/navigation.js';
import { mountFleet } from './views/fleet.js';
import { mountMachine } from './views/machine.js';
import { mountTask } from './views/task.js';
import { mountSession } from './views/session.js';
import { mountSessionSearch } from './views/session-search.js';
import {mountAgentChat} from './views/agent-chat.js';
import {mountSessions} from './views/sessions.js';
import {nativeChatPath, nativeDirectoryPath} from './views/agent-chat/paths.js';
import { mountUsage } from './views/usage.js';
import { mountAssistant } from './views/assistant.js';
import { mountMuse } from './views/muse.js';
import { mountMonitoring } from './views/monitoring.js';
import { mountPlugins } from './views/plugins.js';
import { mountLogin, redirectToLogin, showAccessState } from './shell/operator.js';
import './ui/ChatPromptInput.js';
import './ui/StreamMarkdown.js';
import './ui/ToolCallBadge.js';
import './ui/ThinkingBlock.js';
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
  let settingsBackgroundView = null, settingsReturnUrl = '/assistant';
  let settingsReturnFocus = null;
  let activeRouteView = null, disposeNavigation, mounted = false, checking = false, redirecting = false, account = null;
  let lastSessionCheckAt = 0;
  let sessionRecheckPending = false;
  let sessionResetPending = false;
  let loggingOut = false;
  let identityLifetime = null;
  function routeKey(value) {
    if (value.page === 'agent-chat') return value.page + ':' + value.nativeKind + ':' + (value.id || value.machine || '');
    if (value.page === 'sessions') return value.page + ':' + (value.machine || '');
    return value.page + ':' + (value.id || '');
  }
  function routeUrl(value) {
    if (value.page === 'agent-chat') return nativeChatPath(value.nativeKind === 'session' ? {sessionId: value.id} : value.nativeKind === 'task' ? {taskId: value.id} : {machine: value.machine});
    if (value.page === 'sessions') return nativeDirectoryPath(value.machine);
    if (value.page === 'assistant') return value.id ? pagePath('conversation', value.id) : pagePath('assistant');
    return value.id ? pagePath(value.page, value.id) : pagePath(value.page);
  }
  function suspendRouteView(view) {
    if (!view || view.suspended) return;
    // Hidden/detached scroll containers lose their layout offsets. Capture
    // while visible, before custom elements disconnect and polling pauses.
    view.scrollPositions = [...view.host.querySelectorAll('[data-chat-scroll]')].map(node => ({
      node, top: node.scrollTop, left: node.scrollLeft,
      following: node.querySelector('auto-scroll-anchor')?.isAtBottom === true,
    }));
    view.suspended = true;
    view.host.hidden = true;
    view.host.inert = true;
    if (view.teardown && typeof view.teardown.suspend === 'function') view.teardown.suspend();
    view.host.remove();
  }
  function resumeRouteView(view, {animateEntrance = true} = {}) {
    if (!view || !view.suspended) return;
    view.suspended = false;
    view.host.hidden = false;
    view.host.inert = false;
    document.getElementById('route-view').appendChild(view.host);
    for (const {node, top, left, following} of view.scrollPositions || []) {
      node.scrollTop = following ? node.scrollHeight : top;
      node.scrollLeft = left;
      // Reconnected AutoScrollAnchor must know the restored position before
      // resumed requests render more output.
      node.dispatchEvent(new Event('scroll'));
    }
    view.scrollPositions = null;
    if (view.teardown && typeof view.teardown.resume === 'function') view.teardown.resume({animateEntrance});
  }
  function destroyRouteView(view) {
    if (!view) return;
    if (typeof view.teardown === 'function') view.teardown();
    view.host.remove();
    routeViews.delete(view.key);
    if (activeRouteView === view) activeRouteView = null;
  }
  function disposeView() {
    identityLifetime?.abort();
    shell.hidden = true; sse.stop();
    if (disposeNavigation) disposeNavigation();
    disposeNavigation = undefined;
    for (const view of routeViews.values()) {
      if (typeof view.teardown === 'function') view.teardown();
      view.host.remove();
    }
    routeViews.clear();
    activeRouteView = null;
    settingsBackgroundView = null;
    settingsReturnFocus = null;
    mounted = false;
    for (const id of ['route-view', 'sidebar-conversations']) {
      document.getElementById(id).replaceChildren();
    }
  }
  function requireLogin() {
    if (redirecting) return;
    showAccessState('正在前往登录页…');
    redirecting = true; disposeView();
    redirectToLogin();
  }
  function adoptConversation(view, conversationId) {
    adoptRoute(view, {page: 'assistant', id: conversationId});
  }
  function adoptRoute(view, nextRoute) {
    if (routeViews.get(view.key) !== view) return;
    const key = routeKey(nextRoute);
    const previous = routeViews.get(key);
    const replacingActive = previous && previous !== view && activeRouteView === previous;
    if (previous && previous !== view) destroyRouteView(previous);
    routeViews.delete(view.key);
    view.key = key;
    view.host.dataset.routeCacheEntry = key;
    routeViews.set(key, view);
    if (replacingActive) {
      activeRouteView = view;
      resumeRouteView(view);
    }
    // A background submission may finish after the user opens another page.
    // Update its cache identity without navigating away from that page.
    if (settingsBackgroundView === view) settingsReturnUrl = routeUrl(nextRoute);
    if (activeRouteView !== view) return;
    route = nextRoute;
    window.history.replaceState(null, '', routeUrl(nextRoute));
    disposeNavigation?.updateRoute?.(route);
  }
  function mountRoute(view, {animateEntrance = true} = {}) {
    const target = view.host;
    const mountEntity = {machine: mountMachine, task: mountTask, session: mountSession};
    if (route.page === 'settings') view.teardown = mountSettings(target, {section: route.id, account, identitySignal: identityLifetime.signal, animateEntrance, onClose: () => {
      const opener = settingsReturnFocus;
      navigateTo(settingsReturnUrl);
      settingsReturnFocus = null;
      const available = node => node?.isConnected && node !== document.body && !node.matches(':disabled') && !node.closest('[inert], [hidden]') && node.getClientRects().length;
      const fallback = document.querySelector('[data-nav="settings"]');
      (available(opener) ? opener : available(fallback) ? fallback : document.getElementById('navigation-toggle'))?.focus({preventScroll: true});
    }});
    else if (route.page === 'fleet') view.teardown = mountFleet(target, store, client);
    else if (route.page === 'assistant') view.teardown = mountAssistant(target, {
      conversationId: route.id, account, store, identitySignal: identityLifetime.signal,
      onConversationCreated: id => adoptConversation(view, id),
    });
    else if (route.page === 'agent-chat') view.teardown = mountAgentChat(target, {
      sessionId: route.nativeKind === 'session' ? route.id : null,
      taskId: route.nativeKind === 'task' ? route.id : null, machine: route.machine,
      api: client, store, account, identitySignal: identityLifetime.signal,
      onIdentity: ref => adoptRoute(view, {page: 'agent-chat', nativeKind: ref.sessionId ? 'session' : 'task', id: ref.sessionId || ref.taskId}),
    });
    else if (route.page === 'sessions') view.teardown = mountSessions(target, {api: client, store, machine: route.machine, identitySignal: identityLifetime.signal});
    else if (route.page === 'muse') view.teardown = mountMuse(target);
    else if (route.page === 'monitoring') view.teardown = mountMonitoring(target, client);
    else if (route.page === 'plugins') view.teardown = mountPlugins(target);
    else if (route.page === 'session-search') view.teardown = mountSessionSearch(target, client);
    else if (route.page === 'usage') view.teardown = mountUsage(target, client);
    else if (route.page === 'session' && route.id) {
      view.teardown = mountSession(target, route.id, store, client, route.sequence);
    }
    else if (mountEntity[route.page] && route.id) view.teardown = mountEntity[route.page](target, route.id, store, client);
    else target.textContent = '页面不存在或缺少标识，请从导航重新进入。';
  }
  function renderRoute(nextRoute) {
    const key = routeKey(nextRoute);
    const animateEntrance = nextRoute.page !== 'settings' || route.page !== 'settings';
    if (activeRouteView && activeRouteView.key === key) {
      route = nextRoute;
      if (typeof activeRouteView.teardown?.updateSequence === 'function') {
        activeRouteView.teardown.updateSequence(route.sequence);
      }
      if (disposeNavigation && typeof disposeNavigation.updateRoute === 'function') disposeNavigation.updateRoute(route);
      return;
    }
    if (nextRoute.page === 'settings' && route.page !== 'settings') {
      settingsReturnFocus = document.activeElement;
      while (settingsReturnFocus?.shadowRoot?.activeElement) settingsReturnFocus = settingsReturnFocus.shadowRoot.activeElement;
      settingsBackgroundView = activeRouteView;
      settingsReturnUrl = routeUrl(route);
      if (settingsBackgroundView) settingsBackgroundView.host.inert = true;
    } else suspendRouteView(activeRouteView);
    if (nextRoute.page !== 'settings' && settingsBackgroundView) {
      if (settingsBackgroundView.key !== key) suspendRouteView(settingsBackgroundView);
      else settingsBackgroundView.host.inert = false;
      settingsBackgroundView = null;
    }
    route = nextRoute;
    if (disposeNavigation && typeof disposeNavigation.updateRoute === 'function') {
      disposeNavigation.updateRoute(route);
    }
    let view = routeViews.get(key);
    if (view) {
      routeViews.delete(key);
      routeViews.set(key, view);
      resumeRouteView(view, {animateEntrance});
      if (typeof view.teardown?.updateSequence === 'function') {
        view.teardown.updateSequence(nextRoute.sequence);
      }
    } else {
      const host = document.createElement('div');
      host.className = 'route-view-entry';
      host.dataset.routeCacheEntry = key;
      view = {key, host, teardown: undefined, suspended: false};
      const routeHost = document.getElementById('route-view');
      if (!routeViews.size) routeHost.replaceChildren();
      routeHost.appendChild(host);
      routeViews.set(key, view);
      mountRoute(view, {animateEntrance});
    }
    activeRouteView = view;
    while (routeViews.size > MAX_CACHED_ROUTES) {
      const oldest = [...routeViews.values()].find(entry => entry !== activeRouteView && entry !== settingsBackgroundView);
      if (!oldest) break;
      destroyRouteView(oldest);
    }
  }
  function navigateTo(url, {replace = false, fromHistory = false} = {}) {
    let target;
    try { target = new URL(url, window.location.href); } catch (_) { return false; }
    if (target.origin !== window.location.origin) return false;
    let nextRoute = resolveRoute(target);
    if (nextRoute.page === 'invalid') return false;
    const forbiddenForAccount = !isPageAllowedForAccount(nextRoute.page, account);
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
    if (mounted) renderRoute(nextRoute);
    else route = nextRoute;
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
    // Sensitive results can outlive a cached route, but never its identity.
    identityLifetime = new AbortController();
    disposeNavigation = mountNavigation(route, store, account);
    if (route.page === 'settings') {
      const settingsRoute = route;
      route = {page: 'assistant'};
      renderRoute(route);
      renderRoute(settingsRoute);
    } else renderRoute(route);
    if ((!account || account.role === 'admin') && route.page !== 'fleet') client.getStatus().then(status => store.setStatus(status)).catch(() => {
      // Status errors are shown by the overview and node panel.
    });
    mounted = true;
  }
  async function verifyAccess({force = false} = {}) {
    const now = Date.now();
    if (redirecting || loggingOut) return;
    if (checking) {
      if (force || now - lastSessionCheckAt >= 60000) sessionRecheckPending = true;
      return;
    }
    if (!force && now - lastSessionCheckAt < 60000) return;
    checking = true;
    lastSessionCheckAt = now;
    try {
      const session = await api.getOperatorSession({deferUnauthorized: true});
      if (redirecting || loggingOut || sessionRecheckPending) return;
      const nextAccount = session.user || null;
      if (mounted && (account?.id !== nextAccount?.id || account?.role !== nextAccount?.role)) {
        // A session may have changed in another tab. The old view, async work,
        // navigation and store belong to that identity; recreate the document.
        redirecting = true;
        showAccessState('账号已切换，正在更新工作空间…');
        disposeView();
        window.location.reload();
        return;
      }
      account = nextAccount;
      sessionResetPending = false;
      document.getElementById('account-menu-name').textContent = account?.name || account?.email || '操作员';
      document.getElementById('account-menu-role').textContent = account ? (account.role === 'admin' ? '管理员' : '个人账号') : '令牌访问';
      document.querySelector('[data-nav="settings"]').hidden = false;
      if (account && account.role !== "admin") {
        document.querySelector('[data-nav="fleet"]').hidden = true;
        document.querySelector('[data-nav="monitoring"]').hidden = true;
        if (!isPageAllowedForAccount(route.page, account)) {
          window.history.replaceState(null, '', '/assistant');
          route = resolveRoute();
        }
      }
      if (redirecting) return;
      if (!mounted) mount();
      // Identity is verified. Restore layout before restoring scroll offsets.
      shell.hidden = false;
      resumeRouteView(activeRouteView);
      gate.hidden = true; if (!account || account.role === "admin") sse.start();
    } catch (error) {
      if (redirecting || loggingOut || sessionRecheckPending) return;
      if (error.status === 401) requireLogin();
      else if (!mounted || sessionResetPending) {
        const message = '工作空间暂时无法连接，稍后会自动重试。';
        showAccessState(message, {retry: true});
      }
    } finally {
      checking = false;
      if (sessionRecheckPending && !redirecting) {
        sessionRecheckPending = false;
        verifyAccess({force: true});
      }
    }
  }
  window.addEventListener('fleet-auth-required', () => {
    if (redirecting || loggingOut || sessionResetPending) return;
    sessionResetPending = true;
    // Keep the view and unsent input until the authoritative identity check
    // returns. A feature's 401 alone does not prove the browser was logged out.
    showAccessState('正在重新验证登录状态…');
    document.querySelectorAll('.shell-menu:popover-open').forEach(menu => menu.hidePopover());
    sse.stop();
    suspendRouteView(activeRouteView);
    shell.hidden = true;
    verifyAccess({force: true});
  });
  const logout = document.getElementById('operator-logout');
  logout.addEventListener('click', async () => {
    if (loggingOut) return;
    loggingOut = true;
    logout.disabled = true;
    logout.querySelector('span:last-child').textContent = '正在退出…';
    logout.setAttribute('aria-label', '正在退出…');
    const status = document.getElementById('account-menu-status');
    status.hidden = false; status.textContent = '正在安全退出登录…';
    try { if (account) await accountRequest('logout', {}); requireLogin(); }
    catch (error) {
      if (error.status === 401) requireLogin();
      else status.textContent = '退出登录失败，请重试';
    } finally {
      loggingOut = false; logout.disabled = false;
      logout.querySelector('span:last-child').textContent = '退出登录';
      logout.setAttribute('aria-label', '退出登录');
    }
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
    // A restored document can belong to a logged-out or switched account.
    // Revalidate before exposing cached content or restarting its streams.
    sessionResetPending = true;
    showAccessState('正在验证登录状态…'); shell.hidden = true;
    verifyAccess({force: true});
  });
  showAccessState('正在验证登录状态…');
  verifyAccess({force: true});
}

if (window.location.pathname === '/login') mountLogin();
else protectConsole();
