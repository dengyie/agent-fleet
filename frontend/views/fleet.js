/* frontend/views/fleet.js — Fleet 总览视图（Task 15）
 *
 * 只接收已通过 contracts 校验的数据，使用 store 订阅与注入的 client 方法；
 * 绝不直接发 HTTP、原生流式事件或访问 Flask 模板对象。所有动态文本一律经
 * textContent / createTextNode 写入，不做 HTML 字符串拼接（反而用建元素与
 * 属性赋值 API）；导航一律用 pagePath("machine", name) 编码生成。
 *
 * 支持四种显式 UI 状态：loading / error / empty / ready（健康计数 + 机器卡片
 * + 脱敏 agent 摘要 + 系统指标 + 有界事件列表）。
 */

import { pagePath } from '../routes.js';

var MSG_LOADING = '加载中…';
var MSG_NO_DATA = '暂无机器';
var MSG_NO_SESSIONS = '暂无会话';
var MAX_EVENT_RENDER = 50;   // 视图展示有界：最多渲染最近 50 条事件
var MAX_EVENT_BUFFER = 100;  // 事件漏斗总容量上限
var MAX_SESSION_STRIP = 20;

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

function kvSpan(label, value) {
  var span = h('span', null, label + ': ');
  var b = h('b', null, String(value));
  span.appendChild(b);
  return span;
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

/* -- 机器卡片 ---------------------------------------------------------------- */

function renderMachineCard(grid, machine) {
  var card = h('div', 'card');
  var name = (typeof machine.machine === 'string' && machine.machine)
    ? machine.machine : 'unknown';

  var h2 = h('h2');
  h2.appendChild(h('span', 'dot' + (machine.online ? ' on' : ' off')));
  var nameLink = h('a', null, name);
  // 机器名导航必须是编码后的 pagePath("machine", name)
  nameLink.setAttribute('href', pagePath('machine', name));
  h2.appendChild(nameLink);
  if (machine.has_hermes) {
    h2.appendChild(h('span', 'badge-hermes',
      'Hermes ' + fmtValue(machine.hermes_state)));
  }
  card.appendChild(h2);

  card.appendChild(h('div', 'meta', fmtValue(machine.desc)));

  if (!machine.online) {
    card.appendChild(h('div', 'err', '⚠️ ' + fmtValue(machine.error)));
  } else {
    renderAgentSummaries(card, machine.agent_summaries);
    renderSystemStat(card, machine);
  }

  grid.appendChild(card);
}

function renderAgentSummaries(card, summaries) {
  var rows = Array.isArray(summaries) ? summaries : [];
  if (rows.length === 0) {
    return;
  }
  card.appendChild(h('div', 'meta', '📦 agent 连接器'));
  rows.forEach(function (summary) {
    var row = h('div', 'agent-row');
    row.appendChild(h('span', 'k', fmtValue(summary.type)));
    var statusClass;
    var statusText;
    if (summary.status === 'ok') {
      statusClass = 'st-ok';
      statusText = '● ok';
    } else if (summary.status === 'absent') {
      statusClass = 'st-absent';
      statusText = '○ 未部署';
    } else {
      statusClass = 'st-err';
      statusText = '● ' + fmtValue(summary.detail);
    }
    row.appendChild(h('span', statusClass, statusText));
    card.appendChild(row);
  });
}

function renderSystemStat(card, machine) {
  var sys = (machine.system && typeof machine.system === 'object')
    ? machine.system : {};
  var stat = h('div', 'stat');
  stat.appendChild(kvSpan('📦 agent', fmtValue(machine.agent_count)));
  stat.appendChild(kvSpan('📈 负载', fmtValue(sys.load)));
  stat.appendChild(kvSpan('💾 磁盘', fmtValue(sys.disk_used_pct)));
  stat.appendChild(kvSpan('⏱️ 运行', fmtValue(sys.uptime)));
  card.appendChild(stat);
}

/* -- 事件列表（有界） --------------------------------------------------------- */

function renderEventList(host, events) {
  var list = Array.isArray(events) ? events : [];
  if (list.length === 0) {
    host.appendChild(h('div', 'meta', '暂无事件'));
    return;
  }
  var bound = list.slice(-MAX_EVENT_RENDER);
  bound.forEach(function (ev) {
    var item = h('div', 'event-item' + eventClass(ev));
    item.appendChild(h('div', 't', fmtValue(ev.ts)));
    var prefix = (typeof ev.machine === 'string' && ev.machine)
      ? ev.machine : 'fleet';
    item.appendChild(h('div', null,
      eventIcon(ev) + ' ' + prefix + ': ' + fmtValue(ev.event)));
    host.appendChild(item);
  });
}

function eventIcon(ev) {
  if (ev.event === 'state_changed') {
    return '✓';
  }
  if (ev.event === 'scan_error') {
    return '✗';
  }
  return '•';
}

function eventClass(ev) {
  if (ev.event === 'state_changed') {
    return ' ok';
  }
  if (ev.event === 'scan_error') {
    return ' bad';
  }
  return '';
}

/* -- 健康计数 ------------------------------------------------------------------ */

function countByOnline(machines) {
  var total = 0;
  var online = 0;
  if (Array.isArray(machines)) {
    total = machines.length;
    machines.forEach(function (m) {
      if (m && m.online) {
        online += 1;
      }
    });
  }
  return { total: total, online: online, alerts: total - online };
}

function renderHealthBar(rows) {
  var counts = countByOnline(rows);
  var bar = h('div', 'healthbar');
  bar.appendChild(healthItem('hb-ok', '在线', counts.online));
  bar.appendChild(healthItem('hb-bad', '告警', counts.alerts));
  bar.appendChild(healthItem('hb-total', '机器', counts.total));
  return bar;
}

function healthItem(className, label, value) {
  var span = h('span', className);
  span.appendChild(document.createTextNode(label + ' '));
  span.appendChild(h('b', null, String(value)));
  return span;
}

function renderSessionStrip(sessions, errorText) {
  var panel = h('div', 'panel session-strip');
  panel.appendChild(h('h3', null, '活跃会话'));
  if (errorText) {
    panel.appendChild(h('div', 'err', String(errorText)));
  }
  var rows = Array.isArray(sessions) ? sessions : [];
  var managed = 0;
  var unmanaged = 0;
  rows.forEach(function (session) {
    if (session && session.managed === true) {
      managed += 1;
    } else {
      unmanaged += 1;
    }
  });
  var counts = h('div', 'meta');
  counts.appendChild(document.createTextNode(
    '受管 ' + String(managed) + ' · 非受管 ' + String(unmanaged)));
  panel.appendChild(counts);
  if (rows.length === 0) {
    panel.appendChild(h('div', 'meta', MSG_NO_SESSIONS));
    return panel;
  }
  var list = h('div', 'session-strip-list');
  rows.slice(0, MAX_SESSION_STRIP).forEach(function (session) {
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
    if (typeof session.machine_id === 'string' && session.machine_id) {
      row.appendChild(h('span', 'meta', session.machine_id));
    }
    row.appendChild(h('span', 'meta', session.managed ? '受管' : '非受管'));
    list.appendChild(row);
  });
  panel.appendChild(list);
  return panel;
}

/* -- Fleet 视图 ---------------------------------------------------------------- */

/**
 * 挂载 Fleet 总览视图。source 从 store 读取（status / machines / events /
 * connection）；client 用于首屏与刷新（getStatus / getEvents）。
 * 返回 () => () 卸载函数。
 */
export function mountFleet(root, store, client) {
  if (!root || !store) {
    return noop;
  }
  var clientMethods = (client && typeof client === 'object') ? client : {};
  if (typeof clientMethods.getStatus !== 'function' ||
      typeof clientMethods.getEvents !== 'function') {
    return noop;
  }

  var disposed = false;

  var viewState = {
    loading: true,
    error: null,
    initialEvents: null,   // 首屏 getEvents 历史（SSE 事件到达前兜底展示）
    sessions: [],
    sessionError: null,
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
      root.appendChild(h('div', 'err', '⚠️ 加载失败：' + fmtValue(viewState.error)));
      return;
    }
    if (viewState.loading) {
      root.appendChild(h('div', 'meta', MSG_LOADING));
      return;
    }
    renderReady();
  }

  function renderReady() {
    var layout = h('div', 'fleet-layout');
    var mainColumn = h('section');
    var status = store.getState().status;
    var rows = listMachines(store.getState().machines, status);
    mainColumn.appendChild(renderHealthBar(rows));
    mainColumn.appendChild(renderSessionStrip(viewState.sessions,
      viewState.sessionError));

    if (rows.length === 0) {
      mainColumn.appendChild(h('div', 'meta', MSG_NO_DATA));
    } else {
      var grid = h('div', 'grid');
      rows.forEach(function (machine) {
        renderMachineCard(grid, machine);
      });
      mainColumn.appendChild(grid);
    }

    layout.appendChild(mainColumn);

    var aside = h('aside', 'event-stream');
    aside.appendChild(h('h3', null, '实时事件'));
    renderEventList(aside, eventSnapshot(store, viewState));
    layout.appendChild(aside);

    root.appendChild(layout);
  }

  function eventSnapshot(targetStore, state) {
    // 优先展示 SSE 实时事件；SSE 未到达前用首屏拉取的历史兜底。
    var live = targetStore.getState().events;
    if (Array.isArray(live) && live.length > 0) {
      return live.slice(-MAX_EVENT_BUFFER);
    }
    if (Array.isArray(state.initialEvents) && state.initialEvents.length > 0) {
      return state.initialEvents.slice(-MAX_EVENT_BUFFER);
    }
    return [];
  }

  // 订阅后立即渲染一次，让 loading 态先行绘制（异步请求尚未返回前）。
  render();

  // 首屏：并行拉取 status + events（任一失败进入显式 error 状态）。
  Promise.resolve()
    .then(function () {
      return Promise.all([
        clientMethods.getStatus(),
        clientMethods.getEvents(50),
      ]);
    })
    .then(function (results) {
      if (disposed) {
        return;
      }
      var status = results[0];
      var events = results[1];
      if (status) {
        store.setStatus(status);
      }
      if (events && Array.isArray(events.events)) {
        viewState.initialEvents = events.events;
      }
      viewState.loading = false;
      viewState.error = null;
      render();
      if (typeof clientMethods.listSessions === 'function') {
        clientMethods.listSessions({ limit: 50 })
          .then(function (res) {
            if (disposed) {
              return;
            }
            viewState.sessions = (res && Array.isArray(res.sessions)) ?
              res.sessions : [];
            viewState.sessionError = null;
            render();
          })
          .catch(function (err) {
            if (disposed) {
              return;
            }
            viewState.sessions = [];
            viewState.sessionError = (err && (err.detail || err.code)) ?
              String(err.detail || err.code) : '会话列表加载失败';
            render();
          });
      }
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
    disposed = true;   // 护栏：此后 render / 异步回调一律 no-op
    unsubscribe();
    removeAllChildren(root);
  };
}

function noop() {}

function listMachines(machines, status) {
  if (machines) {
    var keys = Object.keys(machines);
    if (keys.length > 0) {
      return keys.map(function (k) {
        return machines[k];
      });
    }
  }
  return (status && Array.isArray(status.machines)) ? status.machines : [];
}
