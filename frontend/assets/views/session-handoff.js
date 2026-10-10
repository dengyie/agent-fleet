/* Deterministic evidence preview; all I/O goes through the injected client. */
import { pagePath, uiIcon } from '../routes.js';

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function field(label, control) {
  const node = element('label', label);
  node.style.display = 'block';
  node.appendChild(control);
  return node;
}

export function renderHandoffPrompt(snapshot, override = '') {
  const slice = (text, length) => Array.from(text).slice(0, length).join("");
  const sections = ['会话接力（脱敏证据，内容可能不完整；先核对工作区状态）'];
  if (override) sections.push('接力指令：' + slice(override, 500));
  const fields = [['目标', 'objective', override ? 300 : 650], ['状态', 'current_state', 40],
    ['最近结论', 'last_conclusion', override ? 300 : 500], ['已记录完成', 'completed', override ? 140 : 180],
    ['写入目标', 'changed_files', override ? 100 : 140], ['测试结果', 'tests', 100],
    ['待核对', 'open_items', 180], ['采集质量', 'capture_quality', 30]];
  for (const [label, key, limit] of fields) {
    const value = snapshot[key];
    let text = typeof value === 'string' ? value : JSON.stringify(value);
    if (Array.from(text).length > limit) text = slice(text, limit - 1) + '…';
    sections.push(label + '：' + text);
  }
  sections.push('引用与完整有界快照见源会话接力预览；省略号表示截断。');
  return slice(sections.join('\n'), 2000);
}

function clientToken() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  return 'handoff-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2);
}

/** Keep host mounted across session redraws to preserve edits and retry tokens. */
export function mountHandoffPanel(host, { api, sessionId }) {
  let disposed = false;
  let preview = null;
  let pending = null;
  let busy = false;
  let receipt = null;
  host.classList.add('panel');
  host.dataset.handoffPanel = 'true';
  const heading = element('h2');
  heading.append(uiIcon('git-branch', { size: 16 }), document.createTextNode(' 会话接力'));
  const notice = element('p', '从脱敏记录生成预览，核对后创建新的接力任务。', 'meta');
  const previewButton = element('button', '预览接力包');
  previewButton.type = 'button';
  previewButton.dataset.action = 'handoff-preview';
  const content = element('div');
  const status = element('p', '', 'meta');
  status.setAttribute('role', 'status');
  host.replaceChildren(heading, notice, previewButton, content, status);
  const available = typeof api.getSessionHandoff === 'function' && typeof api.createSessionHandoff === 'function';
  previewButton.disabled = !available;
  if (!available) status.textContent = '接力接口不可用';

  function setStatus(message, failed = false) {
    if (disposed) return;
    status.textContent = message;
    status.className = failed ? 'err' : 'meta';
  }

  function renderPreview() {
    content.replaceChildren();
    const details = element('details');
    details.open = true;
    details.appendChild(element('summary', '结构化证据（脱敏）'));
    const labels = { objective: '用户目标', current_state: '当前状态', completed: '已记录完成',
      changed_files: '写入目标', tests: '测试结果', last_conclusion: '最近结论',
      open_items: '待核对', source_event_refs: '来源事件', capture_quality: '采集质量' };
    for (const [key, label] of Object.entries(labels)) {
      const value = preview.snapshot[key];
      const text = element('pre', typeof value === 'string' ? value : JSON.stringify(value, null, 2));
      text.style.whiteSpace = 'pre-wrap';
      text.style.overflowWrap = 'anywhere';
      details.append(element('h3', label), text);
    }
    const prompt = element('textarea');
    prompt.value = '';
    prompt.maxLength = 500;
    prompt.rows = 4;
    prompt.style.width = '100%';
    prompt.dataset.handoffPrompt = 'true';
    const finalPrompt = element('pre', preview.prompt);
    finalPrompt.dataset.handoffFinalPrompt = 'true';
    finalPrompt.style.whiteSpace = 'pre-wrap';
    finalPrompt.style.overflowWrap = 'anywhere';
    prompt.addEventListener('input', () => {
      finalPrompt.textContent = renderHandoffPrompt(preview.snapshot, prompt.value);
    });
    content.append(details, field('接力初始指令（可选，最多 500 字符）', prompt),
      element('h3', '最终 Prompt（提交时应用脱敏）'), finalPrompt);
    if (!preview.can_create || !preview.targets.length) {
      prompt.readOnly = true;
      content.appendChild(element('p', '当前会话仅可预览；创建接力需要受管会话和已注册的同一项目。', 'meta'));
      return;
    }
    const machine = element('select');
    const project = element('select');
    const agent = element('select');
    const machines = new Map(preview.targets.map(target => [target.machine, target]));
    for (const target of machines.values()) {
      const option = element('option', target.machine);
      option.value = target.machine;
      machine.appendChild(option);
    }
    function updateTargets() {
      const target = machines.get(machine.value);
      project.replaceChildren(element('option', target.project));
      agent.replaceChildren();
      for (const family of target.agents) agent.appendChild(element('option', family));
    }
    machine.addEventListener('change', updateTargets);
    updateTargets();
    const form = element('form');
    const submit = element('button', '确认创建接力任务');
    submit.type = 'submit';
    submit.dataset.action = 'handoff-create';
    form.append(field('目标机器', machine), field('项目', project), field('目标 Agent', agent),
      element('p', '创建后仍需在新任务页确认放行。', 'meta'), submit);
    content.appendChild(form);

    function freeze(value) {
      for (const input of [prompt, machine, project, agent]) input.disabled = value;
      previewButton.disabled = value;
    }

    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (busy || disposed || receipt) return;
      if (!preview.snapshot.objective && !prompt.value.trim()) {
        setStatus('未采集到用户目标，请填写接力初始指令', true); return;
      }
      pending = pending || { target_machine: machine.value, target_project: project.value,
        target_agent: agent.value, instruction_override: prompt.value, client_token: clientToken() };
      busy = true;
      freeze(true);
      submit.disabled = true;
      setStatus('正在创建接力任务…');
      try {
        receipt = await api.createSessionHandoff(sessionId, pending);
        if (disposed) return;
        const taskLink = element('a', '打开新任务 ' + receipt.task_id);
        taskLink.href = pagePath('task', receipt.task_id);
        const attemptLink = element('a', '创建 attempt ' + receipt.attempt_id);
        attemptLink.href = pagePath('task', receipt.task_id) + '#task-actions';
        content.append(element('p', '接力已创建，等待任务页确认。'), taskLink, element('br'), attemptLink);
        setStatus('接力标识：' + receipt.handoff_id);
      } catch (error) {
        if (disposed) return;
        const definitive = error && error.status >= 400 && error.status < 500;
        if (definitive) pending = null;
        freeze(!definitive);
        submit.disabled = false;
        submit.textContent = definitive ? '确认创建接力任务' : '使用原凭据重试';
        setStatus((error && (error.detail || error.message) || '接力请求失败') +
          (definitive ? '' : '；结果尚不确定，重试将沿用原请求。'), true);
      } finally {
        busy = false;
      }
    });
  }

  previewButton.addEventListener('click', async () => {
    if (busy || disposed || pending) return;
    busy = true;
    previewButton.disabled = true;
    setStatus('正在读取脱敏证据…');
    try {
      preview = await api.getSessionHandoff(sessionId);
      if (disposed) return;
      renderPreview();
      setStatus('预览不会创建任务。请核对内容和目标后提交。');
    } catch (error) {
      setStatus(error && (error.detail || error.message) || '预览读取失败', true);
    } finally {
      busy = false;
      if (!disposed) previewButton.disabled = false;
    }
  });
  return () => { disposed = true; };
}
