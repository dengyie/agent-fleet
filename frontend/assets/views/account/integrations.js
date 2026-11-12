import {listMCPConnectors, getMCPConnector, manageMCPConnector} from '../../api/integrations.js';

const stateLabels = {registered: '待发现', active: '已批准', pending: '有待审版本', revoked: '已撤销', restore_disabled: '恢复后禁用'};
function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}
function button(text, action) {
  const element = node('button', text, 'button-secondary'); element.type = 'button';
  element.addEventListener('click', action); return element;
}

export function mountMCPIntegrations(root, {suspended: initiallySuspended = false} = {}) {
  const section = node('section', undefined, 'panel account-panel mcp-management');
  section.setAttribute('aria-label', 'MCP 集成管理');
  const heading = node('h2', 'MCP 集成管理');
  const status = node('p', '', 'mcp-status'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
  const reload = button('重新读取目录', () => readPage(true));
  const toolbar = node('div', undefined, 'mcp-toolbar'); toolbar.append(heading, reload);
  const list = node('div', undefined, 'mcp-list'); list.setAttribute('aria-label', 'MCP 连接目录');
  const more = button('加载更多连接', () => readPage(false)); more.hidden = true;
  const listColumn = node('div'); listColumn.append(list, more);
  const inspector = node('section', undefined, 'mcp-inspector'); inspector.setAttribute('aria-label', 'MCP 连接详情');
  const layout = node('div', undefined, 'mcp-layout'); layout.append(listColumn, inspector);
  section.append(toolbar, node('p', '审阅已登记的只读工具、批准待审版本或撤销连接。地址、密钥与运行时适配器由管理员可信配置管理。', 'meta'), status, layout);
  root.append(section);
  const rows = new Map();
  let disposed = false, suspended = initiallySuspended, generation = 0, busy = false, cursor = null;
  let selected = null, detail = null, dialog = null, controller = null;
  let actionButtons = [];

  function sync() {
    reload.disabled = busy; more.disabled = busy; more.hidden = cursor === null;
    for (const element of list.querySelectorAll('button')) element.disabled = busy;
    for (const element of actionButtons) element.disabled = busy;
    section.setAttribute('aria-busy', String(busy));
  }
  async function run(operation, apply, fail) {
    if (disposed || suspended || busy) return false;
    const current = generation; const request = new AbortController(); controller = request;
    busy = true; sync();
    try {
      const result = await operation(request.signal);
      if (disposed || suspended || generation !== current) return false;
      apply(result); return true;
    } catch (error) {
      if (disposed || suspended || generation !== current || request.signal.aborted) return false;
      if (fail) fail(error);
      else status.textContent = error.detail || '目录读取失败，请重新读取';
      return false;
    } finally {
      if (generation === current) { controller = null; busy = false; sync(); }
    }
  }
  function drawList() {
    const fragment = document.createDocumentFragment();
    for (const item of rows.values()) {
      const row = button('', () => readDetail(item.connector_id));
      row.classList.add('mcp-row'); row.dataset.connectorId = item.connector_id;
      row.setAttribute('aria-pressed', String(selected === item.connector_id));
      row.append(node('strong', item.connector_id), node('span', stateLabels[item.state], 'mcp-state'),
        node('span', item.owner_id + ' / ' + item.workspace_id, 'meta'));
      fragment.append(row);
    }
    list.replaceChildren(fragment);
    if (!rows.size) list.append(node('p', '尚无连接登记', 'meta'));
    sync();
  }
  function snapshot(title, value, expanded = false) {
    if (value === null) return node('p', title + '：无', 'meta');
    const disclosure = node('details', undefined, 'mcp-snapshot'); disclosure.open = expanded;
    disclosure.append(node('summary', title + ' ' + value.revision),
      node('p', '内容摘要 ' + value.content_digest, 'meta'), node('pre', JSON.stringify(value, null, 2)));
    return disclosure;
  }
  function drawDetail() {
    actionButtons = []; inspector.replaceChildren();
    if (!detail) { inspector.append(node('p', '选择连接后查看完整定义', 'meta')); return; }
    const item = detail.summary;
    inspector.append(node('h3', item.connector_id), node('p', stateLabels[item.state], 'mcp-state'));
    const metadata = node('dl', undefined, 'mcp-metadata');
    const values = [['归属', item.owner_id + ' / ' + item.workspace_id], ['服务来源', item.endpoint_origin],
      ['固定工具', item.selected_tools.join(', ')], ['权限', 'read_only / read_only'],
      ['调用上限', item.timeout_s + ' 秒 / ' + item.result_limit_bytes + ' 字节'],
      ['目录版本', String(item.epoch)], ['运行时适配器', item.adapter_attached ? '本进程已附着' : '本进程未附着'],
      ['本进程工具', item.runtime_ready ? '可提供已批准工具；未检测远端健康' : '暂不提供工具'],
      ['创建时间 UTC', item.created_at], ['更新时间 UTC', item.updated_at]];
    if (item.revoked_at !== null) values.push(['撤销时间 UTC', item.revoked_at]);
    for (const [label, value] of values) metadata.append(node('dt', label), node('dd', value));
    inspector.append(metadata, node('p', '以下是外部服务提供的定义文本。请核对协议、服务身份、描述、schema 与 annotations 后再批准。', 'meta'),
      snapshot('已批准版本', detail.active), snapshot('待审版本', detail.pending, true));
    const actions = node('div', undefined, 'mcp-actions');
    if (item.state === 'pending') actionButtons.push(button('批准待审版本', event => confirm('admit', detail, event.currentTarget)));
    if (!['revoked', 'restore_disabled'].includes(item.state)) actionButtons.push(button('撤销连接', event => confirm('revoke', detail, event.currentTarget)));
    actions.append(...actionButtons); inspector.append(actions); sync();
  }
  function applyDetail(value) {
    detail = value.connector; selected = detail.summary.connector_id;
    if (rows.has(selected)) rows.set(selected, detail.summary);
    drawList(); drawDetail();
  }
  async function readPage(reset) {
    if (busy || disposed || suspended) return;
    closeDialog(); status.textContent = '正在读取目录…';
    const keep = reset ? selected : null;
    if (reset) { detail = null; drawDetail(); }
    await run(async signal => {
      const page = await listMCPConnectors(reset ? null : cursor, {signal});
      const record = keep === null ? null : await getMCPConnector(keep, {signal});
      return {page, record};
    }, ({page, record}) => {
      if (reset) rows.clear();
      for (const item of page.connectors) rows.set(item.connector_id, item);
      cursor = page.next_cursor; drawList();
      if (record) applyDetail(record);
      status.textContent = rows.size ? '已读取 ' + rows.size + ' 条连接登记' : '尚无连接登记；请通过可信配置登记';
    });
  }
  async function readDetail(connectorId, message = '') {
    if (busy) return;
    closeDialog(); detail = null; drawDetail(); status.textContent = '正在读取连接定义…';
    await run(signal => getMCPConnector(connectorId, {signal}), result => {
      applyDetail(result); status.textContent = message || '已读取当前定义';
    });
  }
  function closeDialog() {
    if (dialog) dialog.close();
  }
  function confirm(action, original, opener) {
    if (busy || disposed || suspended || dialog) return;
    const item = original.summary; const admission = action === 'admit';
    const modal = node('dialog', undefined, 'mcp-confirm'); dialog = modal;
    const title = admission ? '批准待审版本' : '撤销连接';
    const label = node('h3', title); label.id = 'mcp-confirm-title'; modal.setAttribute('aria-labelledby', label.id);
    modal.append(label, node('p', item.connector_id + ' · 目录版本 ' + item.epoch +
      (admission ? ' · 待审版本 ' + item.pending_revision : '')),
      node('p', admission ? '批准后新运行可以选择此定义；已有运行继续受原版本约束。' :
        '撤销不可恢复。后续工具授权将被拒绝；已开始的调用保留实际结果，已有任务不会因此取消。'));
    const submit = button(admission ? '确认批准' : '确认撤销', async () => {
      closeDialog(); await mutate(action, item);
    });
    if (admission) {
      const check = node('input'); check.type = 'checkbox'; const wrapper = node('label');
      wrapper.className = 'mcp-review-check'; wrapper.append(check, node('span', '已核对待审定义'));
      submit.disabled = true; check.addEventListener('change', () => { submit.disabled = !check.checked; }); modal.append(wrapper);
    }
    const controls = node('div', undefined, 'mcp-actions'); controls.append(button('取消', () => modal.close()), submit);
    modal.append(controls); section.append(modal);
    modal.addEventListener('close', () => { modal.remove(); if (dialog === modal) dialog = null; if (opener.isConnected) opener.focus(); }, {once: true});
    modal.showModal();
  }
  async function mutate(action, item) {
    let conflict = false;
    const payload = {expected_epoch: item.epoch};
    if (action === 'admit') payload.pending_revision = item.pending_revision;
    status.textContent = '正在提交目录操作…';
    await run(signal => manageMCPConnector(item.connector_id, action, payload, {signal}), result => {
      applyDetail(result); status.textContent = action === 'admit' ? '待审版本已批准' : '连接已撤销';
    }, error => {
      detail = null; drawDetail();
      conflict = error.status === 409;
      status.textContent = conflict ? '目录状态已变化，正在读取当前定义…' :
        '操作结果待核对，请重新读取目录。请勿重复提交。';
    });
    if (conflict) await readDetail(item.connector_id, '目录状态已变化，已读取当前定义，请重新审阅');
  }
  function stop() {
    generation += 1; controller?.abort(); controller = null; busy = false; closeDialog();
  }
  function dispose() { disposed = true; stop(); rows.clear(); detail = null; section.remove(); }
  dispose.suspend = () => { suspended = true; stop(); };
  dispose.resume = () => { if (!disposed && suspended) { suspended = false; readPage(true); } };
  drawDetail(); if (!suspended) readPage(true);
  return dispose;
}
