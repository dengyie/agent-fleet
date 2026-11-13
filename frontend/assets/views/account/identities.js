import {listIntegrationIdentities, getIntegrationIdentity, revokeIntegrationIdentity,
  registerIntegrationIdentity, parseIdentityRegistrationCommand, sortIdentityReferences} from '../../api/integration-identities.js';

const labels = {active: '有效登记', revoked: '已撤销', restore_disabled: '恢复后禁用'};
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
function rowKey(item) { return item.owner_id + '\0' + item.integration_id; }

export function mountIntegrationIdentities(root, {suspended: initiallySuspended = false} = {}) {
  const panel = node('section', undefined, 'panel account-panel identity-management'); panel.setAttribute('aria-label', 'Fleet 集成身份管理');
  const status = node('p', '', 'integration-status'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
  const reload = button('重新读取身份', () => readPage(true));
  const create = button('登记新身份', () => openRegistration(create));
  const toolbar = node('div', undefined, 'integration-toolbar'); toolbar.append(node('h2', 'Fleet 集成身份管理'), create, reload);
  const list = node('div', undefined, 'integration-list'); list.setAttribute('aria-label', 'Fleet 身份列表');
  const more = button('加载更多身份', () => readPage(false)); more.hidden = true;
  const column = node('div'); column.append(list, more);
  const inspector = node('section', undefined, 'integration-inspector'); inspector.setAttribute('aria-label', 'Fleet 身份详情');
  const layout = node('div', undefined, 'integration-layout'); layout.append(column, inspector);
  panel.append(toolbar, node('p', '登记集成身份，配置授权范围和额度，查看或撤销已有身份。', 'meta'), status, layout); root.append(panel);
  const rows = new Map();
  let disposed = false, suspended = initiallySuspended, generation = 0, busy = false, cursor = null;
  let selected = null, detail = null, dialog = null, controller = null, revoke = null;

  function sync() {
    reload.disabled = busy; create.disabled = busy; more.disabled = busy; more.hidden = cursor === null;
    for (const element of list.querySelectorAll('button')) element.disabled = busy;
    if (revoke) revoke.disabled = busy;
    panel.setAttribute('aria-busy', String(busy));
  }
  async function run(operation, apply, fail) {
    if (disposed || suspended || busy) return false;
    const current = generation, request = new AbortController(); controller = request; busy = true; sync();
    try {
      const result = await operation(request.signal);
      if (disposed || suspended || current !== generation) return false;
      apply(result); return true;
    } catch (error) {
      if (disposed || suspended || current !== generation || request.signal.aborted) return false;
      if (fail) fail(error); else status.textContent = error.detail || '身份读取失败，请重新读取';
      return false;
    } finally {
      if (current === generation) { controller = null; busy = false; sync(); }
    }
  }
  function drawList() {
    const fragment = document.createDocumentFragment();
    for (const item of rows.values()) {
      const row = button('', () => readDetail(item)); row.classList.add('integration-row');
      row.dataset.ownerId = item.owner_id; row.dataset.integrationId = item.integration_id;
      row.setAttribute('aria-pressed', String(selected !== null && rowKey(selected) === rowKey(item)));
      row.append(node('strong', item.integration_id), node('span', item.owner_id, 'meta'), node('span', labels[item.state], 'integration-state')); fragment.append(row);
    }
    list.replaceChildren(fragment);
    if (!rows.size) list.append(node('p', '尚无身份登记', 'meta'));
    sync();
  }
  function drawDetail() {
    revoke = null; inspector.replaceChildren();
    if (!detail) { inspector.append(node('p', '选择身份后查看完整权限', 'meta')); return; }
    const item = detail.summary;
    inspector.append(node('h3', item.integration_id), node('p', labels[item.state], 'integration-state'));
    const metadata = node('dl', undefined, 'integration-metadata');
    const values = [['归属', item.owner_id], ['工具权限', item.scopes.join(', ') || '无工具权限'],
      ['产物条数上限', String(item.max_artifacts)], ['Token 预算上限', String(item.max_budget_tokens)],
      ['请求预算上限', String(item.max_budget_requests)], ['创建时间 UTC', item.created_at]];
    if (item.revoked_at !== null) values.push(['撤销时间 UTC', item.revoked_at]);
    for (const [label, value] of values) metadata.append(node('dt', label), node('dd', value));
    inspector.append(metadata, node('p', '有效登记仅表示当前凭据未禁用；实际调用仍需原工作流授权和预算检查。', 'meta'));
    for (const [title, values] of [['授权模板', detail.template_ids], ['授权工作区', detail.workspace_ids]]) {
      const section = node('details', undefined, 'integration-snapshot'); section.open = true;
      section.append(node('summary', title + ' · ' + values.length), node('pre', JSON.stringify(values, null, 2))); inspector.append(section);
    }
    if (item.state === 'active') {
      revoke = button('撤销身份', event => confirm(item, event.currentTarget)); inspector.append(revoke);
    } else inspector.append(node('p', item.state === 'restore_disabled' ? '恢复副本的凭据已禁用，可信重复登记也不能重新启用。' : '此身份已撤销，已有工作流保留原归属。', 'meta'));
    sync();
  }
  function applyDetail(result) {
    detail = result.identity; selected = {owner_id: detail.summary.owner_id, integration_id: detail.summary.integration_id};
    if (rows.has(rowKey(selected))) rows.set(rowKey(selected), detail.summary);
    drawList(); drawDetail();
  }
  function closeDialog() { if (dialog) dialog.close(); }
  async function readPage(reset) {
    if (busy || disposed || suspended) return;
    closeDialog(); status.textContent = '正在读取身份…'; const keep = reset ? selected : null;
    if (reset) { detail = null; drawDetail(); }
    await run(async signal => {
      const page = await listIntegrationIdentities(reset ? null : cursor, {signal});
      const record = keep === null ? null : await getIntegrationIdentity(keep.owner_id, keep.integration_id, {signal});
      return {page, record};
    }, ({page, record}) => {
      if (reset) rows.clear();
      for (const item of page.identities) rows.set(rowKey(item), item);
      cursor = page.next_cursor; drawList(); if (record) applyDetail(record);
      status.textContent = rows.size ? '已读取 ' + rows.size + ' 条身份登记' : '尚无身份登记；可以登记新身份';
    });
  }
  async function readDetail(item, message = '', failure = '') {
    if (busy || disposed || suspended) return;
    closeDialog(); selected = {owner_id: item.owner_id, integration_id: item.integration_id}; detail = null; drawDetail();
    status.textContent = '正在读取身份权限…';
    await run(signal => getIntegrationIdentity(item.owner_id, item.integration_id, {signal}), result => {
      applyDetail(result); status.textContent = message || '已读取当前身份权限';
    }, error => { status.textContent = failure || error.detail || '身份读取失败，请重新读取'; });
  }
  function openRegistration(opener) {
    if (busy || disposed || suspended || dialog) return;
    const modal = node('dialog', undefined, 'integration-confirm integration-registration'); dialog = modal;
    const title = node('h3', '登记 Fleet 集成身份'); title.id = 'identity-registration-title'; modal.setAttribute('aria-labelledby', title.id);
    const form = node('form'), fields = node('fieldset', undefined, 'integration-registration-fields'), inputs = {};
    function field(label, name, {multiline = false, value = '', minimum, maximum} = {}) {
      const input = node(multiline ? 'textarea' : 'input'); input.name = name; input.value = value; input.required = true;
      if (multiline) input.rows = 3;
      else input.type = minimum === undefined ? 'text' : 'number';
      if (minimum !== undefined) { input.min = String(minimum); input.max = String(maximum); input.step = '1'; }
      const wrapper = node('label'); wrapper.append(node('span', label), input); fields.append(wrapper); inputs[name] = input;
    }
    field('归属身份', 'owner_id'); field('集成身份标识', 'integration_id');
    field('授权模板（每行一个）', 'template_ids', {multiline: true});
    field('授权工作区（每行一个）', 'workspace_ids', {multiline: true});
    field('产物条数上限', 'max_artifacts', {value: '100', minimum: 1, maximum: 100});
    field('Token 预算上限', 'max_budget_tokens', {value: '600000', minimum: 0, maximum: 2000000});
    field('请求预算上限', 'max_budget_requests', {value: '150', minimum: 0, maximum: 1000});
    const scopes = [];
    for (const [name, text] of [['workflow.create_from_template', '创建工作流'], ['workflow.get', '查看工作流'], ['workflow.list_artifacts', '列出产物']]) {
      const input = node('input'); input.type = 'checkbox'; input.value = name; input.checked = true; scopes.push(input);
      const wrapper = node('label', undefined, 'integration-registration-scope'); wrapper.append(input, node('span', text)); fields.append(wrapper);
    }
    const message = node('p', '', 'integration-status'); message.setAttribute('role', 'status'); message.setAttribute('aria-live', 'polite');
    const tokenWrapper = node('label'), tokenField = node('input'); tokenField.type = 'text'; tokenField.readOnly = true;
    tokenField.setAttribute('aria-label', '访问令牌');
    tokenWrapper.className = 'integration-registration-token'; tokenWrapper.hidden = true;
    tokenWrapper.append(node('span', '访问令牌'), tokenField, node('span', '关闭窗口后无法再次查看此令牌，请保存后用于集成连接。', 'meta'));
    const submit = button('生成令牌并登记', () => form.requestSubmit());
    const controls = node('div', undefined, 'integration-actions'); controls.append(button('关闭', () => modal.close()), submit);
    form.append(fields, node('p', '授权预算需要覆盖所选模板全部步骤的预算；默认额度可覆盖现有三步代码交付模板。', 'meta'), tokenWrapper, message, controls); modal.append(title, form); panel.append(modal);
    let command = null, preparing = false, observing = false, finished = false, registrationSignal = null;
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (preparing || observing || finished || busy || disposed || suspended || dialog !== modal || !modal.open) return;
      const current = generation;
      const active = () => !disposed && !suspended && generation === current && dialog === modal && modal.open && modal.isConnected;
      preparing = true; submit.disabled = true;
      try {
        if (command === null) {
          const selected = name => sortIdentityReferences(inputs[name].value.split(/\r?\n/).filter(value => value !== ''));
          const candidate = {identity: {owner_id: inputs.owner_id.value, integration_id: inputs.integration_id.value,
            template_ids: selected('template_ids'), workspace_ids: selected('workspace_ids'), token_digest: '0'.repeat(64),
            scopes: sortIdentityReferences(scopes.filter(input => input.checked).map(input => input.value)),
            max_artifacts: Number(inputs.max_artifacts.value), max_budget_tokens: Number(inputs.max_budget_tokens.value),
            max_budget_requests: Number(inputs.max_budget_requests.value)}};
          parseIdentityRegistrationCommand(candidate); fields.disabled = true;
          message.textContent = '正在生成访问令牌…';
          const hex = bytes => Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
          const token = 'fleet_' + hex(crypto.getRandomValues(new Uint8Array(32)));
          const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(token));
          if (!active()) return;
          candidate.identity.token_digest = hex(new Uint8Array(digest)); command = candidate;
          tokenField.value = token; tokenWrapper.hidden = false;
          tokenWrapper.scrollIntoView({block: 'center'}); tokenField.focus({preventScroll: true}); tokenField.select();
        }
        preparing = false; observing = true; message.textContent = '正在登记身份…';
        await run(signal => { registrationSignal = signal; return registerIntegrationIdentity(command, {signal}); }, result => {
          if (!active()) return;
          rows.set(rowKey(result.identity.summary), result.identity.summary); applyDetail(result); finished = true;
          message.textContent = result.identity.summary.state === 'active' ? '身份已登记，请保存访问令牌。' :
            '身份已登记，但当前状态为' + labels[result.identity.summary.state] + '，令牌不可用。';
          message.scrollIntoView({block: 'nearest'});
          submit.disabled = true;
        }, error => {
          if (!active()) return;
          if (error.status === 400) {
            command = null; fields.disabled = false; tokenField.value = ''; tokenWrapper.hidden = true;
            message.textContent = error.detail || '登记配置无效，请修改后提交'; submit.textContent = '生成令牌并登记';
          } else if (error.status === 409 || error.status === 403 || error.status === 401) {
            finished = true; message.textContent = error.detail || '登记被拒绝，请核对身份、工作区和权限';
          } else {
            message.textContent = '登记结果待核对。再次提交将沿用同一身份、权限和令牌。'; submit.textContent = '核对并重试原登记';
          }
        });
      } catch (error) {
        if (active()) { fields.disabled = false; message.textContent = error.name === 'ContractError' ?
          '登记配置无效，请核对标识、列表和额度' : error.detail || '登记配置或令牌生成失败，请核对后重试'; }
      } finally {
        preparing = false; observing = false;
        if (active()) submit.disabled = finished;
      }
    });
    modal.addEventListener('close', () => {
      if (observing && controller?.signal === registrationSignal) { generation += 1; controller.abort(); controller = null; busy = false; }
      tokenField.value = ''; command = null; modal.remove(); if (dialog === modal) dialog = null;
      sync(); if (opener.isConnected) opener.focus();
    }, {once: true}); modal.showModal();
  }
  function confirm(item, opener) {
    if (busy || disposed || suspended || dialog) return;
    const modal = node('dialog', undefined, 'integration-confirm'); dialog = modal;
    const title = node('h3', '撤销 Fleet 集成身份'); title.id = 'identity-confirm-title'; modal.setAttribute('aria-labelledby', title.id);
    modal.append(title, node('p', '归属：' + item.owner_id), node('p', '身份：' + item.integration_id),
      node('p', '撤销不可恢复。后续调用将被拒绝；已有工作流保持原归属，不会因此取消或重新执行。'));
    const check = node('input'); check.type = 'checkbox'; const wrapper = node('label', undefined, 'integration-review-check');
    wrapper.append(check, node('span', '已核对身份及全部授权范围'));
    const submit = button('确认撤销身份', async () => { closeDialog(); await mutate(item); }); submit.disabled = true;
    check.addEventListener('change', () => { submit.disabled = !check.checked; });
    const controls = node('div', undefined, 'integration-actions'); controls.append(button('取消', () => modal.close()), submit);
    modal.append(wrapper, controls); panel.append(modal);
    modal.addEventListener('close', () => { modal.remove(); if (dialog === modal) dialog = null; if (opener.isConnected) opener.focus(); }, {once: true}); modal.showModal();
  }
  async function mutate(item) {
    let conflict = false; status.textContent = '正在提交身份撤销…';
    const committed = await run(signal => revokeIntegrationIdentity(item.owner_id, item.integration_id, {signal}), () => {
      detail = null; drawDetail(); status.textContent = '撤销已确认，正在读取当前状态…';
    }, error => {
      detail = null; drawDetail(); conflict = error.status === 409;
      status.textContent = conflict ? '身份状态已变化，正在读取当前事实…' : '撤销结果待核对，请重新读取身份。请勿重复提交。';
    });
    if (committed) await readDetail(item, '撤销已确认，已读取当前状态', '撤销已确认，但详情读取失败，请重新读取身份');
    else if (conflict) await readDetail(item, '身份状态已变化，已读取当前事实，请重新核对');
  }
  function stop() { generation += 1; controller?.abort(); controller = null; busy = false; closeDialog(); }
  const dispose = () => { disposed = true; stop(); rows.clear(); detail = null; panel.remove(); };
  dispose.suspend = () => { suspended = true; stop(); };
  dispose.resume = () => { if (!disposed && suspended) { suspended = false; readPage(true); } };
  drawDetail(); if (!suspended) readPage(true); return dispose;
}
