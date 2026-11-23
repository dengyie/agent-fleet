import {pagePath, uiIcon} from '../../routes.js';
import {getStatus} from '../../api/client.js';
import {mountCatalog} from '../settings/catalogs.js';
import {el} from './panels.js';
import {nativeChatPath, nativeDirectoryPath} from '../agent-chat/paths.js';

// Presentation only. Node writes use the same settings catalog and permissions
// as the settings dialog; observed machines and execution registrations differ.
export function mountNodePanel(root, store, {identitySignal} = {}) {
  let disposed = false, loading = false, activated = false, catalog, unsubscribe;
  const title = el('h2', 'assistant-section-title', '计算节点');
  const description = el('p', 'meta', '查看电脑与 VM 的连接状态，阅读原生会话、启动 Agent，或管理执行配置。');
  const status = el('p', 'meta'); status.setAttribute('role', 'status');
  const list = el('div', 'workspace-node-list');
  const refresh = el('button', 'button-secondary', '刷新状态'); refresh.type = 'button';
  refresh.appendChild(uiIcon('refresh', {size: 14}));
  const config = el('details', 'workspace-node-config');
  config.appendChild(el('summary', null, '管理执行配置'));
  const editor = el('div', 'workspace-node-editor'); config.appendChild(editor);
  config.addEventListener('toggle', () => { if (config.open && !catalog && !disposed) catalog = mountCatalog(editor, 'nodes', {identitySignal}); });
  const hosts = el('a', 'workspace-node-hosts', '节点展示与心跳设置'); hosts.href = pagePath('settings', 'hosts');
  [title, description, status, refresh, list, config, hosts].forEach(child => root.appendChild(child));
  function render() {
    const snapshot = store?.getState().status;
    if (!snapshot) return;
    const machines = Object.values(store.getState().machines || {});
    const rows = machines.length ? machines : snapshot.machines || [];
    list.replaceChildren();
    for (const machine of rows) {
      const entry = el('section', 'workspace-node-entry');
      const link = el('a', 'workspace-node-row'); link.href = pagePath('machine', machine.machine);
      const dot = el('i', 'node-dot' + (machine.online ? ' online' : ''));
      link.appendChild(dot);
      link.appendChild(el('span', null, machine.machine));
      link.appendChild(el('small', null, machine.online ? '在线' : '离线'));
      const actions = el('div', 'workspace-node-actions');
      const sessions = el('a', null, '查看会话'); sessions.href = nativeDirectoryPath(machine.machine);
      sessions.setAttribute('aria-label', machine.machine + ' · 查看会话');
      const create = el('a', null, '新建 Agent 对话'); create.href = nativeChatPath({machine: machine.machine});
      create.setAttribute('aria-label', machine.machine + ' · 新建 Agent 对话');
      actions.append(sessions, create); entry.append(link, actions); list.append(entry);
    }
    status.textContent = rows.length ? `${rows.filter(row => row.online).length} / ${rows.length} 台机器在线` : '尚未连接机器。可在下方登记执行节点。';
  }
  async function load() {
    if (loading || disposed) return;
    loading = true; refresh.disabled = true; status.textContent = '正在获取节点状态…';
    try {
      const snapshot = await getStatus();
      if (disposed) return;
      if (store) { store.setStatus(snapshot); render(); }
      else status.textContent = '节点状态不可用';
    } catch (error) { if (!disposed) status.textContent = error.detail || '节点状态加载失败，请刷新重试。'; }
    finally { loading = false; refresh.disabled = false; }
  }
  refresh.addEventListener('click', load);
  const dispose = () => { disposed = true; unsubscribe?.(); catalog?.(); };
  dispose.activate = () => {
    if (activated) return;
    activated = true;
    unsubscribe = store?.subscribe(render); render(); load();
  };
  return dispose;
}
