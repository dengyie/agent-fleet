/* frontend/state/store.js — DOM / 客户端无关的纯状态存储（Task 14）
 *
 * 约束：
 *   - 本模块绝不依赖 api/client 或 realtime/sse；不引用 window / document /
 *     fetch / EventSource / location。它只保存“已经过 contracts 校验”的模型
 *     与外部注入的回调钩子（terminal task 刷新钩子）。
 *   - applySseEvent 接收 { type, data } 信封：type 为四种公开 SSE 类型之一
 *     （machine_update / task_update / task_log / fleet_event），data 为
 *     contracts.js parseSseEvent 返回的公开模型（已 allowlist，无内部字段）。
 *   - 时间戳栅栏：只接受比 lastEventTs 更新的 ts（严格旧事件去重），新 ts
 *     用 Math.max 更新游标；未知事件类型只推进游标、不应用业务状态，防止
 *     since 重放风暴。
 *   - 日志/事件/任务模型都是**有界**的：每条任务日志至多 500 行且每行至多
 *     4096 字符（纯文本型，绝不放行非文本）。日志映射按不同 task_id 计数
 *     有界（复用 MAX_TASKS），且任务被淘汰时同步清理其日志——task_log 对
 *     任意多个 ID 都不能让映射无限增长。事件列表至多 100 条、任务模型
 *     至多 200 个。
 *
 * 本模块是纯 ES module，可被 node 作为可选本地校验工具加载（仅测试，非运行时依赖）。
 */

/* -- 公开常量（便于 source 契约校验） -------------------------------------- */

export var TERMINAL_TASK_STATES = ['succeeded', 'failed', 'cancelled', 'expired'];
export var CONNECTION_STATUSES = ['open', 'disconnected', 'closed'];
export var SSE_EVENT_TYPES = ['machine_update', 'task_update',
                              'task_log', 'fleet_event', 'session_update',
                              'session_summary'];
export var MAX_LOG_LINES = 500;         // 每条任务日志文本行上限（客户端展示界）
export var MAX_EVENTS = 100;            // 事件列表有界
export var MAX_TASKS = 200;             // 任务/日志模型有界（FIFO 淘汰）
export var MAX_LOG_LINE_LENGTH = 4096;  // 单条日志文本最大字符数（超长截断）
export var MAX_SESSION_SUMMARIES = 50;  // 会话摘要映射有界（FIFO 淘汰）

/* -- 内部工具 -------------------------------------------------------------- */

function isObject(v) {
  return v !== null && typeof v === 'object' && !Array.isArray(v);
}

function isFiniteNumber(v) {
  return typeof v === 'number' && isFinite(v);
}

/* -- FleetStore ------------------------------------------------------------ */

export function FleetStore() {
  this._listeners = [];
  this._terminalHandler = null;
  this._state = {
    status: null,        // contracts.parseStatus 模型
    machineDetail: null, // contracts.parseMachine 模型
    machines: {},        // machine -> 摘要（status + machine_update 增量）
    tasks: {},           // task_id -> 任务模型（含 task_update 增量与 setTask 结果）
    taskLogs: {},        // task_id -> 文本行数组（每行 ≤4096 字符，纯文本）
    events: [],          // fleet 事件列表（≤100 条）
    sessionSummaries: {}, // session_id -> 有界会话摘要（SSE summary，FIFO 淘汰）
    connection: 'closed',
    lastEventTs: 0,      // SSE 游标；对旧 ts 事件做去重栅栏
  };
}

/* -- 状态快照 -------------------------------------------------------------- */

FleetStore.prototype.getState = function () {
  var s = this._state;
  return {
    status: s.status,
    machineDetail: s.machineDetail,
    machines: Object.assign({}, s.machines),
    tasks: Object.assign({}, s.tasks),
    taskLogs: Object.assign({}, s.taskLogs),
    events: s.events.slice(),
    sessionSummaries: Object.assign({}, s.sessionSummaries),
    connection: s.connection,
    lastEventTs: s.lastEventTs,
  };
};

FleetStore.prototype.getLastEventTs = function () {
  return this._state.lastEventTs;
};

/* -- 订阅 / 通知 ----------------------------------------------------------- */

/** 订阅状态变化；返回退订函数。listener 不接收参数（视图自行 getState）。 */
FleetStore.prototype.subscribe = function (listener) {
  if (typeof listener !== 'function') {
    throw new Error('store.subscribe 需要函数');
  }
  this._listeners.push(listener);
  var self = this;
  return function unsubscribe() {
    var idx = self._listeners.indexOf(listener);
    if (idx !== -1) {
      self._listeners.splice(idx, 1);
    }
  };
};

FleetStore.prototype._notify = function () {
  var snapshot = this._listeners.slice();
  for (var i = 0; i < snapshot.length; i += 1) {
    try {
      snapshot[i]();
    } catch (err) {
      // 监听器异常不得阻断其它监听器或状态推进
    }
  }
};

/* -- 显式 setter（来自 client 的已校验模型） -------------------------------- */

FleetStore.prototype.setStatus = function (data) {
  if (!isObject(data)) {
    return;
  }
  this._state.status = data;
  this._indexMachineSummaries(data.machines);
  this._notify();
};

FleetStore.prototype.setMachine = function (data) {
  if (!isObject(data)) {
    return;
  }
  this._state.machineDetail = data;
  if (typeof data.machine === 'string' && data.machine) {
    var summary = this._state.machines[data.machine] || {};
    this._state.machines[data.machine] = Object.assign({}, summary, {
      machine: data.machine,
      current: data.current,
    });
  }
  this._notify();
};

/** 接受 client.getTask 的 parseTask 模型 {ok, task}，或直接的任务模型。 */
FleetStore.prototype.setTask = function (data) {
  var task = (isObject(data) && data.task) ? data.task : data;
  if (!isObject(task) || typeof task.task_id !== 'string' || !task.task_id) {
    return;
  }
  this._putTask(task.task_id, task);
  this._notify();
};

FleetStore.prototype.setConnection = function (status) {
  if (CONNECTION_STATUSES.indexOf(status) === -1) {
    return;
  }
  this._state.connection = status;
  this._notify();
};

/** 显式写入一条会话摘要（SSE summary 已由 contracts 校验后调用）。 */
FleetStore.prototype.setSessionSummary = function (sessionId, summary) {
  if (typeof sessionId !== 'string' || !sessionId || !isObject(summary)) {
    return;
  }
  this._state.sessionSummaries[sessionId] = summary;
  this._evictSessionSummaries();
  this._notify();
};

FleetStore.prototype._applySessionSummary = function (data) {
  var id = (typeof data.session_id === 'string' && data.session_id) ?
    data.session_id : null;
  if (!id) {
    return;
  }
  this.setSessionSummary(id, data);
};

/* 会话摘要映射有界：超过 MAX_SESSION_SUMMARIES 时淘汰最旧条目（FIFO）。 */
FleetStore.prototype._evictSessionSummaries = function () {
  var keys = Object.keys(this._state.sessionSummaries);
  if (keys.length > MAX_SESSION_SUMMARIES) {
    var remove = keys.length - MAX_SESSION_SUMMARIES;
    for (var i = 0; i < remove; i += 1) {
      delete this._state.sessionSummaries[keys[i]];
    }
  }
};

/* -- 终态任务刷新桥（回调钩子；本模块不依赖 client） --------------------- */

/**
 * 注入终态任务刷新回调。终态 task_update 在写入状态后调用
 * handler(taskId)；由应用组合层把 handler 连到 client.getTask + setTask，
 * store 本身保持客户端无关（不依赖 client）。
 */
FleetStore.prototype.setRefreshTaskHandler = function (handler) {
  this._terminalHandler = (typeof handler === 'function') ? handler : null;
  return this;
};

/* -- 任务/机器内部有界写入 ------------------------------------------------ */

FleetStore.prototype._putTask = function (taskId, task) {
  this._state.tasks[taskId] = task;
  var keys = Object.keys(this._state.tasks);
  if (keys.length > MAX_TASKS) {
    var remove = keys.length - MAX_TASKS;
    for (var i = 0; i < remove; i += 1) {
      var evicted = keys[i];
      delete this._state.tasks[evicted];
      // 任务被淘汰时，其日志映射一并清理，避免 taskLogs 无限增长
      delete this._state.taskLogs[evicted];
    }
  }
};

FleetStore.prototype._indexMachineSummaries = function (rows) {
  if (!Array.isArray(rows)) {
    return;
  }
  for (var i = 0; i < rows.length; i += 1) {
    var row = rows[i];
    if (isObject(row) && typeof row.machine === 'string' && row.machine) {
      this._state.machines[row.machine] = Object.assign({}, row);
    }
  }
};

FleetStore.prototype._applyMachineUpdate = function (data) {
  var name = data.machine;
  if (typeof name !== 'string' || !name) {
    return;
  }
  var prev = this._state.machines[name] || {};
  var summary = Object.assign({}, prev, {
    machine: name,
    timestamp: data.ts,
    agents: isObject(data.agents) ? data.agents : prev.agents,
    system: isObject(data.system) ? data.system : prev.system,
    changes: Array.isArray(data.changes) ? data.changes : prev.changes,
  });
  if (typeof data.online === 'boolean') {
    summary.online = data.online;
  }
  this._state.machines[name] = summary;

  // 同步进 status.machines 列表（保留 desc / error 等既有字段）
  if (this._state.status &&
      Array.isArray(this._state.status.machines)) {
    var rows = this._state.status.machines;
    var idx = -1;
    for (var i = 0; i < rows.length; i += 1) {
      if (isObject(rows[i]) && rows[i].machine === name) {
        idx = i;
        break;
      }
    }
    var merged = Object.assign(
      idx !== -1 ? rows[idx] : {}, summary);
    if (idx !== -1) {
      rows[idx] = merged;
    } else {
      rows.push(merged);
    }
  }

  // 单机详情若正对应当前机器，同步 current
  if (this._state.machineDetail &&
      this._state.machineDetail.machine === name &&
      isObject(this._state.machineDetail.current)) {
    this._state.machineDetail.current.reachable =
      typeof data.online === 'boolean' ? data.online
        : this._state.machineDetail.current.reachable;
    this._state.machineDetail.current.timestamp = data.ts;
  }
};

FleetStore.prototype._applyTaskUpdate = function (data) {
  var id = data.task_id;
  if (typeof id !== 'string' || !id) {
    return;
  }
  var prev = this._state.tasks[id] || {};
  this._putTask(id, Object.assign({}, prev, {
    task_id: id,
    machine: data.machine,
    state: data.state,
    event: data.event,
    ts: data.ts,
  }));
  // 终态：在状态落定后调用注入的刷新钩子（应用组合层负责 getTask 回填结果）
  if (TERMINAL_TASK_STATES.indexOf(data.state) !== -1) {
    this._emitTerminalTask(id);
  }
};

FleetStore.prototype._applyTaskLog = function (data) {
  var id = data.task_id;
  if (typeof id !== 'string' || !id || typeof data.line !== 'string') {
    return; // 只接受文本日志行
  }
  var line = data.line;
  if (line.length > MAX_LOG_LINE_LENGTH) {
    line = line.slice(0, MAX_LOG_LINE_LENGTH); // 超长单行截断，防行内无界增长
  }
  var lines = this._state.taskLogs[id];
  if (!lines) {
    lines = [];
    this._state.taskLogs[id] = lines;
  }
  lines.push(line);
  if (lines.length > MAX_LOG_LINES) {
    lines.splice(0, lines.length - MAX_LOG_LINES); // 保留最近 500 行
  }
  this._evictTaskLogOverflow(); // 按不同 task_id 计数有界，淘汰最旧
};

/** 日志映射按不同 task_id 数量有界（复用 MAX_TASKS）；淘汰最旧条目。 */
FleetStore.prototype._evictTaskLogOverflow = function () {
  var keys = Object.keys(this._state.taskLogs);
  if (keys.length > MAX_TASKS) {
    var remove = keys.length - MAX_TASKS;
    for (var i = 0; i < remove; i += 1) {
      delete this._state.taskLogs[keys[i]];
    }
  }
};

FleetStore.prototype._applyFleetEvent = function (data) {
  this._state.events.push({
    event: typeof data.event === 'string' ? data.event : '',
    machine: typeof data.machine === 'string' ? data.machine : null,
    changes: Array.isArray(data.changes) ? data.changes : [],
    ts: data.ts,
  });
  if (this._state.events.length > MAX_EVENTS) {
    this._state.events.splice(0, this._state.events.length - MAX_EVENTS);
  }
};

FleetStore.prototype._emitTerminalTask = function (taskId) {
  if (this._terminalHandler) {
    try {
      this._terminalHandler(taskId);
    } catch (err) {
      // 刷新回调失败不影响状态
    }
  }
};

/* -- SSE 事件应用 ----------------------------------------------------------- */

/**
 * 应用一条已由 contracts 校验的公开 SSE 事件信封。
 * @param {object} envelope { type, data } — type ∈ 四种公开类型；data 为公开模型。
 * 返回布尔：true = 已应用（并可能通知），false = 陈旧/未知被忽略。
 */
FleetStore.prototype.applySseEvent = function (envelope) {
  if (!isObject(envelope) || !isObject(envelope.data)) {
    return false;
  }
  var type = envelope.type;
  var data = envelope.data;
  var ts = isFiniteNumber(data.ts) ? data.ts : 0;

  // 时间戳栅栏：严格旧事件拒绝（去重）；新 ts 用 Math.max 更新游标。
  if (ts > 0 && ts < this._state.lastEventTs) {
    return false; // 陈旧事件：拒绝
  }
  this._state.lastEventTs = Math.max(this._state.lastEventTs, ts);

  if (type === 'machine_update') {
    this._applyMachineUpdate(data);
  } else if (type === 'task_update') {
    this._applyTaskUpdate(data);
  } else if (type === 'task_log') {
    this._applyTaskLog(data);
  } else if (type === 'fleet_event') {
    this._applyFleetEvent(data);
  } else if (type === 'session_update' || type === 'session_summary') {
    this._applySessionSummary(data);
  } else {
    // 未知事件类型：只推进游标，不应用业务状态，防止 since 重放风暴。
    return false;
  }
  this._notify();
  return true;
};
