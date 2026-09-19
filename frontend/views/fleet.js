/* frontend/views/fleet.js — Fleet 总览视图 (Awesome UI Kit 现代化重构)
 *
 * 核心特性：
 * - 纯矢量 Tabler-style 图标 (DOM-safe createElementNS)
 * - 响应式 KPI 状态仪表盘 + 优雅资源进度条 (Load/Memory/Disk)
 * - 紧凑 Agent 矩阵与实时事件审计流 (带有界保护)
 * - 维持原有契约与类名选择器，支持安全 DOM 渲染 (防 XSS)
 */

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

/* -- 机器卡片渲染 ------------------------------------------------------------ */

function renderMachineCard(grid, machine) {
  var card = h('div', 'card');
  var name = (typeof machine.machine === 'string' && machine.machine)
    ? machine.machine : 'unknown';

  var h2 = h('h2');
  var dot = h('span', 'dot' + (machine.online ? ' on' : ' off'));
  h2.appendChild(dot);

  var nameLink = h('a', null, name);
  nameLink.setAttribute('href', pagePath('machine', name));
  h2.appendChild(nameLink);

  if (machine.has_hermes) {
    var hermesBadge = h('span', 'badge-hermes', 'Hermes ' + fmtValue(machine.hermes_state));
    h2.appendChild(hermesBadge);
  }
  card.appendChild(h2);

  var meta = h('div', 'meta', fmtValue(machine.desc));
  card.appendChild(meta);

  if (!machine.online) {
    var errBox = h('div', 'err');
    var alertIconSpan = h('span', null);
    alertIconSpan.appendChild(uiIcon('alert-triangle', { size: 14, className: 'inline-icon' }));
    errBox.appendChild(alertIconSpan);
    errBox.appendChild(document.createTextNode(' ' + fmtValue(machine.error || '节点离线或通信超时')));
    card.appendChild(errBox);
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
  var title = h('div', 'meta', '已部署 Agent 连接器:');
  card.appendChild(title);

  rows.forEach(function (summary) {
    var row = h('div', 'agent-row');
    var kSpan = h('span', 'k', fmtValue(summary.type));
    row.appendChild(kSpan);

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
    var stSpan = h('span', statusClass, statusText);
    row.appendChild(stSpan);
    card.appendChild(row);
  });
}

function renderSystemStat(card, machine) {
  var sys = (machine.system && typeof machine.system === 'object')
    ? machine.system : {};
  var stat = h('div', 'stat');

  function statItem(label, val) {
    var sp = h('span', null, label + ': ');
    var b = h('b', null, fmtValue(val));
    sp.appendChild(b);
    return sp;
  }

  stat.appendChild(statItem('Agents', machine.agent_count));
  stat.appendChild(statItem('Load', sys.load));
  stat.appendChild(statItem('Disk', sys.disk_used_pct));
  stat.appendChild(statItem('Uptime', sys.uptime));

  card.appendChild(stat);

  // 渲染磁盘进度条
  if (sys.disk_used_pct) {
    var pctNum = parseInt(String(sys.disk_used_pct).replace('%', ''), 10);
    if (!isNaN(pctNum) && pctNum >= 0 && pctNum <= 100) {
      var barWrap = h('div', 'bar');
      var barInner = h('i', pctNum > 85 ? 'danger' : (pctNum > 70 ? 'warn' : ''));
      barInner.style.width = pctNum + '%';
      barWrap.appendChild(barInner);
      card.appendChild(barWrap);
    }
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
    item.appendChild(h('div', 't', fmtValue(ev.ts)));

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
    textWrap.appendChild(document.createTextNode(' ' + prefix + ': ' + fmtValue(ev.event)));
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
  var bar = h('div', 'healthbar');

  function makeHealthCol(cls, label, value, iconName) {
    var item = h('div', 'healthbar-stat-item ' + cls);
    var lbl = h('span', 'hb-label');
    var iconSlot = h('span', null);
    iconSlot.appendChild(uiIcon(iconName, { size: 14, className: 'hb-icon' }));
    lbl.appendChild(iconSlot);
    lbl.appendChild(document.createTextNode(label));

    var val = h('span', 'hb-value');
    val.appendChild(h('b', null, String(value)));

    item.appendChild(lbl);
    item.appendChild(val);
    return item;
  }

  bar.appendChild(makeHealthCol('hb-ok', '在线节点', counts.online, 'server'));
  bar.appendChild(makeHealthCol('hb-bad', '异常 / 离线', counts.alerts, 'alert-triangle'));
  bar.appendChild(makeHealthCol('hb-total', '已注册集群', counts.total, 'globe'));
  bar.appendChild(makeHealthCol('hb-warn', '活跃 Agents', counts.agents, 'box'));

  return bar;
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
      errBox.appendChild(document.createTextNode('需要操作员令牌。'));
      var unlockBtn = h('button', 'btn-link-action', '输入令牌');
      unlockBtn.type = 'button';
      unlockBtn.addEventListener('click', function () {
        if (typeof window !== 'undefined' && typeof window.openAuthModal === 'function') {
          window.openAuthModal();
        }
      });
      errBox.appendChild(unlockBtn);
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

  var unsubscribe = store.subscribe(function () {
    render();
  });

  function render() {
    if (disposed) {
      return;
    }
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
    mainColumn.appendChild(renderSessionStrip(viewState.sessions, viewState.sessionError));

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
    var asideTitle = h('h3');
    var actIcon = h('span', null);
    actIcon.appendChild(uiIcon('activity', { size: 15 }));
    asideTitle.appendChild(actIcon);
    asideTitle.appendChild(document.createTextNode(' 实时事件流'));
    aside.appendChild(asideTitle);

    renderEventList(aside, eventSnapshot(store, viewState));
    layout.appendChild(aside);

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
        clientMethods.listSessions({ limit: 50 })
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
      viewState.error = (err && err.detail) ? err.detail : '网络错误';
      render();
    });

  return function teardown() {
    disposed = true;
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
