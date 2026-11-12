import {parseMCPRegistrationCommand, registerMCPConnector} from '../../api/integrations.js';

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

export function openMCPRegistration(section, {onRegistered, onClose, isCurrent, ownerId = '', opener}) {
  const modal = node('dialog', undefined, 'mcp-confirm mcp-registration');
  modal.setAttribute('aria-label', '登记 MCP 连接');
  const form = node('form'), status = node('p', '', 'mcp-status');
  status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
  const fields = node('fieldset', undefined, 'mcp-registration-fields'), inputs = {};
  function field(name, caption, value = '', type = 'text', maximum = 128) {
    const input = node(type === 'textarea' ? 'textarea' : 'input');
    if (type !== 'textarea') input.type = type;
    input.value = value; input.required = true; input.maxLength = maximum;
    input.setAttribute('aria-label', caption);
    input.addEventListener('focus', () => {
      if (!modal.open) return;
      const bounds = input.getBoundingClientRect(), panel = modal.getBoundingClientRect();
      const actions = form.querySelector('.mcp-actions');
      const bottom = Math.min(window.innerHeight - 24, actions.getBoundingClientRect().top - 12);
      if (bounds.top < Math.max(0, panel.top + 24) || bounds.bottom > bottom) {
        input.scrollIntoView({block: 'center', inline: 'nearest'});
      }
    });
    if (type === 'textarea') input.rows = 3;
    const label = node('label'); label.append(node('span', caption), input); fields.append(label);
    inputs[name] = input; return input;
  }
  field('owner_id', '归属身份', ownerId);
  field('workspace_id', '工作区标识');
  field('connector_id', '连接标识');
  field('endpoint', '服务地址', '', 'url', 2048).placeholder = 'https://mcp.example.com/mcp';
  field('secret_ref', '凭据引用', '', 'text', 134).placeholder = 'env://MCP_API_KEY';
  field('selected_tools', '选定工具（每行一个）', '', 'textarea', 2600);
  const timeout = field('timeout_s', '单次请求上限（秒）', '10', 'number'); timeout.min = '0'; timeout.max = '30'; timeout.step = 'any';
  const bytes = field('result_limit_bytes', '结果字节上限', '65536', 'number'); bytes.min = '1'; bytes.max = '262144'; bytes.step = '1';
  const tools = field('max_tools', '发现工具数量上限', '100', 'number'); tools.min = '1'; tools.max = '100'; tools.step = '1';
  const submit = node('button', '登记连接', 'button-secondary'); submit.type = 'submit';
  const close = node('button', '取消', 'button-secondary'); close.type = 'button';
  const actions = node('div', undefined, 'mcp-actions'); actions.append(close, submit);
  form.append(node('h3', '登记 MCP 连接'),
    node('p', '指定已有的归属身份和工作区，只登记选定的只读工具。登记完成后需另行发现工具。'),
    node('p', '凭据引用指向服务端已配置的环境变量，例如 env://MCP_API_KEY；此处不填写访问令牌。', 'meta'),
    status, fields, actions);
  modal.append(form); section.append(modal);
  let closed = false, pending = false, complete = false, command = null, controller = null;
  function current() { return !closed && modal.open && modal.isConnected && isCurrent(); }
  function sync() {
    fields.disabled = command !== null || pending;
    submit.disabled = pending || complete;
    submit.textContent = command !== null && !complete ? '核对并重试原登记' : '登记连接';
    close.textContent = complete ? '关闭' : '取消';
    form.setAttribute('aria-busy', String(pending));
  }
  function finish() {
    if (closed) return;
    closed = true; controller?.abort(); controller = null;
    if (modal.open) modal.close();
    modal.remove(); onClose();
    if (opener.isConnected && isCurrent()) opener.focus();
  }
  close.addEventListener('click', finish);
  modal.addEventListener('cancel', () => controller?.abort());
  modal.addEventListener('close', finish, {once: true});
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (!current() || pending || complete) return;
    try {
      if (command === null) command = parseMCPRegistrationCommand({connector: {
        connector_id: inputs.connector_id.value, owner_id: inputs.owner_id.value,
        workspace_id: inputs.workspace_id.value, endpoint: inputs.endpoint.value,
        secret_ref: inputs.secret_ref.value,
        selected_tools: inputs.selected_tools.value.split(/\r?\n/).filter(line => line !== ''),
        risk: 'read_only', retry_class: 'read_only', timeout_s: Number(inputs.timeout_s.value),
        result_limit_bytes: Number(inputs.result_limit_bytes.value), max_tools: Number(inputs.max_tools.value),
      }});
    } catch (error) { status.textContent = error.detail || '登记配置无效'; return; }
    if (!current()) return;
    const request = new AbortController(); controller = request; pending = true; sync();
    status.textContent = '正在登记连接…';
    try {
      const result = await registerMCPConnector(command, {signal: request.signal});
      if (!current() || request.signal.aborted) return;
      complete = true; onRegistered(result);
      const item = result.connector.summary;
      status.textContent = ['revoked', 'restore_disabled'].includes(item.state) ? '原登记已禁用，重复登记不会重新启用。' :
        item.active_revision !== null ? '原连接登记已核对，保留已有工具版本。' : '连接已登记。关闭窗口后可显式发现工具。';
    } catch (error) {
      if (!current() || request.signal.aborted) return;
      status.textContent = error.status === 409 ? '连接标识或工作区状态冲突，请关闭窗口后核对原登记。' :
        '登记结果待核对。再次提交将使用同一连接配置。';
    } finally {
      if (controller === request) { controller = null; pending = false; if (current()) sync(); }
    }
  });
  modal.showModal(); inputs.owner_id.focus();
  return finish;
}
