import {accountRequest} from './api/accounts.js';
import {mountAccount} from './views/account.js';
/* Render navigation immediately; APIs enforce access and sessions revalidate in the background. */
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
  const route = resolveRoute();
  const sse = new SseClient(store, {client});
  const shell = document.querySelector('.console-layout');
  const gate = document.getElementById('access-state');
  const retry = gate.querySelector('button');
  let teardown, unsubscribe, mounted = false, checking = false, redirecting = false, account = null;
  let identityVerified = false, sidebarLoaded = false, suspended = false;
  let resolveAccount;
  const accountReady = new Promise(resolve => { resolveAccount = resolve; });
  // Verification errors use an inline notice; ordinary checks never hide the page.
  document.getElementById('main-content').prepend(gate);
  gate.classList.add('access-state-inline');
  function disposeView() {
    shell.hidden = true; sse.stop();
    if (typeof teardown === 'function') teardown();
    if (unsubscribe) unsubscribe();
    teardown = unsubscribe = undefined;
    for (const id of ['route-view', 'sidebar-conversations', 'sidebar-nodes']) {
      document.getElementById(id).replaceChildren();
    }
  }
  function requireLogin() {
    if (redirecting) return;
    redirecting = true; disposeView();
    redirectToLogin();
  }
  function mount() {
    const target = document.getElementById('route-view');
    target.replaceChildren();
    const mountEntity = {machine: mountMachine, task: mountTask, session: mountSession};
    if (route.page === 'account') teardown = mountAccount(target);
    else if (route.page === 'fleet') teardown = mountFleet(target, store, client);
    else if (route.page === 'assistant') teardown = mountAssistant(target, {conversationId: route.id, accountReady});
    else if (route.page === 'monitoring') teardown = mountMonitoring(target, client);
    else if (mountEntity[route.page] && route.id) teardown = mountEntity[route.page](target, route.id, store, client);
    else target.textContent = '页面不存在或缺少标识，请从导航重新进入。';
    mounted = true;
  }
  async function verifyAccess() {
    if (checking || redirecting || suspended) return;
    checking = true;
    retry.disabled = true;
    try {
      const session = await api.getOperatorSession();
      if (redirecting || suspended) return;
      const nextAccount = session.user || null;
      if (identityVerified && (account?.id !== nextAccount?.id || account?.role !== nextAccount?.role)) {
        // A session may have changed in another tab. The old view, async work,
        // navigation and store belong to that identity; recreate the document.
        redirecting = true;
        disposeView();
        window.location.reload();
        return;
      }
      account = nextAccount;
      identityVerified = true;
      document.querySelector('[data-nav="account"]').hidden = !account;
      const canAdminister = !account || account.role === 'admin';
      document.querySelector('[data-nav="fleet"]').hidden = !canAdminister;
      document.querySelector('[data-nav="monitoring"]').hidden = !canAdminister;
      if (!canAdminister) {
        document.getElementById("sidebar-nodes").textContent = "节点由管理员分配";
        if (!["assistant", "account"].includes(route.page)) { redirecting = true; disposeView(); window.location.replace("/assistant"); return; }
      }
      if (redirecting) return;
      resolveAccount(account);
      if (!mounted) mount();
      gate.hidden = true;
      if (canAdminister) {
        if (!sidebarLoaded && route.page !== 'fleet') {
          sidebarLoaded = true;
          client.getStatus().then(status => { if (!redirecting) store.setStatus(status); }).catch(() => {
            if (!redirecting) document.getElementById('sidebar-nodes').textContent = '节点列表暂时不可用';
          });
        }
        sse.start();
      }
    } catch (error) {
      if (error.status === 401) requireLogin();
      else if (!redirecting && !suspended) {
        gate.hidden = false; retry.hidden = false;
        gate.querySelector('p').textContent = '暂时无法验证登录状态';
      }
    } finally { checking = false; retry.disabled = false; }
  }
  window.addEventListener('fleet-auth-required', requireLogin);
  document.getElementById('operator-logout').addEventListener('click', async () => {
    try {
      // Logout must revoke a cookie even while the initial identity check is pending.
      try { await accountRequest('logout', {}); }
      catch (error) { if (error.status !== 404) throw error; } // Legacy token deployments.
      requireLogin();
    } catch (error) { gate.hidden = false; gate.querySelector('p').textContent = '退出登录失败，请重试'; }
  });
  document.getElementById('refresh-page').addEventListener('click', () => window.location.reload());
  retry.addEventListener('click', verifyAccess);
  window.addEventListener('focus', verifyAccess);
  const sessionTimer = window.setInterval(async () => {
    if (document.hidden || checking || redirecting) return;
    await verifyAccess();
  }, 60000);
  window.addEventListener('pagehide', event => {
    suspended = true; shell.hidden = true; sse.stop();
    if (!event.persisted) { window.clearInterval(sessionTimer); disposeView(); }
  });
  window.addEventListener('pageshow', event => {
    if (event.persisted && !redirecting) { suspended = false; shell.hidden = false; verifyAccess(); }
  });
  unsubscribe = mountNavigation(route, store);
  // Owner-scoped views can load through the authenticated API immediately.
  // Cluster views and streams wait for the server-confirmed role.
  if (['assistant', 'account'].includes(route.page)) mount();
  shell.hidden = false;
  verifyAccess();
}

if (window.location.pathname === '/login') mountLogin();
else protectConsole();
