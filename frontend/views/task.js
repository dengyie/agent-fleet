/* frontend/views/task.js — 任务详情视图与取消/重试（Task 16）
 *
 * 只接收已通过 api/contracts.js 校验的模型，使用 store 订阅与注入的
 * client 方法（getTask / cancelTask / retryTask / pauseTask / continueTask /
 * confirmTask / rejectTask / getTaskDiff / listTaskFiles / getTaskFile）；绝不直接发 HTTP、原生
 * 流式事件、整页刷新或访问 Flask 模板对象。所有动态文本与日志
 * 一律经 textContent / createTextNode 写入，不做 HTML 字符串拼接，属性经
 * setAttribute。cancel / retry / pause / continue / confirm / reject 通过容器级事件委托绑定按钮，并用 busy 栅栏
 * 防止重复/在途操作；卸载后迟到回调一律 no-op。
 *
 * 渲染来源：
 *   - 首屏：client.getTask(taskId) 种子进 store（store.setTask）。
 *   - 实时增量：store.subscribe 监听 task_update / task_log（queue 已由
 *     SseClient 终态 getTask 回填 result；视图只从 store 读）。
 *   - 日志：优先展示 store.taskLogs[taskId]（SSE task_log 已界 ≤500 行、
 *     纯文本），无实时日志时回退到 result.log_summary（also bounded）。
 *   - 快照：终态后 listTaskFiles；点击路径再 getTaskFile。非终态/重试清空。
 *
 * 显式 UI 状态：loading / error / empty（结果未完成、日志为空）/ ready。
 * Phase 3：若公开 DTO 带可选 session_id，渲染有界深链；没有就不显示。
 */

import { pagePath } from '../routes.js';

var MSG_LOADING = '加载中…';
var MSG_NO_RESULT = '任务尚未完成';
var MSG_NO_LOGS = '暂无日志';
var MSG_NO_FILES = '无快照文件';
var MAX_DIFF_RENDER = 5120;        // diff 摘要展示有界（与后端 MAX_DIFF_STAT 一致）
var MAX_LOG_RENDER = 500;          // 日志行展示上限（复用 store.MAX_LOG_LINES）
var MAX_LOG_SUMMARY_RENDER = 4096; // 终态 log_summary 文本块展示上限（有界）
var MAX_FILE_RENDER = 16384;       // 单文件正文展示上限（与后端 MAX_FILE_BYTES 一致）
var MAX_PATCH_RENDER = 102400;     // 脱敏 patch 展示上限（与后端 MAX_DIFF_PATCH 一致）
var MAX_FAILED_NAMES = 20;

/* -- state 文案与 CSS class ------------------------------------------------ */

// 可取消状态（与 hub/domain/task.can_cancel 一致）
var CANCELLABLE_STATES = ['queued', 'leased', 'running', 'paused'];
var PAUSABLE_STATES = ['queued', 'leased', 'running'];
// 终态（可重试；与 store.TERMINAL_TASK_STATES 一致）
var TERMINAL_STATES = ['succeeded', 'failed', 'cancelled', 'expired'];
// 终态 → CSS class 显式白名单映射：绝不把状态名直接拼进 class 串。
// 任何不在此映射中的状态只落到默认 'state-node current'，不会产生
// 任意/注入 class 名。
var TERMINAL_CLASS = {
  failed: 'state-node terminal-failed current',
  cancelled: 'state-node terminal-cancelled current',
  expired: 'state-node terminal-expired current',
};

function cancellable(state) {
  return CANCELLABLE_STATES.indexOf(state) !== -1;
}
function pausable(state) {
  return PAUSABLE_STATES.indexOf(state) !== -1;
}
function terminal(state) {
  return TERMINAL_STATES.indexOf(state) !== -1;
}
function pendingGate(task) {
  return !!(task && task.gate && task.gate.state === 'pending');
}
function isTerminalNode(state) {
  return Object.prototype.hasOwnProperty.call(TERMINAL_CLASS, state);
}
function terminalClass(state) {
  return TERMINAL_CLASS[state] || 'state-node current';
}

/* -- DOM 安全写入工具 -------------------------------------------------------- */

function h(tag, className, text) {
  var el = document.createElement(tag);
  if (className) {
    el.setAttribute('class', className);
  }
  if (text !== undefined) {
    el.textContent = text;
  }
  return el;
}

function fmtValue(v) {
  if (v === null || v === undefined || v === '') {
    return '?';
  }
  return String(v);
}

function removeAllChildren(el) {
  while (el.firstChild) {
    el.removeChild(el.firstChild);
  }
}

/* -- 状态流（bounded，仅渲染公开状态名） -------------------------------------- */

function renderStateFlow(host, state) {
  var flow = h('div', 'stateflow');
  var ordered = ['queued', 'leased', 'running', 'succeeded'];
  ordered.forEach(function (s, idx) {
    flow.appendChild(h('span',
      'state-node' + (state === s ? ' current' : ''), s));
    if (idx < ordered.length - 1) {
      flow.appendChild(h('span', 'state-arrow', '→'));
    }
  });
  if (state === 'paused') {
    flow.appendChild(h('span', 'state-arrow', '→'));
    flow.appendChild(h('span', 'state-node current', 'paused'));
  }
  if (isTerminalNode(state)) {
    flow.appendChild(h('span', 'state-arrow', '→'));
    flow.appendChild(h('span', terminalClass(state), state));
  }
  host.appendChild(flow);
}

/* -- 结果字段（有界） ------------------------------------------------------------ */

function kv(label, value) {
  var div = h('div', 'metric');
  div.appendChild(h('span', null, label));
  div.appendChild(h('b', null, String(value)));
  return div;
}

function renderResult(host, result) {
  if (!result || typeof result !== 'object') {
    host.appendChild(h('div', 'meta', MSG_NO_RESULT));
    return;
  }
  var exitCode = (typeof result.exit_code === 'number') ? String(result.exit_code)
    : 'n/a';
  host.appendChild(kv('退出码', exitCode));
  var duration = 'n/a';
  if (typeof result.duration_s === 'number' && isFinite(result.duration_s)) {
    duration = String(Math.round(result.duration_s * 10) / 10) + 's';
  }
  host.appendChild(kv('耗时', duration));
  if (typeof result.finished_at === 'string' && result.finished_at) {
    host.appendChild(kv('完成时间', result.finished_at));
  }

  host.appendChild(h('h3', null, 'diff 摘要'));
  var diff = (typeof result.diff_stat === 'string' && result.diff_stat) ?
    result.diff_stat.slice(0, MAX_DIFF_RENDER) : '（无）';
  var pre = document.createElement('pre');
  pre.setAttribute('class', 'diffstat');
  pre.textContent = diff;
  host.appendChild(pre);

  host.appendChild(h('h3', null, '测试摘要'));
  renderTestSummary(host, result.test_summary);
}

function renderTestSummary(host, summary) {
  if (!summary || typeof summary !== 'object') {
    host.appendChild(h('div', 'meta', '无测试结果'));
    return;
  }
  var framework = (typeof summary.framework === 'string' && summary.framework)
    ? summary.framework : 'unknown';
  host.appendChild(kv('框架', framework));
  host.appendChild(kv('通过', fmtCount(summary.passed)));
  host.appendChild(kv('失败', fmtCount(summary.failed)));
  host.appendChild(kv('跳过', fmtCount(summary.skipped)));
  host.appendChild(kv('错误', fmtCount(summary.errors)));
  if (typeof summary.duration_s === 'number' && isFinite(summary.duration_s)) {
    host.appendChild(kv('测试耗时',
      String(Math.round(summary.duration_s * 10) / 10) + 's'));
  }
  var names = Array.isArray(summary.failed_names) ? summary.failed_names : [];
  if (!names.length) {
    return;
  }
  host.appendChild(h('div', 'meta', '失败用例'));
  var list = h('div', 'failed-names');
  var limit = Math.min(names.length, MAX_FAILED_NAMES);
  for (var i = 0; i < limit; i += 1) {
    var row = document.createElement('div');
    row.setAttribute('class', 'failed-name');
    row.textContent = String(names[i]).slice(0, 200);
    list.appendChild(row);
  }
  host.appendChild(list);
}

function fmtCount(v) {
  return (typeof v === 'number' && isFinite(v)) ? String(v) : '0';
}

/* -- 日志区（纯文本，有界） -------------------------------------------------------- */

function renderLogs(host, liveLines, logSummary) {
  var logs = h('div', 'log-area');
  var lines = liveLines;
  if (Array.isArray(lines) && lines.length > 0) {
    // 实时 SSE task_log 优先（纯文本行），只展示最近 MAX_LOG_RENDER 行
    var shown = lines.slice(-MAX_LOG_RENDER);
    for (var i = 0; i < shown.length; i += 1) {
      var line = document.createElement('div');
      line.textContent = shown[i];   // 纯文本：绝不把日志当 HTML 渲染
      logs.appendChild(line);
    }
    host.appendChild(logs);
    return;
  }
  // 回退到终态 result.log_summary（textContent 分段）
  var summary = (typeof logSummary === 'string') ? logSummary.trim() : '';
  if (summary) {
    var block = document.createElement('pre');
    block.textContent = summary.slice(0, MAX_LOG_SUMMARY_RENDER);
    logs.appendChild(block);
    host.appendChild(logs);
    return;
  }
  host.appendChild(h('div', 'meta', MSG_NO_LOGS));
}

/* -- 操作按钮 + 容器级事件委托 ----------------------------------------------------- */

/**
 * 渲染 cancel / retry 按钮与错误盒，并在 actionsFl（渲染时重建的容器）上
 * 绑定单个委托监听。用 taskId + 状态判定按钮显隐：
 *   - cancel: 可取消状态（queued/leased/running/paused）
 *   - pause: queued/leased/running
 *   - continue: paused
 *   - confirm/reject: gate.state === pending
 *   - retry: 终态（succeeded/failed/cancelled/expired）
 * 委托回调经 ctx.performAction()（isActive + busy 栅栏）：重复点击、在途
 * 操作与卸载后迟到回调一律静默返回。
 */
function addActionButton(row, action, label, busy) {
  var btn = h('button', null, label);
  btn.setAttribute('data-action', action);
  if (busy) {
    btn.setAttribute('disabled', 'disabled');
  }
  row.appendChild(btn);
}

function renderActions(actionsHost, taskId, task, ctx) {
  var errBox = h('div', 'err');
  errBox.setAttribute('id', 'task-action-error');
  // 操作失败的错误一律 textContent：绝不把后端原始文本当作 HTML 渲染。
  errBox.textContent = (typeof ctx.actionError === 'string' && ctx.actionError)
    ? ctx.actionError : '';
  actionsHost.appendChild(errBox);

  if (pendingGate(task)) {
    actionsHost.appendChild(h('div', 'meta',
      '待人工确认：确认前不会派发给 runner'));
  }

  var btnRow = h('div', 'actions');
  var showCancel = cancellable(task.state);
  var showRetry = terminal(task.state);
  var showPause = pausable(task.state) && !pendingGate(task) && ctx.canPause;
  var showContinue = task.state === 'paused' && ctx.canContinue;
  var showConfirm = pendingGate(task) && ctx.canConfirm;
  var showReject = pendingGate(task) && ctx.canReject;
  var showGate = showConfirm || showReject;

  btnRow.setAttribute('data-task-id', taskId);
  if (showCancel || showRetry || showPause || showContinue || showGate) {
    if (showCancel) {
      addActionButton(btnRow, 'cancel', '取消', ctx.busy);
    }
    if (showPause) {
      addActionButton(btnRow, 'pause', '暂停', ctx.busy);
    }
    if (showContinue) {
      addActionButton(btnRow, 'continue', '继续', ctx.busy);
    }
    if (showConfirm) {
      addActionButton(btnRow, 'confirm', '确认', ctx.busy);
    }
    if (showReject) {
      addActionButton(btnRow, 'reject', '拒绝', ctx.busy);
    }
    if (showRetry) {
      addActionButton(btnRow, 'retry', '重试', ctx.busy);
    }
  }
  actionsHost.appendChild(btnRow);

  // 容器级委托：单个监听处理本容器内所有 action 按钮的点击。
  actionsHost.addEventListener('click', function (ev) {
    var btn = ev.target;
    if (!btn || btn.tagName !== 'BUTTON' || !btn.hasAttribute('data-action')) {
      return;
    }
    var action = btn.getAttribute('data-action');
    if (action === 'cancel' || action === 'retry' || action === 'pause' ||
        action === 'continue' || action === 'confirm' || action === 'reject') {
      ctx.performAction(action);
    }
  });
}

/* -- Task 视图 ---------------------------------------------------------------- */

function noop() {}

/**
 * 挂载任务详情视图。
 * @param {Element} root   挂载点
 * @param {string}  taskId 任务 id（URL 路径段/查询已解码）
 * @param {FleetStore} store  状态存储（tasks / taskLogs）
 * @param {object} client 注入客户端（须含 getTask / cancelTask / retryTask）
 * @returns {function} teardown（卸载函数）
 */
export function mountTask(root, taskId, store, client) {
  if (!root || !store || typeof taskId !== 'string' || !taskId) {
    return noop;
  }
  var clientMethods = (client && typeof client === 'object') ? client : {};
  if (typeof clientMethods.getTask !== 'function' ||
      typeof clientMethods.cancelTask !== 'function' ||
      typeof clientMethods.retryTask !== 'function') {
    return noop;
  }
  var canListFiles = typeof clientMethods.listTaskFiles === 'function';
  var canGetFile = typeof clientMethods.getTaskFile === 'function';
  var canGetDiff = typeof clientMethods.getTaskDiff === 'function';
  var canPause = typeof clientMethods.pauseTask === 'function';
  var canContinue = typeof clientMethods.continueTask === 'function';
  var canConfirm = typeof clientMethods.confirmTask === 'function';
  var canReject = typeof clientMethods.rejectTask === 'function';

  var disposed = false;
  var loadSeq = 0;      // 加载代际：新 loadTask 使旧请求的成功/失败回调失效

  var viewState = {
    loading: true,     // 首屏 getTask 尚未返回
    error: null,       // 加载失败文本（textContent）
    busy: false,       // 取消/重试在途栅栏（防止重复/叠加操作）
    actionError: '',   // 操作失败可见错误
    files: [],         // Hub-local 快照列表（无正文）
    openPath: '',      // 当前展开的相对路径
    openContent: '',   // 当前展开正文（有界）
    fileError: '',     // 文件读取失败文案
    fileBusy: false,
    patchOpen: false,
    patchText: '',
    patchTruncated: false,
    patchError: '',
    patchBusy: false,
  };

  function isActive() {
    return !disposed;
  }

  var unsubscribe = store.subscribe(function () {
    render();
    maybeLoadFiles();
  });

  function render() {
    if (disposed) {
      return;
    }
    removeAllChildren(root);
    if (viewState.error) {
      root.appendChild(h('h1', 'task-heading', '任务 ' + fmtValue(taskId)));
      root.appendChild(h('div', 'err', '⚠️ 加载失败：' + fmtValue(viewState.error)));
      var retryLoad = h('button', null, '重试');
      retryLoad.setAttribute('data-action', 'load');
      root.appendChild(retryLoad);
      retryLoad.addEventListener('click', function () {
        viewState.error = null;
        viewState.loading = true;
        render();
        loadTask();
      });
      return;
    }
    if (viewState.loading) {
      root.appendChild(h('h1', 'task-heading', '任务 ' + fmtValue(taskId)));
      root.appendChild(h('div', 'meta', MSG_LOADING));
      return;
    }

    var state = store.getState();
    var task = state.tasks[taskId];
    if (!task || typeof task.state !== 'string') {
      // 显式空态：任务不在 store 中（契约拒绝内部字段；不渲染占位业务数据）
      root.appendChild(h('h1', 'task-heading', '任务 ' + fmtValue(taskId)));
      root.appendChild(h('div', 'meta', '任务不存在或已过期'));
      return;
    }

    root.appendChild(h('h1', 'task-heading', '任务 ' + fmtValue(task.task_id)));
    var meta = h('div', 'meta');
    meta.appendChild(h('span', null,
      fmtValue(task.machine) + ' · ' + fmtValue(task.agent_type) +
      ' · ' + fmtValue(task.project) + ' · ' + fmtValue(task.requested_by) +
      (fmtValue(task.created_at) !== '?' ? (' · ' + fmtValue(task.created_at)) : '')));
    root.appendChild(meta);

    if (typeof task.session_id === 'string' && task.session_id) {
      var sessionLink = h('a', 'session-link', '查看会话 ' + task.session_id);
      sessionLink.setAttribute('href', pagePath('session', task.session_id));
      root.appendChild(sessionLink);
    }

    // 状态流
    var flowPanel = h('div', 'panel');
    flowPanel.appendChild(h('h3', null, '状态'));
    renderStateFlow(flowPanel, task.state);
    root.appendChild(flowPanel);

    // 指令 + 操作
    var instrPanel = h('div', 'panel');
    instrPanel.appendChild(h('h3', null, '指令'));
    var instrPre = document.createElement('pre');
    instrPre.setAttribute('class', 'instruction');
    instrPre.textContent = (typeof task.instruction === 'string')
      ? task.instruction.slice(0, 2000) : '';
    instrPanel.appendChild(instrPre);

    // 取消 / 重试动作（委托 + 错误盒）
    var actionsHost = h('div');
    actionsHost.setAttribute('id', 'task-actions');
    renderActions(actionsHost, taskId, task, actionContext());
    instrPanel.appendChild(actionsHost);
    root.appendChild(instrPanel);

    // 结果
    var resultPanel = h('div', 'panel');
    resultPanel.appendChild(h('h3', null, '结果'));
    renderResult(resultPanel, task.result);
    root.appendChild(resultPanel);

    var patchPanel = h('div', 'panel');
    patchPanel.appendChild(h('h3', null, '脱敏 patch'));
    renderPatch(patchPanel, task.result);
    root.appendChild(patchPanel);

    // 日志
    var logPanel = h('div', 'panel');
    logPanel.appendChild(h('h3', null, '实时日志'));
    var liveLines = (state.taskLogs[taskId] && Array.isArray(state.taskLogs[taskId]))
      ? state.taskLogs[taskId] : [];
    var logSummary = (task.result && typeof task.result.log_summary === 'string')
      ? task.result.log_summary : '';
    renderLogs(logPanel, liveLines, logSummary);
    root.appendChild(logPanel);

    var filesPanel = h('div', 'panel');
    filesPanel.appendChild(h('h3', null, '快照文件'));
    renderFiles(filesPanel);
    root.appendChild(filesPanel);
  }

  function renderPatch(host, result) {
    if (viewState.patchError) {
      host.appendChild(h('div', 'err', fmtValue(viewState.patchError)));
    }
    var hasPatch = !!(result && result.has_diff_patch);
    if (!hasPatch) {
      host.appendChild(h('div', 'meta', '无脱敏 patch'));
      return;
    }
    var meta = h('div', 'meta');
    var bytes = (result && typeof result.diff_patch_bytes === 'number')
      ? String(result.diff_patch_bytes) : '?';
    meta.textContent = '有界 unified diff · ' + bytes + ' bytes（点开再拉取正文）';
    host.appendChild(meta);
    var btn = h('button', null, viewState.patchOpen ? '收起 patch' : '展开 patch');
    btn.setAttribute('data-action', 'toggle-patch');
    if (viewState.patchBusy) {
      btn.setAttribute('disabled', 'disabled');
    }
    host.appendChild(btn);
    if (viewState.patchOpen && viewState.patchText) {
      var pre = document.createElement('pre');
      pre.setAttribute('class', 'diff-patch');
      pre.textContent = String(viewState.patchText).slice(0, MAX_PATCH_RENDER);
      host.appendChild(pre);
      if (viewState.patchTruncated) {
        host.appendChild(h('div', 'meta', '已截断'));
      }
    }
  }

  function togglePatch() {
    if (!canGetDiff || disposed || viewState.patchBusy) {
      return;
    }
    if (viewState.patchOpen) {
      viewState.patchOpen = false;
      render();
      return;
    }
    if (viewState.patchText) {
      viewState.patchOpen = true;
      render();
      return;
    }
    viewState.patchBusy = true;
    viewState.patchError = '';
    render();
    Promise.resolve()
      .then(function () {
        return clientMethods.getTaskDiff(taskId);
      })
      .then(function (res) {
        if (disposed) {
          return;
        }
        viewState.patchBusy = false;
        if (res && res.ok && typeof res.diff_patch === 'string') {
          viewState.patchOpen = true;
          viewState.patchText = res.diff_patch;
          viewState.patchTruncated = res.truncated === true;
        } else {
          viewState.patchError = '读取 patch 失败';
        }
        render();
      })
      .catch(function (err) {
        if (disposed) {
          return;
        }
        viewState.patchBusy = false;
        viewState.patchError = (err && err.detail) ? err.detail : '读取 patch 失败';
        render();
      });
  }

  function renderFiles(host) {
    if (viewState.fileError) {
      host.appendChild(h('div', 'err', fmtValue(viewState.fileError)));
    }
    var files = Array.isArray(viewState.files) ? viewState.files : [];
    if (!files.length) {
      host.appendChild(h('div', 'meta', MSG_NO_FILES));
      return;
    }
    for (var i = 0; i < files.length; i += 1) {
      var item = files[i];
      if (!item || typeof item.path !== 'string') {
        continue;
      }
      var row = h('div', 'file-row');
      var btn = h('button', null, fmtValue(item.path));
      btn.setAttribute('data-file-path', item.path);
      if (viewState.fileBusy) {
        btn.setAttribute('disabled', 'disabled');
      }
      row.appendChild(btn);
      if (item.truncated) {
        row.appendChild(h('span', 'meta', 'truncated'));
      }
      if (item.redacted) {
        row.appendChild(h('span', 'meta', 'redacted'));
      }
      host.appendChild(row);
      if (viewState.openPath === item.path && viewState.openContent) {
        var pre = document.createElement('pre');
        pre.setAttribute('class', 'file-body');
        pre.textContent = String(viewState.openContent).slice(0, MAX_FILE_RENDER);
        host.appendChild(pre);
      }
    }
  }

  function openFile(relpath) {
    if (!canGetFile || disposed || viewState.fileBusy || !relpath) {
      return;
    }
    if (viewState.openPath === relpath && viewState.openContent) {
      viewState.openPath = '';
      viewState.openContent = '';
      render();
      return;
    }
    viewState.fileBusy = true;
    viewState.fileError = '';
    render();
    Promise.resolve()
      .then(function () {
        return clientMethods.getTaskFile(taskId, relpath);
      })
      .then(function (res) {
        if (disposed) {
          return;
        }
        viewState.fileBusy = false;
        if (res && res.ok && typeof res.content === 'string') {
          viewState.openPath = res.path || relpath;
          viewState.openContent = res.content;
        } else {
          viewState.fileError = '读取失败';
        }
        render();
      })
      .catch(function (err) {
        if (disposed) {
          return;
        }
        viewState.fileBusy = false;
        viewState.fileError = (err && err.detail) ? err.detail : '读取失败';
        render();
      });
  }

  var filesLoadSeq = 0;
  var filesLoadedForTerminal = false;

  function loadFiles() {
    if (!canListFiles || disposed) {
      return;
    }
    var generation = ++filesLoadSeq;
    Promise.resolve()
      .then(function () {
        return clientMethods.listTaskFiles(taskId);
      })
      .then(function (res) {
        if (disposed || generation !== filesLoadSeq) {
          return;
        }
        viewState.files = (res && Array.isArray(res.files)) ? res.files : [];
        render();
      })
      .catch(function () {
        if (disposed || generation !== filesLoadSeq) {
          return;
        }
        viewState.files = [];
      });
  }

  function maybeLoadFiles() {
    if (disposed) {
      return;
    }
    var task = store.getState().tasks[taskId];
    if (!task || !terminal(task.state)) {
      if (filesLoadedForTerminal || viewState.files.length) {
        filesLoadedForTerminal = false;
        filesLoadSeq += 1;
        viewState.files = [];
        viewState.openPath = '';
        viewState.openContent = '';
        viewState.fileError = '';
        viewState.patchOpen = false;
        viewState.patchText = '';
        viewState.patchTruncated = false;
        viewState.patchError = '';
        render();
      }
      return;
    }
    if (filesLoadedForTerminal) {
      return;
    }
    filesLoadedForTerminal = true;
    loadFiles();
  }

  function onRootClick(ev) {
    if (disposed) {
      return;
    }
    var node = ev.target;
    while (node && node !== root) {
      if (node.tagName === 'BUTTON' && node.getAttribute('data-action') === 'toggle-patch') {
        togglePatch();
        return;
      }
      if (node.tagName === 'BUTTON' && node.hasAttribute('data-file-path')) {
        openFile(node.getAttribute('data-file-path'));
        return;
      }
      node = node.parentNode;
    }
  }

  /** 构造 actionContext：按钮委托回调共用的安全上下文（visit 栅栏）。 */
  function actionContext() {
    var self = viewState;
    return {
      get busy() {
        return self.busy;
      },
      get actionError() {
        return self.actionError;
      },
      canPause: canPause,
      canContinue: canContinue,
      canConfirm: canConfirm,
      canReject: canReject,
      performAction: function (kind) {
        if (disposed || self.busy) {
          return; // 在途/卸载：忽略重复或迟到点击
        }
        self.busy = true;
        self.actionError = '';
        // 立即重绘以禁用当前动作按钮；busy 仍由回调入口再次校验。
        render();
        var methods = {
          cancel: clientMethods.cancelTask,
          retry: clientMethods.retryTask,
          pause: clientMethods.pauseTask,
          continue: clientMethods.continueTask,
          confirm: clientMethods.confirmTask,
          reject: clientMethods.rejectTask,
        };
        var method = methods[kind];
        if (typeof method !== 'function') {
          self.busy = false;
          return;
        }
        Promise.resolve()
          .then(function () {
            return method(taskId);
          })
          .then(function (res) {
            if (disposed) {
              return; // 已卸载：不再写 store 或触碰 DOM
            }
            self.busy = false;
            if (res && res.ok && res.task && typeof res.task.task_id === 'string') {
              try {
                // 成功：更新 store，触发订阅回调重绘（按钮角态随新状态切换）
                store.setTask(res);
              } catch (err) {
                // store 写失败不阻断已成功的结果
              }
            } else {
              self.actionError = (res && res.detail) ? res.detail : '操作失败';
            }
            render();
          })
          .catch(function (err) {
            if (disposed) {
              return;
            }
            self.busy = false;
            self.actionError = (err && err.detail) ? err.detail : '网络错误';
            render();
          });
      },
    };
  }

  function loadTask() {
    // 每次发起都取一个新代际；已在途的旧请求即使成功后也因代际不匹配
    // 而静默返回，绝不覆盖最新请求写入的状态。
    var generation = ++loadSeq;
    Promise.resolve()
      .then(function () {
        return clientMethods.getTask(taskId);
      })
      .then(function (res) {
        if (disposed || generation !== loadSeq) {
          return;
        }
        if (res && res.ok && res.task) {
          try {
            if (store && typeof store.setTask === 'function') {
              store.setTask(res);
            }
          } catch (err) {
            // 种子写失败不阻断渲染
          }
        }
        viewState.loading = false;
        viewState.error = null;
        render();
      })
      .catch(function (err) {
        if (disposed || generation !== loadSeq) {
          return;
        }
        viewState.loading = false;
        viewState.error = (err && err.detail) ? err.detail : '网络错误';
        render();
      });
  }

  // 订阅后立即渲染一次，让 loading（或已缓存的 store 任务）先行绘制。
  var state0 = store.getState();
  if (state0.tasks[taskId]) {
    // store 已有此任务（createTask / machine 列表种子 / SSE）：直接 ready
    viewState.loading = false;
  }
  root.addEventListener('click', onRootClick);
  render();
  maybeLoadFiles();

  // 首屏：总是拉取最新详情（含 result），填充 store 后重绘。
  loadTask();

  return function teardown() {
    disposed = true;      // 护栏：此后 render / 异步回调 / 按钮委托一律 no-op
    root.removeEventListener('click', onRootClick);
    unsubscribe();
    removeAllChildren(root);
  };
}