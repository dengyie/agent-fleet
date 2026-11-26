import * as defaultApi from '../../api/client.js';
import {ACCOUNT_STATES, ACCOUNT_OPERATIONS} from '../../api/provider-account-contracts.js';
import {createNativeReader} from '../agent-chat/live.js';
import {uncertainWrite} from '../agent-chat/state.js';
import {el, button, field, panel} from './forms.js';

const terminal = new Set(['succeeded', 'already_finished', 'failed', 'expired', 'rejected', 'superseded']);
const receiptLabels = {queued: '已入队', delivered: '已投递', accepted: '节点已接受', executing: '执行中', succeeded: '节点已执行', already_finished: '节点已执行', failed: '失败', expired: '已过期', rejected: '已拒绝', superseded: '已被后续操作取代'};
const errors = {provider_accounts_unavailable: '节点尚未支持多账号，或账号存储暂不可用。请确认节点在线、已安装 AGY 并升级客户端。',
  machine_offline: '电脑节点离线，请恢复连接后刷新。', provider_login_encryption_unavailable: '服务端尚未配置授权加密密钥，请联系管理员。',
  provider_account_busy: '账号已有登录流程，请先完成或取消原登录。', login_unavailable: '原登录流程已结束或过期，请刷新当前状态。',
  invalid_login_response: '授权结果无效，请粘贴本次 Google 登录返回的完整回调地址或授权码。',
  idempotency_conflict: '恢复内容与原请求不一致；原操作没有被重新执行。请核对原账号、操作及授权结果。',
  forbidden: '当前管理员授权不可用。', unauthorized: '请重新登录后查看账号状态。'};
const errorMessage = error => errors[error.code] || '请求未完成，请刷新账号状态后检查。' + (error.code ? '（' + error.code + '）' : '');
const token = () => crypto.randomUUID();

// Only this view orchestrates account use cases. HTTP, validation and all node
// lifecycle decisions remain in the public API and backend respectively.
export function mountProviderAccounts(root, {api = defaultApi, ownerId = ''} = {}) {
  let disposed = false, suspended = false, sending = false, machines = [], accounts = [], machineId = '';
  let pending = null, lastOperation = null, readError = '', mutationNotice = '', loaded = false, generation = 0;
  const drafts = new Map();
  const section = panel(root, 'Google 账号', '管理电脑节点上的 AGY 独立账号。每个账号独立登录；不会导入终端里的 Google 凭据。', 'globe');
  const toolbar = el('div', null, 'provider-account-toolbar');
  const machine = field(toolbar, 'machine', '电脑节点', '', {options: []});
  const refresh = button('刷新账号状态', () => reader.wake()); toolbar.append(refresh); section.append(toolbar);
  const status = el('p', '', 'settings-notice'); status.setAttribute('role', 'status'); section.append(status);
  const createForm = el('form', null, 'provider-account-create');
  const name = field(createForm, 'account-name', '账号名称', '', {required: true, help: '便于识别的名称，例如“工作账号”；不是已验证的邮箱。'}); name.maxLength = 80;
  const createButton = button('创建独立账号', () => createForm.requestSubmit()); createForm.append(createButton); section.append(createForm);
  const recovery = el('div', null, 'provider-account-recovery'); recovery.setAttribute('role', 'status'); section.append(recovery);
  const receipt = el('p', '', 'meta'); receipt.setAttribute('role', 'status'); section.append(receipt);
  const list = el('div', null, 'provider-account-list'); section.append(list);
  section.append(el('p', '登录命令执行成功仅表示流程已启动；账号显示“可用”后才能启动新任务。禁用或退出登录会停止该账号的托管执行，其他账号不受影响。', 'settings-description'));
  const journalKey = JSON.stringify(['fleet-provider-account-operation', window.FleetConfig?.apiBaseUrl || '/api', ownerId]);
  function savePending() {
    if (!ownerId) return;
    try {
      if (!pending) {
        if (lastOperation && !terminal.has(lastOperation.status)) sessionStorage.setItem(journalKey, JSON.stringify({accepted: true, machine: machineId, operation: lastOperation}));
        else sessionStorage.removeItem(journalKey);
        return;
      }
      const body = {...pending.body}; delete body.response;
      sessionStorage.setItem(journalKey, JSON.stringify({...pending, body, needsResponse: pending.body.operation === 'submit_login'}));
    } catch (_) { /* In-memory frozen operation still supports explicit retry. */ }
  }
  try {
    const saved = ownerId && JSON.parse(sessionStorage.getItem(journalKey) || 'null');
    if (saved?.accepted && /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(saved.machine) && /^pa_[a-f0-9]{32}$/.test(saved.operation?.account_id) && /^cmd_[a-f0-9]{32}$/.test(saved.operation?.command_id)) {
      machineId = saved.machine; lastOperation = saved.operation;
    }
    if (saved && typeof saved.machine === 'string' && /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(saved.machine) &&
        saved.body && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(saved.body.client_token) &&
        (saved.kind === 'create' && typeof saved.body.name === 'string' && [...saved.body.name].length <= 80 ||
         saved.kind === 'operate' && /^pa_[a-f0-9]{32}$/.test(saved.id) && Object.hasOwn(ACCOUNT_OPERATIONS, saved.body.operation))) {
      pending = saved; machineId = saved.machine;
    }
  } catch (_) { /* Invalid local recovery data grants no authority. */ }
  function busy() { return sending || !!pending; }
  function supported() {
    const node = machines.find(row => row.machine === machineId);
    return node?.online && node.agents?.agy?.installed === true && node.agents.agy.provider_accounts?.schema_version === 1;
  }
  function renderRecovery() {
    const key = JSON.stringify([pending?.kind, pending?.id, pending?.body.client_token, pending?.needsResponse, !!pending?.body.response, sending]);
    if (recovery.dataset.key === key) return;
    recovery.dataset.key = key;
    recovery.replaceChildren(); recovery.hidden = !pending;
    if (!pending) return;
    recovery.append(el('strong', '原请求结果待确认'), el('p', '恢复使用原账号、操作和 client_token，不会发起另一条新命令。'));
    let response;
    if (pending.needsResponse && !pending.body.response && !pending.command) {
      response = field(recovery, 'recover-code', '原授权结果', '', {type: 'password', help: '授权码不保存到浏览器存储。请重新粘贴原请求的同一授权结果。'});
      response.maxLength = 8192; response.autocomplete = 'off';
    }
    recovery.append(button('恢复原账号操作', () => {
      if (response) {
        if (!response.value.trim()) { response.focus(); return; }
        pending.body = Object.freeze({...pending.body, response: response.value.trim()}); response.value = '';
      }
      execute();
    }));
    recovery.querySelector('button').disabled = sending;
  }
  function render() {
    machine.disabled = busy(); createButton.disabled = busy() || !loaded || !!readError || !supported();
    name.disabled = busy(); refresh.disabled = sending;
    status.textContent = readError || mutationNotice || (!loaded ? '正在读取节点与账号…' : !supported() ? '此节点尚未上报在线 AGY 多账号能力。请安装 AGY 并升级节点客户端；已有账号仍可查看。' : accounts.length ? '账号状态由节点确认，将持续更新。' : '此节点还没有独立 Google 账号。创建后可发起登录。');
    renderRecovery();
    receipt.textContent = lastOperation ? ACCOUNT_OPERATIONS[lastOperation.operation] + ' · 命令回执：' + (receiptLabels[lastOperation.status] || lastOperation.status) + ' · ' + lastOperation.command_id : '';
    for (const row of accounts) {
      let card = list.querySelector('[data-account-id="' + row.account_id + '"]');
      if (!card) { card = el('article', null, 'provider-account-card'); card.dataset.accountId = row.account_id; list.append(card); }
      const signature = JSON.stringify([row, busy(), readError, supported(), Math.floor(Date.now() / 1000) >= row.expires_at]);
      if (card.dataset.signature === signature) continue;
      // Preserve authorization drafts across polling; never copy them to a DTO or storage.
      const oldInput = card.querySelector('input'); if (oldInput) drafts.set(row.account_id, oldInput.value);
      card.dataset.signature = signature; card.replaceChildren();
      const header = el('div', null, 'provider-account-header');
      header.append(el('h3', row.name), el('span', ACCOUNT_STATES[row.state], 'provider-account-state')); card.append(header);
      card.append(el('p', row.account_id, 'meta'));
      if (row.error) card.append(el('p', '节点报告：' + row.error, 'err'));
      const actions = el('div', null, 'provider-account-actions');
      const action = (op, title = ACCOUNT_OPERATIONS[op], extra = {}) => {
        const control = button(title, () => begin('operate', row.account_id, {operation: op, ...extra}));
        control.disabled = busy() || !!readError || !supported(); actions.append(control);
      };
      const flow = row.login_command_id;
      if (!readError && row.state === 'waiting' && row.authorization_url && row.expires_at > Date.now() / 1000) {
        const auth = el('a', '打开 Google 授权页面', 'button-secondary'); auth.href = row.authorization_url; auth.target = '_blank'; auth.rel = 'noopener noreferrer'; card.append(auth);
        card.append(el('p', '在 ' + new Date(row.expires_at * 1000).toLocaleTimeString() + ' 前完成授权，并粘贴返回的完整回调地址或授权码。', 'meta'));
        const form = el('form', null, 'provider-account-login');
        const code = field(form, 'login-response', '授权结果 · ' + row.name, drafts.get(row.account_id) || '', {type: 'password', required: true});
        code.maxLength = 8192; code.autocomplete = 'off'; code.disabled = busy() || !supported();
        code.addEventListener('input', () => drafts.set(row.account_id, code.value));
        const submit = el('button', '提交授权结果', 'button-secondary'); submit.type = 'submit'; submit.disabled = busy() || !supported();
        form.append(submit); form.addEventListener('submit', event => {
          event.preventDefault(); const response = code.value.trim(); if (!response) return;
          code.value = ''; drafts.delete(row.account_id);
          begin('operate', row.account_id, {operation: 'submit_login', login_command_id: flow, response});
        }); card.append(form);
      } else {
        const expired = row.expires_at <= Date.now() / 1000;
        if (row.state !== 'waiting' || expired) drafts.delete(row.account_id);
        if (row.state === 'waiting') card.append(el('p', expired ? '授权窗口已过期，请取消原登录并重新发起。' : '暂时无法确认授权状态，请刷新后继续。', 'meta'));
      }
      if (['starting', 'waiting', 'verifying'].includes(row.state) && flow) action('cancel_login', '取消本次登录', {login_command_id: flow});
      if (['signed_out', 'failed'].includes(row.state) && row.enabled) action('login');
      action(row.enabled ? 'disable' : 'enable');
      if (!['signed_out', 'starting'].includes(row.state)) {
        const confirm = el('details'); confirm.append(el('summary', '退出此账号登录'));
        confirm.append(el('p', '将停止此账号的托管会话并清除节点登录凭据，保留会话历史和工作区。', 'meta'));
        const logout = button('确认退出 Google 登录', () => begin('operate', row.account_id, {operation: 'logout'})); logout.disabled = busy() || !!readError || !supported();
        confirm.append(logout); card.append(confirm);
      }
      card.append(actions);
    }
    const ids = new Set(accounts.map(row => row.account_id));
    for (const card of list.children) if (!ids.has(card.dataset.accountId)) { card.querySelectorAll('input').forEach(input => {input.value = '';}); card.remove(); }
    for (const id of drafts.keys()) if (!ids.has(id)) drafts.delete(id);
  }
  function begin(kind, id, body) {
    if (busy() || disposed || suspended || !loaded || readError || !supported()) return;
    pending = {kind, id, machine: machineId, body: Object.freeze({...body, client_token: token()})}; savePending(); execute();
  }
  async function execute() {
    if (!pending || sending || disposed || suspended) return;
    sending = true; mutationNotice = ''; render();
    const original = pending, epoch = generation;
    try {
      const result = original.command ? await api.getProviderAccountOperation(original.id, original.command) : original.kind === 'create'
        ? await api.createProviderAccount(original.machine, original.body) : await api.operateProviderAccount(original.id, original.body);
      if (disposed || epoch !== generation) return;
      lastOperation = result.operation;
      // Once admission is known, GET receipts recover without replaying secrets.
      pending = null; savePending();
      mutationNotice = '请求已受理，正在等待节点更新账号状态。';
      if (original.kind === 'create') name.value = '';
      reader.wake();
    } catch (error) {
      if (disposed || epoch !== generation) return;
      mutationNotice = errorMessage(error);
      if (!uncertainWrite(error) && !original.recovering) { pending = null; savePending(); }
      else { original.recovering = true; savePending(); }
    } finally { sending = false; if (!disposed && epoch === generation) render(); }
  }
  const reader = createNativeReader({interval: 3000, async read(signal, current) {
    {
      const data = await api.getStatus({signal}); if (!current()) return;
      machines = data.machines || []; machine.replaceChildren();
      if (machineId && !machines.some(row => row.machine === machineId)) machines.unshift({machine: machineId, online: false});
      for (const row of machines) { const option = el('option', [row.machine, row.system?.platform, row.online ? '在线' : '离线'].filter(Boolean).join(' · ')); option.value = row.machine; machine.append(option); }
      machineId ||= machines.find(row => row.agents?.agy?.installed)?.machine || machines.find(row => row.online)?.machine || '';
      machine.value = machineId;
    }
    if (!machineId) { loaded = true; readError = '暂无可用电脑节点，请先连接节点。'; render(); return; }
    const id = machineId, data = await api.listProviderAccounts(id, {signal});
    if (!current() || machineId !== id) return;
    accounts = data.accounts; loaded = true; readError = '';
    if (lastOperation && !terminal.has(lastOperation.status)) {
      try {
        const result = await api.getProviderAccountOperation(lastOperation.account_id, lastOperation.command_id, {signal});
        if (!current()) return; lastOperation = result.operation; savePending();
      } catch (_) { if (!current()) return; }
    }
    render();
  }, onError(error) { readError = errorMessage(error); render(); }});
  createForm.addEventListener('submit', event => { event.preventDefault(); if (name.value.trim()) begin('create', '', {name: name.value.trim()}); });
  machine.addEventListener('change', () => {
    reader.stop(); generation++; machineId = machine.value; accounts = []; loaded = false; readError = ''; lastOperation = null; mutationNotice = ''; drafts.clear(); render(); reader.start();
  });
  const visibility = () => { if (document.hidden || suspended) reader.stop(); else reader.start(); };
  document.addEventListener('visibilitychange', visibility);
  render(); visibility();
  const dispose = () => { disposed = true; generation++; reader.dispose(); drafts.clear(); section.querySelectorAll('input').forEach(input => {input.value = '';}); pending = null; document.removeEventListener('visibilitychange', visibility); };
  dispose.suspend = () => {
    suspended = true; reader.stop(); drafts.clear(); section.querySelectorAll('input[type=password]').forEach(input => {input.value = '';});
    if (pending?.body.response) { const body = {...pending.body}; delete body.response; pending.body = Object.freeze(body); pending.needsResponse = true; }
  };
  dispose.resume = () => { suspended = false; visibility(); };
  return dispose;
}
