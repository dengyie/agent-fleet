/* frontend/api/client.js — 前端唯一 HTTP 入口（Task 13）
 *
 * view / state 层不得直接调用 fetch，也不得自行拼接 /api 路径。本模块：
 *   - 解析 window.FleetConfig.apiBaseUrl（默认同源 /api）；
 *   - 对动态路径段做 percent-encoding（机器名、任务 id 等）；
 *   - cache: "no-store"，mutation 请求带 JSON Content-Type；
 *   - JSON 只解析一次；非 2xx 与网络/超时失败统一转 ApiError；
 *   - 响应经过 api/contracts.js 的公开 DTO 校验；
 *   - 不添加任何 ingest / runner 认证 header（浏览器只依赖 operator 会话）；
 *   - 请求超时使用有界默认值（AbortController）。
 *
 * client_token 仅在创建任务时作为浏览器幂等值生成；它绝不是凭据，也不会被
 * 当作认证 header 发送。
 */
import {
  parseError,
  parseStatus,
  parseMachine,
  parseEvents,
  parseTask,
  parseTaskList,
  parseSession,
  parseSessionList,
  parseSessionEvents,
  parseSessionSignals,
  parseAgentTypes,
  parseAdoption,
  parseAdoptionList,
  parseControlEnqueue,
  parseTaskFile,
  parseTaskFileList,
  parseTaskDiff,
} from './contracts.js';

/* 公开 API 路径的唯一来源；动态段由 encodeURIComponent 编码。
 * 保持字面量 "/status" "/machines/" "/tasks" "/events" 便于 source 契约校验。 */
var API = {
  status: '/status',
  machines: '/machines/',
  events: '/events',
  tasks: '/tasks',
  sessions: '/sessions',
  agentTypes: '/agent-types',
  adoptions: '/adoptions',
};

var DEFAULT_TIMEOUT_MS = 15000;
var MAX_TIMEOUT_MS = 60000;
var JSON_HEADERS = { 'Content-Type': 'application/json' };

/* -- ApiError ------------------------------------------------------------ */

export function ApiError(kind, status, code, detail, requestId) {
  this.name = 'ApiError';
  this.kind = kind;        // 'http' | 'network' | 'timeout' | 'parse' | 'contract'
  this.status = status;    // HTTP status（网络/超时为 0）
  this.code = code;        // 公开错误码
  this.detail = detail || '';
  this.requestId = requestId || null;
  this.message = this.detail || this.code || this.kind;
  if (Error.captureStackTrace) {
    Error.captureStackTrace(this, ApiError);
  } else {
    this.stack = (new Error(this.message)).stack;
  }
}
ApiError.prototype = Object.create(Error.prototype);
ApiError.prototype.constructor = ApiError;

/* -- 内部工具 ------------------------------------------------------------ */

/**
 * 解析 API 基址（同源 /api 缺省）。导出供 realtime/sse.js 复用，
 * 保证「前端唯一 HTTP 入口」对 apiBaseUrl 有单一事实来源。
 */
export function resolveApiBaseUrl() {
  var cfg = (typeof window !== 'undefined' && window.FleetConfig) ?
    window.FleetConfig : {};
  var base = (typeof cfg.apiBaseUrl === 'string' && cfg.apiBaseUrl) ?
    cfg.apiBaseUrl : '/api';
  return base.replace(/\/+$/, '');
}

function queryString(params) {
  var keys = params ? Object.keys(params) : [];
  var parts = [];
  for (var i = 0; i < keys.length; i += 1) {
    var value = params[keys[i]];
    if (value === undefined || value === null || value === '') {
      continue;
    }
    parts.push(encodeURIComponent(keys[i]) + '=' +
      encodeURIComponent(String(value)));
  }
  return parts.length ? '?' + parts.join('&') : '';
}

function boundedTimeout(timeoutMs) {
  if (typeof timeoutMs === 'number' && timeoutMs > 0) {
    return Math.min(timeoutMs, MAX_TIMEOUT_MS);
  }
  return DEFAULT_TIMEOUT_MS;
}

function makeClientToken() {
  // 浏览器幂等值（防重复提交），不是凭据。secure context 用 crypto.randomUUID，
  // 其余环境退化为随机串。
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return 'ct-' + Date.now().toString(36) + '-' +
    Math.random().toString(36).slice(2, 12);
}

/**
 * 唯一 request 通道：解析 apiBaseUrl、编码路径、no-store、JSON 解析一次、
 * 非 2xx / 网络 / 超时统一 ApiError。options.body 会被 JSON.stringify；
 * options.method 缺省为 GET；mutation 调用方应传 JSON_HEADERS。
 */
async function request(path, options) {
  options = options || {};
  var controller = new AbortController();
  var timer = setTimeout(function () {
    controller.abort();
  }, boundedTimeout(options.timeoutMs));

  var response;
  try {
    response = await fetch(resolveApiBaseUrl() + path, {
      method: options.method || 'GET',
      headers: options.headers || {},
      body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
      cache: 'no-store',
      signal: controller.signal,
    });
  } catch (err) {
    var aborted = err && (err.name === 'AbortError' || err.code === 20);
    if (aborted) {
      throw new ApiError('timeout', 0, 'request_timeout',
        '请求超时，请稍后重试', null);
    }
    throw new ApiError('network', 0, 'network_error',
      '网络错误，无法连接服务', null);
  } finally {
    clearTimeout(timer);
  }

  var data = null;
  try {
    data = await response.json();
  } catch (e) {
    if (!response.ok) {
      throw new ApiError('http', response.status, 'http_error',
        '服务返回异常，请稍后重试', null);
    }
    throw new ApiError('parse', response.status, 'invalid_json',
      '响应不是合法 JSON', null);
  }

  if (!response.ok) {
    // 统一错误契约只保留 ok/error/detail/request_id；非对象错误体也收敛为 ApiError。
    var errBody;
    try {
      errBody = parseError(data);
    } catch (e) {
      errBody = { ok: false, error: 'http_error',
                  detail: '服务返回异常，请稍后重试', request_id: null };
    }
    throw new ApiError('http', response.status, errBody.error,
      errBody.detail, errBody.request_id);
  }
  return data;
}

/* -- 对外异步接口 --------------------------------------------------------- */

export async function getStatus() {
  return parseStatus(await request(API.status));
}

export async function getMachine(name) {
  return parseMachine(await request(API.machines + encodeURIComponent(name)));
}

export async function getEvents(limit) {
  return parseEvents(await request(API.events + queryString({
    limit: limit || 50,
  })));
}

export async function listTasks(filters) {
  return parseTaskList(await request(API.tasks + queryString(filters || {})));
}

export async function getTask(taskId) {
  return parseTask(await request(API.tasks + '/' + encodeURIComponent(taskId)));
}

export async function createTask(input) {
  var payload = Object.assign({}, input || {});
  if (!payload.client_token) {
    // 浏览器幂等值，仅用于服务端去重；绝不作为凭据或 header 发送。
    payload.client_token = makeClientToken();
  }
  return parseTask(await request(API.tasks, {
    method: 'POST',
    headers: JSON_HEADERS,
    body: payload,
  }));
}

export async function cancelTask(taskId) {
  return parseTask(await request(
    API.tasks + '/' + encodeURIComponent(taskId) + '/cancel', {
      method: 'POST',
      headers: JSON_HEADERS,
    }));
}

export async function retryTask(taskId) {
  return parseTask(await request(
    API.tasks + '/' + encodeURIComponent(taskId) + '/retry', {
      method: 'POST',
      headers: JSON_HEADERS,
    }));
}

export async function pauseTask(taskId) {
  return parseTask(await request(
    API.tasks + '/' + encodeURIComponent(taskId) + '/pause', {
      method: 'POST',
      headers: JSON_HEADERS,
    }));
}

export async function continueTask(taskId) {
  return parseTask(await request(
    API.tasks + '/' + encodeURIComponent(taskId) + '/continue', {
      method: 'POST',
      headers: JSON_HEADERS,
    }));
}

export async function confirmTask(taskId) {
  return parseTask(await request(
    API.tasks + '/' + encodeURIComponent(taskId) + '/confirm', {
      method: 'POST',
      headers: JSON_HEADERS,
    }));
}

export async function rejectTask(taskId) {
  return parseTask(await request(
    API.tasks + '/' + encodeURIComponent(taskId) + '/reject', {
      method: 'POST',
      headers: JSON_HEADERS,
    }));
}

export async function getTaskDiff(taskId) {
  return parseTaskDiff(await request(
    API.tasks + '/' + encodeURIComponent(taskId) + '/diff'));
}

export async function listTaskFiles(taskId) {
  return parseTaskFileList(await request(
    API.tasks + '/' + encodeURIComponent(taskId) + '/files'));
}

export async function getTaskFile(taskId, relpath) {
  var parts = String(relpath || '').split('/');
  var encoded = parts.map(encodeURIComponent).join('/');
  return parseTaskFile(await request(
    API.tasks + '/' + encodeURIComponent(taskId) + '/files/' + encoded));
}

/**
 * 可下发 agent 类型（GET /agent-types，从 agent_profiles 注册表派生）。
 * 用于任务创建表单的下拉填充；失败时由调用方回退到内置清单。
 */
export async function listAgentTypes() {
  return parseAgentTypes(await request(API.agentTypes));
}

/* -- session query surface（Task 11：有界会话数据面，纯 operator 只读）------ */

export async function listSessions(filters) {
  return parseSessionList(await request(API.sessions + queryString(filters || {})));
}

export async function getSession(sessionId) {
  return parseSession(await request(
    API.sessions + '/' + encodeURIComponent(sessionId)));
}

export async function getSessionEvents(sessionId, limit) {
  return parseSessionEvents(await request(
    API.sessions + '/' + encodeURIComponent(sessionId) + '/events' +
    queryString({ limit: limit || 100 })));
}

export async function getSessionSignals(sessionId) {
  return parseSessionSignals(await request(
    API.sessions + '/' + encodeURIComponent(sessionId) + '/policy-signals'));
}

/* -- adoption + source control（Phase 2：纳管座位与五动作；路径只在本模块） */

export async function listAdoptions(machineId) {
  return parseAdoptionList(await request(
    API.adoptions + queryString({ machine_id: machineId })));
}

export async function adoptInstance(machineId, pid, startedAt) {
  return parseAdoption(await request(API.adoptions, {
    method: 'POST',
    headers: JSON_HEADERS,
    body: {
      machine_id: machineId,
      pid: pid,
      started_at: startedAt,
    },
  }));
}

export async function revokeAdoption(sessionId) {
  return parseAdoption(await request(
    API.adoptions + '/' + encodeURIComponent(sessionId), {
      method: 'DELETE',
    }));
}

export async function retryAdoption(sessionId) {
  return parseAdoption(await request(
    API.adoptions + '/' + encodeURIComponent(sessionId) + '/retry', {
      method: 'POST',
      headers: JSON_HEADERS,
    }));
}

export async function controlSession(sessionId, action, reasonCode, payload) {
  var body = {
    action: action,
    reason_code: reasonCode || 'operator_requested',
  };
  if (payload && typeof payload === 'object') {
    if (action === 'append_user_turn' && typeof payload.text === 'string') {
      body.payload = { text: payload.text };
    } else if (action === 'apply_local_profile' &&
               typeof payload.profile_id === 'string') {
      body.payload = { profile_id: payload.profile_id };
    }
  }
  return parseControlEnqueue(await request(
    '/adoption/' + encodeURIComponent(sessionId) + '/source/control', {
      method: 'POST',
      headers: JSON_HEADERS,
      body: body,
    }));
}
