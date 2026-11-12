import {getPlatformWorkspaces} from '../../api/platform.js';
import {getWorkflowSummaryHead, getWorkflowSummaryVersion, listWorkflowSummaries, publishWorkflowSummary, setWorkflowSummaryEnabled} from '../../api/workflow-summaries.js';

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
function field(tag, label, name, required = true) {
  const wrapper = node('label', label); const input = node(tag); input.name = name; input.required = required;
  wrapper.append(input); return {wrapper, input};
}

export function mountWorkflowSummaries(root, {suspended: initiallySuspended = false} = {}) {
  const panel = node('section', undefined, 'panel account-panel workflow-summary-management');
  panel.setAttribute('aria-label', '工作流摘要管理');
  const status = node('p', '', 'workflow-summary-status'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
  const workspace = node('select'); workspace.setAttribute('aria-label', '摘要工作区');
  const reload = button('重新读取摘要', () => readPage(true));
  const toolbar = node('div', undefined, 'integration-toolbar');
  toolbar.append(node('h2', '工作流摘要管理'), workspace, reload);
  const list = node('div', undefined, 'integration-list'); list.setAttribute('aria-label', '工作流摘要列表');
  const more = button('加载更多摘要', () => readPage(false)); more.hidden = true;
  const listColumn = node('div'); listColumn.append(list, more);
  const inspector = node('section', undefined, 'integration-inspector'); inspector.setAttribute('aria-label', '摘要版本详情');
  const layout = node('div', undefined, 'integration-layout'); layout.append(listColumn, inspector);
  const editor = node('form', undefined, 'workflow-summary-editor');
  editor.append(node('h3', '发布摘要版本'));
  const idField = field('input', '摘要 ID', 'summary_id'); idField.input.maxLength = 128; idField.input.autocomplete = 'off';
  const titleField = field('input', '标题', 'title'); titleField.input.maxLength = 160;
  const contentField = field('textarea', '完整内容（最多 8192 字节 UTF-8）', 'content'); contentField.input.maxLength = 8192;
  contentField.input.rows = 8;
  const publish = node('button', '发布新版本'); publish.type = 'submit';
  editor.append(idField.wrapper, titleField.wrapper, contentField.wrapper, publish);
  panel.append(toolbar, node('p', '摘要由管理员明确编写并按不可变版本保存；发布不会自动生成或选择摘要，也不会改变已提交流程。', 'meta'), status, layout, editor);
  root.append(panel);

  let disposed = false, suspended = initiallySuspended, busy = false, generation = 0, controller = null;
  let cursor = null, selected = null, detail = null, rows = new Map(), workspaces = [];
  let pendingPublish = null;

  function sync() {
    reload.disabled = busy || !workspace.value; more.disabled = busy; more.hidden = cursor === null;
    workspace.disabled = busy;
    for (const element of list.querySelectorAll('button')) element.disabled = busy;
    for (const element of inspector.querySelectorAll('button')) element.disabled = busy;
    for (const element of editor.querySelectorAll('input,textarea,button')) element.disabled = busy;
    panel.setAttribute('aria-busy', String(busy));
  }
  async function run(operation, apply) {
    if (disposed || suspended || busy) return false;
    const current = generation, request = new AbortController(); controller = request; busy = true; sync();
    try {
      const result = await operation(request.signal);
      if (disposed || suspended || current !== generation) return false;
      apply(result); return true;
    } catch (error) {
      if (disposed || suspended || current !== generation || request.signal.aborted) return false;
      status.textContent = error.detail || '摘要操作失败，请重新读取';
      return false;
    } finally {
      if (current === generation) { controller = null; busy = false; sync(); }
    }
  }
  function drawList() {
    const fragment = document.createDocumentFragment();
    for (const item of rows.values()) {
      const row = button('', () => readDetail(item)); row.classList.add('integration-row');
      row.dataset.summaryId = item.summary_id;
      row.setAttribute('aria-pressed', String(selected === item.summary_id));
      row.append(node('strong', item.summary_id), node('span', item.enabled ? '已启用' : '已停用', 'integration-state'),
        node('span', '版本 ' + item.latest_revision + ' · epoch ' + item.epoch, 'meta'));
      fragment.append(row);
    }
    list.replaceChildren(fragment);
    if (!rows.size) list.append(node('p', '此工作区尚无摘要', 'meta'));
    sync();
  }
  function drawDetail() {
    inspector.replaceChildren();
    if (!detail) { inspector.append(node('p', '选择摘要后查看当前版本', 'meta')); return; }
    const head = detail.head;
    inspector.append(node('h3', head.summary_id), node('p', (head.enabled ? '已启用' : '已停用') + ' · epoch ' + head.epoch, 'integration-state'));
    const state = button(head.enabled ? '停用摘要' : '启用摘要', () => changeEnabled(!head.enabled));
    inspector.append(state);
    if (detail.version) {
      inspector.append(node('h4', '版本 ' + detail.version.revision + ' · ' + detail.version.title),
        node('p', detail.version.created_by + ' · ' + new Date(detail.version.created_at_ms).toLocaleString()),
        node('pre', detail.version.content), node('small', 'SHA-256 ' + detail.version.sha256));
    }
    const revision = field('input', '读取历史版本', 'revision'); revision.input.type = 'number'; revision.input.min = '1'; revision.input.max = String(head.latest_revision);
    revision.input.value = String(head.latest_revision);
    const read = button('读取版本', () => readRevision(Number(revision.input.value)));
    const history = node('div', undefined, 'workflow-summary-history'); history.append(revision.wrapper, read); inspector.append(history);
    sync();
  }
  async function loadWorkspaces() {
    status.textContent = '正在读取工作区…';
    const okay = await run(signal => getPlatformWorkspaces({signal}), result => {
      workspaces = Array.isArray(result.workspaces) ? result.workspaces : [];
      workspace.replaceChildren();
      for (const item of workspaces) {
        if (typeof item.workspace_id !== 'string') continue;
        const option = node('option', item.name || item.workspace_id); option.value = item.workspace_id; workspace.append(option);
      }
      status.textContent = workspaces.length ? '已读取工作区' : '当前没有可用工作区';
    });
    if (okay && workspace.value) await readPage(true);
  }
  async function readPage(reset) {
    if (busy || disposed || suspended || !workspace.value) return;
    const keep = reset ? selected : null; status.textContent = '正在读取摘要…';
    await run(async signal => {
      const page = await listWorkflowSummaries(workspace.value, reset ? null : cursor, {signal});
      const head = keep ? page.summaries.find(item => item.summary_id === keep) : null;
      const version = head ? await getWorkflowSummaryVersion(workspace.value, keep, head.latest_revision, {signal}) : null;
      return {page, head, version};
    }, ({page, head, version}) => {
      if (reset) rows.clear();
      for (const item of page.summaries) rows.set(item.summary_id, item);
      cursor = page.next_cursor;
      if (!rows.has(selected)) { selected = null; detail = null; }
      if (head && version) { selected = keep; detail = {head, version: version.summary}; }
      drawList();
      drawDetail();
      status.textContent = head ? '已读取 ' + rows.size + ' 条摘要' : (rows.size ? '已读取 ' + rows.size + ' 条摘要' : '此工作区尚无摘要');
    });
  }
  async function readDetail(item) {
    selected = item.summary_id; detail = null; drawList(); drawDetail(); status.textContent = '正在读取摘要版本…';
    await run(async signal => {
      const version = await getWorkflowSummaryVersion(workspace.value, item.summary_id, item.latest_revision, {signal});
      return {head: item, version: version.summary};
    }, result => {
      detail = result; rows.set(item.summary_id, result.head); drawList(); drawDetail();
      idField.input.value = item.summary_id;
      titleField.input.value = result.version.title;
      contentField.input.value = result.version.content;
      status.textContent = '已读取不可变版本 ' + result.version.revision;
    });
  }
  async function readRevision(revision) {
    if (!detail || !Number.isSafeInteger(revision) || revision < 1 || revision > detail.head.latest_revision) {
      status.textContent = '请输入有效的历史版本号'; return;
    }
    await run(signal => getWorkflowSummaryVersion(workspace.value, selected, revision, {signal}), result => {
      detail = {...detail, version: result.summary}; drawDetail(); status.textContent = '已读取历史版本 ' + revision;
    });
  }
  async function reconcileAfterWrite(message) {
    const id = selected || idField.input.value.trim();
    await run(async signal => {
      const page = await listWorkflowSummaries(workspace.value, null, {signal});
      const headResult = await getWorkflowSummaryHead(workspace.value, id, {signal}).catch(error => {
        if (error.status === 404) return null;
        throw error;
      });
      const head = headResult ? headResult.summary : null;
      const version = head ? await getWorkflowSummaryVersion(workspace.value, id, head.latest_revision, {signal}) : null;
      return {page, head, version};
    }, ({page, head, version}) => {
      rows = new Map(page.summaries.map(item => [item.summary_id, item]));
      cursor = page.next_cursor; selected = head ? id : null;
      detail = head ? {head, version: version.summary} : null;
      drawList(); drawDetail();
      status.textContent = message + (head ? '；已读取服务端当前版本 ' + head.latest_revision : '；尚无已提交版本');
    });
  }
  async function changeEnabled(enabled) {
    if (!detail || busy) return;
    const head = detail.head;
    status.textContent = '正在更新摘要状态…';
    const okay = await run(signal => setWorkflowSummaryEnabled({workspace_id: workspace.value,
      summary_id: head.summary_id, expected_epoch: head.epoch, enabled}, {signal}), result => {
      detail = {...detail, head: result.summary}; rows.set(head.summary_id, result.summary); drawList(); drawDetail();
      status.textContent = enabled ? '摘要已启用' : '摘要已停用';
    });
    if (!okay && !disposed && !suspended) await reconcileAfterWrite('状态变更结果待核对');
  }
  editor.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy || !workspace.value) return;
    const summaryId = idField.input.value.trim();
    const existing = rows.get(summaryId);
    const payload = {workspace_id: workspace.value, summary_id: summaryId, title: titleField.input.value,
      content: contentField.input.value, expected_epoch: existing ? existing.epoch : 0,
      client_token: crypto.randomUUID()};
    pendingPublish = payload;
    status.textContent = '正在发布不可变摘要版本…';
    let ambiguous = false;
    const okay = await run(async signal => {
      try { return await publishWorkflowSummary(payload, {signal}); }
      catch (error) { ambiguous = !(error.status >= 400 && error.status < 500); throw error; }
    }, result => {
      pendingPublish = null; selected = summaryId;
      status.textContent = '摘要版本 ' + result.summary.revision + ' 已发布';
    });
    if (!okay && !disposed && !suspended) {
      const message = ambiguous ? '发布响应未确认，请先读取当前状态' : '发布未完成，请核对当前状态';
      pendingPublish = null;
      await reconcileAfterWrite(message);
    } else if (okay) await reconcileAfterWrite('已发布');
  });
  workspace.addEventListener('change', () => { selected = null; detail = null; cursor = null; rows.clear(); drawList(); drawDetail(); readPage(true); });
  function stop() { generation += 1; controller?.abort(); controller = null; busy = false; sync(); }
  function dispose() { disposed = true; stop(); rows.clear(); detail = null; panel.remove(); }
  dispose.suspend = () => { suspended = true; stop(); };
  dispose.resume = () => { if (!disposed && suspended) { suspended = false; loadWorkspaces(); } };
  drawList(); drawDetail(); loadWorkspaces();
  return dispose;
}
