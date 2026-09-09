/* frontend/api/contracts.js — 公开 DTO 契约（Task 13）
 *
 * 只描述浏览器可见的 public DTO 与 SSE public event DTO。所有解析器：
 *   - 做 allowlist 过滤，返回的模型只含契约内字段（绝不透传内部字段）；
 *   - 在发现契约内不应出现的内部字段（attempt_id / nonce / client_token /
 *     未知 result 内部字段）时，抛有界 ContractError；
 *   - 结构不合法时抛有界 ContractError（不抛内部异常文本）。
 *
 * 本模块是纯 ES module：不引用 window / document / fetch / EventSource，
 * 因此可以被 node 作为可选的本地校验工具加载（仅测试用，非运行时依赖）。
 * 前端运行时是静态原生 JavaScript，无 Node / npm / 构建链。
 */

/* -- 有界契约错误 ------------------------------------------------------ */

export function ContractError(code, detail) {
  this.name = 'ContractError';
  this.kind = 'contract';
  this.code = code;
  this.detail = detail || '';
  this.message = this.detail || this.code;
  if (Error.captureStackTrace) {
    Error.captureStackTrace(this, ContractError);
  } else {
    this.stack = (new Error(this.message)).stack;
  }
}
ContractError.prototype = Object.create(Error.prototype);
ContractError.prototype.constructor = ContractError;

/* -- 字段 allowlist（与后端 public DTO 对齐） ----------------------------- */

// hub/domain/task.py TASK_FIELDS / RESULT_FIELDS
var TASK_FIELDS = ['task_id', 'machine', 'agent_type', 'project', 'state',
                   'requested_by', 'created_at', 'expires_at', 'instruction',
                   'session_id'];
var RESULT_FIELDS = ['exit_code', 'log_summary', 'diff_stat', 'duration_s',
                     'finished_at', 'test_summary', 'has_diff_patch',
                     'diff_patch_bytes'];
// 详情 DTO 不含 patch 正文；正文只走 GET /tasks/<id>/diff。
// paused 必须出现在源码白名单，否则列表/SSE 会把合法状态当未知文本。
var TASK_STATES = ['queued', 'leased', 'running', 'succeeded', 'failed',
                   'cancelled', 'expired', 'paused'];
var GATE_FIELDS = ['kind', 'state', 'requested_by', 'decided_by', 'decided_at',
                   'created_at'];
var TEST_SUMMARY_FIELDS = ['framework', 'passed', 'failed', 'skipped', 'errors',
                           'duration_s', 'failed_names'];
var TASK_DIFF_FIELDS = ['ok', 'task_id', 'diff_patch', 'truncated'];
// GET /api/tasks/<id>/files 列表行 / 单文件 DTO（不含 attempt_id）
var TASK_FILE_META_FIELDS = ['path', 'bytes', 'truncated', 'redacted'];
var TASK_FILE_FIELDS = ['path', 'content', 'bytes', 'truncated', 'redacted'];
// 绝不允许出现在公开任务契约里的内部/幂等字段
var REJECTED_TASK_FIELDS = ['attempt_id', 'nonce', 'client_token'];

// hub/domain/machine.py public_machine_summary / public_machine_detail
var MACHINE_SUMMARY_FIELDS = ['machine', 'desc', 'online', 'error', 'timestamp',
                              'system', 'agents', 'agent_summaries',
                              'agent_count', 'has_hermes', 'hermes_state'];
var MACHINE_DETAIL_FIELDS = ['machine', 'current', 'history'];
// 实例 metadata 只允许这八个脱敏字段（与后端 report_schema.INSTANCE_FIELDS
// 对齐）；conversation / 进程内部状态 / 未知键绝不进入公开模型。
var INSTANCE_FIELDS = ['pid', 'pgid', 'exe_path', 'cmdline', 'agent_family',
                       'native_file_path', 'started_at', 'attachable'];
var CURRENT_FIELDS = ['timestamp', 'reachable', 'remote_error', 'agents',
                      'system', 'instances', 'local_profiles'];

var LOCAL_PROFILE_FIELDS = ['profile_id', 'family', 'label', 'origin',
                            'current'];
var HISTORY_ROW_FIELDS = ['ts', 'reachable'];

// observe_service.events() 摘要行
var EVENT_FIELDS = ['event', 'machine', 'ts', 'changes'];

// /api/status 外层（features 是默认关闭的 Phase 4/5 发行旗标，缺省视为关）
var STATUS_FIELDS = ['updated_at', 'machines', 'features'];
var STATUS_FEATURE_FIELDS = ['append_user_turn', 'apply_local_profile'];

// hub/http/observe_routes._sse_payload() 的四种 public event 形状
var SSE_MACHINE_UPDATE_FIELDS = ['machine', 'changes', 'online', 'ts',
                                 'agents', 'system'];
var SSE_TASK_LOG_FIELDS = ['task_id', 'line', 'ts'];
var SSE_TASK_UPDATE_FIELDS = ['task_id', 'machine', 'state', 'event', 'ts'];
var SSE_FLEET_EVENT_FIELDS = ['event', 'machine', 'changes', 'ts'];

// 统一错误契约 {ok,error,detail,request_id}
var ERROR_FIELDS = ['ok', 'error', 'detail', 'request_id'];

/* -- session public DTO allowlists（Task 11，对齐 session_schema） ---------- */

// session metadata（public_session_dto 的公开字段；attempt_id 仅受管会话出现）
var SESSION_FIELDS = ['machine_id', 'session_id', 'managed', 'capture_quality',
                      'control_capability', 'attempt_id', 'process_group_id',
                      'agent_family', 'status', 'started_at', 'updated_at',
                      'capabilities'];

// 每个 session event 的可见 envelope 字段（公开事件行；payload 有逐 kind 白名单）
var SESSION_EVENT_FIELDS = ['schema_version', 'event_id', 'stream_id',
                            'machine_id', 'session_id', 'sequence', 'kind',
                            'capture_quality', 'emitted_at', 'source'];

// 通用 event 的 payload 允许含的字段：数字/字符串/布尔值都会被 bound，
// 嵌套容器一律拒绝（与 session_schema._GENERIC_PAYLOAD_FIELDS 对齐）。
var EVENT_PAYLOAD_FIELDS = ['text', 'is_complete',
                            'tool_name', 'call_id', 'status', 'arguments',
                            'result', 'digest',
                            'start_sequence', 'end_sequence', 'reason',
                            'old_quality', 'new_quality',
                            'agent_family', 'adapter', 'task_id', 'agent_ref',
                            'process_family', 'owner', 'group_binding',
                            'process_group_id', 'exit_code', 'duration_s',
                            'step_id', 'step_name', 'severity', 'policy',
                            'detail', 'quality', 'old_state', 'new_state'];

// policy-signal 索引：bounded operator 表面（session_service.policy_signals）
var SESSION_SIGNAL_FIELDS = ['session_id', 'emitted_at', 'severity', 'reason'];

// control receipt（控制队列状态）公开字段：绝不包含非ce/signature/token。
var CONTROL_RECEIPT_FIELDS = ['command_id', 'action', 'status', 'issued_at',
                              'expires_at', 'attempt_id', 'machine_id',
                              'session_id', 'reason'];

// 纳管公开投影（对齐 hub/http/adoption_routes._BOUNDED_FIELDS）
var ADOPTION_FIELDS = ['adoption_id', 'machine_id', 'session_id',
                       'agent_family', 'capture_quality', 'status', 'actor',
                       'created_at', 'updated_at'];
var ADOPTION_RESPONSE_FIELDS = ['ok', 'adoption_id', 'machine_id',
                                'session_id', 'status', 'request_id'];
var ADOPTION_LIST_FIELDS = ['ok', 'machine_id', 'items', 'request_id'];
var CONTROL_ENQUEUE_FIELDS = ['ok', 'command_id', 'session_id', 'action',
                              'status', 'request_id'];
var SOURCE_CONTROL_ACTIONS = ['pause_session', 'resume_session',
                              'terminate_session', 'quarantine_session',
                              'cancel_attempt', 'append_user_turn',
                              'apply_local_profile'];
var REJECTED_CONTROL_KEYS = ['pid', 'signal', 'shell', 'command'];

// SSE summary 事件只携带这些公开字段（绝不透传 payload/transcript）。
var SSE_SESSION_SUMMARY_FIELDS = ['session_id', 'sequence', 'cursor', 'kind',
                                  'capture_quality', 'status', 'ts'];

// 任何 session 表面的顶层都不允许出现的内部字段。
var REJECTED_SESSION_KEYS = ['nonce', 'client_token', 'token', 'secret',
                             'signature', 'password', 'raw'];

// 数字/布尔 payload 字段（保持数值，不 bound 成字符串）
var NUMERIC_PAYLOAD_FIELDS = ['start_sequence', 'end_sequence', 'exit_code',
                              'duration_s', 'sequence'];

/* -- 基础守卫 ----------------------------------------------------------- */

function isObject(v) {
  return v !== null && typeof v === 'object' && !Array.isArray(v);
}

function requireObject(v, label) {
  if (!isObject(v)) {
    throw new ContractError('invalid_' + label, '契约字段必须是对象');
  }
}

function pick(obj, fields) {
  var out = {};
  for (var i = 0; i < fields.length; i += 1) {
    var key = fields[i];
    if (obj[key] !== undefined) {
      out[key] = obj[key];
    }
  }
  return out;
}

function boundStr(v, limit) {
  if (v === null || v === undefined) {
    return v;
  }
  if (typeof v === 'string') {
    return v.slice(0, limit);
  }
  return String(v).slice(0, limit);
}

/* -- 实例 metadata 解析（Task 3，只读展示） -------------------------------- */

/**
 * 解析一行实例 metadata 为公开模型：只保留 INSTANCE_FIELDS 八个字段，字符串
 * 有界、整数归一、布尔钳制。匿名公开行可以省略 pid / pgid；身份字段只要出现
 * 就必须完整且合法，避免把部分身份误当成可采纳候选。
 */
function parseInstanceRow(row) {
  requireObject(row, 'machine.instance');
  var pid = row.pid;
  var pgid = row.pgid;
  var anonymous = (pid === undefined || pid === null)
    && (pgid === undefined || pgid === null);
  if (anonymous) {
    var anonymousFamily = typeof row.agent_family === 'string'
      ? row.agent_family.slice(0, 64) : '';
    if (!anonymousFamily) {
      throw new ContractError('invalid_instance', 'instance 缺乏 agent_family');
    }
    return {
      pid: null,
      pgid: null,
      exe_path: '',
      cmdline: '',
      agent_family: anonymousFamily,
      native_file_path: null,
      started_at: '',
      attachable: false,
    };
  }
  if (typeof pid === 'string' && /^\d+$/.test(pid)) {
    pid = Number(pid);
  }
  if (typeof pgid === 'string' && /^\d+$/.test(pgid)) {
    pgid = Number(pgid);
  }
  if (typeof pid !== 'number' || !isFinite(pid) || pid < 0) {
    throw new ContractError('invalid_instance', 'instance 缺乏合法 pid');
  }
  if (typeof pgid !== 'number' || !isFinite(pgid) || pgid < 0) {
    throw new ContractError('invalid_instance', 'instance 缺乏合法 pgid');
  }
  var family = typeof row.agent_family === 'string'
    ? row.agent_family.slice(0, 64) : '';
  if (!family) {
    throw new ContractError('invalid_instance', 'instance 缺乏 agent_family');
  }
  var nativePath = boundStr(row.native_file_path, 200);
  return {
    pid: pid,
    pgid: pgid,
    exe_path: boundStr(row.exe_path, 200) || '',
    cmdline: boundStr(row.cmdline, 200) || '',
    agent_family: family,
    native_file_path: (nativePath && nativePath.length > 0) ? nativePath : null,
    started_at: boundStr(row.started_at, 32) || '',
    attachable: row.attachable === true,
  };
}

/** instances 缺失 → 空列表；逐行走上述 allowlist。 */
function parseInstanceList(value) {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.map(parseInstanceRow);
}

function parseLocalProfileRow(row) {
  requireObject(row, 'local_profile');
  rejectInternalKey(row, 'local_profile');
  rejectControlKeys(row, 'local_profile');
  var profileId = row.profile_id;
  var family = row.family;
  if (typeof profileId !== 'string' || !profileId) {
    throw new ContractError('invalid_local_profile', '缺少 profile_id');
  }
  if (typeof family !== 'string' || !family) {
    throw new ContractError('invalid_local_profile', '缺少 family');
  }
  var lowered = profileId.toLowerCase();
  if (lowered.indexOf('token') !== -1 || lowered.indexOf('secret') !== -1 ||
      lowered.indexOf('password') !== -1 || lowered.indexOf('api_key') !== -1 ||
      lowered.indexOf('credential') !== -1) {
    throw new ContractError('invalid_local_profile', 'profile_id 非法');
  }
  return {
    profile_id: boundOpaque(profileId, 'profile_id'),
    family: String(family).slice(0, 32),
    label: boundStr(row.label || profileId, 64),
    origin: boundStr(row.origin || 'cc_switch', 32),
    current: row.current === true,
  };
}

function parseLocalProfileList(value) {
  if (!Array.isArray(value)) {
    return [];
  }
  var out = [];
  for (var i = 0; i < value.length && out.length < 64; i += 1) {
    out.push(parseLocalProfileRow(value[i]));
  }
  return out;
}

/* -- 公开解析器 ---------------------------------------------------------- */

/**
 * 解析统一错误响应，只保留 ok/error/detail/request_id，丢弃其余一切
 * （traceback、SQL、内部文本等绝不会进入错误模型）。
 */
export function parseError(data) {
  requireObject(data, 'error_response');
  var out = {};
  if (typeof data.ok === 'boolean') {
    out.ok = data.ok;
  }
  out.error = typeof data.error === 'string' ? data.error : 'internal_error';
  out.detail = typeof data.detail === 'string' ? data.detail.slice(0, 200) : '';
  out.request_id = typeof data.request_id === 'string' ? data.request_id : '';
  return out;
}

/**
 * 解析 Fleet 总览（GET /api/status）。
 * 外层 {updated_at, machines: [...]}，无 ok 标记。
 */
export function parseStatus(data) {
  requireObject(data, 'status_response');
  var out = pick(data, STATUS_FIELDS);
  if (typeof out.updated_at !== 'string') {
    out.updated_at = '';
  }
  if (!Array.isArray(out.machines)) {
    out.machines = [];
  } else {
    out.machines = out.machines.map(parseMachineSummary);
  }
  out.features = parseStatusFeatures(data.features);
  return out;
}

function parseStatusFeatures(raw) {
  var src = isObject(raw) ? pick(raw, STATUS_FEATURE_FIELDS) : {};
  return {
    append_user_turn: src.append_user_turn === true,
    apply_local_profile: src.apply_local_profile === true,
  };
}

function parseMachineSummary(row) {
  requireObject(row, 'machine_summary');
  var out = pick(row, MACHINE_SUMMARY_FIELDS);
  if (typeof out.online !== 'boolean') {
    out.online = false;
  }
  if (typeof out.machine !== 'string' || out.machine === '') {
    throw new ContractError('invalid_machine_summary', '机器摘要缺少 machine');
  }
  return out;
}

/**
 * 解析单机详情（GET /api/machines/<name>）。
 * 外层 {ok:true, machine, current, history}；current 只保留脱敏字段。
 */
export function parseMachine(data) {
  requireObject(data, 'machine_response');
  if (data.ok !== true) {
    throw new ContractError('invalid_machine_response', '机器响应标记无效');
  }
  requireObject(data.current, 'machine.current');
  var out = pick(data, MACHINE_DETAIL_FIELDS);
  out.current = pick(data.current, CURRENT_FIELDS);
  if (typeof out.current.timestamp === 'undefined') {
    out.current.timestamp = null;
  }
  if (typeof out.current.reachable !== 'boolean') {
    out.current.reachable = false;
  }
  if (typeof out.current.remote_error !== 'undefined') {
    out.current.remote_error = boundStr(out.current.remote_error, 200);
  }
  if (!isObject(out.current.agents)) {
    out.current.agents = {};
  }
  if (!isObject(out.current.system)) {
    out.current.system = {};
  }
  // 实例 metadata 只读 allowlist：未知键 / 原生内容在 contracts 层再次被丢弃
  out.current.instances = parseInstanceList(data.current.instances);
  out.current.local_profiles = parseLocalProfileList(data.current.local_profiles);
  out.history = [];
  if (Array.isArray(data.history)) {
    for (var i = 0; i < data.history.length; i += 1) {
      var row = data.history[i];
      if (!isObject(row)) {
        throw new ContractError('invalid_machine_history', '历史行必须是对象');
      }
      out.history.push(pick(row, HISTORY_ROW_FIELDS));
    }
  }
  return out;
}

/**
 * 解析最近事件摘要（GET /api/events?limit=）。
 * 外层 {events: [{event,machine,ts,changes}]}，无 ok 标记。
 */
export function parseEvents(data) {
  requireObject(data, 'events_response');
  var out = { events: [] };
  if (Array.isArray(data.events)) {
    out.events = data.events.map(function (row) {
      requireObject(row, 'event');
      return pick(row, EVENT_FIELDS);
    });
  }
  return out;
}

/**
 * 解析可下发 agent 类型清单（GET /api/agent-types）。
 * 外层 {agent_types: [...]}：只保留字符串值，去重、trim、有界
 * （每项 ≤64 字符，至多 64 项）；非字符串 / 空值一律丢弃。
 */
export function parseAgentTypes(data) {
  requireObject(data, 'agent_types_response');
  var raw = Array.isArray(data.agent_types) ? data.agent_types : [];
  var out = [];
  for (var i = 0; i < raw.length && out.length < 64; i += 1) {
    var value = (typeof raw[i] === 'string') ? raw[i].trim() : '';
    if (value && out.indexOf(value) === -1) {
      out.push(value.slice(0, 64));
    }
  }
  return { agent_types: out };
}

/**
 * 解析单个任务 DTO（create / get / cancel / retry 响应中的 task）。
 * 会拒绝 attempt_id / nonce / client_token 以及未知 result 内部字段。
 */
export function parseTask(data) {
  requireObject(data, 'task_response');
  if (data.ok !== true) {
    throw new ContractError('invalid_task_response', '任务响应标记无效');
  }
  var out = { ok: true };
  if (typeof data.created === 'boolean') {
    out.created = data.created;
  }
  if (typeof data.changed === 'boolean') {
    out.changed = data.changed;
  }
  out.task = parseTaskObject(data.task);
  return out;
}

/**
 * 解析任务列表响应（GET /api/tasks）。
 * 外层 {ok:true, tasks:[...]}。
 */
export function parseTaskList(data) {
  requireObject(data, 'task_list_response');
  if (data.ok !== true) {
    throw new ContractError('invalid_task_list', '任务列表响应标记无效');
  }
  var out = { ok: true, tasks: [] };
  if (Array.isArray(data.tasks)) {
    out.tasks = data.tasks.map(parseTaskObject);
  }
  return out;
}

function rejectSecretTaskFields(task) {
  for (var i = 0; i < REJECTED_TASK_FIELDS.length; i += 1) {
    var key = REJECTED_TASK_FIELDS[i];
    if (Object.prototype.hasOwnProperty.call(task, key)) {
      throw new ContractError(
        'rejected_internal_field',
        '公开任务契约不允许字段: ' + key);
    }
  }
}

function rejectUnknownResultFields(result) {
  var keys = Object.keys(result);
  for (var i = 0; i < keys.length; i += 1) {
    if (RESULT_FIELDS.indexOf(keys[i]) === -1) {
      throw new ContractError(
        'rejected_result_internal',
        '任务结果契约包含未知内部字段: ' + keys[i]);
    }
  }
}

function parseTaskObject(task) {
  requireObject(task, 'task');
  rejectSecretTaskFields(task);
  var out = pick(task, TASK_FIELDS);
  if (typeof out.session_id === 'undefined' || out.session_id === null || out.session_id === '') {
    delete out.session_id;
  } else {
    out.session_id = boundOpaque(out.session_id, 'session_id');
    if (!out.session_id) {
      delete out.session_id;
    }
  }
  if (Object.prototype.hasOwnProperty.call(task, 'result')) {
    if (task.result === null) {
      // 后端对尚无结果的任务（queued/leased/running 等）返回 result: null；
      // 显式保留 null，不做对象校验。
      out.result = null;
    } else {
      requireObject(task.result, 'task.result');
      rejectUnknownResultFields(task.result);
      out.result = pick(task.result, RESULT_FIELDS);
      if (out.result.test_summary && typeof out.result.test_summary === 'object') {
        out.result.test_summary = pick(out.result.test_summary, TEST_SUMMARY_FIELDS);
      }
    }
  }
  if (Object.prototype.hasOwnProperty.call(task, 'gate') && task.gate) {
    requireObject(task.gate, 'task.gate');
    out.gate = pick(task.gate, GATE_FIELDS);
  }
  return out;
}

/**
 * 解析脱敏 patch 正文（GET /api/tasks/<id>/diff）。
 * 只保留 ok/task_id/diff_patch/truncated；content 有界；拒绝内部字段。
 */
export function parseTaskDiff(data) {
  requireObject(data, 'task_diff');
  if (data.ok !== true) {
    throw new ContractError('invalid_task_diff', '任务 diff 响应标记无效');
  }
  rejectSecretFileFields(data, 'task_diff');
  var out = pick(data, TASK_DIFF_FIELDS);
  out.ok = true;
  if (typeof out.task_id === 'string') {
    out.task_id = boundOpaque(out.task_id, 'task_id');
  } else {
    out.task_id = '';
  }
  out.diff_patch = (typeof out.diff_patch === 'string')
    ? out.diff_patch.slice(0, 102400) : '';
  out.truncated = out.truncated === true;
  return out;
}

/**
 * 解析一条 SSE public event（name = 事件名，data = 已 parse 的 JSON data）。
 * machine_update / task_update / task_log / fleet_event 四种形状都只保留
 * 公开字段；未知事件名按通用 public event 形状处理。
 */
export function parseSseEvent(name, data) {
  requireObject(data, 'sse_event');
  if (name === 'machine_update') {
    return pick(data, SSE_MACHINE_UPDATE_FIELDS);
  }
  if (name === 'task_log') {
    return pick(data, SSE_TASK_LOG_FIELDS);
  }
  if (name === 'task_update') {
    return pick(data, SSE_TASK_UPDATE_FIELDS);
  }
  if (name === 'session_update' || name === 'session_summary') {
    return parseSessionSummary(data, name);
  }
  return pick(data, SSE_FLEET_EVENT_FIELDS);
}

/* -- session summary（SSE 概要；只保留公开摘要字段） ---------------------- */

/**
 * 解析 session 的 SSE 摘要帧（session_update / session_summary）。
 * 只保留 session_id / sequence / cursor / kind / capture_quality / status /
 * ts 这些 allowlist 字段，其余（payload、text、token、nonce、snapshot…）
 * 一律不进入模型。capture_quality 钳到三档集合，未知一律 best_effort。
 */
export function parseSessionSummary(data, name) {
  requireObject(data, name || 'session_summary');
  var out = pick(data, SSE_SESSION_SUMMARY_FIELDS);
  if (typeof out.capture_quality !== 'string' ||
      capQualities.indexOf(out.capture_quality) === -1) {
    out.capture_quality = 'best_effort';
  }
  return out;
}

/* -- session parsers（Task 11：有大会话数据面） ------------------------------ */

/**
 * 解析单个受管/非受管会话 DTO（GET /api/sessions/<id>）。
 * 外层 {session: {...}}（无 ok）。允许 attempt_id/process_group_id（受管会话
 * 的公开 opaque 标识），但一旦出现 nonce/client_token/token/secret/signature
 * 即整体拒绝。
 */
export function parseSession(data) {
  requireObject(data, 'session_response');
  if (!isObject(data.session)) {
    throw new ContractError('invalid_session_response',
                            '会话响应缺少 session 对象');
  }
  var row = parseSessionRow(data.session);
  return { session: row };
}

/**
 * 解析会话列表（GET /api/sessions）。外层 {sessions: [...]}，无 ok。
 */
export function parseSessionList(data) {
  requireObject(data, 'session_list_response');
  var out = { sessions: [] };
  if (Array.isArray(data.sessions)) {
    out.sessions = data.sessions.map(parseSessionRow);
  }
  return out;
}

/**
 * 解析单个会话的有界事件流（GET /api/sessions/<id>/events）。
 * 每个事件行走同一 allowlist；payload 是逐 kind 白名单（见 parseEventPayload）。
 * 拒绝顶层 nonce/client_token/token/secret/signature。
 */
export function parseSessionEvents(data) {
  requireObject(data, 'session_events_response');
  var out = { events: [] };
  if (Array.isArray(data.events)) {
    out.events = data.events.map(parseSessionEventRow);
  }
  return out;
}

/**
 * 解析策略信号索引（GET /api/sessions/<id>/policy-signals）。
 * 只保留 session_id / emitted_at / severity / reason。
 */
export function parseSessionSignals(data) {
  requireObject(data, 'session_signals_response');
  var out = { signals: [] };
  if (Array.isArray(data.signals)) {
    out.signals = data.signals.map(parseSessionSignalRow);
  }
  return out;
}

/**
 * 解析控制回执列表（前瞻：未来 operator 控制详情接口；现无该 HTTP route）。
 * 只保留 command_id/action/attempt_id/status 等公开字段，拒绝 nonce/token。
 */
export function parseControlReceipts(data) {
  requireObject(data, 'control_receipts_response');
  var out = { receipts: [] };
  if (Array.isArray(data.receipts)) {
    out.receipts = data.receipts.map(parseControlReceiptRow);
  }
  return out;
}

/**
 * 解析单条纳管响应（POST /adoptions、DELETE、retry）。
 * 只保留有界公开字段；拒绝 nonce/token/signature 与 pid/signal/shell。
 */
export function parseAdoption(data) {
  requireObject(data, 'adoption');
  rejectInternalKey(data, 'adoption');
  rejectControlKeys(data, 'adoption');
  var out = pick(data, ADOPTION_RESPONSE_FIELDS);
  if (typeof out.ok === 'undefined') {
    out.ok = true;
  }
  if (typeof out.session_id !== 'undefined') {
    out.session_id = boundOpaque(out.session_id, 'session_id');
  }
  if (typeof out.machine_id !== 'undefined') {
    out.machine_id = boundOpaque(out.machine_id, 'machine_id');
  }
  if (typeof out.adoption_id !== 'undefined') {
    out.adoption_id = boundOpaque(out.adoption_id, 'adoption_id');
  }
  if (typeof out.status === 'string') {
    out.status = String(out.status).slice(0, 32);
  }
  return out;
}

/**
 * 解析 GET /adoptions?machine_id= 列表。items 走 bounded 投影。
 */
export function parseAdoptionList(data) {
  requireObject(data, 'adoption_list');
  rejectInternalKey(data, 'adoption_list');
  rejectControlKeys(data, 'adoption_list');
  var out = pick(data, ADOPTION_LIST_FIELDS);
  out.items = [];
  if (typeof out.machine_id !== 'undefined') {
    out.machine_id = boundOpaque(out.machine_id, 'machine_id');
  }
  if (Array.isArray(data.items)) {
    out.items = data.items.map(parseAdoptionRow);
  }
  return out;
}

/**
 * 解析 POST /adoption/<id>/source/control 的 202 入队回执。
 * 体字段只允许 action/status/command_id 等公开键；拒绝 pid/signal/shell。
 */
export function parseControlEnqueue(data) {
  requireObject(data, 'control_enqueue');
  rejectInternalKey(data, 'control_enqueue');
  rejectControlKeys(data, 'control_enqueue');
  var out = pick(data, CONTROL_ENQUEUE_FIELDS);
  if (typeof out.ok === 'undefined') {
    out.ok = true;
  }
  if (typeof out.session_id !== 'undefined') {
    out.session_id = boundOpaque(out.session_id, 'session_id');
  }
  if (typeof out.command_id !== 'undefined') {
    out.command_id = boundOpaque(out.command_id, 'command_id');
  }
  if (typeof out.action === 'string') {
    if (SOURCE_CONTROL_ACTIONS.indexOf(out.action) === -1) {
      throw new ContractError('invalid_control_action',
                              '控制动作不在白名单');
    }
  }
  if (typeof out.status === 'string') {
    out.status = String(out.status).slice(0, 32);
  }
  return out;
}

/* -- session 解析内部实现 ------------------------------------------------- */

// capture quality 三档枚举（与 session_schema.CAPTURE_QUALITIES 一致）
var capQualities = ['exact', 'structured', 'best_effort'];

function rejectInternalKey(row, label) {
  for (var i = 0; i < REJECTED_SESSION_KEYS.length; i += 1) {
    if (Object.prototype.hasOwnProperty.call(row, REJECTED_SESSION_KEYS[i])) {
      throw new ContractError('rejected_internal_field',
                              label + ' 契约不允许字段: ' +
                              REJECTED_SESSION_KEYS[i]);
    }
  }
}

function rejectControlKeys(row, label) {
  for (var i = 0; i < REJECTED_CONTROL_KEYS.length; i += 1) {
    if (Object.prototype.hasOwnProperty.call(row, REJECTED_CONTROL_KEYS[i])) {
      throw new ContractError('rejected_internal_field',
                              label + ' 契约不允许字段: ' +
                              REJECTED_CONTROL_KEYS[i]);
    }
  }
}

function parseAdoptionRow(row) {
  requireObject(row, 'adoption_row');
  rejectInternalKey(row, 'adoption_row');
  rejectControlKeys(row, 'adoption_row');
  var out = pick(row, ADOPTION_FIELDS);
  if (typeof out.session_id !== 'undefined') {
    out.session_id = boundOpaque(out.session_id, 'session_id');
  }
  if (typeof out.machine_id !== 'undefined') {
    out.machine_id = boundOpaque(out.machine_id, 'machine_id');
  }
  if (typeof out.adoption_id !== 'undefined') {
    out.adoption_id = boundOpaque(out.adoption_id, 'adoption_id');
  }
  if (typeof out.status === 'string') {
    out.status = String(out.status).slice(0, 32);
  }
  return out;
}

function clampQuality(v) {
  return (capQualities.indexOf(String(v)) !== -1) ? String(v) : 'best_effort';
}

function parseSessionRow(row) {
  requireObject(row, 'session');
  rejectInternalKey(row, 'session');
  var out = pick(row, SESSION_FIELDS);
  var managed = (out.managed === true);
  out.managed = managed;
  if (typeof out.control_capability === 'undefined') {
    out.control_capability = managed ? 'available' : 'unavailable';
  }
  // 非受管会话强制 best_effort + control unavailable（后端同一钳制）
  if (!managed) {
    out.capture_quality = 'best_effort';
    out.control_capability = 'unavailable';
  } else {
    out.capture_quality = clampQuality(out.capture_quality);
    if (out.control_capability !== 'available') {
      out.control_capability = 'unavailable';
    }
  }
  if (!managed) {
    // 非受管会话不应携带 attempt_id（后端 DTO 同样去掉）
    delete out.attempt_id;
  }
  // bounded opaque 标识（与后端 _public_id 一致）
  out.session_id = boundOpaque(out.session_id, 'session_id');
  out.machine_id = boundOpaque(out.machine_id, 'machine_id');
  if (typeof out.capabilities !== 'undefined' && !Array.isArray(out.capabilities)) {
    delete out.capabilities;
  }
  return out;
}

function boundOpaque(v, label) {
  if (typeof v !== 'string' || !v) {
    return '';
  }
  if (v.length > 256 || /[\/\\]/.test(v)) {
    throw new ContractError('invalid_opaque_id', label + ' 不是合法 opaque id');
  }
  return v.slice(0, 128);
}

function parseSessionEventRow(row) {
  requireObject(row, 'session_event');
  rejectInternalKey(row, 'session_event');
  var out = pick(row, SESSION_EVENT_FIELDS);
  if (typeof out.event_id !== 'string') {
    out.event_id = '';
  }
  if (typeof out.session_id !== 'string') {
    out.session_id = '';
  }
  if (typeof out.sequence !== 'number' || !isFinite(out.sequence)) {
    throw new ContractError('invalid_session_event',
                            '事件缺少有效 sequence');
  }
  out.capture_quality = clampQuality(out.capture_quality);
  if (row.payload !== undefined) {
    out.payload = parseEventPayload(String(row.kind), row.payload);
  } else {
    delete out.payload;
  }
  return out;
}

function parseEventPayload(kind, payload) {
  if (!isObject(payload)) {
    throw new ContractError('invalid_event_payload', '事件 payload 必须是对象');
  }
  var out = {};
  var keys = Object.keys(payload);
  for (var i = 0; i < keys.length; i += 1) {
    var key = keys[i];
    if (EVENT_PAYLOAD_FIELDS.indexOf(key) === -1) {
      continue; // 未知键直接丢弃（绝不透传）
    }
    var value = payload[key];
    if (NUMERIC_PAYLOAD_FIELDS.indexOf(key) !== -1) {
      if (typeof value === 'number' && isFinite(value)) {
        out[key] = value;
      }
      continue;
    }
    if (typeof value === 'boolean') {
      out[key] = value;
      continue;
    }
    if (value === null || value === undefined) {
      continue;
    }
    if (typeof value === 'object') {
      throw new ContractError('invalid_payload_type',
                              key + ' 必须是原始值，拒绝嵌套容器');
    }
    out[key] = String(value).slice(0, 4096);
  }
  return out;
}

function parseSessionSignalRow(row) {
  requireObject(row, 'session_signal');
  var out = pick(row, SESSION_SIGNAL_FIELDS);
  if (typeof out.severity !== 'string') {
    out.severity = '';
  }
  if (typeof out.reason !== 'string') {
    out.reason = '';
  }
  return out;
}

function parseControlReceiptRow(row) {
  requireObject(row, 'control_receipt');
  rejectInternalKey(row, 'control_receipt');
  var out = pick(row, CONTROL_RECEIPT_FIELDS);
  if (typeof out.status !== 'string') {
    throw new ContractError('invalid_control_receipt',
                            '控制回执缺少 status');
  }
  return out;
}

function rejectSecretFileFields(row, label) {
  for (var i = 0; i < REJECTED_TASK_FIELDS.length; i += 1) {
    var key = REJECTED_TASK_FIELDS[i];
    if (Object.prototype.hasOwnProperty.call(row, key)) {
      throw new ContractError(
        'rejected_internal_field',
        label + ' 不允许字段: ' + key);
    }
  }
}

function boundFilePath(v) {
  if (typeof v !== 'string' || !v || v.length > 256) {
    return '';
  }
  if (v.charAt(0) === '/' || v.indexOf('\\') !== -1 || v.indexOf('..') !== -1) {
    return '';
  }
  return v;
}

function parseTaskFileMeta(row) {
  requireObject(row, 'task_file_meta');
  rejectSecretFileFields(row, 'task_file_meta');
  var out = pick(row, TASK_FILE_META_FIELDS);
  out.path = boundFilePath(out.path);
  if (!out.path) {
    throw new ContractError('invalid_task_file', '文件路径不合法');
  }
  out.bytes = (typeof out.bytes === 'number' && isFinite(out.bytes)) ? out.bytes : 0;
  out.truncated = out.truncated === true;
  out.redacted = out.redacted === true;
  return out;
}

/**
 * 解析任务文件列表（GET /api/tasks/<id>/files）。
 * 外层 {ok:true, task_id, files:[{path,bytes,truncated,redacted}]}。
 */
export function parseTaskFileList(data) {
  requireObject(data, 'task_file_list');
  if (data.ok !== true) {
    throw new ContractError('invalid_task_file_list', '任务文件列表标记无效');
  }
  var out = { ok: true, files: [] };
  if (typeof data.task_id === 'string') {
    out.task_id = boundOpaque(data.task_id, 'task_id');
  }
  if (Array.isArray(data.files)) {
    for (var i = 0; i < data.files.length && out.files.length < 32; i += 1) {
      out.files.push(parseTaskFileMeta(data.files[i]));
    }
  }
  return out;
}

/**
 * 解析单文件快照（GET /api/tasks/<id>/files/<path>）。
 * 只保留 path/content/bytes/truncated/redacted；content 有界。
 */
export function parseTaskFile(data) {
  requireObject(data, 'task_file');
  if (data.ok !== true) {
    throw new ContractError('invalid_task_file', '任务文件响应标记无效');
  }
  rejectSecretFileFields(data, 'task_file');
  var out = pick(data, TASK_FILE_FIELDS);
  out.ok = true;
  out.path = boundFilePath(out.path);
  if (!out.path) {
    throw new ContractError('invalid_task_file', '文件路径不合法');
  }
  out.content = (typeof out.content === 'string') ? out.content.slice(0, 16384) : '';
  out.bytes = (typeof out.bytes === 'number' && isFinite(out.bytes)) ? out.bytes : out.content.length;
  out.truncated = out.truncated === true;
  out.redacted = out.redacted === true;
  return out;
}
