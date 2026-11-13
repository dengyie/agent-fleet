/* GitHub CLI contribution: persisted operations recover device login on reload. */
import {el, button, field, errorText} from './dom.js';

const terminal = new Set(['succeeded', 'failed', 'unknown', 'expired']);
const activeLogin = new Set(['starting', 'waiting_authorization', 'authorizing', 'recovering']);
const states = {
  ready: '可用', not_installed: '尚未接入插件节点', offline: '离线或状态过期',
  node_disabled: '节点已停用', dependency_missing: '未安装 GitHub CLI', plugin_incompatible: '插件版本不兼容',
  authenticated: '认证正常', partial: '部分账号认证异常', authentication_failed: '认证失败',
  not_logged_in: '尚未登录', starting: '正在启动登录', waiting_authorization: '等待浏览器授权',
  authorizing: '正在验证授权', succeeded: '已完成', failed: '失败', unknown: '结果待核对',
  expired: '已过期', queued: '等待节点执行', leased: '节点处理中', running: '正在执行',
  cancelled: '已取消', recovering: '正在恢复登录进度',
};
const actions = {status: '检查登录态', login_start: '登录 GitHub', login_status: '登录进度',
  login_cancel: '取消登录', switch: '切换账号', logout: '退出账号', repo_check: '验证仓库访问'};
const errors = {
  environment_override: '当前使用环境变量令牌，请先在节点调整该环境变量再管理本地账号。',
  context_busy: '此运行环境正在登录，请完成或取消后再试。', cli_upgrade_required: '请升级节点上的 GitHub CLI 以支持结构化登录状态。',
  repository_inaccessible: '当前账号无法访问该仓库，或仓库不存在。', network_unreachable: '节点无法连接 GitHub，请检查网络。',
  network_timeout: '连接 GitHub 超时，请检查节点网络。', dependency_missing: '请先在节点安装 GitHub CLI。',
  git_protocol_unavailable: '无法读取此主机的 Git 协议设置，请检查节点上的 gh config。',
  node_stopped: '登录已随节点停止，请检查状态后重新发起。', node_restarted: '节点已重启，请检查当前登录态。',
  login_worker_stopped: '登录进程已停止，请检查当前登录态后重试。', login_not_found: '节点已无此登录任务，请检查状态后重新登录。',
};

function key() { return globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`; }
function options(select, values, selected) {
  const signature = JSON.stringify(values);
  if (select.dataset.options !== signature) {
    select.replaceChildren(...values.map(([value, text]) => {
      const option = el('option', '', text); option.value = value; return option;
    }));
    select.dataset.options = signature;
  }
  if (values.some(([v]) => v === selected)) select.value = selected;
}

export function mountGitHubCli(host, {plugin: initialPlugin, client}) {
  let plugin = initialPlugin, nodes = [], operations = [], selectedNode = '', selectedContext = '';
  let disposed = false, active = true, generation = 0, timer, loading = false, posting = false, pendingIntent;
  let currentJob, latestLogin;
  const refreshedLogins = new Set();
  const message = el('p', 'plugin-message'); message.setAttribute('role', 'status');
  const intro = el('p', 'plugin-meta', '在目标节点的系统用户环境中管理 GitHub 账号。');
  const setup = el('details', 'plugin-setup');
  setup.append(el('summary', '', '接入节点'), el('p', '',
    '为目标节点配置插件节点凭据、命令公钥和 GitHub CLI 运行环境，然后以开发用户运行 python -m tools.plugins.node --config <配置文件>。接入后在此启用节点。完整步骤见项目 docs/platform/official-plugins.md。'));
  const controls = el('div', 'plugin-controls');
  const nodeSelect = el('select'); nodeSelect.setAttribute('aria-label', '目标节点');
  const contextSelect = el('select'); contextSelect.setAttribute('aria-label', '运行环境');
  const hostname = el('input'); hostname.value = 'github.com'; hostname.setAttribute('aria-label', 'GitHub 主机');
  const selectionKey = 'fleet-plugin-selection:' + plugin.plugin_id;
  try {
    const saved = JSON.parse(window.sessionStorage.getItem(selectionKey) || '{}');
    if (typeof saved.node === 'string') selectedNode = saved.node;
    if (typeof saved.context === 'string') selectedContext = saved.context;
    if (typeof saved.hostname === 'string') hostname.value = saved.hostname;
  } catch (_) { /* Selection recovery is optional. */ }
  function rememberSelection() {
    try { window.sessionStorage.setItem(selectionKey, JSON.stringify({node: selectedNode, context: selectedContext, hostname: hostname.value})); }
    catch (_) { /* Storage can be unavailable in private browser contexts. */ }
  }
  const targetToggle = button('启用此节点', toggleTarget);
  controls.append(field('目标节点', nodeSelect), field('运行环境', contextSelect), field('GitHub 主机', hostname), targetToggle);
  const identity = el('p', 'plugin-identity');
  const toolbar = el('div', 'plugin-actions');
  const inspect = button('检查状态', () => perform('status'), 'button-primary');
  const login = button('登录 GitHub', () => perform('login_start'));
  const checkLogin = button('查看登录进度', () => currentJob && perform('login_status', {login_id: currentJob.login_id}));
  const cancel = button('取消登录', () => currentJob && perform('login_cancel', {login_id: currentJob.login_id}));
  toolbar.append(inspect, login, checkLogin, cancel);
  const auth = el('section', 'plugin-auth');
  const loginPanel = el('section', 'plugin-login'); loginPanel.setAttribute('aria-live', 'polite');
  const repoControls = el('div', 'plugin-controls');
  const repo = el('input'); repo.placeholder = 'owner/repository'; repo.setAttribute('aria-label', '仓库');
  const repoCheck = button('验证仓库访问', () => perform('repo_check', {repository: repo.value.trim()}));
  repoControls.append(field('仓库', repo), repoCheck);
  const repoResult = el('p', 'plugin-repo-result');
  const history = el('div', 'plugin-history');
  host.replaceChildren(intro, setup, message, controls, identity, toolbar, auth, loginPanel,
                       repoControls, repoResult, el('h3', '', '最近操作'), history);

  function args(extra = {}) { return {context_id: selectedContext, hostname: hostname.value.trim().toLowerCase(), ...extra}; }
  function target() { return nodes.find(n => n.node_id === selectedNode); }
  function matching(op) { return op.node_id === selectedNode && op.arguments?.context_id === selectedContext && op.arguments?.hostname === hostname.value.trim().toLowerCase(); }
  function setMessage(text) { message.textContent = text; }
  function render() {
    options(nodeSelect, nodes.map(n => [n.node_id, `${n.label || n.node_id} · ${states[n.readiness] || n.readiness}`]), selectedNode);
    selectedNode = nodeSelect.value;
    const node = target();
    const contexts = node?.offer?.contexts || [];
    options(contextSelect, contexts.map(c => [c.context_id, `${c.label || c.context_id} · ${c.os_user}`]), selectedContext);
    selectedContext = contextSelect.value;
    rememberSelection();
    const context = contexts.find(c => c.context_id === selectedContext);
    const host = hostname.value.trim().toLowerCase();
    const tokenNames = host === 'github.com' || host.endsWith('.ghe.com') ? ['GH_TOKEN', 'GITHUB_TOKEN'] : ['GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN'];
    const envOverride = context?.environment_sources?.some(name => tokenNames.includes(name));
    identity.textContent = context ? `${context.os_user} · ${context.config_dir} · ${node.offer.dependency_version || ''}` : '此节点尚未报告可用的运行环境。';
    targetToggle.textContent = node?.enabled ? '停用此节点' : '启用此节点';
    targetToggle.disabled = !node || posting;
    targetToggle.setAttribute('aria-pressed', String(!!node?.enabled));
    const available = !!(plugin.enabled && node?.enabled && node.readiness === 'ready' && context);
    inspect.disabled = login.disabled = repoCheck.disabled = !available || posting;
    const relevant = operations.filter(matching);
    currentJob = latestLogin && matching({node_id: latestLogin.node_id, arguments: latestLogin}) ? latestLogin : undefined;
    checkLogin.hidden = !currentJob;
    cancel.hidden = !currentJob || !activeLogin.has(currentJob.state);
    checkLogin.disabled = cancel.disabled = posting || !['ready', 'dependency_missing'].includes(node?.readiness);
    const busy = relevant.some(o => !terminal.has(o.status) && ['switch', 'logout', 'login_start'].includes(o.action));
    login.disabled ||= busy || activeLogin.has(currentJob?.state) || !!envOverride;
    auth.replaceChildren();
    const observation = node?.observations?.find(o => o.context_id === selectedContext && o.hostname === hostname.value.trim().toLowerCase());
    auth.append(el('h3', 'plugin-auth-state', states[observation?.state] || '尚未检查登录态'));
    if (observation) auth.append(el('p', 'plugin-meta', `最后检查：${new Date(observation.observed_at * 1000).toLocaleString()}`));
    if (envOverride || observation?.environment_source) {
      auth.append(el('p', 'plugin-notice', `当前认证由 ${observation?.environment_source || '节点环境变量'} 提供。切换本地账号不会覆盖环境变量令牌。`));
    }
    for (const account of observation?.accounts || []) {
      const row = el('div', 'plugin-account');
      row.append(el('strong', '', account.login), el('span', '', `${account.active ? '当前账号 · ' : ''}${account.state === 'success' ? '有效' : '认证异常'} · ${account.token_source}`));
      const change = button('切换到 ' + account.login, () => perform('switch', {username: account.login}));
      const logout = button('退出 ' + account.login, () => {
        if (window.confirm(`退出 ${selectedNode} 上的 GitHub 账号 ${account.login}？`)) perform('logout', {username: account.login});
      });
      change.disabled = logout.disabled = !available || posting || busy || activeLogin.has(currentJob?.state) || !!envOverride;
      change.disabled ||= account.active;
      row.append(change, logout); auth.append(row);
    }
    loginPanel.replaceChildren();
    if (currentJob) {
      loginPanel.append(el('strong', 'plugin-login-state', states[currentJob.state] || currentJob.state));
      if (activeLogin.has(currentJob.state) && currentJob.user_code) {
        loginPanel.append(el('code', 'plugin-device-code', currentJob.user_code));
        try {
          const url = new URL(currentJob.verification_uri);
          if (url.protocol === 'https:' && url.hostname === hostname.value.trim().toLowerCase() && !url.username && !url.password) {
            const link = el('a', 'button-primary', '打开 GitHub 授权页面');
            link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer'; loginPanel.append(link);
          }
        } catch (_) { /* The code can arrive before its URL. */ }
      }
      if (currentJob.error_code) loginPanel.append(el('p', '', errors[currentJob.error_code] || currentJob.error_code));
    }
    const repositoryOp = relevant.find(o => o.action === 'repo_check' && terminal.has(o.status));
    repoResult.textContent = repositoryOp?.result?.repository ?
      `${repositoryOp.result.repository.name}：可读取${repositoryOp.result.repository.permissions?.push === true ? '、可推送' : ''}` : '';
    history.replaceChildren();
    for (const op of relevant.filter(o => o.action !== 'login_status').slice(0, 12)) {
      const row = el('div', 'plugin-operation'); row.dataset.operationId = op.operation_id;
      row.append(el('span', '', actions[op.action] || op.action), el('span', '', states[op.status] || op.status));
      if (op.error_code) row.append(el('span', 'plugin-error', errors[op.error_code] || op.error_code));
      history.append(row);
    }
    if (!nodes.length) setMessage('尚无执行节点。请先按接入说明注册插件节点。');
    else if (!plugin.enabled) setMessage('插件已停用。启用后可发起操作，已有登录仍可查看进度或取消。');
  }

  async function toggleTarget() {
    if (!target() || posting) return;
    posting = true; render();
    try { await client.configurePluginNode(plugin.plugin_id, selectedNode, !target().enabled); }
    catch (error) { if (!disposed) setMessage(errorText(error)); }
    finally { posting = false; await refresh(); }
  }
  async function perform(action, extra = {}, automatic = false) {
    if (disposed || !active || posting || !selectedNode || !selectedContext) return;
    const arguments_ = args(extra), node = selectedNode;
    const signature = JSON.stringify([node, action, arguments_]);
    if (!pendingIntent || pendingIntent.signature !== signature) pendingIntent = {signature, key: key()};
    const intent = pendingIntent;
    posting = true; render();
    try {
      const result = await client.submitPluginOperation(plugin.plugin_id, node, action, arguments_, intent.key);
      pendingIntent = undefined;
      if (!disposed && active) {
        operations = [result.operation, ...operations.filter(o => o.operation_id !== result.operation.operation_id)];
        if (!automatic) setMessage('操作已提交，正在等待节点回执。');
      }
    } catch (error) { if (!disposed && active) setMessage(errorText(error)); }
    finally { posting = false; if (!disposed && active) render(); }
    schedule(250);
  }
  function schedule(delay = 2000) {
    clearTimeout(timer);
    if (!disposed && active) timer = setTimeout(refresh, delay);
  }
  async function refresh() {
    if (disposed || !active || loading) return;
    const ticket = generation;
    loading = true;
    try {
      const targetData = await client.getPluginNodes(plugin.plugin_id);
      if (disposed || !active || ticket !== generation) return;
      if (!Array.isArray(targetData.nodes)) throw new Error('插件状态格式无效');
      nodes = targetData.nodes;
      if (!selectedNode) selectedNode = nodes[0]?.node_id || '';
      setMessage(''); render();
      if (!selectedNode || !selectedContext) return;
      const operationData = await client.getPluginOperations(plugin.plugin_id, {node_id: selectedNode, ...args()});
      if (disposed || !active || ticket !== generation) return;
      if (!Array.isArray(operationData.operations)) throw new Error('插件状态格式无效');
      operations = operationData.operations; latestLogin = operationData.login;
      render();
      // Disabling the plugin stops the normal background status traffic, but a
      // successful cancel request still needs recovery polling: the cancel
      // receipt acknowledges the request and can contain the pre-cancel state
      // until the node worker persists the terminal state.
      const allowRecoveryPoll = plugin.enabled || currentJob?.cancel_requested === true;
      if (allowRecoveryPoll && currentJob && activeLogin.has(currentJob.state)
          && target()?.readiness === 'ready' && !posting) {
        const pending = operations.some(o => matching(o) && o.action === 'login_status' && !terminal.has(o.status));
        if (!pending) await perform('login_status', {login_id: currentJob.login_id}, true);
      } else if (currentJob?.state === 'succeeded' && !refreshedLogins.has(currentJob.login_id) && plugin.enabled && target()?.enabled) {
        refreshedLogins.add(currentJob.login_id);
        await perform('status', {}, true);
      }
    } catch (error) { if (!disposed && active && ticket === generation) setMessage(errorText(error)); }
    finally { loading = false; schedule(); }
  }
  function selectionChanged() { generation++; operations = []; latestLogin = undefined; render(); refresh(); }
  nodeSelect.addEventListener('change', () => { selectedNode = nodeSelect.value; selectedContext = ''; selectionChanged(); });
  contextSelect.addEventListener('change', () => { selectedContext = contextSelect.value; selectionChanged(); });
  hostname.addEventListener('change', selectionChanged);
  const dispose = () => { disposed = true; generation++; clearTimeout(timer); };
  dispose.suspend = () => { active = false; generation++; clearTimeout(timer); };
  dispose.resume = () => { if (!disposed) { active = true; refresh(); } };
  dispose.updatePlugin = value => { plugin = value; render(); refresh(); };
  refresh();
  return dispose;
}
