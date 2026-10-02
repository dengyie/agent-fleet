import { uiIcon, pagePath } from '../routes.js';
import { getConversations } from '../api/platform.js';
const labels = {
  account: ['账号设置', '账号设置', '管理个人资料、密码与登录会话。', 'ACCOUNT'],
  fleet: ['总览', '集群总览', '节点、任务与服务状态，一处掌握。', 'WORKSPACE OVERVIEW'],
  assistant: ['主助手', '主助手', '从一个想法开始，让助手把工作推进到完成。', 'ASSISTANT'],
  monitoring: ['服务监控', '服务监控', '查看服务健康与待处理事件，按授权执行操作。', 'SERVICE HEALTH'],
  machine: ['计算节点', '节点详情', '资源状态、运行中的任务与纳管会话。', 'COMPUTE NODE'],
  task: ['任务', '任务详情', '跟踪执行过程，查看结果与交付文件。', 'TASK DETAIL'],
  session: ['会话', '会话详情', '连接你的执行会话，保留完整工作上下文。', 'SESSION'],
};
export function resolveRoute(location = window.location) {
  const parts = location.pathname.split('/').filter(Boolean);
  const query = new URLSearchParams(location.search);
  let page = parts[0] || 'fleet';
  let id = parts[1];
  if (page === 'index.html') page = 'fleet';
  if (page === 'fleet') {
    for (const name of ['machine', 'task', 'session', 'conversation', 'assistant', 'monitoring']) {
      if (query.has(name)) { page = name; id = query.get(name); break; }
    }
  } else if (id) {
    try { id = decodeURIComponent(id); } catch (_) { return {page: 'invalid', id: null}; }
  }
  if (page === 'conversation') return {page: 'assistant', id};
  return {page: labels[page] ? page : 'invalid', id};
}
export function mountNavigation(route, store) {
  document.querySelectorAll('[data-icon]').forEach(slot => slot.replaceChildren(uiIcon(slot.dataset.icon, {size: 18})));
  const copy = labels[route.page] || ['页面不存在', '页面不存在', '请从左侧导航选择一个工作空间页面。', 'NOT FOUND'];
  ['route-label', 'page-title', 'page-description', 'page-eyebrow'].forEach((id, i) => { document.getElementById(id).textContent = copy[i]; });
  document.title = copy[0] + ' · Agent Fleet';
  document.body.dataset.page = route.page;
  document.querySelectorAll('[data-nav]').forEach(link => {
    if (link.dataset.nav === route.page) link.setAttribute('aria-current', 'page');
  });
  const sidebar = document.getElementById('sidebar');
  const backdrop = document.getElementById('sidebar-backdrop');
  const button = document.getElementById('mobile-menu');
  const mobile = window.matchMedia('(max-width: 767px)');
  function resetDrawer() {
    sidebar.classList.remove('open');
    sidebar.inert = mobile.matches;
    backdrop.hidden = true;
    button.setAttribute('aria-expanded', 'false');
    document.body.classList.remove('navigation-open');
  }
  resetDrawer();
  mobile.addEventListener('change', resetDrawer);
  function toggle(open) {
    sidebar.classList.toggle('open', open);
    sidebar.inert = mobile.matches && !open;
    backdrop.hidden = !open;
    button.setAttribute('aria-expanded', String(open));
    document.body.classList.toggle('navigation-open', open);
    if (open) sidebar.querySelector('a').focus(); else button.focus();
  }
  const onMenu = () => toggle(button.getAttribute('aria-expanded') !== 'true');
  const onBackdrop = () => toggle(false);
  button.addEventListener('click', onMenu);
  backdrop.addEventListener('click', onBackdrop);
  function onKeydown(event) {
    if (button.getAttribute('aria-expanded') !== 'true') return;
    if (event.key === 'Escape') toggle(false);
    if (event.key === 'Tab') {
      const links = [...sidebar.querySelectorAll('a, button')];
      const first = links[0], last = links[links.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  }
  document.addEventListener('keydown', onKeydown);
  let disposed = false;
  let historyRequest = 0;
  async function refreshHistory() {
    const request = ++historyRequest;
    const list = document.getElementById('sidebar-conversations');
    try {
      const data = await getConversations(50);
      if (disposed || request !== historyRequest) return;
      list.replaceChildren();
      for (const row of (data.conversations || []).slice(0, 50)) {
        if (!row.conversation_id) continue;
        const link = document.createElement('a');
        link.href = pagePath('conversation', row.conversation_id);
        link.textContent = row.title || row.last_message_preview || '未命名对话';
        link.title = link.textContent;
        if (resolveRoute().id === row.conversation_id) link.setAttribute('aria-current', 'page');
        list.appendChild(link);
      }
      if (!list.childElementCount) list.textContent = '暂无 Hub 历史对话';
    } catch (error) {
      if (disposed || request !== historyRequest) return;
      list.replaceChildren();
      const message = document.createElement('p'); message.className = 'sidebar-empty';
      message.textContent = error.status === 401 ? '登录后查看 Hub 历史对话' : '历史对话加载失败';
      const retry = document.createElement('button'); retry.type = 'button'; retry.textContent = '重试';
      retry.addEventListener('click', refreshHistory);
      list.append(message, retry);
    }
  }
  refreshHistory();
  window.addEventListener('fleet-conversations-updated', refreshHistory);
  window.addEventListener('focus', refreshHistory);
  let renderedNodes;
  const unsubscribe = store.subscribe(() => {
    const state = store.getState();
    const status = document.getElementById('connection-status');
    status.dataset.state = state.connection;
    status.querySelector('span').textContent = state.connection === 'open' ? '实时连接' : state.connection === 'connecting' ? '正在连接' : '连接中断';
    if (!state.status) return;
    const rows = state.status.machines || [];
    const nodes = JSON.stringify(rows.map(machine => [machine.machine, machine.online]));
    if (renderedNodes === nodes) return;
    renderedNodes = nodes;
    const list = document.getElementById('sidebar-nodes');
    list.replaceChildren();
    document.getElementById('node-count').textContent = String(rows.length);
    if (!rows.length) {
      const empty = document.createElement('p'); empty.className = 'sidebar-empty'; empty.textContent = '尚未连接计算节点'; list.appendChild(empty);
    }
    for (const machine of rows) {
      const link = document.createElement('a'); link.href = pagePath('machine', machine.machine);
      const dot = document.createElement('i'); dot.className = machine.online ? 'node-dot online' : 'node-dot'; link.appendChild(dot);
      const text = document.createElement('span'); text.textContent = machine.machine; link.appendChild(text);
      if (route.page === 'machine' && route.id === machine.machine) link.setAttribute('aria-current', 'page');
      list.appendChild(link);
    }
  });
  return () => {
    disposed = true;
    window.removeEventListener('fleet-conversations-updated', refreshHistory);
    window.removeEventListener('focus', refreshHistory);
    unsubscribe();
    mobile.removeEventListener('change', resetDrawer);
    button.removeEventListener('click', onMenu);
    backdrop.removeEventListener('click', onBackdrop);
    document.removeEventListener('keydown', onKeydown);
  };
}
