/* Composition only: API, store and stream are shared by every view. */
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
import './ui/ChatPromptInput.js';
import './ui/StreamMarkdown.js';
import './ui/ToolCallBadge.js';
import './ui/AutoScrollAnchor.js';

import { mountOperator } from './shell/operator.js';
mountOperator();

const client = {...api, ...platform};
const store = new FleetStore();
const route = resolveRoute();
const unsubscribe = mountNavigation(route, store);
const sse = new SseClient(store, {client});
const target = document.getElementById('route-view');
let teardown;
function mount() {
  target.replaceChildren();
  const mountEntity = {machine: mountMachine, task: mountTask, session: mountSession};
  if (route.page === 'fleet') teardown = mountFleet(target, store, client);
  else if (route.page === 'assistant') teardown = mountAssistant(target, {conversationId: route.id});
  else if (route.page === 'monitoring') teardown = mountMonitoring(target, client);
  else if (mountEntity[route.page] && route.id) teardown = mountEntity[route.page](target, route.id, store, client);
  else target.textContent = '页面不存在或缺少标识，请从导航重新进入。';
}
mount();
sse.start();
if (route.page !== 'fleet') client.getStatus().then(status => store.setStatus(status)).catch(() => {
  document.getElementById('sidebar-nodes').textContent = '节点列表暂时不可用';
});
document.getElementById('refresh-page').addEventListener('click', () => window.location.reload());
window.addEventListener('pagehide', event => {
  sse.stop();
  if (!event.persisted) { if (typeof teardown === 'function') teardown(); unsubscribe(); }
});
window.addEventListener('pageshow', event => { if (event.persisted) sse.start(); });
