import {getPlatformModels, getConversations, getConversation, cancelRun} from '../../api/platform.js';
import {createWorkflowSummaryGeneration, findWorkflowSummaryGeneration, getWorkflowSummaryGeneration,
  getWorkflowSummaryHead, publishGeneratedWorkflowSummary} from '../../api/workflow-summaries.js';

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}
function field(label, tag = 'input') {
  const wrapper = node('label', label), input = node(tag);
  input.setAttribute('aria-label', label); input.required = true;
  wrapper.append(input); return {wrapper, input};
}
function button(label, action) {
  const element = node('button', label, 'button-secondary'); element.type = 'button';
  element.addEventListener('click', action); return element;
}
const terminal = new Set(['succeeded', 'failed', 'unknown', 'cancelled']);
const errors = {summary_input_too_large: '所选完整内容过大，请减少来源运行后生成。',
  summary_output_too_large: '生成内容过大，无法完整发布；请重新生成或人工编写摘要。',
  summary_output_empty: '模型未返回有效摘要，请重新生成。',
  summary_source_unavailable: '来源运行或对话不可用，请重新选择已成功的运行。',
  revision_conflict: '摘要版本已变化，请读取当前摘要后重新生成。'};

export function mountWorkflowSummaryGeneration(root, {ownerId = '', suspended: initiallySuspended = false, onPublished = () => {}} = {}) {
  const panel = node('section', undefined, 'panel account-panel workflow-summary-generation');
  panel.setAttribute('aria-label', '模型摘要生成');
  const scope = node('p', '请选择摘要工作区', 'meta');
  const status = node('p', '', 'generation-status'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
  const form = node('form', undefined, 'workflow-summary-editor');
  const id = field('生成摘要 ID'), title = field('生成摘要标题'), conversation = field('来源对话', 'select'), model = field('摘要模型', 'select');
  id.input.maxLength = 128; title.input.maxLength = 160;
  const sources = node('fieldset'); sources.append(node('legend', '选择已成功的来源运行（最多 16 项）'));
  const choices = node('div', undefined, 'generation-sources'); sources.append(choices);
  const earlier = button('加载较早来源运行', () => loadTurns(true)); earlier.hidden = true; sources.append(earlier);
  const submit = node('button', '生成摘要候选'); submit.type = 'submit';
  form.append(id.wrapper, title.wrapper, conversation.wrapper, model.wrapper, sources, submit);
  const retry = button('读取或恢复生成请求', () => recover(true));
  const cancel = button('取消生成', () => stopRun());
  const publish = button('发布生成摘要', () => publishResult());
  const actions = node('div', undefined, 'integration-toolbar'); actions.append(retry, cancel, publish);
  const result = node('div', undefined, 'generation-result');
  panel.append(node('h2', '模型摘要生成'), scope,
    node('p', '选择完整成功结果生成候选，确认后发布。已提交的来源与模型保持固定；人工改写请使用人工摘要发布。', 'meta'),
    status, form, actions, result); root.append(panel);
  let disposed = false, suspended = initiallySuspended, busy = false, serial = 0, controller = null, timer = null;
  let workspace = '', pointer = null, item = null, initialized = false, beforeSequence = null;
  const key = () => ownerId && workspace ? 'workflow-summary-generation/' + encodeURIComponent(ownerId) + '/' + encodeURIComponent(workspace) : null;
  function save(value) {
    pointer = value;
    try { if (key()) { if (value) sessionStorage.setItem(key(), JSON.stringify(value)); else sessionStorage.removeItem(key()); } } catch {}
  }
  function readPointer() {
    try {
      const raw = key() ? sessionStorage.getItem(key()) : null;
      if (!raw || raw.length > 8192) return null;
      const value = JSON.parse(raw);
      return value && typeof value === 'object' && value.payload?.workspace_id === workspace &&
        typeof value.payload?.client_token === 'string' ? value : null;
    } catch { return null; }
  }
  function sync() {
    const live = Boolean(item && !terminal.has(item.state)) || Boolean(pointer && !item);
    for (const control of form.querySelectorAll('input,select,button')) control.disabled = busy || live || !workspace;
    submit.disabled ||= !model.input.value || !conversation.input.value;
    earlier.hidden = beforeSequence === null;
    retry.disabled = busy || !workspace;
    cancel.hidden = !item || terminal.has(item.state); cancel.disabled = busy || item?.state === 'cancelling';
    publish.hidden = !item?.candidate || item.published_revision !== null;
    publish.disabled = busy;
    panel.setAttribute('aria-busy', String(busy));
  }
  async function run(operation, apply) {
    if (disposed || suspended || busy) return false;
    const current = serial, request = new AbortController(); controller = request; busy = true; sync();
    try {
      const value = await operation(request.signal);
      if (disposed || suspended || current !== serial || request.signal.aborted) return false;
      apply(value); return true;
    } catch (error) {
      if (disposed || suspended || current !== serial || request.signal.aborted) return false;
      status.textContent = errors[error.code] || error.detail || '生成操作尚未确认，请读取或恢复原请求。';
      return false;
    } finally { if (current === serial) { controller = null; busy = false; sync(); } }
  }
  function schedule() {
    clearTimeout(timer); timer = null;
    if (!disposed && !suspended && item && !terminal.has(item.state)) timer = setTimeout(() => recover(false), 2000);
  }
  function render(value) {
    if (value.workspace_id !== workspace) throw new Error('生成工作区不匹配');
    item = value; result.replaceChildren();
    result.append(node('p', value.title + ' · ' + value.source_run_ids.length + ' 个固定来源', 'meta'));
    if (value.candidate !== null) {
      const content = node('pre', value.candidate); content.style.whiteSpace = 'pre-wrap'; content.style.overflowWrap = 'anywhere'; result.append(content);
    }
    const labels = {queued: '生成任务已排队', running: '模型正在生成摘要', waiting_node: '生成任务正在等待',
      cancelling: '正在取消生成', succeeded: '候选摘要已生成，请查看后发布', failed: '摘要生成失败，可新建生成请求',
      unknown: '模型结果待确认，原请求不会自动重发', cancelled: '摘要生成已取消'};
    status.textContent = value.published_revision !== null ? '此结果已发布为版本 ' + value.published_revision :
      (errors[value.error_code] || labels[value.state]);
    sync(); schedule();
  }
  async function loadTurns(append = false) {
    if (!append) { beforeSequence = null; choices.replaceChildren(); sync(); }
    if (!conversation.input.value) { choices.replaceChildren(); return; }
    const sourceId = conversation.input.value, cursor = append ? beforeSequence : null;
    await run(signal => getConversation(sourceId, {signal, limit: 20, beforeSequence: cursor}), value => {
      if (sourceId !== conversation.input.value) return;
      const data = value.conversation || {}, messages = new Map((data.messages || []).map(message => [message.message_id, message]));
      beforeSequence = data.history?.has_more ? data.history.before_sequence : null;
      for (const empty of choices.querySelectorAll('p')) empty.remove();
      for (const source of data.runs || []) {
        if (source.state !== 'succeeded') continue;
        const label = node('label'), checkbox = node('input'); checkbox.type = 'checkbox'; checkbox.dataset.sourceRun = source.run_id;
        const text = messages.get(source.trigger_message_id)?.content || source.run_id;
        label.append(checkbox, node('span', text.trim().slice(0, 100))); label.style.overflowWrap = 'anywhere';
        choices.append(label);
      }
      if (!choices.children.length) choices.append(node('p', beforeSequence === null ? '此对话没有成功运行，请选择其他对话。' : '本页没有成功运行，可加载较早来源运行。', 'meta'));
    });
  }
  async function load() {
    const okay = await run(signal => Promise.all([getPlatformModels({signal}), getConversations(100, false, {signal})]), values => {
      model.input.replaceChildren(); conversation.input.replaceChildren();
      for (const profile of values[0].models || []) {
        if (!profile.enabled) continue;
        const option = node('option', profile.model || profile.profile_id); option.value = profile.profile_id; model.input.append(option);
      }
      for (const source of values[1].conversations || []) {
        if (source.workspace_id !== workspace) continue;
        const option = node('option', source.title || source.conversation_id); option.value = source.conversation_id; conversation.input.append(option);
      }
      if (pointer?.payload) {
        id.input.value = pointer.payload.summary_id; title.input.value = pointer.payload.title;
        model.input.value = pointer.payload.model_profile_id;
        if (pointer.source_conversation_id) conversation.input.value = pointer.source_conversation_id;
      }
      status.textContent = '请选择成功运行，生成候选摘要。'; initialized = true;
    });
    if (okay && conversation.input.value) await loadTurns();
    if (okay && pointer) await recover(false);
  }
  async function recover(allowRetry) {
    if (!pointer) { await load(); return; }
    const saved = pointer;
    const okay = await run(async signal => {
      if (saved.run_id) return getWorkflowSummaryGeneration(saved.run_id, {signal});
      try { return await findWorkflowSummaryGeneration(saved.payload.client_token, {signal}); }
      catch (error) {
        if (error.status !== 404 || !allowRetry) throw error;
        return createWorkflowSummaryGeneration(saved.payload, {signal});
      }
    }, value => { save({...saved, run_id: value.run_id}); render(value); });
    if (!okay) schedule();
  }
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const ids = [...choices.querySelectorAll('input:checked')].map(input => input.dataset.sourceRun);
    if (!ids.length || ids.length > 16) { status.textContent = '请选择 1–16 个已成功的来源运行。'; return; }
    const selectedWorkspace = workspace, sourceConversation = conversation.input.value;
    const payload = {workspace_id: selectedWorkspace, summary_id: id.input.value.trim(), title: title.input.value.trim(),
      model_profile_id: model.input.value, source_run_ids: ids, client_token: crypto.randomUUID()};
    status.textContent = '正在提交生成请求…';
    await run(async signal => {
      const head = await getWorkflowSummaryHead(selectedWorkspace, payload.summary_id, {signal}).catch(error => {
        if (error.status === 404) return null;
        throw error;
      });
      if (signal.aborted || selectedWorkspace !== workspace) throw new DOMException('Aborted', 'AbortError');
      payload.expected_epoch = head ? head.summary.epoch : 0;
      save({payload, run_id: null, source_conversation_id: sourceConversation});
      item = null; result.replaceChildren(); sync();
      try { return await createWorkflowSummaryGeneration(payload, {signal}); }
      catch (error) { if (error.status >= 400 && error.status < 500 && !signal.aborted) save(null); throw error; }
    }, value => { save({...pointer, run_id: value.run_id}); render(value); });
  });
  async function stopRun() {
    if (!item) return;
    const id = item.run_id;
    const okay = await run(async signal => { await cancelRun(id, {signal}); return getWorkflowSummaryGeneration(id, {signal}); }, render);
    if (!okay) schedule();
  }
  async function publishResult() {
    if (!item?.candidate) return;
    const id = item.run_id;
    await run(signal => publishGeneratedWorkflowSummary(id, {signal}), value => {
      item = {...item, published_revision: value.summary.revision}; render(item);
      status.textContent = '已发布生成摘要版本 ' + value.summary.revision;
      onPublished(value.summary);
    });
  }
  conversation.input.addEventListener('change', () => loadTurns());
  function stop() { serial += 1; controller?.abort(); controller = null; busy = false; clearTimeout(timer); timer = null; sync(); }
  const dispose = () => { disposed = true; stop(); panel.remove(); };
  dispose.setWorkspace = value => {
    if (value === workspace && initialized) return;
    stop(); workspace = value; pointer = readPointer(); item = null; initialized = false;
    id.input.value = ''; title.input.value = ''; beforeSequence = null; choices.replaceChildren(); result.replaceChildren();
    scope.textContent = value ? '当前摘要工作区：' + value : '请选择摘要工作区'; sync();
    if (value) load();
  };
  dispose.suspend = () => { suspended = true; stop(); };
  dispose.resume = () => { if (!disposed && suspended) { suspended = false; if (workspace) load(); } };
  sync(); return dispose;
}
