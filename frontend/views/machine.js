/* frontend/views/machine.js — 单机详情视图与任务创建（Task 15）
 *
 * 通过注入的 client 方法加载机器详情与最近任务；store 订阅负责增量状态/
 * 任务重绘。所有动态文本用 textContent / createTextNode，属性经 setAttribute，
 * 导航一律用 pagePath(...) 编码，不做 HTML 字符串拼接（用建元素与属性赋值
 * API），不直接发 HTTP 或原生流式事件。
 *
 * 表单只做客户端 UX 校验（project/instruction 必填），授权与最终校验仍由
 * 后端决定；创建成功用 pagePath("task", taskId) 跳转，错误一律 textContent。
 * 显式状态：loading / error / task 空列表 / 无时间线数据。
 */

import { pagePath } from '../routes.js';

var MSG_LOADING = '加载中…';
var MSG_NO_TASKS = '暂无任务';
var MSG_NO_TIMELINE = '无 24h 历史数据';
var MAX_VISIBLE_TASKS = 10;
var MAX_TIMELINE_BARS = 144;   // 视图有界：最多渲染 144 条刻度
var MAX_VISIBLE_INSTANCES = 20; // 实例 metadata 有界展示
var MAX_VISIBLE_SESSIONS = 50;
var FIVE_CONTROL_ACTIONS = [
  'pause_session', 'resume_session', 'terminate_session',
  'quarantine_session', 'cancel_attempt',
];
var MAX_TURN_TEXT = 2000;
var MAX_VISIBLE_PROFILES = 32;

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

function kv(label, value) {
  var div = h('div', 'metric');
  div.appendChild(h('span', null, label));
  div.appendChild(h('b', null, String(value)));
  return div;
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

/* -- 系统指标 ------------------------------------------------------------------ */

function renderSystemMetrics(host, system) {
  var sys = (system && typeof system === 'object') ? system : {};
  host.appendChild(h('h3', null, '系统指标'));
  host.appendChild(kv('平台', fmtValue(sys.platform)));
  host.appendChild(kv('负载', fmtValue(sys.load)));
  host.appendChild(kv('内存', fmtValue(sys.mem_used_mb) + ' / ' +
    fmtValue(sys.mem_total_mb) + ' MB'));
  host.appendChild(kv('磁盘', fmtValue(sys.disk_used_pct)));
  host.appendChild(kv('运行时间', fmtValue(sys.uptime)));
}

/* --- Agent 状态表 ------------------------------------------------------------ */

function renderAgentTable(host, agents) {
  var items = (agents && typeof agents === 'object') ? agents : {};
  var keys = Object.keys(items);

  host.appendChild(h('h3', null, 'Agent 状态'));
  if (keys.length === 0) {
    host.appendChild(h('div', 'meta', '无 agent 数据'));
    return;
  }

  var table = document.createElement('table');
  table.setAttribute('class', 'agent-table');
  var headRow = document.createElement('tr');
  ['类型', '状态', '会话', '进程'].forEach(function (label) {
    var th = document.createElement('th');
    th.textContent = label;
    headRow.appendChild(th);
  });
  table.appendChild(headRow);

  keys.forEach(function (atype) {
    var agent = (items[atype] && typeof items[atype] === 'object')
      ? items[atype] : {};
    var tr = document.createElement('tr');
    tr.appendChild(h('td', null, fmtValue(atype)));
    var status = agentStatus(agent);
    tr.appendChild(h('td', status.className, status.text));
    tr.appendChild(h('td', null, fmtValue(agent.session_count)));
    tr.appendChild(h('td', null, fmtValue(agent.process_count)));
    table.appendChild(tr);
  });
  host.appendChild(table);
}

function agentStatus(agent) {
  if (agent.error) {
    return { className: 'st-err', text: fmtValue(agent.error) };
  }
  if (agent.installed === false) {
    return { className: 'st-absent', text: '未部署' };
  }
  if (agent.gateway_state) {
    return { className: 'st-ok', text: fmtValue(agent.gateway_state) };
  }
  return { className: 'st-ok', text: 'ok' };
}

/* --- 发现实例（只读 metadata 展示） --------------------------------------- */

/**
 * 只读渲染 Task 2 探针公布的 sanitized instances metadata。
 *
 * 每个实例行只含 pid/pgid/exe_path/cmdline/agent_family/native_file_path/
 * started_at/attachable 八个 allowlist 字段；这里只展示其中的公开 metadata
 * 文本，绝不展示原生内容、不构建进程句柄、不发起任何纳管动作。
 * ``onAdopt`` 在 attachable 行上被调用；pid 仍以 textContent 呈现，
 * 不进入控制请求。视图本身不 fetch。
 *
 * 返回待挂载的面板节点（调用方负责 appendChild），便于独立测试与后置纳管组合。
 */
export function renderInstances(instances, onAdopt) {
  var panel = h('div', 'panel');
  panel.appendChild(h('h3', null, '发现 Agent 实例'));
  var rows = Array.isArray(instances) ? instances : [];
  if (rows.length === 0) {
    panel.appendChild(h('div', 'meta', '暂无实例'));
    return panel;
  }
  var shown = rows.slice(0, MAX_VISIBLE_INSTANCES);
  var table = document.createElement('table');
  table.setAttribute('class', 'instance-table');
  var headRow = document.createElement('tr');
  ['家族', 'pid', '进程组', '可纳管', '启动时间', 'exe 路径', 'cmdline', '操作']
    .forEach(function (label) {
      var th = document.createElement('th');
      th.textContent = label;
      headRow.appendChild(th);
    });
  table.appendChild(headRow);
  shown.forEach(function (inst) {
    if (!inst || typeof inst !== 'object') {
      return; // 契约保证结构，防御性跳过
    }
    var tr = document.createElement('tr');
    tr.appendChild(h('td', null, fmtValue(inst.agent_family)));
    tr.appendChild(h('td', null, fmtValue(inst.pid)));
    tr.appendChild(h('td', null, fmtValue(inst.pgid)));
    tr.appendChild(h('td', null, inst.attachable === true ? '是' : '否'));
    tr.appendChild(h('td', null, fmtValue(inst.started_at)));
    // 有界显示：路径 / 命令行可能很长，只展示前段
    tr.appendChild(h('td', null, String(fmtValue(inst.exe_path)).slice(0, 60)));
    tr.appendChild(h('td', null, String(fmtValue(inst.cmdline)).slice(0, 80)));
    var actionCell = document.createElement('td');
    if (inst.attachable === true && typeof onAdopt === 'function') {
      var adoptBtn = document.createElement('button');
      adoptBtn.setAttribute('type', 'button');
      adoptBtn.textContent = '纳管';
      adoptBtn.addEventListener('click', function () {
        onAdopt(inst);
      });
      actionCell.appendChild(adoptBtn);
    } else {
      actionCell.textContent = '—';
    }
    tr.appendChild(actionCell);
    table.appendChild(tr);
  });
  panel.appendChild(table);
  if (rows.length > MAX_VISIBLE_INSTANCES) {
    panel.appendChild(h('div', 'meta',
      '… 共 ' + String(rows.length) + ' 个实例，仅显示最近 ' +
      String(MAX_VISIBLE_INSTANCES) + ' 个'));
  }
  return panel;
}

/* --- 任务列表（有界） ---------------------------------------------------------- */

// 任务状态 → 状态样式类的显式白名单映射。类型来自 hub/domain/task.py 的
// 已知状态集合；任何不在白名单内的状态一律落入安全回退类，绝不把原始状态
// 字符串拼进 class 属性（状态文本仍由调用方以 textContent 呈现，可安全显示）。
var TASK_STATE_CLASSES = {
  queued: 'st-queued',
  leased: 'st-leased',
  running: 'st-running',
  paused: 'st-paused',
  succeeded: 'st-succeeded',
  failed: 'st-failed',
  cancelled: 'st-cancelled',
  expired: 'st-expired',
};
var TASK_STATE_FALLBACK_CLASS = 'st-unknown';

function taskStateClass(stateText) {
  var key = String(stateText);
  if (Object.prototype.hasOwnProperty.call(TASK_STATE_CLASSES, key)) {
    return TASK_STATE_CLASSES[key];
  }
  return TASK_STATE_FALLBACK_CLASS;
}

function renderTaskList(host, tasks) {
  host.appendChild(h('h3', null, '任务'));
  var all = Array.isArray(tasks) ? tasks : [];
  var shown = all.slice(0, MAX_VISIBLE_TASKS);
  if (shown.length === 0) {
    host.appendChild(h('div', 'meta', MSG_NO_TASKS));
    return;
  }
  shown.forEach(function (task) {
    if (!task || typeof task.task_id !== 'string') {
      return; // 契约保证结构，防御性跳过
    }
    var row = h('div', 'task-row');
    var link = h('a', null, task.task_id);
    link.setAttribute('href', pagePath('task', task.task_id));
    row.appendChild(link);
    var stateText = fmtValue(task.state);
    // class 只能来自白名单映射或安全回退；动态状态文本仍走 textContent 安全呈现
    row.appendChild(
      h('span', 'task-state ' + taskStateClass(stateText), stateText));
    if (typeof task.session_id === 'string' && task.session_id) {
      var sessionLink = h('a', 'session-link', '会话');
      sessionLink.setAttribute('href', pagePath('session', task.session_id));
      row.appendChild(sessionLink);
    }
    var metaText = fmtValue(task.agent_type) + ' · ' + fmtValue(task.project) +
      ' · ' + String(fmtValue(task.instruction)).slice(0, 60) + ' · ' +
      fmtValue(task.created_at);
    row.appendChild(h('span', 'meta', metaText));
    host.appendChild(row);
  });
  if (all.length > MAX_VISIBLE_TASKS) {
    host.appendChild(h('div', 'meta',
      '… 共 ' + String(all.length) + ' 条，仅显示最近 ' +
      String(MAX_VISIBLE_TASKS) + ' 条'));
  }
}

/* --- 24h 在线时间线（占位 + 有界渲染） ----------------------------------------- */

function renderTimeline(host, history) {
  host.appendChild(h('h3', null, '24h 在线时间线'));
  var rows = Array.isArray(history) ? history : [];
  var now = Date.now() / 1000;
  var dayAgo = now - 86400;
  var kept = rows.filter(function (row) {
    return row && typeof row.ts === 'number' && row.ts >= dayAgo;
  });
  var bound = kept.slice(-MAX_TIMELINE_BARS);
  if (bound.length === 0) {
    host.appendChild(h('div', 'meta', MSG_NO_TIMELINE));
    return;
  }
  var track = h('div', 'timeline');
  bound.forEach(function (row) {
    var bar = document.createElement('i');
    if (row.reachable === false) {
      bar.setAttribute('class', 'down');
    }
    track.appendChild(bar);
  });
  host.appendChild(track);

  var legend = h('div', 'timeline-legend');
  var start = new Date(dayAgo * 1000).toLocaleTimeString('zh-CN',
    { hour: '2-digit', minute: '2-digit' });
  var end = new Date(now * 1000).toLocaleTimeString('zh-CN',
    { hour: '2-digit', minute: '2-digit' });
  legend.appendChild(h('span', null, start + ' — ' + end));
  host.appendChild(legend);
}

/* --- 源控制（当前受控纳管会话）--------------------------------------------- */

// 仅五个既有动作令牌：从固定映射枚举，绝不从任意命令文本构造动作名。
var SOURCE_ACTIONS_LABELS = {
  pause_session: '暂停会话',
  resume_session: '恢复会话',
  terminate_session: '终止会话',
  quarantine_session: '隔离会话',
  cancel_attempt: '取消任务',
};

// 有界状态白名单（pending|adopted|revoked|terminal）；白名单之外一律落入
// 安全回退类，绝不把原始状态字符串拼进 class 属性（状态文本仍由 h() 的
// textContent 呈现）。
var SOURCE_STATUS_CLASSES = {
  pending: 'st-queued',
  adopted: 'st-ok',
  revoked: 'st-err',
  terminal: 'st-unknown',
};
var SOURCE_STATUS_FALLBACK_CLASS = 'st-unknown';

function sourceStatusClass(statusText) {
  var key = String(statusText);
  if (Object.prototype.hasOwnProperty.call(SOURCE_STATUS_CLASSES, key)) {
    return SOURCE_STATUS_CLASSES[key];
  }
  return SOURCE_STATUS_FALLBACK_CLASS;
}

// 可交互控制只允许在“已纳管”座席上出现（adopted 的五个固定动作令牌）。
var SOURCE_GATED_STATUS = 'adopted';
// 非 admitted 状态展示的固定标签（有界常量串，绝不拼接任意状态文本）。
var SOURCE_PENDING_LABEL = '纳管处理中…';
var SOURCE_REVOKED_LABEL = '纳管已撤销';
var SOURCE_OTHER_LABEL = '无可用控制动作';

/**
 * 渲染源控制面板（有界、DOM-safe）。
 *
 * source 只带视图层已有的有界字段（session_id + 固定 status），actions 是可
 * 执行动作令牌子集。非仅纳管状态把行动按钮团门控到 ``status == adopted``：
 * pending -> 状态章 + 固定“纳管处理中…”标签（无按钮）；revoked -> 状态章 +
 * 固定“纳管已撤销”标签（无按钮）；其它状态只显示章 + 固定 empty 标签。
 * 点击仅把固定令牌 + session_id 交给 onAction —— 本模块不 fetch、不拼
 * /api 路径、不读取任何输入文本 / pid / signal（client.js 拥有全部 HTTP）。
 * 返回面板容器节点，由调用方负责追加。
 */
export function renderSourceControl(source, actions, onAction) {
  var panel = h('div', 'panel');
  panel.appendChild(h('h3', null, '源控制'));
  if (!source || typeof source !== 'object' ||
      typeof source.session_id !== 'string' || !source.session_id) {
    panel.appendChild(h('div', 'meta', '暂无纳管会话'));
    return panel;
  }
  var statusText = fmtValue(source.status);
  panel.appendChild(h('div', 'source-status ' + sourceStatusClass(statusText),
                      statusText));
  if (statusText === 'pending') {
    panel.appendChild(h('div', 'meta', SOURCE_PENDING_LABEL));
    return panel;
  }
  if (statusText === 'revoked') {
    panel.appendChild(h('div', 'meta', SOURCE_REVOKED_LABEL));
    return panel;
  }
  if (statusText !== SOURCE_GATED_STATUS) {
    // terminal / 未知状态：只显示，不存在可交互按钮。
    panel.appendChild(h('div', 'meta', SOURCE_OTHER_LABEL));
    return panel;
  }
  var allowed = {};
  if (Array.isArray(actions)) {
    for (var i = 0; i < actions.length; i += 1) {
      if (Object.prototype.hasOwnProperty.call(
            SOURCE_ACTIONS_LABELS, actions[i])) {
        allowed[actions[i]] = true;
      }
    }
  }
  var keys = Object.keys(SOURCE_ACTIONS_LABELS).filter(function (tk) {
    return allowed[tk] === true;
  });
  if (keys.length === 0) {
    panel.appendChild(h('div', 'meta', SOURCE_OTHER_LABEL));
    return panel;
  }
  keys.forEach(function (tk) {
    var btn = document.createElement('button');
    btn.setAttribute('type', 'button');
    btn.textContent = SOURCE_ACTIONS_LABELS[tk];
    if (typeof onAction === 'function') {
      btn.addEventListener('click', function () {
        // 点击只交固定动作令牌 + 有界 session_id，绝无自由文本接口。
        onAction(tk, source.session_id);
      });
    }
    panel.appendChild(btn);
  });
  return panel;
}

/* --- 源控制区结束 ----------------------------------------------------------- */

/* --- 任务创建表单 -------------------------------------------------------------- */

/* agent 下拉的内置兜底清单：仅作 API 失败时的首屏回退，值必须 ⊆
 * agent_profiles.EXECUTABLE_AGENT_TYPES 注册表（后端以注册表为 SSOT 校验）。 */
var FALLBACK_AGENT_TYPES = ['codex', 'claude_code', 'hermes', 'pi'];

function fillAgentSelect(select, values) {
  // 去重 + 过滤非字符串；用 option 覆盖全部内容（不比较差异，量级有界）
  var seen = {};
  var clean = [];
  (values || []).forEach(function (value) {
    if (typeof value === 'string' && value && !seen[value]) {
      seen[value] = true;
      clean.push(value);
    }
  });
  if (!clean.length) {
    return false;
  }
  while (select.firstChild) {
    select.removeChild(select.firstChild);
  }
  clean.forEach(function (value) {
    var option = document.createElement('option');
    option.setAttribute('value', value);
    option.textContent = value;
    select.appendChild(option);
  });
  return true;
}

function createTaskForm(host, machineName, clientMethods, storeRef, isActive,
                        agentTypesPromise) {
  var form = document.createElement('form');
  form.setAttribute('class', 'task-form');
  form.setAttribute('id', 'task-create-form');

  var agentLabel = h('label', null, 'agent 类型');
  var select = document.createElement('select');
  select.setAttribute('id', 'nt-agent');
  fillAgentSelect(select, FALLBACK_AGENT_TYPES);
  agentLabel.appendChild(select);
  form.appendChild(agentLabel);

  // 动态来源：/api/agent-types（agent_profiles 注册表派生）。请求由
  // mountMachine 发起一次并共享（agentTypesPromise），表单随 render 重建时
  // 只消费结果、绝不重发请求；null（client 无该方法）或失败时保留兜底清单。
  if (agentTypesPromise) {
    agentTypesPromise.then(function (types) {
      if (isActive() && Array.isArray(types) && types.length) {
        fillAgentSelect(select, types);
      }
    });
  }

  var projectLabel = h('label', null, '项目（须在白名单）');
  var projectInput = document.createElement('input');
  projectInput.setAttribute('id', 'nt-project');
  projectInput.setAttribute('placeholder', 'agent-fleet');
  projectLabel.appendChild(projectInput);
  form.appendChild(projectLabel);

  var instructionLabel = h('label', null, '指令（≤2000 字符）');
  var instructionInput = document.createElement('textarea');
  instructionInput.setAttribute('id', 'nt-instruction');
  instructionInput.setAttribute('rows', '5');
  instructionInput.setAttribute('maxlength', '2000');
  instructionLabel.appendChild(instructionInput);
  form.appendChild(instructionLabel);

  var confirmLabel = h('label', null, '创建后先人工确认再派发');
  var confirmInput = document.createElement('input');
  confirmInput.setAttribute('type', 'checkbox');
  confirmInput.setAttribute('id', 'nt-confirm');
  confirmLabel.insertBefore(confirmInput, confirmLabel.firstChild);
  form.appendChild(confirmLabel);

  var errorBox = h('div', 'err');
  errorBox.setAttribute('id', 'nt-error');
  function setError(message) {
    // 错误一律 textContent：绝不把后端原始文本当作 HTML 渲染
    errorBox.textContent = message ? String(message) : '';
  }

  var submit = document.createElement('button');
  submit.setAttribute('type', 'submit');
  submit.textContent = '创建';
  var actions = h('div', 'actions');
  actions.appendChild(submit);
  form.appendChild(actions);
  form.appendChild(errorBox);
  host.appendChild(form);

  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    var project = String(projectInput.value || '').trim();
    var instruction = String(instructionInput.value || '').trim();
    // 客户端 UX 校验：仅提示必填；授权/白名单仍由后端校验
    if (!project) {
      setError('请输入项目名');
      return;
    }
    if (!instruction) {
      setError('请输入指令');
      return;
    }
    submit.disabled = true;
    setError('');
    var payload = {
      machine: machineName,
      agent_type: select.value,
      project: project,
      instruction: instruction,
    };
    if (confirmInput.checked) {
      payload.confirm = true;
    }
    Promise.resolve()
      .then(function () {
        return clientMethods.createTask(payload);
      })
      .then(function (res) {
        if (!isActive()) {
          return; // 已卸载：不再写 store 或触碰 DOM
        }
        var task = res && res.task;
        if (res && res.ok && task && typeof task.task_id === 'string') {
          // 成功写入 store，任务视图可直接读取缓存（store 缺失时跳过）
          try {
            if (storeRef && typeof storeRef.setTask === 'function') {
              storeRef.setTask(res);
            }
          } catch (err) {
            // 缓存写入失败不阻断跳转
          }
          if (typeof window !== 'undefined' && window.location) {
            if (typeof task.session_id === 'string' && task.session_id) {
              window.location.href = pagePath('session', task.session_id);
            } else {
              window.location.href = pagePath('task', task.task_id);
            }
          }
          return;
        }
        setError((res && res.detail) ? res.detail : '任务创建失败');
        submit.disabled = false;
      })
      .catch(function (err) {
        if (!isActive()) {
          return; // 已卸载：不再触碰 DOM
        }
        setError((err && err.detail) ? err.detail : '网络错误');
        submit.disabled = false;
      });
  });
}

/* --- store 读取辅助（实时增量来源） ------------------------------------------- */

/**
 * 聚合当前机器在 store 中的实时增量快照：agent/system/可及性。机器摘要
 * 经 machine_update 更新 online / agents / system，单机详情的 current 由
 * setMachine 写入——两处形状不同，各自做兼容读取。store 均缺失时返回 null，
 * 交由渲染层展示空态。
 */
function machineSnapshot(store, machineName) {
  var state = store.getState();
  var summary = state.machines[machineName];
  var detail = state.machineDetail;
  var agents = null;
  var system = null;
  var instances = null;
  var localProfiles = null;
  var reachable;
  if (summary) {
    agents = (summary.agents && typeof summary.agents === 'object')
      ? summary.agents
      : (summary.current && summary.current.agents) || null;
    system = (summary.system && typeof summary.system === 'object')
      ? summary.system
      : (summary.current && summary.current.system) || null;
    // 实例 metadata 走 detail.current；摘要兜底兼容 summary.current
    if (summary.current && Array.isArray(summary.current.instances)) {
      instances = summary.current.instances;
    }
    if (summary.current && Array.isArray(summary.current.local_profiles)) {
      localProfiles = summary.current.local_profiles;
    }
    if (typeof summary.online === 'boolean') {
      reachable = summary.online;
    } else if (summary.current &&
               typeof summary.current.reachable === 'boolean') {
      reachable = summary.current.reachable;
    }
  }
  if (agents === null && detail && detail.current && detail.current.agents) {
    agents = detail.current.agents;
  }
  if (system === null && detail && detail.current && detail.current.system) {
    system = detail.current.system;
  }
  if (instances === null && detail && detail.current && detail.current.instances) {
    instances = detail.current.instances;
  }
  if (localProfiles === null && detail && detail.current &&
      Array.isArray(detail.current.local_profiles)) {
    localProfiles = detail.current.local_profiles;
  }
  if (reachable === undefined && detail && detail.current &&
      typeof detail.current.reachable === 'boolean') {
    reachable = detail.current.reachable;
  }
  return { agents: agents, system: system, instances: instances,
           local_profiles: localProfiles,
           reachable: reachable, source: machineSource(summary, detail) };
}

/**
 * 提取机器数据中可选的纳管种子（bounded source seed）。
 *
 * 仅接受 ``{ session_id: <string>, status: <固定 string>, actions?: <令牌数组> }``
 * 形状 —— status 不在白名单上的种子视作无源（渲染层以空态回退），绝不让任意
 * 文本出现在源控制区；视图层不做任何 fetch，HTTP 全部在 client.js。
 */
function machineSource(summary, detail) {
  var candidate = (summary && summary.current && summary.current.source) ||
                  (detail && detail.current && detail.current.source);
  if (!candidate || typeof candidate !== 'object' ||
      typeof candidate.session_id !== 'string' || !candidate.session_id ||
      typeof candidate.status !== 'string') {
    return null;
  }
  var bounded = {
    session_id: candidate.session_id,
    status: Object.prototype.hasOwnProperty.call(
      SOURCE_STATUS_CLASSES, String(candidate.status))
        ? String(candidate.status)
        : 'terminal',
  };
  if (Array.isArray(candidate.actions)) {
    bounded.actions = candidate.actions.slice(0, 5);
  }
  return bounded;
}

/**
 * 合并出本机可见的任务列表：以 store.tasks 中本机任务为主（live SSE
 * task_update / createTask 写入均来自这里），首屏 listTasks 条目只在 store
 * 无对应模型时兜底。按 created_at 降序（最近优先），由 renderTaskList 有界展示。
 */
function mergedTaskList(store, fallbackTasks, machineName) {
  var byId = {};
  var state = store.getState();
  var storeTasks = state.tasks;
  var storeKeys = Object.keys(storeTasks);
  for (var i = 0; i < storeKeys.length; i += 1) {
    var task = storeTasks[storeKeys[i]];
    if (task && typeof task.machine === 'string' &&
        task.machine === machineName && typeof task.task_id === 'string') {
      byId[task.task_id] = task;
    }
  }
  if (Array.isArray(fallbackTasks)) {
    for (var j = 0; j < fallbackTasks.length; j += 1) {
      var fb = fallbackTasks[j];
      if (fb && typeof fb.task_id === 'string' && byId[fb.task_id] === undefined) {
        byId[fb.task_id] = fb;
      }
    }
  }
  var keys = Object.keys(byId);
  var list = keys.map(function (k) { return byId[k]; });
  list.sort(function (a, b) {
    var ca = (typeof a.created_at === 'string') ? a.created_at : a.task_id;
    var cb = (typeof b.created_at === 'string') ? b.created_at : b.task_id;
    return (ca < cb) ? 1 : (ca > cb) ? -1 : 0;
  });
  return list;
}

/* --- 视图组合 ------------------------------------------------------------------ */

/* --- 本机会话列表（只读摘要，HTTP 在 client.js） ----------------------------- */

function renderMachineSessions(host, sessions, errorText) {
  host.appendChild(h('h3', null, '本机会话'));
  if (errorText) {
    host.appendChild(h('div', 'err', String(errorText)));
  }
  var rows = Array.isArray(sessions) ? sessions.slice(0, MAX_VISIBLE_SESSIONS) : [];
  if (rows.length === 0) {
    host.appendChild(h('div', 'meta', '暂无会话'));
    return;
  }
  rows.forEach(function (session) {
    if (!session || typeof session.session_id !== 'string' || !session.session_id) {
      return;
    }
    var row = h('div', 'session-row');
    var link = h('a', null, session.session_id);
    var href = pagePath('session', session.session_id);
    if (typeof href === 'string') {
      link.setAttribute('href', href);
    }
    row.appendChild(link);
    row.appendChild(h('span', 'meta', session.managed ? '受管' : '非受管'));
    if (typeof session.capture_quality === 'string' && session.capture_quality) {
      row.appendChild(h('span', 'meta', session.capture_quality));
    }
    if (typeof session.status === 'string' && session.status) {
      row.appendChild(h('span', 'meta', session.status));
    }
    host.appendChild(row);
  });
}

function renderAdoptionControls(host, viewState, clientMethods, redraw, reloadSeats) {
  var paint = (typeof redraw === 'function') ? redraw : function () {};
  var rows = Array.isArray(viewState.adoptions) ? viewState.adoptions : [];
  if (viewState.adoptionError) {
    host.appendChild(h('div', 'err', String(viewState.adoptionError)));
  }
  if (rows.length === 0) {
    host.appendChild(renderSourceControl(null, null, noop));
    return;
  }
  rows.forEach(function (record) {
    if (!record || typeof record.session_id !== 'string' || !record.session_id) {
      return;
    }
    var statusText = fmtValue(record.status);
    var source = { session_id: record.session_id, status: statusText };
    var onAction = function (action, sid) {
      if (typeof clientMethods.controlSession !== 'function' || viewState.busy) {
        return;
      }
      viewState.busy = true;
      viewState.controlError = null;
      viewState.controlNotice = '正在入队…';
      paint();
      clientMethods.controlSession(sid, action, 'operator_requested')
        .then(function (result) {
          viewState.busy = false;
          var queued = (result && result.status) ? String(result.status) : 'pending';
          var cmd = (result && result.command_id) ? String(result.command_id) : '';
          viewState.controlNotice = '已入队 ' + queued +
            (cmd ? (' · ' + cmd) : '') + '（未执行）';
          paint();
        })
        .catch(function (err) {
          viewState.busy = false;
          viewState.controlNotice = null;
          viewState.controlError = (err && (err.detail || err.code)) ?
            String(err.detail || err.code) : '控制失败';
          paint();
        });
    };
    var actions = (statusText === SOURCE_GATED_STATUS) ? FIVE_CONTROL_ACTIONS : [];
    host.appendChild(renderSourceControl(source, actions, onAction));
    if (statusText === 'pending' && typeof clientMethods.retryAdoption === 'function') {
      var retryBtn = document.createElement('button');
      retryBtn.setAttribute('type', 'button');
      retryBtn.textContent = '重试纳管';
      retryBtn.addEventListener('click', function () {
        if (viewState.busy) {
          return;
        }
        viewState.busy = true;
        viewState.controlError = null;
        viewState.controlNotice = '正在重试纳管…';
        paint();
        clientMethods.retryAdoption(record.session_id)
          .then(function () {
            viewState.busy = false;
            viewState.controlNotice = '纳管已重试';
            if (typeof reloadSeats === 'function') {
              return reloadSeats();
            }
            paint();
          })
          .catch(function (err) {
            viewState.busy = false;
            viewState.controlNotice = null;
            viewState.controlError = (err && (err.detail || err.code)) ?
              String(err.detail || err.code) : '重试失败';
            paint();
          });
      });
      host.appendChild(retryBtn);
    }
    if (statusText === SOURCE_GATED_STATUS &&
        typeof clientMethods.revokeAdoption === 'function') {
      var revokeBtn = document.createElement('button');
      revokeBtn.setAttribute('type', 'button');
      revokeBtn.textContent = '撤销纳管';
      revokeBtn.addEventListener('click', function () {
        if (viewState.busy) {
          return;
        }
        viewState.busy = true;
        viewState.controlError = null;
        viewState.controlNotice = '正在撤销纳管…';
        paint();
        clientMethods.revokeAdoption(record.session_id)
          .then(function () {
            viewState.busy = false;
            viewState.controlNotice = '纳管已请求撤销（不发进程信号）';
            if (typeof reloadSeats === 'function') {
              return reloadSeats();
            }
            paint();
          })
          .catch(function (err) {
            viewState.busy = false;
            viewState.controlNotice = null;
            viewState.controlError = (err && (err.detail || err.code)) ?
              String(err.detail || err.code) : '撤销失败';
            paint();
          });
      });
      host.appendChild(revokeBtn);
    }
  });
}

function noop() {}

/**
 * 挂载单机视图。source 通过 client.getMachine(name) / listTasks 拉取，
 * client.createTask 创建任务；store.subscribe 支持 SSE 增量重绘：
 *   - 首屏数据种子进 store（setMachine / setTask），store 即实时渲染来源；
 *   - 渲染从 store 读取 reachability/system/agents（machine_update 增量）与
 *     本机任务（合并 SSE task_update / createTask 写入）；
 *   - 首屏兜底仅用于 store 尚无对应模型之时（见 machineSnapshot / mergedTaskList）。
 * 返回 () => () 卸载函数。
 */
export function mountMachine(root, machineName, store, client) {
  if (!root || !store || typeof machineName !== 'string' || !machineName) {
    return noop;
  }
  var clientMethods = (client && typeof client === 'object') ? client : {};
  if (typeof clientMethods.getMachine !== 'function' ||
      typeof clientMethods.listTasks !== 'function' ||
      typeof clientMethods.createTask !== 'function') {
    return noop;
  }

  var disposed = false;

  // agent 类型清单：mount 生命周期内只请求一次，render 重建表单共享结果。
  // 失败 resolve 为 null（表单保留兜底清单），不进入 reject 路径。
  var agentTypesPromise =
    (typeof clientMethods.listAgentTypes === 'function')
      ? clientMethods.listAgentTypes()
          .then(function (res) {
            var types = res && res.agent_types;
            return Array.isArray(types) ? types : null;
          })
          .catch(function () {
            return null;
          })
      : null;

  var viewState = {
    loading: true,
    error: null,
    history: [],     // 时间线数据：store 无 history 字段，保留首屏兜底
    tasks: [],       // 首屏 listTasks 兜底（仅 store 缺失对应任务时使用）
    adoptions: [],
    sessions: [],
    sessionError: null,
    adoptionError: null,
    controlNotice: null,
    controlError: null,
    busy: false,
  };

  var unsubscribe = store.subscribe(function () {
    render();
  });

  function isActive() {
    return !disposed;
  }

  function reloadSeats() {
    var jobs = [];
    if (typeof clientMethods.listAdoptions === 'function') {
      jobs.push(clientMethods.listAdoptions(machineName)
        .then(function (res) {
          viewState.adoptions = (res && Array.isArray(res.items)) ? res.items : [];
          viewState.adoptionError = null;
        })
        .catch(function (err) {
          viewState.adoptions = [];
          viewState.adoptionError = (err && (err.detail || err.code)) ?
            String(err.detail || err.code) : '纳管列表加载失败';
        }));
    }
    if (typeof clientMethods.listSessions === 'function') {
      jobs.push(clientMethods.listSessions({ machine: machineName, limit: 50 })
        .then(function (res) {
          viewState.sessions = (res && Array.isArray(res.sessions)) ? res.sessions : [];
          viewState.sessionError = null;
        })
        .catch(function (err) {
          viewState.sessions = [];
          viewState.sessionError = (err && (err.detail || err.code)) ?
            String(err.detail || err.code) : '会话列表加载失败';
        }));
    }
    if (jobs.length === 0) {
      render();
      return Promise.resolve();
    }
    return Promise.all(jobs).then(function () {
      if (!disposed) {
        render();
      }
    });
  }

  function render() {
    if (disposed) {
      return;
    }
    removeAllChildren(root);
    if (viewState.error) {
      root.appendChild(h('div', 'err', '⚠️ 加载失败：' + fmtValue(viewState.error)));
      return;
    }
    if (viewState.loading) {
      root.appendChild(h('div', 'meta', MSG_LOADING));
      return;
    }

    root.appendChild(h('h1', 'machine-heading', fmtValue(machineName)));

    var live = machineSnapshot(store, machineName);
    if (typeof live.reachable === 'boolean') {
      root.appendChild(h('div', 'reachability ' +
        (live.reachable ? 'on' : 'off'), live.reachable ? '在线' : '离线'));
    }

    var detailGrid = h('div', 'detail-grid');
    var sysPanel = h('div', 'panel');
    renderSystemMetrics(sysPanel, live.system);
    detailGrid.appendChild(sysPanel);
    var agentPanel = h('div', 'panel');
    renderAgentTable(agentPanel, live.agents);
    detailGrid.appendChild(agentPanel);
    root.appendChild(detailGrid);

    // 发现实例：attachable 行可点「纳管」，HTTP 只走 client.adoptInstance
    function onAdopt(inst) {
      if (!inst || typeof clientMethods.adoptInstance !== 'function' ||
          viewState.busy) {
        return;
      }
      viewState.busy = true;
      viewState.controlError = null;
      viewState.controlNotice = '正在纳管…';
      render();
      clientMethods.adoptInstance(machineName, inst.pid, inst.started_at)
        .then(function () {
          viewState.busy = false;
          viewState.controlNotice = '纳管已提交';
          return reloadSeats();
        })
        .catch(function (err) {
          viewState.busy = false;
          viewState.controlNotice = null;
          viewState.controlError = (err && (err.detail || err.code)) ?
            String(err.detail || err.code) : '纳管失败';
          render();
        });
    }
    var instancesPanel = renderInstances(live.instances, onAdopt);
    root.appendChild(instancesPanel);

    var sessionPanel = h('div', 'panel');
    renderMachineSessions(sessionPanel, viewState.sessions,
      viewState.sessionError);
    root.appendChild(sessionPanel);

    renderAdoptionControls(root, viewState, clientMethods, function () {
      render();
    }, reloadSeats);

    if (viewState.controlNotice) {
      root.appendChild(h('div', 'meta', viewState.controlNotice));
    }
    if (viewState.controlError) {
      root.appendChild(h('div', 'err', viewState.controlError));
    }

    // 任务列表（实时合并）+ 在线时间线面板
    var taskPanel = h('div', 'panel');
    renderTaskList(taskPanel,
      mergedTaskList(store, viewState.tasks, machineName));
    root.appendChild(taskPanel);

    var createPanel = h('div', 'panel');
    createPanel.appendChild(h('h3', null, '新建任务 · ' + fmtValue(machineName)));
    createTaskForm(createPanel, machineName, clientMethods, store, isActive,
      agentTypesPromise);
    root.appendChild(createPanel);

    var timelinePanel = h('div', 'panel');
    renderTimeline(timelinePanel, viewState.history);
    root.appendChild(timelinePanel);
  }

  // 订阅后立即渲染一次，让 loading 态先行绘制（异步请求尚未返回前）。
  render();

  Promise.resolve()
    .then(function () {
      return Promise.all([
        clientMethods.getMachine(machineName),
        clientMethods.listTasks({ machine: machineName, limit: 10 }),
      ]);
    })
    .then(function (results) {
      if (disposed) {
        return;
      }
      var detail = results[0];
      var tasks = results[1];
      if (detail) {
        // 单机详情种子进 store：machineSnapshot 消费 current 实时增量
        store.setMachine(detail);
        viewState.history = detail.history;
      }
      if (tasks && Array.isArray(tasks.tasks)) {
        // 条目级种子：store 缺失对应任务时才由 viewState.tasks 兜底
        try {
          for (var i = 0; i < tasks.tasks.length; i += 1) {
            store.setTask(tasks.tasks[i]);
          }
        } catch (err) {
          // 种子写失败不阻断；视图仍可用 store 实时任务渲染
        }
        viewState.tasks = tasks.tasks;
      }
      viewState.loading = false;
      viewState.error = null;
      render();
      reloadSeats();
    })
    .catch(function (err) {
      if (disposed) {
        return;
      }
      viewState.loading = false;
      viewState.error = (err && err.detail) ? err.detail : '网络错误';
      render();
    });

  return function teardown() {
    disposed = true;      // 护栏：后续 render / 异步回调一律 no-op
    unsubscribe();
    removeAllChildren(root);
  };
}
