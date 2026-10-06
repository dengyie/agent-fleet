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
  const route = resolveRoute();
  const sse = new SseClient(store, {client});
  const shell = document.querySelector('.console-layout');
  const gate = document.getElementById('access-state');
  const retry = gate.querySelector('button');
  let teardown, unsubscribe, mounted = false, checking = false, redirecting = false, account = null;
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
    unsubscribe = mountNavigation(route, store);
    const target = document.getElementById('route-view');
    target.replaceChildren();
    const mountEntity = {machine: mountMachine, task: mountTask, session: mountSession};
    if (route.page === 'account') teardown = mountAccount(target);
    else if (route.page === 'fleet') teardown = mountFleet(target, store, client);
    else if (route.page === 'assistant') teardown = mountAssistant(target, {conversationId: route.id, account});
    else if (route.page === 'monitoring') teardown = mountMonitoring(target, client);
    else if (mountEntity[route.page] && route.id) teardown = mountEntity[route.page](target, route.id, store, client);
    else target.textContent = '页面不存在或缺少标识，请从导航重新进入。';
    if ((!account || account.role === 'admin') && route.page !== 'fleet') client.getStatus().then(status => store.setStatus(status)).catch(() => {
      document.getElementById('sidebar-nodes').textContent = '节点列表暂时不可用';
    });
    mounted = true;
  }
  async function verifyAccess({background = false} = {}) {
    if (checking || redirecting) return;
    checking = true;
    if (!background) {
      shell.hidden = true; gate.hidden = false; retry.hidden = true;
      gate.querySelector('p').textContent = '正在验证登录状态…';
      sse.stop();
    }
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
        if (!["assistant", "account"].includes(route.page)) { window.location.replace("/assistant"); return; }
      }
      if (redirecting) return;
      if (!mounted) mount();
      gate.hidden = true; shell.hidden = false; if (!account || account.role === "admin") sse.start();
    } catch (error) {
      if (error.status === 401) requireLogin();
      else if (!background) gate.querySelector('p').textContent = '暂时无法验证登录状态';
      if (!background) retry.hidden = redirecting;
    } finally { checking = false; }
  }
  window.addEventListener('fleet-auth-required', requireLogin);
  document.getElementById('operator-logout').addEventListener('click', async () => {
    try { if (account) await accountRequest('logout', {}); requireLogin(); }
    catch (error) { gate.hidden = false; gate.querySelector('p').textContent = '退出登录失败，请重试'; }
  });
  document.getElementById('refresh-page').addEventListener('click', () => window.location.reload());
  retry.addEventListener('click', verifyAccess);
  window.addEventListener('focus', verifyAccess);
  const sessionTimer = window.setInterval(async () => {
    if (document.hidden || checking || redirecting) return;
    await verifyAccess({background: true});
  }, 60000);
  window.addEventListener('pagehide', event => {
    shell.hidden = true; sse.stop();
    if (!event.persisted) { window.clearInterval(sessionTimer); disposeView(); }
  });
  window.addEventListener('pageshow', event => { if (event.persisted) verifyAccess(); });
  verifyAccess();
}

if (window.location.pathname === '/login') mountLogin();
else protectConsole();
