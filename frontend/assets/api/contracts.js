/* frontend/assets/api/contracts.js — 公开 DTO 契约（Task 13）
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
var EVENT_FIELDS = ['event', 'machine', 'ts', 'event_seq', 'changes'];

// /api/status 外层（features 是默认关闭的 Phase 4/5 发行旗标，缺省视为关）
var STATUS_FIELDS = ['updated_at', 'machines', 'features'];
var STATUS_FEATURE_FIELDS = ['append_user_turn', 'apply_local_profile'];

// hub/http/observe_routes._sse_payload() 的四种 public event 形状
var SSE_MACHINE_UPDATE_FIELDS = ['machine', 'changes', 'online', 'ts', 'event_seq',
                                 'agents', 'system'];
var SSE_TASK_LOG_FIELDS = ['task_id', 'line', 'ts', 'event_seq'];
var SSE_TASK_UPDATE_FIELDS = ['task_id', 'machine', 'state', 'event', 'ts', 'event_seq'];
var SSE_FLEET_EVENT_FIELDS = ['event', 'machine', 'changes', 'ts', 'event_seq'];

// 统一错误契约 {ok,error,detail,request_id}
var ERROR_FIELDS = ['ok', 'error', 'detail', 'request_id'];

/* -- session public DTO allowlists（Task 11，对齐 session_schema） ---------- */

// session metadata（public_session_dto 的公开字段；attempt_id 仅受管会话出现）
var SESSION_FIELDS = ['machine_id', 'session_id', 'managed', 'capture_quality',
                      'control_capability', 'attempt_id', 'process_group_id',
                      'agent_family', 'status', 'started_at', 'updated_at',
                      'finished_at',
                      'capabilities', 'current_state', 'state_source',
                      'state_confidence'];

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
                            'detail', 'quality', 'old_state', 'new_state',
                            'state', 'source', 'confidence', 'request_id',
                            'question_type', 'prompt', 'options',
                            'allow_custom', 'selected'];

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
                                  'capture_quality', 'status', 'ts', 'event_seq'];

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
      var out = pick(row, EVENT_FIELDS);
      normalizeEventSequence(out);
      return out;
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
 * machine/task/fleet 与 session summary 形状都只保留
 * 公开字段；未知事件名按通用 public event 形状处理。
 */
export function parseSseEvent(name, data) {
  requireObject(data, 'sse_event');
  var out;
  if (name === 'machine_update') {
    out = pick(data, SSE_MACHINE_UPDATE_FIELDS);
  } else if (name === 'task_log') {
    out = pick(data, SSE_TASK_LOG_FIELDS);
  } else if (name === 'task_update') {
    out = pick(data, SSE_TASK_UPDATE_FIELDS);
  } else if (name === 'session_update' || name === 'session_summary') {
    return parseSessionSummary(data, name);
  } else {
    out = pick(data, SSE_FLEET_EVENT_FIELDS);
  }
  normalizeEventSequence(out);
  return out;
}

function normalizeEventSequence(out) {
  if (typeof out.event_seq !== 'number' || !isFinite(out.event_seq) ||
      out.event_seq < 1 || Math.floor(out.event_seq) !== out.event_seq) {
    delete out.event_seq;
  }
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
  if (typeof out.event_seq !== 'number' || !isFinite(out.event_seq) ||
      out.event_seq < 1 || Math.floor(out.event_seq) !== out.event_seq) {
    delete out.event_seq;
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

function handoffObject(value, fields) {
  requireObject(value, 'handoff');
  if (Object.keys(value).some(key => fields.indexOf(key) === -1)) {
    throw new ContractError('invalid_handoff', '接力响应含未知字段');
  }
  return value;
}

function handoffText(value, limit) {
  if (typeof value !== 'string' || value.length > limit * 2) {
    throw new ContractError('invalid_handoff', '接力文本格式不正确');
  }
  return value;
}

function handoffId(value) {
  if (typeof value !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$/.test(value)) {
    throw new ContractError('invalid_handoff', '接力标识格式不正确');
  }
  return value;
}

function handoffList(value, limit, parse) {
  if (!Array.isArray(value) || value.length > limit) {
    throw new ContractError('invalid_handoff', '接力列表超过上限');
  }
  return value.map(parse);
}

export function parseHandoffPreview(data) {
  handoffObject(data, ['snapshot', 'prompt', 'source_project', 'can_create', 'targets']);
  const row = handoffObject(data.snapshot, ['objective', 'current_state', 'completed',
    'changed_files', 'tests', 'last_conclusion', 'open_items', 'source_event_refs', 'capture_quality']);
  if (typeof data.can_create !== 'boolean' || capQualities.indexOf(row.capture_quality) === -1 ||
      agentStates.indexOf(row.current_state) === -1 ||
      JSON.stringify(row).length > 24000) {
    throw new ContractError('invalid_handoff', '接力预览格式不正确');
  }
  const tests = handoffObject(row.tests, ['framework', 'passed', 'failed', 'skipped', 'errors', 'duration_s']);
  for (const key of Object.keys(tests)) {
    const valid = key === 'framework' ? ['pytest', 'unittest', 'unknown'].includes(tests[key]) :
      typeof tests[key] === 'number' && Number.isFinite(tests[key]) && tests[key] >= 0 &&
      (key === 'duration_s' || Number.isSafeInteger(tests[key]));
    if (!valid) throw new ContractError('invalid_handoff', '接力测试结果格式不正确');
  }
  return {
    snapshot: {
      objective: handoffText(row.objective, 3000), current_state: handoffText(row.current_state, 64),
      completed: handoffList(row.completed, 52, value => handoffText(value, 2000)),
      changed_files: handoffList(row.changed_files, 100, value => handoffText(value, 260)),
      tests: Object.assign({}, tests), last_conclusion: handoffText(row.last_conclusion, 3000),
      open_items: handoffList(row.open_items, 24, value => handoffText(value, 240)),
      source_event_refs: handoffList(row.source_event_refs, 100, value => {
        handoffObject(value, ['event_id', 'sequence']);
        if (typeof value.sequence !== 'string' || !/^[0-9]{1,19}$/.test(value.sequence)) {
          throw new ContractError('invalid_handoff', '接力事件序号格式不正确');
        }
        return { event_id: handoffId(value.event_id), sequence: value.sequence };
      }),
      capture_quality: row.capture_quality,
    },
    prompt: handoffText(data.prompt, 2000), can_create: data.can_create,
    source_project: data.source_project === null ? null : handoffId(data.source_project),
    targets: handoffList(data.targets, 200, value => {
      handoffObject(value, ['machine', 'project', 'agents']);
      return { machine: handoffId(value.machine), project: handoffId(value.project),
        agents: handoffList(value.agents, 32, handoffId) };
    }),
  };
}

export function parseHandoffReceipt(data) {
  handoffObject(data, ['task_id', 'attempt_id', 'handoff_id']);
  return { task_id: handoffId(data.task_id), attempt_id: handoffId(data.attempt_id),
    handoff_id: handoffId(data.handoff_id) };
}

/** Search exposes only bounded redacted snippets and stable event references. */
export function parseSessionSearch(data) {
  requireObject(data, 'session_search_response');
  rejectInternalKey(data, 'session_search_response');
  if (!Array.isArray(data.items) || data.items.length > 50 ||
      (data.next_cursor !== null && (typeof data.next_cursor !== 'string' || data.next_cursor.length > 4096))) {
    throw new ContractError('invalid_search_response', '搜索响应不合法');
  }
  var kinds = ['user_message', 'assistant_message', 'tool_call', 'tool_result'];
  function identifier(value, nullable) {
    if (nullable && value === null) return null;
    if (typeof value !== 'string' || !value || value.length > 256 || /[\\/\u0000-\u001f]/.test(value)) {
      throw new ContractError('invalid_search_response', '搜索标识不合法');
    }
    return value;
  }
  return {
    items: data.items.map(function (row) {
      requireObject(row, 'search_result');
      rejectInternalKey(row, 'search_result');
      var sequence = row.sequence;
      var validSequence = typeof sequence === 'number' ? Number.isSafeInteger(sequence) && sequence > 0
        : typeof sequence === 'string' && /^[1-9][0-9]{0,18}$/.test(sequence) && BigInt(sequence) <= 9223372036854775807n;
      if (!validSequence || !kinds.includes(row.kind) || typeof row.snippet !== 'string' ||
          Array.from(row.snippet).length > 400 || typeof row.emitted_at !== 'string' ||
          row.emitted_at.length > 32 || !Number.isFinite(Date.parse(row.emitted_at)) ||
          !Array.isArray(row.highlight) || row.highlight.length > 200) {
        throw new ContractError('invalid_search_response', '搜索内容不合法');
      }
      var length = Array.from(row.snippet).length;
      var end = 0;
      var spans = row.highlight.map(function (span) {
        if (!Array.isArray(span) || span.length !== 2 || !Number.isInteger(span[0]) ||
            !Number.isInteger(span[1]) || span[0] < end || span[1] <= span[0] || span[1] > length) {
          throw new ContractError('invalid_search_response', '搜索高亮不合法');
        }
        end = span[1];
        return [span[0], span[1]];
      });
      return {session_id: identifier(row.session_id), sequence: sequence, kind: row.kind,
        snippet: row.snippet, highlight: spans, emitted_at: row.emitted_at,
        machine_id: identifier(row.machine_id, true), project_id: identifier(row.project_id, true),
        agent_family: identifier(row.agent_family, true)};
    }),
    next_cursor: data.next_cursor,
  };
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

/** Parse the operator-facing status of one supervisor command. */
export function parseControlReceipt(data) {
  requireObject(data, 'control_receipt_response');
  rejectInternalKey(data, 'control_receipt_response');
  rejectControlKeys(data, 'control_receipt_response');
  if (!isObject(data.receipt)) {
    throw new ContractError('invalid_control_receipt',
                            '控制回执响应缺少 receipt 对象');
  }
  return parseControlReceiptRow(data.receipt);
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
var agentStates = ['running', 'waiting_for_user', 'waiting_for_approval',
                   'idle', 'completed', 'failed', 'stopped', 'unknown'];
var agentStateSources = ['hook', 'structured_stream', 'native_transcript',
                         'pty', 'supervisor'];
var agentStateConfidences = ['high', 'medium', 'low'];
var interactionQuestionTypes = ['single', 'multi', 'text', 'confirm'];
var MAX_INTERACTION_OPTIONS = 32;
var MAX_INTERACTION_OPTION_LABEL = 256;
var MAX_INTERACTION_PROMPT = 2000;
var MAX_INTERACTION_TEXT = 2000;
var MAX_INTERACTION_SELECTED = 32;

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

function strictEnum(value, allowed, code, label) {
  if (typeof value !== 'string' || allowed.indexOf(value) === -1) {
    throw new ContractError(code, label + ' 不在白名单');
  }
  return value;
}

function rejectUnknownPayloadKeys(payload, allowed) {
  var keys = Object.keys(payload);
  for (var i = 0; i < keys.length; i += 1) {
    if (allowed.indexOf(keys[i]) === -1) {
      throw new ContractError('unknown_payload_field',
                              'payload 含未知字段');
    }
  }
}

function parseAgentStatePayload(payload) {
  var allowed = ['state', 'source', 'confidence', 'reason'];
  rejectUnknownPayloadKeys(payload, allowed);
  var out = {
    state: strictEnum(payload.state, agentStates, 'invalid_agent_state',
                      'state'),
    source: strictEnum(payload.source, agentStateSources,
                       'invalid_agent_state_source', 'source'),
    confidence: strictEnum(payload.confidence, agentStateConfidences,
                           'invalid_agent_state_confidence', 'confidence'),
    reason: '',
  };
  if (payload.reason !== undefined) {
    if (payload.reason === null || typeof payload.reason === 'object') {
      throw new ContractError('invalid_payload_type',
                              'reason 必须是原始值');
    }
    out.reason = String(payload.reason).slice(0, 128);
  }
  if (out.source === 'pty' && (['unknown', 'idle'].indexOf(out.state) === -1 || out.confidence !== 'low')) {
    throw new ContractError('invalid_agent_state', 'PTY 状态必须是低置信度');
  }
  return out;
}

function parseInteractionOptions(value) {
  if (value === undefined) {
    return [];
  }
  if (!Array.isArray(value)) {
    throw new ContractError('invalid_payload_type', 'options 必须是数组');
  }
  if (value.length > MAX_INTERACTION_OPTIONS) {
    throw new ContractError('options_too_many', 'options 超出数量上限');
  }
  var out = [];
  var seen = Object.create(null);
  for (var i = 0; i < value.length; i += 1) {
    var option = value[i];
    requireObject(option, 'interaction.option');
    var optionKeys = Object.keys(option);
    if (optionKeys.length !== 2 || option.id === undefined ||
        option.label === undefined) {
      throw new ContractError('unknown_payload_field',
                              'option 字段不在白名单');
    }
    var id = boundOpaque(option.id, 'option_id');
    if (!id) {
      throw new ContractError('invalid_option', 'option 缺少 id');
    }
    if (Object.prototype.hasOwnProperty.call(seen, id)) {
      throw new ContractError('duplicate_option_id', 'option id 重复');
    }
    seen[id] = true;
    if (option.label === null || typeof option.label === 'object') {
      throw new ContractError('invalid_payload_type',
                              'option label 必须是原始值');
    }
    var label = String(option.label).slice(0, MAX_INTERACTION_OPTION_LABEL);
    if (!label) {
      throw new ContractError('invalid_option', 'option 缺少 label');
    }
    out.push({id: id, label: label});
  }
  return out;
}

function parseInteractionRequestPayload(payload) {
  var allowed = ['request_id', 'question_type', 'prompt', 'options',
                 'allow_custom', 'tool_name'];
  rejectUnknownPayloadKeys(payload, allowed);
  var requestId = boundOpaque(payload.request_id, 'request_id');
  if (!requestId) {
    throw new ContractError('invalid_request_id', 'request_id 无效');
  }
  if (payload.prompt === undefined || payload.prompt === null ||
      typeof payload.prompt === 'object') {
    throw new ContractError('invalid_payload_type',
                            'prompt 必须是原始值');
  }
  var prompt = String(payload.prompt).slice(0, MAX_INTERACTION_PROMPT);
  if (!prompt) {
    throw new ContractError('invalid_prompt', 'prompt 不能为空');
  }
  var out = {
    request_id: requestId,
    question_type: strictEnum(payload.question_type,
                              interactionQuestionTypes,
                              'invalid_question_type', 'question_type'),
    prompt: prompt,
    options: parseInteractionOptions(payload.options),
    allow_custom: payload.allow_custom === true,
  };
  if (Array.from(out.prompt).length + out.options.reduce((total, option) => total + Array.from(option.label).length, 0) > MAX_INTERACTION_PROMPT) {
    throw new ContractError('invalid_prompt', '问题文本超限');
  }
  if (payload.allow_custom !== undefined &&
      typeof payload.allow_custom !== 'boolean') {
    throw new ContractError('invalid_payload_type',
                            'allow_custom 必须是布尔值');
  }
  if (payload.tool_name !== undefined) {
    if (payload.tool_name === null || typeof payload.tool_name === 'object') {
      throw new ContractError('invalid_payload_type',
                              'tool_name 必须是原始值');
    }
    out.tool_name = String(payload.tool_name).slice(0, 128);
  }
  return out;
}

function parseInteractionSelected(value) {
  if (typeof value === 'string') {
    var one = boundOpaque(value, 'selected');
    if (!one) {
      throw new ContractError('invalid_selected', 'selected 无效');
    }
    return one;
  }
  if (!Array.isArray(value)) {
    throw new ContractError('invalid_payload_type',
                            'selected 必须是 id 或 id 数组');
  }
  if (value.length > MAX_INTERACTION_SELECTED) {
    throw new ContractError('selected_too_many', 'selected 超出数量上限');
  }
  var out = [];
  var seen = Object.create(null);
  for (var i = 0; i < value.length; i += 1) {
    var id = boundOpaque(value[i], 'selected');
    if (!id) {
      throw new ContractError('invalid_selected', 'selected 含无效 id');
    }
    if (Object.prototype.hasOwnProperty.call(seen, id)) {
      throw new ContractError('duplicate_selected_id', 'selected id 重复');
    }
    seen[id] = true;
    out.push(id);
  }
  return out;
}

function parseInteractionResponsePayload(payload) {
  var allowed = ['request_id', 'selected', 'text'];
  rejectUnknownPayloadKeys(payload, allowed);
  var requestId = boundOpaque(payload.request_id, 'request_id');
  if (!requestId) {
    throw new ContractError('invalid_request_id', 'request_id 无效');
  }
  var out = {request_id: requestId};
  if (payload.selected !== undefined) {
    out.selected = parseInteractionSelected(payload.selected);
  }
  if (payload.text !== undefined) {
    if (payload.text === null || typeof payload.text === 'object') {
      throw new ContractError('invalid_payload_type',
                              'text 必须是原始值');
    }
    out.text = String(payload.text).slice(0, MAX_INTERACTION_TEXT);
  }
  return out;
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
    out.current_state = 'unknown';
    out.state_source = 'unknown';
    out.state_confidence = 'low';
  } else {
    out.current_state = (typeof out.current_state === 'string' &&
      agentStates.indexOf(out.current_state) !== -1)
      ? out.current_state : 'unknown';
    out.state_source = (typeof out.state_source === 'string' &&
      agentStateSources.indexOf(out.state_source) !== -1)
      ? out.state_source : 'unknown';
    out.state_confidence = (typeof out.state_confidence === 'string' &&
      agentStateConfidences.indexOf(out.state_confidence) !== -1)
      ? out.state_confidence : 'low';
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
  var lowered = v.toLowerCase();
  if (v.length > 256 || /[\/\\]/.test(v) ||
      lowered.indexOf('token') !== -1 || lowered.indexOf('secret') !== -1 ||
      lowered.indexOf('private') !== -1 || lowered.indexOf('password') !== -1 ||
      lowered.indexOf('key') !== -1) {
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
  var sequenceNumber = typeof out.sequence === 'number' && Number.isSafeInteger(out.sequence) && out.sequence > 0;
  var sequenceString = typeof out.sequence === 'string' && /^[1-9][0-9]{0,18}$/.test(out.sequence) && BigInt(out.sequence) <= 9223372036854775807n;
  if (!sequenceNumber && !sequenceString) {
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
  if (kind === 'agent_state_changed') {
    return parseAgentStatePayload(payload);
  }
  if (kind === 'interaction_request') {
    return parseInteractionRequestPayload(payload);
  }
  if (kind === 'interaction_response') {
    return parseInteractionResponsePayload(payload);
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
    out[key] = String(value);
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

/* Usage DTOs preserve decimal amounts and integers beyond JS's exact range. */
export function parseUsageSummary(data) {
  requireObject(data, 'usage_summary');
  const fail = () => { throw new ContractError('invalid_usage_summary', '用量响应不合法'); };
  const dimensions = ['day', 'project', 'machine', 'agent', 'model'];
  const counters = ['requests', 'input_tokens', 'output_tokens', 'total_tokens', 'unknown_count'];
  const topFields = ['ok', 'from', 'to', 'group_by', 'currency', 'items'];
  if (Object.keys(data).some(key => !topFields.includes(key)) || data.ok !== true || data.currency !== 'USD') fail();
  const validTime = value => typeof value === 'string' && value.length <= 40 && value.endsWith('Z') && Number.isFinite(Date.parse(value));
  if (!validTime(data.from) || !validTime(data.to) || Date.parse(data.to) <= Date.parse(data.from) || Date.parse(data.to) - Date.parse(data.from) > 90 * 86400000) fail();
  if (!Array.isArray(data.group_by) || !data.group_by.length || data.group_by.length > 5 || new Set(data.group_by).size !== data.group_by.length || data.group_by.some(key => !dimensions.includes(key))) fail();
  if (!Array.isArray(data.items) || data.items.length > 1000) fail();
  const validCounter = value => (typeof value === 'number' && Number.isSafeInteger(value) && value >= 0) || (typeof value === 'string' && /^(0|[1-9]\d{0,39})$/.test(value));
  const fields = [...data.group_by, ...counters, 'estimated_cost', 'cost_source'];
  const items = data.items.map(item => {
    requireObject(item, 'usage_item');
    if (Object.keys(item).some(key => !fields.includes(key)) || fields.some(key => !Object.prototype.hasOwnProperty.call(item, key))) fail();
    const row = {};
    for (const key of data.group_by) {
      const value = item[key];
      if (key === 'day') {
        if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value) || !Number.isFinite(Date.parse(value + 'T00:00:00Z'))) fail();
      } else if (value !== null && (typeof value !== 'string' || !value || value.length > (key === 'model' ? 160 : 128) || /[\x00-\x1f\x7f]/.test(value))) fail();
      row[key] = value;
    }
    for (const key of counters) {
      if (item[key] === null && key.endsWith('_tokens')) { row[key] = null; continue; }
      if (!validCounter(item[key])) fail();
      row[key] = item[key];
    }
    if (BigInt(row.unknown_count) > BigInt(row.requests)) fail();
    const tokenValues = ['input_tokens', 'output_tokens', 'total_tokens'].map(key => row[key]);
    if (tokenValues.some(value => value === null) && tokenValues.some(value => value !== null)) fail();
    if (row.total_tokens !== null && BigInt(row.total_tokens) < BigInt(row.input_tokens) + BigInt(row.output_tokens)) fail();
    if (!['provider', 'configured', 'unknown'].includes(item.cost_source)) fail();
    if (item.cost_source === 'unknown') {
      if (item.estimated_cost !== null) fail();
    } else if (typeof item.estimated_cost !== 'string' || item.estimated_cost.length > 64 || !/^(0|[1-9]\d{0,39})(\.\d{1,18})?$/.test(item.estimated_cost)) fail();
    if ((row.total_tokens === null || item.cost_source === 'unknown') && BigInt(row.unknown_count) === 0n) fail();
    row.estimated_cost = item.estimated_cost; row.cost_source = item.cost_source;
    return row;
  });
  return {ok: true, from: data.from, to: data.to, group_by: [...data.group_by], currency: 'USD', items};
}
