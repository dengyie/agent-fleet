/* frontend/views/session.js — 受控会话时间线详情视图（Task 11）
 *
 * 只按授权 API（/api/sessions/<id>、<id>/events、<id>/policy-signals）经过
 * api/contracts.js 校验的公开模型渲染。不直接发起任何 HTTP 长连接或页面跳转，
 * 不访问全局运行时配置，不渲染任何契约拒绝的内部字段或原始 transcript——
 * 所有动态文本一律 textContent。
 *
 * 显式布局：
 *   - 头部：session_id + managed / capture_quality / control_capability 徽章；
 *   - 事件时间线：逐 kind 展开该 kind 的公开字段（session_metadata /
 *     user_message / assistant_message / tool_call / tool_result /
 *     process_spawn / process_exit / step_start / step_end / session_close /
 *     quality / gap / supervisor 状态跃迁）；
 *   - 采集缺口（capture_gap）专属渲染：缺口即降级，不使用成功态样式；
 *   - 控制状态（queued / executing / succeeded / already_finished / failed /
 *     expired / rejected）经显式白名单映射 + 文本呈现，绝不把状态名拼进 class；
 *   - 策略信号面板（有界）；空态 / 加载态 / 错误态明确。
 *
 * 边界：
 *   - MAX_EVENT_RENDER / MAX_SIGNAL_RENDER / MAX_TEXT_RENDER 三段有界；
 *   - 每个事件 payload 只拼契约白名单字段，嵌套容器直接丢弃；
 *   - navigation 一律 pagePath(...) 编码。
 */

import { pagePath } from '../routes.js';
import { renderSourceControl } from './machine.js';

var MSG_LOADING = '加载中…';
var MSG_ERROR = '加载失败：';
var MSG_NO_EVENTS = '暂无事件';
var MSG_NO_SIGNALS = '暂无策略信号';
var MSG_NO_CONTROL = '无控制记录';
var MSG_NO_SESSION = '会话不存在或已过期';
var MSG_CONTROL_UNAVAILABLE = '无可用控制动作';
var FIVE_CONTROL_ACTIONS = [
  'pause_session', 'resume_session', 'terminate_session',
  'quarantine_session', 'cancel_attempt',
];
var MAX_TURN_TEXT = 2000;
var MAX_VISIBLE_PROFILES = 32;
var MAX_EVENT_RENDER = 300;  // 每会话事件展示上限（按时间线有界）
var MAX_SIGNAL_RENDER = 50;
var MAX_TEXT_RENDER = 2000; // 单条消息文本展示边界
var MAX_RESULT_RENDER = 4000;

/* -- 事件 kind → 可见文案标签（与 session_schema.EVENT_KINDS 对齐） ---------- */

var KIND_LABELS = {
  session_start: '会话开始',
  session_metadata: '会话元数据',
  user_message: '用户消息',
  assistant_message: '助手消息',
  tool_call: '工具调用',
  tool_result: '工具结果',
  process_spawn: '进程启动',
  process_exit: '进程退出',
  step_start: '步骤开始',
  step_end: '步骤结束',
  session_close: '会话关闭',
  capture_quality_changed: '采集质量变化',
  capture_gap: '采集缺口',
  policy_signal: '策略信号',
  supervisor_state_changed: '监督状态变化',
};
var KIND_FALLBACK_LABEL = '会话事件';

/* -- 控制状态白名单：状态名永远不进 class，只进 textContent ------------------ */

var CONTROL_STATE_CLASS = {
  'queued': 'st-queued',
  'delivered': 'st-running',
  'accepted': 'st-running',
  'executing': 'st-running',
  'succeeded': 'st-succeeded',
  'already_finished': 'st-succeeded',
  'failed': 'st-failed',
  'rejected': 'st-failed',
  'expired': 'st-expired',
};
var CONTROL_STATE_FALLBACK_CLASS = 'st-unknown';

function controlStateClass(stateText) {
  var key = String(stateText);
  if (Object.prototype.hasOwnProperty.call(CONTROL_STATE_CLASS, key)) {
    return CONTROL_STATE_CLASS[key];
  }
  return CONTROL_STATE_FALLBACK_CLASS;
}

/* -- 采集质量档位（三档枚举，绝不虚标高档） ------------------------------------- */

function qualityLabel(quality) {
  var q = String(quality || '');
  if (q === 'exact') {
    return '精确';
  }
  if (q === 'structured') {
    return '结构化';
  }
  return '略采样'; // exact/structured 以外的未知值一律显示略采样
}

/* -- DOM 安全写入 ------------------------------------------------------------ */

function h(tag, className, text) {
  var el = document.createElement(tag);
  if (className) {
    el.setAttribute('class', className);
  }
  if (text !== undefined && text !== null && String(text) !== '') {
    el.textContent = String(text);
  }
  return el;
}

function removeAllChildren(el) {
  while (el.firstChild) {
    el.removeChild(el.firstChild);
  }
}

function firstBoundTaskId(events) {
  if (!Array.isArray(events)) {
    return '';
  }
  for (var i = 0; i < events.length; i += 1) {
    var ev = events[i];
    var payload = ev && ev.payload;
    if (payload && typeof payload.task_id === 'string' && payload.task_id) {
      return payload.task_id;
    }
  }
  return '';
}

function ellipsis(v, limit) {
  if (typeof v !== 'string') {
    return null;
  }
  var s = v.slice(0, limit);
  if (v.length > limit) {
    s = s + '…';
  }
  return s;
}

function metaTime(ts) {
  // RFC3339 高度敏感：直接展示截断字符串，绝不格式化数字。
  if (typeof ts !== 'string' || !ts) {
    return '';
  }
  return ts.slice(0, 23);
}

function commonHead(host, sessionId) {
  host.appendChild(h('h2', 'session-heading', '会话 ' + String(sessionId)));
}

/* -- 头部徽标 ---------------------------------------------------------------- */

function renderBadges(host, session) {
  var badges = h('div', 'session-badges');
  badges.appendChild(h('span', 'badge ' +
    (session.managed ? 'badge-managed' : 'badge-unmanaged'),
    session.managed ? '受管' : '非受管'));
  badges.appendChild(h('span', 'badge badge-quality',
    '采集 ' + qualityLabel(session.capture_quality)));
  if (session.managed) {
    badges.appendChild(h('span', 'badge badge-control ' +
      controlStateClass(session.control_capability),
      session.control_capability === 'available' ? '可控制' : '控制不可用'));
  }
  if (typeof session.status === 'string' && session.status) {
    badges.appendChild(h('span', 'badge badge-status', session.status));
  }
  host.appendChild(badges);
}

/* -- 事件时间线（有界） ------------------------------------------------------ */

function renderEventList(host, events) {
  var all = Array.isArray(events) ? events : [];
  var shown = all.slice(0, MAX_EVENT_RENDER);
  if (shown.length === 0) {
    host.appendChild(h('p', 'empty', MSG_NO_EVENTS));
    return;
  }
  var list = h('ul', 'session-timeline');
  for (var i = 0; i < shown.length; i += 1) {
    list.appendChild(renderEventRow(shown[i]));
  }
  host.appendChild(list);
  if (all.length > MAX_EVENT_RENDER) {
    host.appendChild(h('p', 'meta', '仅显示最近 ' +
      String(MAX_EVENT_RENDER) + ' 条事件'));
  }
}

function renderEventRow(ev) {
  var li = h('li', 'session-event');
  var kindName = KIND_LABELS[ev.kind] || KIND_FALLBACK_LABEL;
  var left = h('span', 'session-event-kind', kindName);
  li.appendChild(left);

  var seqEl = (typeof ev.sequence === 'number') ?
    h('span', 'session-event-seq', '#' + String(ev.sequence)) : null;
  if (seqEl) {
    li.appendChild(seqEl);
  }
  if (typeof ev.emitted_at === 'string' && ev.emitted_at) {
    li.appendChild(h('span', 'session-event-time', metaTime(ev.emitted_at)));
  }

  var quality = qualityLabel(ev.capture_quality);
  if (ev.capture_quality === 'exact' || ev.capture_quality === 'structured') {
    li.appendChild(h('span', 'session-event-q', quality));
  }

  var body = renderPayloadBody(ev);
  if (body) {
    var wrap = h('div', 'session-event-body');
    for (var b = 0; b < body.length; b += 1) {
      wrap.appendChild(body[b]);
    }
    li.appendChild(wrap);
  }
  return li;
}

/* -- 载荷渲染：逐 kind 白名单 --------------------------------------------- */

function renderPayloadBody(ev) {
  var kind = ev.kind;
  var p = ev.payload || {};
  if (p === null || typeof p !== 'object') {
    return null;
  }
  var nodes = [];
  function textNode(text) {
    var s = ellipsis(text, MAX_TEXT_RENDER);
    if (s) {
      nodes.push(h('div', 'session-event-text', s));
    }
  }
  function resultNode(text) {
    var s = ellipsis(text, MAX_RESULT_RENDER);
    if (s) {
      nodes.push(h('div', 'session-event-text', s));
    }
  }

  if (kind === 'user_message' || kind === 'assistant_message') {
    textNode(p.text);
    return nodes;
  }
  if (kind === 'tool_call' || kind === 'tool_result') {
    var line = '';
    if (typeof p.tool_name === 'string') {
      line = p.tool_name;
    }
    if (typeof p.call_id === 'string') {
      line = line ? line + ' · ' + p.call_id : p.call_id;
    }
    if (kind === 'tool_result' && typeof p.status === 'string') {
      line = line ? line + ' · ' + p.status : p.status;
    }
    if (line) {
      nodes.push(h('div', 'tool-name', line));
    }
    textNode(p.arguments);
    resultNode(p.result);
    return nodes;
  }
  if (kind === 'capture_gap') {
    var gap = '采集缺口';
    if (typeof p.start_sequence === 'number' &&
        typeof p.end_sequence === 'number') {
      gap += ' seq ' + String(p.start_sequence) + '…' +
        String(p.end_sequence);
    }
    if (typeof p.reason === 'string' && p.reason) {
      gap += ' · ' + p.reason;
    }
    nodes.push(h('div', 'gap', gap));
    return nodes;
  }
  if (kind === 'capture_quality_changed') {
    var change = '采集质量 ' + qualityLabel(p.old_quality) + ' → ' +
      qualityLabel(p.new_quality);
    nodes.push(h('div', 'quality-change', change));
    return nodes;
  }
  if (kind === 'process_spawn' || kind === 'process_exit') {
    var parts = [];
    if (typeof p.process_family === 'string') {
      parts.push(p.process_family);
    }
    if (kind === 'process_exit' && typeof p.exit_code === 'number') {
      parts.push('exit ' + String(p.exit_code));
    }
    if (typeof p.reason === 'string' && p.reason) {
      parts.push(p.reason);
    }
    if (parts.length) {
      nodes.push(h('div', 'process-line', parts.join(' · ')));
    }
    return nodes;
  }
  if (kind === 'step_start' || kind === 'step_end') {
    var parts2 = [];
    if (typeof p.step_name === 'string') {
      parts2.push(p.step_name);
    }
    if (kind === 'step_end' && typeof p.status === 'string') {
      parts2.push(p.status);
    }
    if (parts2.length) {
      nodes.push(h('div', 'step-line', parts2.join(' · ')));
    }
    return nodes;
  }
  if (kind === 'session_start' || kind === 'session_metadata') {
    var parts3 = [];
    if (typeof p.agent_family === 'string') {
      parts3.push(p.agent_family);
    }
    if (typeof p.adapter === 'string') {
      parts3.push(p.adapter);
    }
    if (parts3.length) {
      nodes.push(h('div', 'session-meta-line', parts3.join(' · ')));
    }
    return nodes;
  }
  if (kind === 'session_close') {
    var parts4 = [];
    if (typeof p.exit_code === 'number') {
      parts4.push('exit ' + String(p.exit_code));
    }
    if (typeof p.reason === 'string' && p.reason) {
      parts4.push(p.reason);
    }
    if (parts4.length) {
      nodes.push(h('div', 'session-close-line', parts4.join(' · ')));
    }
    return nodes;
  }
  if (kind === 'policy_signal') {
    var tag = (typeof p.severity === 'string') ? p.severity : 'info';
    var s = (typeof p.policy === 'string') ? p.policy : '';
    if (typeof p.reason === 'string' && p.reason) {
      s = s ? s + ' · ' + p.reason : p.reason;
    }
    if (s) {
      var sig = h('span', 'signal-severity ' + severityCssToken(tag), tag);
      var text = h('span', 'signal-text', s);
      var row = h('div', 'signal-row');
      row.appendChild(sig);
      row.appendChild(text);
      nodes.push(row);
    }
    return nodes;
  }
  if (kind === 'supervisor_state_changed') {
    var oldS = (typeof p.old_state === 'string') ? p.old_state : '';
    var newS = (typeof p.new_state === 'string') ? p.new_state : '';
    if (newS) {
      var span = h('span', 'state-node ' + controlStateClass(newS),
        oldS ? (oldS + ' → ' + newS) : newS);
      nodes.push(span);
    }
    return nodes;
  }
  return nodes;
}

function severityCssToken(level) {
  if (level === 'critical' || level === 'high') {
    return 'signal-critical';
  }
  if (level === 'warn') {
    return 'signal-warn';
  }
  return 'signal-info';
}

function controlFeatures(store) {
  var status = store && typeof store.getState === 'function'
    ? store.getState().status : null;
  var features = (status && status.features && typeof status.features === 'object')
    ? status.features : {};
  return {
    append_user_turn: features.append_user_turn === true,
    apply_local_profile: features.apply_local_profile === true,
  };
}

function enqueueControl(clientMethods, viewState, paint, sid, action, payload) {
  if (viewState.controlBusy) {
    return;
  }
  viewState.controlBusy = true;
  viewState.controlError = null;
  viewState.controlNotice = '正在入队…';
  paint();
  var request = (payload && typeof payload === 'object')
    ? clientMethods.controlSession(sid, action, 'operator_requested', payload)
    : clientMethods.controlSession(sid, action, 'operator_requested');
  request.then(function (result) {
    viewState.controlBusy = false;
    var statusText = (result && result.status) ? String(result.status) : 'pending';
    var cmd = (result && result.command_id) ? String(result.command_id) : '';
    viewState.controlNotice = '已入队 ' + statusText +
      (cmd ? (' · ' + cmd) : '') + '（未执行）';
    viewState.controlError = null;
    if (typeof clientMethods.getSessionEvents !== 'function') {
      paint();
      return;
    }
    return clientMethods.getSessionEvents(sid, 100)
      .then(function (events) {
        if (events && Array.isArray(events.events)) {
          viewState.events = events.events;
        }
        paint();
      });
  }).catch(function (err) {
    viewState.controlBusy = false;
    viewState.controlNotice = null;
    viewState.controlError = (err && (err.detail || err.code)) ?
      String(err.detail || err.code) : '控制失败';
    paint();
  });
}

function renderFollowUpTurn(host, session, viewState, clientMethods, paint) {
  if (!session || session.agent_family === 'hermes') {
    return;
  }
  var wrap = h('div', 'follow-up');
  wrap.appendChild(h('h3', null, '追加用户回合'));
  wrap.appendChild(h('p', 'meta', '入队不等于执行；不会写入正在运行的进程 stdin。'));
  var box = document.createElement('textarea');
  box.setAttribute('rows', '3');
  box.setAttribute('maxlength', String(MAX_TURN_TEXT));
  box.setAttribute('data-role', 'follow-up');
  box.value = viewState.followUpText || '';
  box.addEventListener('input', function () {
    viewState.followUpText = String(box.value || '').slice(0, MAX_TURN_TEXT);
  });
  wrap.appendChild(box);
  var btn = document.createElement('button');
  btn.setAttribute('type', 'button');
  btn.textContent = '入队追加回合';
  btn.addEventListener('click', function () {
    var text = String(box.value || '').trim();
    if (!text || text.length > MAX_TURN_TEXT) {
      viewState.controlError = '文本为空或超长';
      paint();
      return;
    }
    viewState.followUpText = text;
    enqueueControl(clientMethods, viewState, paint, session.session_id,
      'append_user_turn', { text: text });
  });
  wrap.appendChild(btn);
  host.appendChild(wrap);
}

function renderLocalProfilePicker(host, session, profiles, viewState,
                                  clientMethods, paint) {
  var rows = Array.isArray(profiles) ? profiles.slice(0, MAX_VISIBLE_PROFILES) : [];
  var family = (session && typeof session.agent_family === 'string')
    ? session.agent_family : '';
  var matched = [];
  for (var i = 0; i < rows.length; i += 1) {
    var row = rows[i];
    if (!row || typeof row.profile_id !== 'string' || !row.profile_id) {
      continue;
    }
    if (family && row.family && row.family !== family) {
      continue;
    }
    matched.push(row);
  }
  var wrap = h('div', 'local-profile');
  wrap.appendChild(h('h3', null, '本机配置'));
  wrap.appendChild(h('p', 'meta', '只下发本机已登记的 profile_id，不下发密钥。'));
  if (matched.length === 0) {
    wrap.appendChild(h('p', 'empty', '无匹配的本机配置'));
    host.appendChild(wrap);
    return;
  }
  var select = document.createElement('select');
  select.setAttribute('data-role', 'local-profile');
  for (var j = 0; j < matched.length; j += 1) {
    var opt = document.createElement('option');
    opt.setAttribute('value', matched[j].profile_id);
    opt.textContent = String(matched[j].label || matched[j].profile_id) +
      (matched[j].current ? '（当前）' : '');
    select.appendChild(opt);
  }
  wrap.appendChild(select);
  var applyBtn = document.createElement('button');
  applyBtn.setAttribute('type', 'button');
  applyBtn.textContent = '切换本机配置';
  applyBtn.addEventListener('click', function () {
    var profileId = String(select.value || '');
    if (!profileId) {
      return;
    }
    enqueueControl(clientMethods, viewState, paint, session.session_id,
      'apply_local_profile', { profile_id: profileId });
  });
  wrap.appendChild(applyBtn);
  host.appendChild(wrap);
}


/* -- 控制状态面板（adopted + available 才接线五动作；queued ≠ executed） --- */

function renderControlPanel(host, events, session, viewState, clientMethods, redraw, store) {
  host.appendChild(h('h3', 'control-heading', '控制状态'));
  var paint = (typeof redraw === 'function') ? redraw : function () {};
  var canControl = session && session.managed === true &&
    session.control_capability === 'available' &&
    typeof clientMethods.controlSession === 'function' &&
    typeof session.session_id === 'string' && session.session_id;
  if (canControl) {
    var source = {
      session_id: session.session_id,
      status: 'adopted',
      actions: FIVE_CONTROL_ACTIONS,
    };
    host.appendChild(renderSourceControl(source, FIVE_CONTROL_ACTIONS,
      function (action, sid) {
        enqueueControl(clientMethods, viewState, paint, sid, action);
      }));
    var flags = controlFeatures(store);
    if (flags.append_user_turn) {
      renderFollowUpTurn(host, session, viewState, clientMethods, paint);
    }
    if (flags.apply_local_profile) {
      renderLocalProfilePicker(host, session, viewState.localProfiles || [],
        viewState, clientMethods, paint);
    }
  } else {
    host.appendChild(h('p', 'empty', MSG_CONTROL_UNAVAILABLE));
  }
  if (viewState.controlNotice) {
    host.appendChild(h('p', 'meta', viewState.controlNotice));
  }
  if (viewState.controlError) {
    host.appendChild(h('p', 'err', viewState.controlError));
  }
  if (typeof clientMethods.revokeAdoption === 'function' &&
      session && session.managed === true &&
      typeof session.session_id === 'string' && session.session_id) {
    var revokeBtn = document.createElement('button');
    revokeBtn.setAttribute('type', 'button');
    revokeBtn.textContent = '撤销纳管';
    revokeBtn.addEventListener('click', function () {
      if (viewState.controlBusy) {
        return;
      }
      viewState.controlBusy = true;
      viewState.controlError = null;
      viewState.controlNotice = '正在撤销纳管…';
      paint();
      clientMethods.revokeAdoption(session.session_id)
        .then(function () {
          viewState.controlBusy = false;
          viewState.controlNotice = '纳管已请求撤销（不发进程信号）';
          if (typeof clientMethods.getSession !== 'function') {
            paint();
            return;
          }
          return clientMethods.getSession(session.session_id)
            .then(function (detail) {
              if (detail && detail.session) {
                viewState.session = detail.session;
              }
              paint();
            });
        })
        .catch(function (err) {
          viewState.controlBusy = false;
          viewState.controlNotice = null;
          viewState.controlError = (err && (err.detail || err.code)) ?
            String(err.detail || err.code) : '撤销失败';
          paint();
        });
    });
    host.appendChild(revokeBtn);
  }
  var receipts = [];
  if (Array.isArray(events)) {
    for (var i = 0; i < events.length; i += 1) {
      var ev = events[i];
      if (ev && ev.kind === 'supervisor_state_changed' &&
          ev.payload && typeof ev.payload.new_state === 'string') {
        receipts.push(ev.payload.new_state);
      }
    }
  }
  if (receipts.length === 0) {
    host.appendChild(h('p', 'empty', MSG_NO_CONTROL));
    return;
  }
  var last = receipts[receipts.length - 1];
  host.appendChild(h('span', 'state-node ' + controlStateClass(last), last));
}

/* -- 策略信号面板（有界） ----------------------------------------------------- */

function renderSignalPanel(host, signals) {
  host.appendChild(h('h3', 'signals-heading', '策略信号（' +
    String(Array.isArray(signals) ? signals.length : 0) + '）'));
  var shown = Array.isArray(signals) ? signals.slice(0, MAX_SIGNAL_RENDER) : [];
  if (shown.length === 0) {
    host.appendChild(h('p', 'empty', MSG_NO_SIGNALS));
    return;
  }
  var list = h('ul', 'signal-list');
  for (var i = 0; i < shown.length; i += 1) {
    var sig = shown[i];
    var li = h('li', 'signal-item');
    li.appendChild(h('span', 'badge badge-' +
      severityCssToken(sig.severity), (typeof sig.severity === 'string' &&
      sig.severity) ? sig.severity : 'info'));
    if (typeof sig.emitted_at === 'string') {
      li.appendChild(h('span', 'signal-time', metaTime(sig.emitted_at)));
    }
    var reason = (typeof sig.reason === 'string') ? sig.reason : '';
    if (reason) {
      li.appendChild(h('span', 'signal-text', ellipsis(reason, MAX_TEXT_RENDER)));
    }
    list.appendChild(li);
  }
  host.appendChild(list);
}

/* -- 视图组合 ---------------------------------------------------------------- */

function noop() {}

/**
 * @param {Element} root      挂载点
 * @param {string}  sessionId 会话 id（URL 已解码的 opaque 标识）
 * @param {FleetStore} store  状态存储（须含 subscribe）
 * @param {object}  client    注入客户端（须含 getSession / getSessionEvents /
 *                            getSessionSignals）
 * @returns {function} teardown
 */
export function mountSession(root, sessionId, store, client) {
  if (!root || !store || typeof sessionId !== 'string' || !sessionId) {
    return noop;
  }
  var clientMethods = (client && typeof client === 'object') ? client : {};
  if (typeof clientMethods.getSession !== 'function' ||
      typeof clientMethods.getSessionEvents !== 'function' ||
      typeof clientMethods.getSessionSignals !== 'function') {
    return noop;
  }

  var disposed = false;
  var viewState = {
    loading: true,
    error: null,
    session: null,   // parseSession 模型 { session }
    events: [],      // parseSessionEvents 模型事件列表
    signals: [],     // parseSessionSignals 模型信号列表
    controlBusy: false,
    controlNotice: null,
    controlError: null,
    localProfiles: [],
    followUpText: '',
  };

  var unsubscribe = store.subscribe(function () {
    render();
  });

  function render() {
    if (disposed) {
      return;
    }
    removeAllChildren(root);
    if (viewState.error) {
      root.appendChild(h('h1', 'session-heading', '会话 ' + String(sessionId)));
      root.appendChild(h('p', 'err', MSG_ERROR));
      root.appendChild(h('p', 'err-detail', ellipsis(viewState.error, 200) || ''));
      var retry = h('button', null, '重试');
      retry.setAttribute('data-action', 'load');
      retry.addEventListener('click', function () {
        viewState.error = null;
        viewState.loading = true;
        render();
        loadSync();
      });
      root.appendChild(retry);
      return;
    }
    if (viewState.loading) {
      root.appendChild(h('h1', 'session-heading', '会话 ' + String(sessionId)));
      root.appendChild(h('p', 'meta', '加载中…'));
      return;
    }

    var session = viewState.session;
    if (!session) {
      root.appendChild(h('h1', 'session-heading', '会话 ' + String(sessionId)));
      root.appendChild(h('p', 'empty', MSG_NO_SESSION));
      return;
    }

    root.appendChild(h('h1', 'session-heading',
      '会话 ' + String(session.session_id)));

    var meta = h('p', 'meta');
    if (typeof session.machine_id === 'string' && session.machine_id) {
      meta.appendChild(h('span', null, '机器 ' + String(session.machine_id)));
    }
    if (typeof session.process_group_id === 'string' &&
        session.process_group_id) {
      meta.appendChild(h('span', null,
        ' · 进程组 ' + String(session.process_group_id)));
    }
    if (typeof session.started_at === 'string') {
      meta.appendChild(h('span', null, ' · ' + metaTime(session.started_at)));
    }
    root.appendChild(meta);

    renderBadges(root, session);

    if (typeof session.machine_id === 'string' && session.machine_id) {
      var link = h('a', 'machine-link',
        '查看机器 ' + String(session.machine_id));
      var machinePath = pagePath('machine', session.machine_id);
      if (typeof machinePath === 'string') {
        link.setAttribute('href', machinePath);
      }
      root.appendChild(link);
    }

    var boundTaskId = firstBoundTaskId(viewState.events);
    if (boundTaskId) {
      var taskLink = h('a', 'task-link', '查看任务 ' + boundTaskId);
      taskLink.setAttribute('href', pagePath('task', boundTaskId));
      root.appendChild(taskLink);
    }

    var timelinePanel = h('section', 'panel');
    timelinePanel.appendChild(h('h2', null, '事件时间线'));
    renderEventList(timelinePanel, viewState.events);
    root.appendChild(timelinePanel);

    var controlPanel = h('section', 'panel');
    controlPanel.appendChild(h('h2', null, '控制'));
    renderControlPanel(controlPanel, viewState.events, session, viewState,
      clientMethods, render, store);
    root.appendChild(controlPanel);

    var signalsPanel = h('section', 'panel');
    renderSignalPanel(signalsPanel, viewState.signals);
    root.appendChild(signalsPanel);
  }

  function loadSync() {
    var jobs = [
      clientMethods.getSession(sessionId),
      clientMethods.getSessionEvents(sessionId, 100),
      clientMethods.getSessionSignals(sessionId),
    ];
    if (typeof clientMethods.getStatus === 'function' &&
        typeof store.setStatus === 'function') {
      jobs.push(clientMethods.getStatus().then(function (status) {
        store.setStatus(status);
        return status;
      }).catch(function () {
        return null;
      }));
    }
    Promise.all(jobs)
      .then(function (results) {
        if (disposed) {
          return;
        }
        var detail = results[0] || null;
        var events = results[1] || null;
        var signals = results[2] || null;
        if (detail && detail.session) {
          viewState.session = detail.session;
        }
        viewState.events = (events && Array.isArray(events.events)) ?
          events.events : [];
        viewState.signals = (signals && Array.isArray(signals.signals)) ?
          signals.signals : [];
        viewState.loading = false;
        viewState.error = null;
        var flags = controlFeatures(store);
        var machineId = viewState.session && viewState.session.machine_id;
        if (flags.apply_local_profile && typeof machineId === 'string' &&
            machineId && typeof clientMethods.getMachine === 'function') {
          return clientMethods.getMachine(machineId).then(function (machine) {
            if (disposed) {
              return;
            }
            if (typeof store.setMachine === 'function') {
              store.setMachine(machine);
            }
            var current = machine && machine.current;
            viewState.localProfiles = (current && Array.isArray(current.local_profiles))
              ? current.local_profiles : [];
            render();
          }).catch(function () {
            if (disposed) {
              return;
            }
            viewState.localProfiles = [];
            render();
          });
        }
        render();
      })
      .catch(function (err) {
        if (disposed) {
          return;
        }
        viewState.loading = false;
        viewState.error = (err && err.detail) ? err.detail : '未知错误';
        render();
      });
  }

  // 首帧即渲染（loading 态），随后异步拉取数据。
  render();
  loadSync();

  return function teardown() {
    disposed = true;
    unsubscribe();
    removeAllChildren(root);
  };
}