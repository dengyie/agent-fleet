import { uiIcon, pagePath, navigatePage } from '../routes.js';
import {mountMenu} from './menus.js';
import {listSessions} from '../api/client.js';
import {nativeChatPath} from '../views/agent-chat/paths.js';
import {
  getConversations, renameConversation, archiveConversation,
  restoreConversation, deleteConversation,
} from '../api/platform.js';
const labels = {
  plugins: ['插件中心', '插件中心', '管理官方插件及其在各节点上的运行状态。', 'PLUGINS'],
  settings: ['设置', '设置', '管理模型源、VM、工作区、集成与系统参数。', 'SETTINGS'],
  fleet: ['总览', '工作空间总览', '系统近况，一目了然。', 'WORKSPACE OVERVIEW'],
  assistant: ['主助手', '主助手', '从一个想法开始，让助手把工作推进到完成。', 'ASSISTANT'],
  muse: ['Muse', 'Muse', '连接 Muse，管理你的对话和任务。', 'MUSE'],
  monitoring: ['服务监控', '服务监控', '查看服务健康与待处理事件，按授权执行操作。', 'SERVICE HEALTH'],
  machine: ['计算节点', '节点详情', '资源状态、运行中的任务与纳管会话。', 'COMPUTE NODE'],
  task: ['任务', '任务详情', '跟踪执行过程，查看结果与交付文件。', 'TASK DETAIL'],
  session: ['会话', '会话详情', '连接你的执行会话，保留完整工作上下文。', 'SESSION'],
  'session-search': ['会话搜索', '会话搜索', '搜索脱敏会话记录，定位消息与工具结果。', 'SESSION SEARCH'],
  usage: ['用量看板', '用量看板', '按日期、项目、机器、Agent 和模型查看用量与费用来源。', 'USAGE'],
  'agent-chat': ['电脑 Agent', '电脑 Agent', '连接电脑上的原生会话。', 'COMPUTER AGENT'],
  sessions: ['电脑会话', '电脑会话', '阅读已同步记录，继续原生会话。', 'COMPUTER SESSIONS'],
};
// The single allow-list of pages a non-admin account may open. Click/history
// navigation and the post-login guard both consult it, so they cannot drift.
export const ACCOUNT_PAGES = Object.freeze(['assistant', 'settings', 'usage', 'muse']);
export function isPageAllowedForAccount(page, account) {
  return !account || account.role === 'admin' || ACCOUNT_PAGES.includes(page);
}
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
  if (page === 'account') return {page: 'settings', id: 'account'};
  if (page === 'conversation') return {page: 'assistant', id};
  if (page === 'assistant' && query.get('view') === 'sessions') return {page: 'sessions', machine: query.get('machine') || ''};
  if (page === 'assistant' && query.get('view') === 'computer') return {page: 'agent-chat', nativeKind: 'new', machine: query.get('machine') || ''};
  if ((page === 'session' || page === 'task') && id && query.get('view') === 'chat') return {page: 'agent-chat', nativeKind: page, id};
  return {page: labels[page] ? page : 'invalid', id,
    sequence: page === 'session' ? query.get('sequence') : null};
}
export function mountNavigation(route, store, account) {
  const canAdminister = !account || account.role === 'admin';
  document.querySelectorAll('[data-native-entry]').forEach(node => { node.hidden = !canAdminister; });
  document.querySelectorAll('[data-icon]').forEach(slot => slot.replaceChildren(uiIcon(slot.dataset.icon, {size: 18})));
  const disposeAccountMenu = mountMenu(document.getElementById('account-menu-trigger'), document.getElementById('account-menu'));
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
    document.querySelectorAll('#sidebar-conversations a').forEach(link => {
      const target = resolveRoute(new URL(link.href, window.location.href));
      if (target.page === currentRoute.page && target.id === currentRoute.id && target.nativeKind === currentRoute.nativeKind) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
  }
  updateRouteMetadata();
  const sidebar = document.getElementById('sidebar');
  const backdrop = document.getElementById('sidebar-backdrop');
  const button = document.getElementById('navigation-toggle');
  const mobile = window.matchMedia('(max-width: 767px)');
  const search = document.getElementById('conversation-search');
  const searchStatus = document.getElementById('conversation-search-status');
  const historyHeading = document.getElementById('conversation-heading');
  const archiveToggle = document.getElementById('archived-toggle');
  const historyList = document.getElementById('sidebar-conversations');
  function closeHistoryMenus(except = null) {
    historyList.querySelectorAll('.conversation-actions[open]').forEach(menu => { if (menu !== except) menu.open = false; });
  }
  const onHistoryOutside = event => closeHistoryMenus(event.target.closest('.conversation-actions'));
  document.addEventListener('pointerdown', onHistoryOutside);
  document.addEventListener('focusin', onHistoryOutside);
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
    const open = mobile.matches ? sidebar.classList.contains('open') : !collapsed;
    sidebar.inert = !open;
    if (!open) closeHistoryMenus();
    button.setAttribute('aria-expanded', String(open));
    const label = open ? '收起导航' : mobile.matches ? '打开导航' : '展开导航';
    button.setAttribute('aria-label', label);
    button.title = label;
  }
  function setCollapsed(value) {
    collapsed = value;
    try { localStorage.setItem('fleet-sidebar-collapsed', String(value)); } catch (_) {}
    applyCollapse();
    button.focus();
  }
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
    const restoreFocus = sidebar.contains(document.activeElement);
    document.getElementById('account-menu').hidePopover();
    closeHistoryMenus();
    sidebar.classList.remove('open');
    document.body.classList.remove('navigation-open');
    applyCollapse();
    if (restoreFocus && sidebar.inert) button.focus();
  }
  resetDrawer();
  mobile.addEventListener('change', resetDrawer);
  function toggle(open) {
    sidebar.classList.toggle('open', open);
    document.body.classList.toggle('navigation-open', open);
    applyCollapse();
    if (open) sidebar.querySelector('a').focus(); else button.focus();
  }
  const onMenu = () => mobile.matches ? toggle(button.getAttribute('aria-expanded') !== 'true') : setCollapsed(!collapsed);
  const onBackdrop = () => toggle(false);
  button.addEventListener('click', onMenu);
  backdrop.addEventListener('click', onBackdrop);
  function onKeydown(event) {
    if (document.querySelector('.shell-menu:popover-open')) return;
    if (document.querySelector('dialog:modal')) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') event.preventDefault();
      return;
    }
    const historyMenu = historyList.querySelector('.conversation-actions[open]');
    if (historyMenu && event.key === 'Escape') {
      event.preventDefault(); closeHistoryMenus(); historyMenu.querySelector('summary').focus(); return;
    }
    if (historyMenu && historyMenu.contains(event.target)) {
      if (event.key === 'Tab') closeHistoryMenus();
      else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key) && !event.isComposing && !event.metaKey && !event.ctrlKey && !event.altKey) {
        event.preventDefault();
        const items = [...historyMenu.querySelectorAll('button:not(:disabled)')];
        const current = items.indexOf(document.activeElement);
        const index = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1
          : (current + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
        items[index]?.focus(); return;
      }
    }
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      if (mobile.matches) toggle(true); else if (collapsed) setCollapsed(false);
      search.focus();
      return;
    }
    if (!mobile.matches || button.getAttribute('aria-expanded') !== 'true') return;
    if (event.key === 'Escape') toggle(false);
    if (event.key === 'Tab') {
      const links = [button, ...sidebar.querySelectorAll('a, button, input, summary')].filter(node => !node.hidden && !node.disabled && node.getClientRects().length);
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
    trigger.append(uiIcon('sliders', {size: 16}));
    trigger.addEventListener('keydown', event => {
      if (!['ArrowDown', 'ArrowUp'].includes(event.key) || event.isComposing || event.metaKey || event.ctrlKey || event.altKey) return;
      event.preventDefault(); event.stopPropagation(); closeHistoryMenus(menu); menu.open = true;
      const items = [...menu.querySelectorAll('button:not(:disabled)')];
      (event.key === 'ArrowUp' ? items.at(-1) : items[0])?.focus();
    });
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
        if (resolveRoute().page === 'assistant' && resolveRoute().id === row.conversation_id) navigatePage(pagePath('assistant'), {replace: true});
      }));
    }
    entry.appendChild(menu);
    return entry;
  }
  function nativeHistoryEntry(row) {
    const entry = document.createElement('div'); entry.className = 'conversation-history-entry native-history-entry';
    entry.dataset.searchText = [row.agent_family, row.machine_id, row.session_id].join(' ').toLocaleLowerCase();
    const link = document.createElement('a'); link.href = nativeChatPath({sessionId: row.session_id});
    const text = document.createElement('span'); text.textContent = [row.agent_family || 'Agent', row.machine_id].filter(Boolean).join(' · ');
    link.append(uiIcon('monitor', {size: 13}), text); link.title = text.textContent + ' · 电脑会话';
    if (currentRoute.page === 'agent-chat' && currentRoute.nativeKind === 'session' && currentRoute.id === row.session_id) link.setAttribute('aria-current', 'page');
    entry.append(link); return entry;
  }
  async function refreshHistory() {
    const request = ++historyRequest;
    const list = document.getElementById('sidebar-conversations');
    const mode = historyArchived;
    let assistantRows = [], nativeRows = [], failed = [], pending = 2;
    function paintHistory() {
      if (disposed || request !== historyRequest || mode !== historyArchived) return;
      list.replaceChildren();
      const items = assistantRows.filter(row => row.conversation_id).map(row => ({row, native: false}));
      items.push(...nativeRows.filter(row => row.session_id).map(row => ({row, native: true})));
      items.sort((a, b) => (Date.parse(b.row.updated_at || b.row.created_at) || 0) - (Date.parse(a.row.updated_at || a.row.created_at) || 0));
      for (const item of items.slice(0, 50)) {
        list.appendChild(item.native ? nativeHistoryEntry(item.row) : historyEntry(item.row));
      }
      if (!list.childElementCount) {
        const empty = document.createElement('p'); empty.className = 'sidebar-empty';
        empty.textContent = pending ? '正在获取对话…' : failed.length ? failed.join('；') : historyArchived ? '没有已归档对话' : '新对话会保存在这里';
        list.appendChild(empty);
      }
      if (!pending && failed.length) {
        const retry = document.createElement('button'); retry.type = 'button'; retry.textContent = '重试加载对话';
        retry.title = failed.join('；'); retry.addEventListener('click', refreshHistory); list.append(retry);
      }
      filterHistory();
    }
    // Each source publishes independently: a slow node/session API cannot hold
    // the owner's platform history behind its own timeout (and vice versa).
    await Promise.all([
      getConversations(50, mode).then(data => { assistantRows = data.conversations || []; }, () => { failed.push('主助手历史加载失败'); }).finally(() => { pending--; paintHistory(); }),
      (canAdminister && !mode ? listSessions({limit: 12}) : Promise.resolve({sessions: []})).then(data => { nativeRows = data.sessions; }, () => { failed.push('电脑会话加载失败'); }).finally(() => { pending--; paintHistory(); }),
    ]);
  }
  refreshHistory();
  window.addEventListener('fleet-conversations-updated', refreshHistory);
  window.addEventListener('fleet-native-sessions-updated', refreshHistory);
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
      if (succeeded && resolveRoute().page === 'assistant' && resolveRoute().id === conversationId) navigatePage(pagePath('assistant'), {replace: true});
    }).finally(() => {
      deleteConfirm.disabled = false;
      deleteDialog.close();
      deleteConversationId = null;
    });
  };
  deleteConfirm.addEventListener('click', onDeleteConfirm);
  const unsubscribe = store.subscribe(() => {
    const state = store.getState();
    const status = document.getElementById('connection-status');
    status.dataset.state = state.connection;
    status.querySelector('span').textContent = state.connection === 'open' ? '实时连接' : state.connection === 'connecting' ? '正在连接' : '连接中断';
  });
  const dispose = () => {
    disposed = true;
    disposeAccountMenu();
    search.removeEventListener('input', filterHistory);
    archiveToggle.removeEventListener('click', onArchiveToggle);
    titleSave.removeEventListener('click', onTitleSave);
    deleteConfirm.removeEventListener('click', onDeleteConfirm);
    cancelButtons.forEach(item => item.removeEventListener('click', onDialogCancel));
    window.removeEventListener('fleet-conversations-updated', refreshHistory);
    window.removeEventListener('fleet-native-sessions-updated', refreshHistory);
    window.removeEventListener('focus', refreshHistory);
    unsubscribe();
    mobile.removeEventListener('change', resetDrawer);
    button.removeEventListener('click', onMenu);
    backdrop.removeEventListener('click', onBackdrop);
    document.removeEventListener('keydown', onKeydown);
    document.removeEventListener('pointerdown', onHistoryOutside);
    document.removeEventListener('focusin', onHistoryOutside);
  };
  dispose.updateRoute = nextRoute => {
    currentRoute = nextRoute;
    updateRouteMetadata();
    if (mobile.matches) resetDrawer();
  };
  return dispose;
}
