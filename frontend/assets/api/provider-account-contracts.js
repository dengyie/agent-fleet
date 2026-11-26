// Public node account DTOs. Credentials and native profile paths never enter views.
export const ACCOUNT_STATES = {starting: '等待节点确认', waiting: '等待 Google 授权', verifying: '正在验证授权', ready: '可用', signed_out: '未登录', disabled: '已禁用', failed: '失败'};
export const ACCOUNT_OPERATIONS = {create: '创建账号', login: '登录', submit_login: '提交授权', cancel_login: '取消登录', enable: '启用', disable: '禁用', logout: '退出登录'};
export const isProviderAccountId = value => typeof value === 'string' && /^pa_[a-f0-9]{32}$/.test(value);
const commandId = value => typeof value === 'string' && /^cmd_[a-f0-9]{32}$/.test(value);
const object = value => value && typeof value === 'object' && !Array.isArray(value);
const text = (value, max) => typeof value === 'string' && value.length <= max && !/[\x00-\x1f\x7f]/.test(value);
const date = value => text(value, 64) && Number.isFinite(Date.parse(value));
function invalid() { throw new Error('Google 账号响应不合法，请重新读取当前状态'); }
export function authorizationUrl(value) {
  if (!text(value, 8192) || /\s/.test(value)) return null;
  try {
    const url = new URL(value), keys = [...url.searchParams.keys()];
    return url.origin === 'https://accounts.google.com' && !url.username && !url.password && !url.hash &&
      url.pathname === '/o/oauth2/auth' && keys.length <= 64 && new Set(keys).size === keys.length &&
      url.searchParams.get('response_type') === 'code' && url.searchParams.get('state') &&
      url.searchParams.get('redirect_uri') === 'https://antigravity.google/oauth-callback' ? value : null;
  } catch (_) { return null; }
}
function account(row) {
  if (!object(row) || !isProviderAccountId(row.account_id) || !text(row.machine, 64) ||
      !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(row.machine) || !text(row.name, 160) || !row.name.trim() ||
      [...row.name].length > 80 || row.provider !== 'agy' || !Object.hasOwn(ACCOUNT_STATES, row.state) ||
      !Number.isSafeInteger(row.revision) || row.revision < 1 || typeof row.enabled !== 'boolean' ||
      !date(row.created_at) || !date(row.updated_at)) invalid();
  const out = Object.fromEntries(['account_id', 'machine', 'name', 'provider', 'revision', 'state', 'enabled', 'created_at', 'updated_at'].map(key => [key, row[key]]));
  if (row.login_command_id !== undefined) {
    if (!commandId(row.login_command_id)) invalid();
    out.login_command_id = row.login_command_id;
  }
  if (row.authorization_url !== undefined) {
    if (row.state !== 'waiting' || !authorizationUrl(row.authorization_url) || !out.login_command_id ||
        !Number.isFinite(row.expires_at) || row.expires_at < 0) invalid();
    out.authorization_url = row.authorization_url; out.expires_at = row.expires_at;
  }
  if (row.error !== undefined) { if (!text(row.error, 80)) invalid(); out.error = row.error; }
  return out;
}
function operation(row) {
  if (!object(row) || !commandId(row.command_id) || !isProviderAccountId(row.account_id) ||
      !Number.isSafeInteger(row.revision) || row.revision < 1 || !Object.hasOwn(ACCOUNT_OPERATIONS, row.operation) ||
      !Object.hasOwn(ACCOUNT_STATES, row.state) || !['queued', 'delivered', 'accepted', 'executing', 'succeeded', 'already_finished', 'failed', 'expired', 'rejected', 'superseded'].includes(row.status) ||
      !date(row.created_at) || !date(row.expires_at)) invalid();
  return Object.fromEntries(['command_id', 'account_id', 'revision', 'operation', 'state', 'status', 'created_at', 'expires_at'].map(key => [key, row[key]]));
}
export function parseProviderAccounts(data, {machine, accountId, command, mutation = false} = {}) {
  if (!object(data) || data.ok !== true) invalid();
  const out = {ok: true};
  if (machine && !mutation) {
    if (!Array.isArray(data.accounts) || data.accounts.length > 64) invalid();
    out.accounts = data.accounts.map(account);
    if (out.accounts.some(row => row.machine !== machine) || new Set(out.accounts.map(row => row.account_id)).size !== out.accounts.length) invalid();
    return out;
  }
  if (!command || mutation) {
    out.account = account(data.account);
    if (accountId && out.account.account_id !== accountId || machine && out.account.machine !== machine) invalid();
  }
  if (command || mutation) {
    out.operation = operation(data.operation);
    if (out.operation.account_id !== (accountId || out.account?.account_id) || command && out.operation.command_id !== command) invalid();
  }
  if (mutation) { if (typeof data.created !== 'boolean') invalid(); out.created = data.created; }
  return out;
}
