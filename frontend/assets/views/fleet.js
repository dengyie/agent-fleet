/* Overview renders current reachability and keeps operational detail available on demand. */

import { pagePath } from '../routes.js';
import { uiIcon } from '../routes.js';

var MSG_LOADING = '正在同步集群节点…';
var MSG_NO_DATA = '当前集群暂无已注册机器节点';
var MSG_NO_SESSIONS = '暂无活跃会话';
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

function fmtValue(v) {
  if (v === null || v === undefined || v === '') {
    return '—';
  }
  return String(v);
}

function removeAllChildren(el) {
  while (el.firstChild) {
    el.removeChild(el.firstChild);
  }
}

/* -- 事件流列表（有界展示） -------------------------------------------------- */

function renderEventList(host, events) {
  var list = Array.isArray(events) ? events : [];
  if (list.length === 0) {
    host.appendChild(h('div', 'meta', '暂无实时审计事件'));
    return;
  }
  var bound = list.slice(-MAX_EVENT_RENDER);
  bound.forEach(function (ev) {
    var item = h('div', 'event-item' + eventClass(ev));
    var timestamp = typeof ev.ts === 'number' ? new Date(ev.ts * 1000) : null;
    var time = h('time', 't', timestamp && Number.isFinite(timestamp.getTime())
      ? timestamp.toLocaleString('zh-CN', {hour12: false}) : fmtValue(ev.ts));
    if (timestamp && Number.isFinite(timestamp.getTime())) time.setAttribute('datetime', timestamp.toISOString());
    item.appendChild(time);

    var prefix = (typeof ev.machine === 'string' && ev.machine)
      ? ev.machine : 'fleet';

    var textWrap = h('div', null);
    var iconSpan = h('span', null);
    if (ev.event === 'state_changed') {
      iconSpan.appendChild(uiIcon('check-circle', { size: 12, className: 'inline-icon' }));
    } else if (ev.event === 'scan_error') {
      iconSpan.appendChild(uiIcon('circle-x', { size: 12, className: 'inline-icon' }));
    } else {
      iconSpan.appendChild(uiIcon('activity', { size: 12, className: 'inline-icon' }));
    }
    textWrap.appendChild(iconSpan);
    textWrap.appendChild(document.createTextNode(' ' + prefix + ': ' + fmtValue({state_changed: '状态更新', scan_error: '采集失败', scan_complete: '采集完成'}[ev.event] || ev.event)));
    item.appendChild(textWrap);

    host.appendChild(item);
  });
}

function eventClass(ev) {
  if (ev.event === 'state_changed') return ' ok';
  if (ev.event === 'scan_error') return ' bad';
  return '';
}

/* -- 健康度大盘指标卡 (Health KPI Bar) ---------------------------------------- */

function countByOnline(machines) {
  var total = 0;
  var online = 0;
  var totalAgents = 0;
  if (Array.isArray(machines)) {
    total = machines.length;
    machines.forEach(function (m) {
      if (m && m.online) {
        online += 1;
        totalAgents += (Number(m.agent_count) || 0);
      }
    });
  }
  return { total: total, online: online, alerts: total - online, agents: totalAgents };
}

function renderHealthBar(rows) {
  var counts = countByOnline(rows);
  var bar = h('div', 'healthbar fleet-summary');
  var heading = h('div', 'fleet-summary-heading');
  var state = counts.alerts ? '需要关注' : (counts.total ? '运行正常' : '等待节点连接');
  var stateIcon = uiIcon(counts.alerts ? 'alert-triangle' : 'check-circle', {size: 18});
  heading.appendChild(h('span', 'fleet-summary-state ' + (counts.alerts ? 'warning' : 'healthy'), state));
  heading.querySelector('span').prepend(stateIcon);
  heading.appendChild(h('span', 'fleet-summary-caption', counts.alerts ? counts.alerts + ' 台机器离线' : '节点与 Agent 状态持续同步'));
  bar.appendChild(heading);
  var stats = h('div', 'fleet-summary-stats');
  for (var item of [
    ['在线机器', counts.online, 'server'],
    ['已登记机器', counts.total, 'monitor'],
    ['活跃 Agent', counts.agents, 'cpu'],
  ]) {
    var stat = h('div', 'fleet-summary-stat');
    stat.appendChild(uiIcon(item[2], {size: 16})); stat.appendChild(h('span', null, item[0]));
    stat.appendChild(h('strong', null, String(item[1]))); stats.appendChild(stat);
  }
  bar.appendChild(stats);
  return bar;
}

function renderMachineOverview(machine) {
  var row = h('div', 'fleet-machine-row');
  var main = h('div', 'fleet-machine-main');
  main.appendChild(h('span', 'dot' + (machine.online ? ' on' : ' off')));
  main.appendChild(h('a', null, machine.machine || 'unknown'));
  main.appendChild(h('span', 'fleet-machine-description', machine.online ? (machine.desc || '在线') : (machine.error || '离线')));
  row.appendChild(main);
  var data = h('div', 'fleet-machine-meta');
  if (machine.online) {
    data.appendChild(h('span', null, 'Agent ' + fmtValue(machine.agent_count || 0)));
    var load = machine.system && machine.system.load;
    if (load !== null && load !== undefined && load !== '') data.appendChild(h('span', null, '负载 ' + fmtValue(load)));
    if (machine.client && machine.client.version) data.appendChild(h('span', null, '版本 ' + machine.client.version));
  }
  row.appendChild(data);
  var details = h('a', 'fleet-machine-link'); details.href = pagePath('machine', machine.machine);
  details.setAttribute('aria-label', machine.machine + ' 节点详情'); details.title = '节点详情';
  details.appendChild(uiIcon('arrow-right', {size: 17})); row.appendChild(details);
  return row;
}

/* -- 活跃会话带 (Active Sessions) -------------------------------------------- */

function renderSessionStrip(sessions, sessionError) {
  var panel = h('div', 'panel session-strip');
  var header = h('h3');
  var iconSlot = h('span', null);
  iconSlot.appendChild(uiIcon('terminal', { size: 16 }));
  header.appendChild(iconSlot);
  header.appendChild(document.createTextNode(' 活跃纳管会话'));
  panel.appendChild(header);

  if (sessionError) {
    var errBox = h('div', 'err');
    if (sessionError.status === 401) {
      // 诚实契约：operator 身份由部署边缘（CF Access 头）与 hub 配置授予，
      // 前端无凭据可补救 —— 只陈述事实，不提供假解锁入口。
      errBox.appendChild(document.createTextNode(
        '会话控制面需要 operator 身份（CF Access / hub 配置），当前请求未获授权。'));
    } else {
      errBox.appendChild(document.createTextNode('会话列表加载失败：' +
        fmtValue(sessionError.detail || sessionError.code || sessionError.message)));
    }
    panel.appendChild(errBox);
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

/* -- 挂载主函数 -------------------------------------------------------------- */

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
    initialEvents: null,
    sessions: [],
    sessionError: null,
  };
  var openDisclosures = new Set();

  function onStoreChange() { render(); }
  var unsubscribe = store.subscribe(onStoreChange);

  function render() {
    if (disposed) {
      return;
    }
    openDisclosures = new Set(Array.from(root.querySelectorAll('.fleet-secondary-detail[open]'))
      .map(function (details) { return details.getAttribute('data-disclosure'); }));
    removeAllChildren(root);
    if (viewState.error) {
      var errBox = h('div', 'err');
      var warnIcon = h('span', null);
      warnIcon.appendChild(uiIcon('alert-triangle', { size: 16 }));
      errBox.appendChild(warnIcon);
      errBox.appendChild(document.createTextNode(' 加载失败：' + fmtValue(viewState.error)));
      root.appendChild(errBox);
      return;
    }
    if (viewState.loading) {
      var loadBox = h('div', 'meta');
      var spinIcon = h('span', 'topbar-action-btn loading');
      spinIcon.appendChild(uiIcon('refresh', { size: 16 }));
      loadBox.appendChild(spinIcon);
      loadBox.appendChild(document.createTextNode(' ' + MSG_LOADING));
      root.appendChild(loadBox);
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
    var nodesHeading = h('div', 'fleet-section-heading');
    nodesHeading.appendChild(h('h2', null, '计算节点'));
    var allNodes = h('a', null, '查看全部'); allNodes.href = '/settings/nodes'; nodesHeading.appendChild(allNodes);
    mainColumn.appendChild(nodesHeading);
    if (rows.length === 0) {
      mainColumn.appendChild(h('div', 'fleet-empty-state', MSG_NO_DATA));
    } else {
      var list = h('div', 'fleet-machine-list');
      rows.forEach(function (machine) { list.appendChild(renderMachineOverview(machine)); });
      mainColumn.appendChild(list);
    }
    var sessionDetails = h('details', 'fleet-secondary-detail');
    sessionDetails.setAttribute('data-disclosure', 'sessions');
    sessionDetails.open = openDisclosures.has('sessions');
    sessionDetails.appendChild(h('summary', null, '活跃会话 (' + viewState.sessions.length + ')'));
    sessionDetails.appendChild(renderSessionStrip(viewState.sessions, viewState.sessionError));
    mainColumn.appendChild(sessionDetails);
    var eventDetails = h('details', 'fleet-secondary-detail');
    eventDetails.setAttribute('data-disclosure', 'events');
    eventDetails.open = openDisclosures.has('events');
    eventDetails.appendChild(h('summary', null, '最近系统事件'));
    var eventList = h('div', 'event-stream fleet-event-list');
    renderEventList(eventList, eventSnapshot(store, viewState));
    eventDetails.appendChild(eventList);
    mainColumn.appendChild(eventDetails);
    layout.appendChild(mainColumn);


    root.appendChild(layout);
  }

  function eventSnapshot(targetStore, state) {
    var live = targetStore.getState().events;
    if (Array.isArray(live) && live.length > 0) {
      return live.slice(-MAX_EVENT_BUFFER);
    }
    if (Array.isArray(state.initialEvents) && state.initialEvents.length > 0) {
      return state.initialEvents.slice(-MAX_EVENT_BUFFER);
    }
    return [];
  }

  // 订阅后立即渲染一次，让 loading 态先绘制出来。
  render();

  Promise.resolve()
    .then(function () {
      return Promise.all([
        clientMethods.getStatus(),
        clientMethods.getEvents(50),
      ]);
    })
    .then(function (results) {
      if (disposed) return;
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
        clientMethods.listSessions({ limit: 50, active: true })
          .then(function (res) {
            if (disposed) return;
            viewState.sessions = (res && Array.isArray(res.sessions)) ? res.sessions : [];
            viewState.sessionError = null;
            render();
          })
          .catch(function (err) {
            if (disposed) return;
            viewState.sessions = [];
            // 保留完整 ApiError：status / code / detail 供渲染层判定
            viewState.sessionError = err || new Error('会话列表加载失败');
            render();
          });
      }
    })
    .catch(function (err) {
      if (disposed) return;
      viewState.loading = false;
      viewState.error = err && (err.detail || err.code || err.message)
        ? (err.detail || err.code || err.message) : '网络错误';
      render();
    });

  function teardown() {
    disposed = true;
    if (unsubscribe) unsubscribe();
    unsubscribe = null;
    removeAllChildren(root);
  }
  teardown.suspend = function () {
    if (unsubscribe) unsubscribe();
    unsubscribe = null;
  };
  teardown.resume = function () {
    if (disposed || unsubscribe) return;
    unsubscribe = store.subscribe(onStoreChange);
    render();
  };
  return teardown;
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
