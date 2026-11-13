import { el, clear, showError } from './panels.js';

const ACTIVE = new Set(['queued', 'running', 'waiting_node', 'waiting_external', 'waiting_approval', 'paused']);
const REASONS = {
  external_agent_reported: '外部 Agent 自报结束，校验与验收状态见下方',
  external_confirmed: '外部执行已确认结束', external_input_required: '外部 Agent 正在等待输入',
  external_observation_unavailable: '暂时无法读取外部任务，继续等待原任务回执',
  external_agents_disabled: '外部 Agent 派发已关闭', external_configuration_changed: '外部 Agent 配置已变更，需要核对原任务',
  external_status_unconfirmed: '外部任务状态尚未确认', external_stop_unconfirmed: '尚未确认外部任务停止',
};
const LABELS = {
  queued: '排队中', pending: '等待中', ready: '准备执行', dispatching: '正在派发', running: '执行中', waiting_node: '等待节点',
  waiting_external: '等待执行回执', waiting_approval: '等待授权', paused: '已暂停', cancelling: '正在停止', succeeded: '执行完成',
  failed: '失败', unknown: '需要核对', cancelled: '已取消', not_requested: '未要求', passed: '通过', inconclusive: '不确定',
  accepted: '已接受', rejected: '已退回', validation_failed: '校验失败', awaiting_validation: '等待校验',
  awaiting_acceptance: '等待人工验收', delivered: '已交付', in_progress: '进行中', needs_reconciliation: '需要核对', execution_failed: '执行失败'
};
function status(value) { return LABELS[value] || value || '未知'; }
function token() { return crypto.randomUUID(); }
function displayState(workflow) {
  return workflow.delivery_state && (workflow.execution_state === 'succeeded' || workflow.validation_state === 'failed')
    ? status(workflow.delivery_state) : status(workflow.execution_state);
}
function button(label, action, disabled = false, focusKey = label) {
  const node = el('button', 'button-secondary', label); node.type = 'button'; node.disabled = disabled;
  node.dataset.workflowFocus = focusKey;
  node.addEventListener('click', action); return node;
}
function restoreFocus(container, previous) {
  if (!previous) return;
  const key = previous.dataset.workflowFocus;
  const replacement = key ? container.querySelector('[data-workflow-focus="' + CSS.escape(key) + '"]') : container;
  const target = replacement && !replacement.disabled ? replacement : container;
  if (target === container) container.tabIndex = -1;
  target.focus({preventScroll: true});
}

export function mountWorkflowPanel(target, options = {}) {
  const service = options.service;
  if (!service) throw new TypeError('Workflow panel requires an assistant service');
  clear(target);
  const root = el('section', 'assistant-workflow-panel'); root.setAttribute('aria-label', '工作流');
  const heading = el('div', 'assistant-workflow-heading'); heading.appendChild(el('h2', 'assistant-section-title', '工作流交付'));
  const refresh = button('刷新流程', () => refreshAll()); heading.appendChild(refresh); root.appendChild(heading);
  const notice = el('div', 'assistant-workflow-notice meta'); notice.setAttribute('role', 'status'); root.appendChild(notice);
  const readNotice = el('div', 'assistant-workflow-notice meta'); readNotice.setAttribute('role', 'status'); root.appendChild(readNotice);
  const retryHost = el('div', 'assistant-workflow-actions'); root.appendChild(retryHost);
  const form = el('div', 'assistant-workflow-create'); const template = el('select'); template.setAttribute('aria-label', '流程模板'); form.appendChild(template);
  const inputs = el('div', 'assistant-workflow-inputs meta'); form.appendChild(inputs);
  template.addEventListener('change', renderTemplateInputs);
  const create = button('创建流程', () => createSelected(), true); form.appendChild(create); root.appendChild(form);
  const list = el('div', 'assistant-workflow-list'); root.appendChild(list);
  const detail = el('div', 'assistant-workflow-detail'); root.appendChild(detail);
  target.appendChild(root);

  let disposed = false, suspended = false, disabled = false, busy = false, listing = false;
  let selected = null, current = null, timer = null, eventCursor = 0, detailVersion = 0, listVersion = 0;
  let workflows = [], templates = [], recentEvents = [], pending = null, activeRead = null;
  const readErrors = new Map();
  const scope = typeof options.scope === 'string' && /^[a-f0-9]{64}$/.test(options.scope) ? options.scope : null;
  const storageKey = scope ? 'fleet.workflow.' + scope : null;
  function save() {
    if (!storageKey || disposed) return;
    try { sessionStorage.setItem(storageKey, JSON.stringify({selected, eventCursor, pending})); }
    catch (error) { notice.textContent = '浏览器无法保存流程选择，刷新后可重新选择。'; }
  }
  if (storageKey) {
    try {
      const raw = sessionStorage.getItem(storageKey);
      const value = raw && raw.length <= 32768 ? JSON.parse(raw) : null;
      if (value && typeof value.selected === 'string' && value.selected.length <= 128) {
        selected = value.selected; eventCursor = Number.isSafeInteger(value.eventCursor) && value.eventCursor >= 0 ? value.eventCursor : 0;
      }
      if (value && value.pending && ['create', 'control', 'step', 'acceptance'].includes(value.pending.type) && value.pending.payload && typeof value.pending.payload.client_token === 'string') pending = value.pending;
    } catch (error) { notice.textContent = '已忽略失效的流程恢复记录。'; }
  }
  function renderRetry() {
    const focused = retryHost.contains(document.activeElement) ? document.activeElement : null;
    clear(retryHost);
    if (pending) retryHost.appendChild(button('重试上次操作', () => submit(pending), busy));
    restoreFocus(retryHost, focused);
  }
  function setReadError(source, error = null) {
    if (error) readErrors.set(source, error);
    else readErrors.delete(source);
    clear(readNotice);
    for (const value of readErrors.values()) showError(readNotice, value);
  }
  function schedule() {
    if (disposed || suspended || disabled || timer !== null || document.hidden) return;
    timer = setTimeout(() => { timer = null; refreshAll(); }, 2500);
  }
  function choose(id) {
    selected = id; current = null; eventCursor = 0; recentEvents = [];
    setReadError('detail');
    detailVersion += 1; activeRead?.abort(); save(); renderList(); renderDetail(); refreshDetail();
  }
  function renderList() {
    const focused = list.contains(document.activeElement) ? document.activeElement : null;
    clear(list);
    if (!workflows.length) list.appendChild(el('p', 'assistant-workflow-empty meta', '暂无工作流'));
    for (const item of workflows) {
      const row = button('', () => choose(item.workflow_id), false, item.workflow_id); row.className = 'assistant-workflow-row';
      row.setAttribute('aria-pressed', String(item.workflow_id === selected)); row.setAttribute('data-workflow-id', item.workflow_id);
      row.appendChild(el('strong', null, item.template_id)); row.appendChild(el('span', null, displayState(item)));
      row.appendChild(el('small', null, item.updated_at)); list.appendChild(row);
    }
    restoreFocus(list, focused);
  }
  function download(reference, label) {
    const link = el('a', 'assistant-workflow-download', label);
    link.dataset.workflowFocus = label + ':' + reference.artifact_id;
    link.href = service.getArtifactContentUrl(reference.artifact_id, current.workspace_id); link.download = ''; return link;
  }
  function renderDetail() {
    const focused = detail.contains(document.activeElement) ? document.activeElement : null;
    clear(detail);
    if (!current) { detail.appendChild(el('p', 'assistant-workflow-empty meta', '选择一个工作流查看步骤和证据')); restoreFocus(detail, focused); return; }
    const title = el('div', 'assistant-workflow-detail-header'); title.appendChild(el('h3', null, current.template_id));
    const state = el('span', 'assistant-workflow-state', displayState(current)); state.setAttribute('data-state', current.execution_state); title.appendChild(state); detail.appendChild(title);
    const meta = el('div', 'assistant-workflow-meta');
    for (const text of ['版本 ' + current.revision, '执行：' + status(current.execution_state), '校验：' + status(current.validation_state), '验收：' + status(current.acceptance_state)]) meta.appendChild(el('span', null, text));
    detail.appendChild(meta);
    if (current.reason_code) detail.appendChild(el('p', 'assistant-workflow-reason', '等待原因：' + (REASONS[current.reason_code] || current.reason_code)));
    const actions = el('div', 'assistant-workflow-actions'); const locked = busy || !!pending;
    if (current.execution_state === 'cancelling' || current.cancel_requested && ACTIVE.has(current.execution_state)) actions.appendChild(el('span', null, '正在停止，等待实际回执'));
    else if (current.cancel_requested && current.execution_state === 'unknown') actions.appendChild(el('span', null, '停止尚未确认，请核对执行'));
    else if (ACTIVE.has(current.execution_state)) {
      const action = current.pause_requested ? 'resume' : 'pause';
      actions.appendChild(button(current.pause_requested ? '继续流程' : '暂停流程', () => control(action), locked));
      actions.appendChild(button('取消流程', () => control('cancel'), locked));
    }
    if (current.delivery_state === 'awaiting_acceptance' && options.canAccept !== false) {
      actions.appendChild(button('接受交付', () => acceptance('accepted'), locked));
      actions.appendChild(button('退回返工', () => acceptance('rejected'), locked));
    }
    detail.appendChild(actions);
    for (const step of current.steps) {
      const card = el('article', 'assistant-workflow-step'); card.setAttribute('data-step', step.step_key);
      const title = el('div', 'assistant-workflow-step-title'); title.appendChild(el('strong', null, step.step_key));
      title.appendChild(el('span', null, status(step.execution_state))); title.appendChild(el('small', null, '第 ' + step.generation + ' 代')); card.appendChild(title);
      if (step.reason_code) card.appendChild(el('p', 'assistant-workflow-reason', REASONS[step.reason_code] || step.reason_code));
      const controls = el('div', 'assistant-workflow-actions');
      const focusKey = step.step_key + ':' + step.generation + ':';
      if (step.execution_state === 'unknown') controls.appendChild(button('核对执行', () => stepControl(step, 'reconcile'), locked, focusKey + 'reconcile'));
      if (step.execution_state === 'failed') controls.appendChild(button('重试步骤', () => stepControl(step, 'retry'), locked, focusKey + 'retry'));
      if (['succeeded', 'failed'].includes(step.execution_state) && !['cancelled', 'unknown', 'cancelling'].includes(current.execution_state)) controls.appendChild(button('使此步及后继失效', () => stepControl(step, 'invalidate'), locked, focusKey + 'invalidate'));
      card.appendChild(controls);
      for (const output of step.outputs) {
        const row = el('div', 'assistant-workflow-output'); row.appendChild(download(output, output.output_key)); row.appendChild(el('small', null, output.sha256)); card.appendChild(row);
      }
      for (const evidence of step.evidence) {
        const row = el('div', 'assistant-workflow-evidence'); row.appendChild(download(evidence.report, evidence.validator_ref));
        row.appendChild(el('span', null, '校验：' + status(evidence.result))); row.appendChild(el('small', null, evidence.report.sha256)); card.appendChild(row);
      }
      detail.appendChild(card);
    }
    const budget = el('div', 'assistant-workflow-budget'); budget.appendChild(el('h4', null, '预算'));
    for (const row of current.budget) {
      const charged = row.charged_tokens === null ? '用量待结算；已预留 ' + row.reserved_tokens : '已计入 ' + row.charged_tokens;
      budget.appendChild(el('p', null, row.step_key + ' · tokens ' + charged + ' · 费用' + (row.cost_state === 'unknown' ? '未知' : row.cost)));
    }
    detail.appendChild(budget); detail.appendChild(el('small', 'assistant-workflow-digest', '交付摘要：' + current.delivery_digest));
    const events = el('div', 'assistant-workflow-events'); events.appendChild(el('h4', null, '最近事件'));
    for (const event of recentEvents.slice(-20)) events.appendChild(el('p', 'meta', event.sequence + ' · ' + event.kind));
    detail.appendChild(events);
    restoreFocus(detail, focused);
  }
  async function submit(operation) {
    if (busy || disposed || disabled) return;
    busy = true; pending = operation; save(); notice.textContent = '正在提交操作…'; renderRetry(); renderDetail();
    try {
      const p = operation.payload;
      if (operation.type === 'create') {
        const created = await service.createWorkflow(p); if (disposed) return; selected = created.workflow_id; eventCursor = 0; recentEvents = [];
      } else if (operation.type === 'acceptance') await service.acceptWorkflow(operation.workflow, p);
      else if (operation.type === 'step') await service.controlWorkflowStep(operation.workflow, operation.step, operation.action, p);
      else await service.controlWorkflow(operation.workflow, operation.action, p.expected_revision, p.client_token);
      if (disposed) return;
      pending = null; save(); notice.textContent = '操作已记录，等待执行回执。'; await refreshDetail();
    } catch (error) {
      if (disposed) return;
      clear(notice);
      if (error.status >= 400 && error.status < 500) {
        pending = null; save();
        if (error.status === 409) { await refreshDetail(); notice.textContent = '版本已更新，请查看当前结果后重新确认操作。'; }
        else showError(notice, error);
      } else { showError(notice, error); notice.appendChild(el('p', null, '结果待确认，请重试上次操作。')); }
    } finally {
      busy = false; if (!disposed) { renderRetry(); renderDetail(); create.disabled = !templates.length || !!pending; schedule(); }
    }
  }
  function control(action) {
    if (current) submit({type: 'control', workflow: current.workflow_id, action, payload: {expected_revision: current.revision, client_token: token()}});
  }
  function stepControl(step, action) {
    if (!current) return;
    const payload = {expected_revision: current.revision, client_token: token()};
    if (action === 'reconcile') payload.mode = 'inspect';
    else payload.expected_generation = step.generation;
    if (action === 'invalidate') payload.reason = 'operator_requested_rework';
    submit({type: 'step', workflow: current.workflow_id, step: step.step_key, action, payload});
  }
  function acceptance(decision) {
    if (current) submit({type: 'acceptance', workflow: current.workflow_id, payload: {decision, delivery_digest: current.delivery_digest, expected_revision: current.revision, client_token: token()}});
  }
  function createSelected() {
    const item = templates[Number(template.value)];
    if (item) submit({type: 'create', payload: {template_id: item.template_id, revision: item.revision, workspace_id: item.workspace_id,
      input_refs: item.input_refs, client_token: token(), expected_policy_revision: item.policy_revision}});
  }
  function renderTemplateInputs() {
    clear(inputs);
    const item = templates[Number(template.value)];
    if (!item?.input_refs.length) return;
    inputs.appendChild(el('strong', null, '固定输入'));
    for (const ref of item.input_refs) {
      const version = ref.kind === 'code_revision' ? '源码 ' + ref.repository_id + ' · ' + ref.head_commit.slice(0, 12) : '文件 · ' + ref.sha256.slice(0, 12);
      inputs.appendChild(el('p', null, ref.ref_key + ' · ' + version));
    }
  }
  async function refreshDetail() {
    if (!selected || disposed || disabled) { if (!selected) setReadError('detail'); renderDetail(); return; }
    const id = selected, version = ++detailVersion; activeRead?.abort(); activeRead = new AbortController(); const signal = activeRead.signal;
    try {
      const data = await service.getWorkflow(id, {signal});
      if (disposed || version !== detailVersion || id !== selected) return;
      current = data.workflow; renderDetail();
      const events = await service.getWorkflowEvents(id, eventCursor, 100, {signal});
      if (disposed || version !== detailVersion || id !== selected) return;
      eventCursor = events.next_cursor === null ? eventCursor : events.next_cursor;
      recentEvents = recentEvents.concat(events.events).slice(-100); setReadError('detail'); save(); renderDetail();
    } catch (error) {
      if (disposed || version !== detailVersion || signal.aborted) return;
      if (error.status === 404) { selected = null; current = null; eventCursor = 0; setReadError('detail'); save(); renderDetail(); }
      else setReadError('detail', error);
    } finally { schedule(); }
  }
  async function refreshAll() {
    if (disposed || suspended || disabled || listing || busy) { schedule(); return; }
    listing = true; const version = ++listVersion;
    try {
      const data = await service.listWorkflows(100);
      if (disposed || version !== listVersion) return;
      workflows = data.workflows;
      setReadError('list');
      if (!selected && workflows.length) { selected = workflows[0].workflow_id; save(); }
      renderList(); await refreshDetail();
    } catch (error) {
      if (disposed) return;
      setReadError('list', error);
      if (error.status === 404 || error.status === 401) { disabled = true; clear(list); clear(detail); }
    } finally { listing = false; schedule(); }
  }
  async function loadTemplates() {
    try {
      const data = await service.getWorkflowTemplates(); if (disposed || disabled) return;
      setReadError('templates');
      templates = data.templates; clear(template);
      templates.forEach((item, index) => { const option = el('option', null, item.template_id + ' · v' + item.revision); option.value = String(index); template.appendChild(option); });
      if (!templates.length) template.appendChild(el('option', null, '暂无已登记模板'));
      renderTemplateInputs();
      create.disabled = !templates.length || busy || !!pending;
    } catch (error) { if (!disposed) setReadError('templates', error); }
  }
  function visibility() {
    if (document.hidden) { if (timer !== null) clearTimeout(timer); timer = null; }
    else refreshAll();
  }
  document.addEventListener('visibilitychange', visibility);
  renderRetry(); loadTemplates(); refreshAll();
  function teardown() {
    disposed = true; detailVersion += 1; listVersion += 1; activeRead?.abort();
    document.removeEventListener('visibilitychange', visibility); if (timer !== null) clearTimeout(timer);
  }
  teardown.suspend = function () {
    if (disposed || suspended) return;
    suspended = true; detailVersion += 1; listVersion += 1; activeRead?.abort();
    if (timer !== null) clearTimeout(timer);
    timer = null;
  };
  teardown.resume = function () {
    if (disposed || !suspended) return;
    suspended = false; refreshAll();
  };
  return teardown;
}
