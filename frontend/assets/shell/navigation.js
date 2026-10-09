import { uiIcon, pagePath, navigatePage } from '../routes.js';
import {
  getConversations, renameConversation, archiveConversation,
  restoreConversation, deleteConversation,
} from '../api/platform.js';
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
  let currentRoute = route;
  function updateRouteMetadata() {
    const copy = labels[currentRoute.page] || ['页面不存在', '页面不存在', '请从左侧导航选择一个工作空间页面。', 'NOT FOUND'];
    ['route-label', 'page-title', 'page-description', 'page-eyebrow'].forEach((id, i) => { document.getElementById(id).textContent = copy[i]; });
    document.title = copy[0] + ' · Agent Fleet';
    document.body.dataset.page = currentRoute.page;
    document.querySelectorAll('[data-nav]').forEach(link => {
      if (link.dataset.nav === currentRoute.page) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
    document.querySelectorAll('#sidebar-conversations a, #sidebar-nodes a').forEach(link => {
      if (new URL(link.href, window.location.href).pathname === window.location.pathname) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
  }
  updateRouteMetadata();
  const sidebar = document.getElementById('sidebar');
  const backdrop = document.getElementById('sidebar-backdrop');
  const button = document.getElementById('mobile-menu');
  const mobile = window.matchMedia('(max-width: 767px)');
  const collapse = document.getElementById('sidebar-collapse');
  const expand = document.getElementById('desktop-menu');
  const search = document.getElementById('conversation-search');
  const searchStatus = document.getElementById('conversation-search-status');
  const historyHeading = document.getElementById('conversation-heading');
  const archiveToggle = document.getElementById('archived-toggle');
  const resizeHandle = document.getElementById('sidebar-nodes-resize');
  const historyList = document.getElementById('sidebar-conversations');
  const nodeList = document.getElementById('sidebar-nodes');
  const adjustableLists = document.getElementById('sidebar-adjustable-lists');
  const titleDialog = document.getElementById('conversation-title-dialog');
  const titleInput = document.getElementById('conversation-title-input');
  const titleError = document.getElementById('conversation-title-error');
  const titleSave = document.getElementById('conversation-title-save');
  const deleteDialog = document.getElementById('conversation-delete-dialog');
  const deleteConfirm = document.getElementById('conversation-delete-confirm');
  let historyArchived = false;
  let dialogConversationId = null;
  let deleteConversationId = null;
  let collapsed = false;
  try { collapsed = localStorage.getItem('fleet-sidebar-collapsed') === 'true'; } catch (_) {}
  function applyCollapse() {
    document.body.classList.toggle('sidebar-collapsed', collapsed && !mobile.matches);
    sidebar.inert = mobile.matches ? !sidebar.classList.contains('open') : collapsed;
  }
  function setCollapsed(value) {
    collapsed = value;
    try { localStorage.setItem('fleet-sidebar-collapsed', String(value)); } catch (_) {}
    applyCollapse();
    (value ? expand : collapse).focus();
  }
  const onCollapse = () => mobile.matches ? toggle(false) : setCollapsed(true);
  const onExpand = () => setCollapsed(false);
  collapse.addEventListener('click', onCollapse);
  expand.addEventListener('click', onExpand);
  const MIN_NODE_HEIGHT = 60;
  const MIN_HISTORY_HEIGHT = 70;
  function maxNodeHeight() {
    const group = adjustableLists.getBoundingClientRect();
    const history = historyList.getBoundingClientRect();
    const nodes = nodeList.getBoundingClientRect();
    const fixedHeight = history.top - group.top + nodes.top - history.bottom + Math.max(0, group.bottom - nodes.bottom);
    return Math.max(MIN_NODE_HEIGHT, Math.floor(Math.min(480, window.innerHeight * 0.5, group.height - fixedHeight - MIN_HISTORY_HEIGHT)));
  }
  function setNodeHeight(value, persist = false) {
    const max = maxNodeHeight();
    const height = Math.round(Math.max(MIN_NODE_HEIGHT, Math.min(max, value)));
    sidebar.style.setProperty('--sidebar-nodes-height', height + 'px');
    resizeHandle.setAttribute('aria-valuemin', String(MIN_NODE_HEIGHT));
    resizeHandle.setAttribute('aria-valuemax', String(Math.round(max)));
    resizeHandle.setAttribute('aria-valuenow', String(height));
    if (persist) {
      try { localStorage.setItem('fleet-sidebar-nodes-height', String(height)); } catch (_) {}
    }
    return height;
  }
  let savedNodeHeight = Number.NaN;
  try { savedNodeHeight = Number(localStorage.getItem('fleet-sidebar-nodes-height')); } catch (_) {}
  const initialNodeHeight = Number.isFinite(savedNodeHeight) && savedNodeHeight > 0
    ? savedNodeHeight : window.innerHeight * 0.22;
  setNodeHeight(initialNodeHeight);
  let resizeStart = null;
  function onResizePointerDown(event) {
    if (!event.isPrimary || event.button !== 0) return;
    event.preventDefault();
    resizeHandle.focus();
    resizeStart = {y: event.clientY, height: nodeList.getBoundingClientRect().height};
    resizeHandle.setPointerCapture(event.pointerId);
  }
  function onResizePointerMove(event) {
    if (!resizeStart) return;
    setNodeHeight(resizeStart.height - (event.clientY - resizeStart.y));
  }
  function onResizePointerUp(event) {
    if (!resizeStart) return;
    resizeStart = null;
    setNodeHeight(Number(resizeHandle.getAttribute('aria-valuenow')), true);
    if (resizeHandle.hasPointerCapture(event.pointerId)) resizeHandle.releasePointerCapture(event.pointerId);
  }
  function onResizeKeydown(event) {
    const current = Number(resizeHandle.getAttribute('aria-valuenow'));
    if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
      event.preventDefault();
      setNodeHeight(current + (event.key === 'ArrowUp' ? 16 : -16), true);
    } else if (event.key === 'Home') {
      event.preventDefault(); setNodeHeight(MIN_NODE_HEIGHT, true);
    } else if (event.key === 'End') {
      event.preventDefault(); setNodeHeight(maxNodeHeight(), true);
    }
  }
  const onViewportResize = () => setNodeHeight(Number(resizeHandle.getAttribute('aria-valuenow')));
  resizeHandle.addEventListener('pointerdown', onResizePointerDown);
  resizeHandle.addEventListener('pointermove', onResizePointerMove);
  resizeHandle.addEventListener('pointerup', onResizePointerUp);
  resizeHandle.addEventListener('pointercancel', onResizePointerUp);
  resizeHandle.addEventListener('keydown', onResizeKeydown);
  window.addEventListener('resize', onViewportResize);
  function filterHistory() {
    const query = search.value.trim().toLocaleLowerCase();
    const links = [...document.querySelectorAll('#sidebar-conversations .conversation-history-entry')];
    let count = 0;
    for (const link of links) {
      link.hidden = !(link.dataset.searchText || '').includes(query);
      if (!link.hidden) count++;
    }
    searchStatus.hidden = !query || !links.length;
    searchStatus.textContent = count ? `找到 ${count} 条对话` : '没有匹配的对话';
  }
  search.addEventListener('input', filterHistory);
  function openHistoryMode(archived) {
    historyArchived = archived;
    archiveToggle.setAttribute('aria-pressed', String(archived));
    archiveToggle.textContent = archived ? '最近对话' : '查看归档';
    historyHeading.textContent = archived ? '已归档对话' : '最近对话';
    search.setAttribute('aria-label', archived ? '搜索已归档对话' : '搜索最近对话');
    search.placeholder = archived ? '搜索已归档对话' : '搜索最近对话';
    refreshHistory();
  }
  const onArchiveToggle = () => openHistoryMode(!historyArchived);
  archiveToggle.addEventListener('click', onArchiveToggle);
  function resetDrawer() {
    sidebar.classList.remove('open');
    sidebar.inert = mobile.matches;
    backdrop.hidden = true;
    button.setAttribute('aria-expanded', 'false');
    document.body.classList.remove('navigation-open');
    applyCollapse();
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
    if (document.querySelector('dialog:modal')) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') event.preventDefault();
      return;
    }
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      if (mobile.matches) toggle(true); else if (collapsed) setCollapsed(false);
      search.focus();
      return;
    }
    if (button.getAttribute('aria-expanded') !== 'true') return;
    if (event.key === 'Escape') toggle(false);
    if (event.key === 'Tab') {
      const links = [...sidebar.querySelectorAll('a, button, input, summary')].filter(node => !node.hidden && node.getClientRects().length);
      const first = links[0], last = links[links.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  }
  document.addEventListener('keydown', onKeydown);
  let disposed = false;
  let historyRequest = 0;
  function showHistoryError(error) {
    searchStatus.hidden = false;
    searchStatus.textContent = (error && (error.detail || error.message)) || '对话操作失败';
  }
  async function runHistoryAction(action) {
    try {
      await action();
      searchStatus.hidden = true;
      await refreshHistory();
    } catch (error) {
      showHistoryError(error);
      return false;
    }
    return true;
  }
  function openRename(row) {
    dialogConversationId = row.conversation_id;
    titleInput.value = row.title || row.last_message_preview || '';
    titleError.hidden = true;
    titleError.textContent = '';
    titleDialog.showModal();
    titleInput.focus();
    titleInput.select();
  }
  function openDelete(row) {
    deleteConversationId = row.conversation_id;
    document.getElementById('conversation-delete-copy').textContent =
      '“' + (row.title || row.last_message_preview || '未命名对话') + '”的消息和运行记录将被删除，不能撤销。';
    deleteDialog.showModal();
  }
  function addHistoryAction(menu, label, action, destructive = false) {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'conversation-menu-item' + (destructive ? ' destructive' : '');
    item.textContent = label;
    item.addEventListener('click', async () => {
      menu.open = false;
      await action();
    });
    menu.querySelector('.conversation-action-menu').appendChild(item);
  }
  function historyEntry(row) {
    const entry = document.createElement('div');
    entry.className = 'conversation-history-entry';
    entry.dataset.searchText = [row.title, row.last_message_preview]
      .filter(value => typeof value === 'string' && value)
      .join(' ').toLocaleLowerCase();
    const link = document.createElement('a');
    link.href = pagePath('conversation', row.conversation_id);
    link.textContent = row.title || row.last_message_preview || '未命名对话';
    link.title = link.textContent;
    if (resolveRoute().id === row.conversation_id) link.setAttribute('aria-current', 'page');
    entry.appendChild(link);
    const menu = document.createElement('details');
    menu.className = 'conversation-actions';
    const trigger = document.createElement('summary');
    trigger.setAttribute('aria-label', '对话操作');
    trigger.title = '对话操作';
    trigger.textContent = '···';
    const actionList = document.createElement('div');
    actionList.className = 'conversation-action-menu';
    menu.append(trigger, actionList);
    addHistoryAction(menu, '重命名', () => { openRename(row); });
    if (historyArchived) {
      addHistoryAction(menu, '取消归档', () => runHistoryAction(
        () => restoreConversation(row.conversation_id)));
      addHistoryAction(menu, '永久删除', () => { openDelete(row); }, true);
    } else {
      addHistoryAction(menu, '归档', () => runHistoryAction(async () => {
        await archiveConversation(row.conversation_id);
        if (resolveRoute().id === row.conversation_id) navigatePage(pagePath('assistant'), {replace: true});
      }));
    }
    entry.appendChild(menu);
    return entry;
  }
  async function refreshHistory() {
    const request = ++historyRequest;
    const list = document.getElementById('sidebar-conversations');
    try {
      const mode = historyArchived;
      const data = await getConversations(50, mode);
      if (disposed || request !== historyRequest || mode !== historyArchived) return;
      list.replaceChildren();
      for (const row of (data.conversations || []).slice(0, 50)) {
        if (!row.conversation_id) continue;
        list.appendChild(historyEntry(row));
      }
      if (!list.childElementCount) { const empty = document.createElement('p'); empty.className = 'sidebar-empty'; empty.textContent = historyArchived ? '没有已归档对话' : '新对话会保存在这里'; list.appendChild(empty); }
      filterHistory();
    } catch (error) {
      if (disposed || request !== historyRequest) return;
      list.replaceChildren();
      searchStatus.hidden = true;
      const message = document.createElement('p'); message.className = 'sidebar-empty';
      message.textContent = error.status === 401 ? '登录后查看最近对话' : '历史对话加载失败';
      const retry = document.createElement('button'); retry.type = 'button'; retry.textContent = '重试';
      retry.addEventListener('click', refreshHistory);
      list.append(message, retry);
    }
  }
  refreshHistory();
  window.addEventListener('fleet-conversations-updated', refreshHistory);
  window.addEventListener('focus', refreshHistory);
  const onTitleSave = async () => {
    if (!dialogConversationId) return;
    const title = titleInput.value.trim();
    if (!title) {
      titleError.textContent = '对话名称不能为空';
      titleError.hidden = false;
      titleInput.focus();
      return;
    }
    const conversationId = dialogConversationId;
    titleError.hidden = true;
    titleSave.disabled = true;
    try {
      await renameConversation(conversationId, title);
      titleDialog.close();
      dialogConversationId = null;
      searchStatus.hidden = true;
      await refreshHistory();
    } catch (error) {
      titleError.textContent = (error && (error.detail || error.message)) || '重命名失败，请重试';
      titleError.hidden = false;
      titleInput.focus();
    } finally {
      titleSave.disabled = false;
    }
  };
  titleSave.addEventListener('click', onTitleSave);
  titleInput.addEventListener('keydown', event => {
    if (event.key === 'Enter') { event.preventDefault(); onTitleSave(); }
  });
  function onDialogCancel(event) { event.preventDefault(); event.currentTarget.closest('dialog').close(); }
  const cancelButtons = [...document.querySelectorAll('[data-dialog-cancel]')];
  cancelButtons.forEach(item => item.addEventListener('click', onDialogCancel));
  const onDeleteConfirm = () => {
    if (!deleteConversationId) return;
    const conversationId = deleteConversationId;
    deleteConfirm.disabled = true;
    runHistoryAction(() => deleteConversation(conversationId)).then(succeeded => {
      if (succeeded && resolveRoute().id === conversationId) navigatePage(pagePath('assistant'), {replace: true});
    }).finally(() => {
      deleteConfirm.disabled = false;
      deleteDialog.close();
      deleteConversationId = null;
    });
  };
  deleteConfirm.addEventListener('click', onDeleteConfirm);
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
    const list = nodeList;
    list.replaceChildren();
    document.getElementById('node-count').textContent = String(rows.length);
    if (!rows.length) {
      const empty = document.createElement('p'); empty.className = 'sidebar-empty'; empty.textContent = '尚未连接计算节点'; list.appendChild(empty);
    }
    for (const machine of rows) {
      const link = document.createElement('a'); link.href = pagePath('machine', machine.machine);
      const dot = document.createElement('i'); dot.className = machine.online ? 'node-dot online' : 'node-dot'; link.appendChild(dot);
      const text = document.createElement('span'); text.textContent = machine.machine; link.appendChild(text);
      if (currentRoute.page === 'machine' && currentRoute.id === machine.machine) link.setAttribute('aria-current', 'page');
      list.appendChild(link);
    }
  });
  const dispose = () => {
    disposed = true;
    collapse.removeEventListener('click', onCollapse);
    expand.removeEventListener('click', onExpand);
    search.removeEventListener('input', filterHistory);
    archiveToggle.removeEventListener('click', onArchiveToggle);
    titleSave.removeEventListener('click', onTitleSave);
    deleteConfirm.removeEventListener('click', onDeleteConfirm);
    cancelButtons.forEach(item => item.removeEventListener('click', onDialogCancel));
    resizeHandle.removeEventListener('pointerdown', onResizePointerDown);
    resizeHandle.removeEventListener('pointermove', onResizePointerMove);
    resizeHandle.removeEventListener('pointerup', onResizePointerUp);
    resizeHandle.removeEventListener('pointercancel', onResizePointerUp);
    resizeHandle.removeEventListener('keydown', onResizeKeydown);
    window.removeEventListener('resize', onViewportResize);
    window.removeEventListener('fleet-conversations-updated', refreshHistory);
    window.removeEventListener('focus', refreshHistory);
    unsubscribe();
    mobile.removeEventListener('change', resetDrawer);
    button.removeEventListener('click', onMenu);
    backdrop.removeEventListener('click', onBackdrop);
    document.removeEventListener('keydown', onKeydown);
  };
  dispose.updateRoute = nextRoute => {
    currentRoute = nextRoute;
    updateRouteMetadata();
    if (mobile.matches) resetDrawer();
  };
  return dispose;
}
