/* Service monitoring and explicitly gated actions. Dynamic values are text nodes. */
import { uiIcon, pagePath } from '../routes.js';

var MAX_SERVICES = 100;
var MAX_EVIDENCE = 5;
var MAX_INCIDENTS = 50;
var MAX_APPROVAL_POLLS = 5;
var APPROVAL_POLL_DELAY_MS = 600;
var DIMENSIONS = [
  ['host_reachability', '主机可达性'],
  ['process_state', '进程状态'],
  ['application_health', '应用健康'],
  ['external_availability', '外部可用性'],
];
var STATUS_LABELS = {
  healthy: '健康',
  degraded: '降级',
  unhealthy: '异常',
  unknown: '未知',
  stale: '过期',
  unsupported: '不支持',
};
var STATUS_CLASSES = {
  healthy: 'monitoring-status-ok',
  degraded: 'monitoring-status-warn',
  unhealthy: 'monitoring-status-bad',
  unknown: 'monitoring-status-unknown',
  stale: 'monitoring-status-warn',
  unsupported: 'monitoring-status-unknown',
};

function el(tag, className, text) {
  var node = document.createElement(tag);
  if (className) node.setAttribute('class', className);
  if (text !== undefined) node.textContent = text;
  return node;
}

function clear(node) {
  while (node && node.firstChild) node.removeChild(node.firstChild);
}

function safeText(value, fallback) {
  if (value === null || value === undefined || value === '') return fallback || '—';
  return String(value).slice(0, 160);
}

function statusLabel(value) {
  return STATUS_LABELS[value] || '未知';
}

function statusClass(value) {
  return STATUS_CLASSES[value] || STATUS_CLASSES.unknown;
}

function statusBadge(value) {
  return el('span', 'monitoring-status ' + statusClass(value), statusLabel(value));
}

function formatTime(value) {
  if (typeof value !== 'number' || !isFinite(value) || value <= 0) return '—';
  try {
    return new Date(value * 1000).toISOString().slice(0, 19).replace('T', ' ');
  } catch (error) {
    return '—';
  }
}

function errorText(error) {
  return safeText(error && (error.detail || error.message || error.code), '平台请求失败');
}

function validateServiceRows(data) {
  if (!data || !Array.isArray(data.services)) throw new Error('服务列表格式无效');
  var rows = data.services.slice(0, 100);
  for (var i = 0; i < rows.length; i += 1) {
    if (!rows[i] || typeof rows[i] !== 'object' ||
        !rows[i].service || typeof rows[i].service !== 'object') {
      throw new Error('服务列表格式无效');
    }
  }
  return rows;
}

function validateIncidents(data) {
  if (!data || !Array.isArray(data.incidents)) throw new Error('事件列表格式无效');
  var rows = data.incidents.slice(0, 50);
  for (var i = 0; i < rows.length; i += 1) {
    if (!rows[i] || typeof rows[i] !== 'object') throw new Error('事件列表格式无效');
  }
  return rows;
}

function renderState(host, className, text) {
  clear(host);
  host.appendChild(el('div', className, text));
}

function renderServiceRow(host, row, selectedId, onSelect) {
  var service = row.service;
  var health = row.health && typeof row.health === 'object' ? row.health : {};
  var serviceId = safeText(service.service_id, 'unknown');
  var button = document.createElement('button');
  button.type = 'button';
  button.setAttribute('class', 'monitoring-service-row' +
    (serviceId === selectedId ? ' selected' : ''));
  button.appendChild(el('span', 'monitoring-service-copy'));
  var copy = button.firstChild;
  copy.appendChild(el('strong', null, safeText(service.name, serviceId)));
  copy.appendChild(el('span', 'monitoring-service-meta',
    safeText(service.adapter, 'adapter') + ' · ' + safeText(service.node_id, 'node')));
  button.appendChild(statusBadge(health.overall));
  button.appendChild(uiIcon('chevron-right', { size: 15, className: 'monitoring-row-icon' }));
  button.addEventListener('click', function () { onSelect(serviceId); });
  host.appendChild(button);
}

function renderDimensions(host, health) {
  var dimensions = health && health.dimensions && typeof health.dimensions === 'object'
    ? health.dimensions : {};
  var list = el('div', 'monitoring-dimensions');
  DIMENSIONS.forEach(function (item) {
    var row = el('div', 'monitoring-dimension');
    var state = dimensions[item[0]] && typeof dimensions[item[0]] === 'object'
      ? dimensions[item[0]] : {};
    row.appendChild(el('span', null, item[1]));
    var stateWrap = el('span', 'monitoring-dimension-state');
    stateWrap.appendChild(statusBadge(state.state));
    stateWrap.appendChild(el('small', null, safeText(state.freshness, 'missing')));
    row.appendChild(stateWrap);
    list.appendChild(row);
  });
  host.appendChild(list);
}

function renderEvidence(host, evidence) {
  var rows = Array.isArray(evidence) ? evidence.slice(0, 5) : [];
  host.appendChild(el('h3', null, '最近证据'));
  if (!rows.length) {
    host.appendChild(el('div', 'monitoring-empty', '暂无健康证据'));
    return;
  }
  var list = el('div', 'monitoring-evidence-list');
  rows.forEach(function (item) {
    if (!item || typeof item !== 'object') return;
    var row = el('div', 'monitoring-evidence-row');
    row.appendChild(el('span', 'monitoring-evidence-dimension', safeText(item.dimension, 'dimension')));
    row.appendChild(statusBadge(item.state));
    row.appendChild(el('span', 'monitoring-evidence-source', safeText(item.source, 'source')));
    row.appendChild(el('time', 'monitoring-evidence-time', formatTime(item.observed_at)));
    list.appendChild(row);
  });
  host.appendChild(list);
}

function boundedAction(value) {
  return value === 'inspect' || value === 'restart' ? value : null;
}

function renderCommandMeta(host, command) {
  if (!command || typeof command !== 'object') return;
  var row = el('div', 'monitoring-action-status');
  row.appendChild(el('span', null, '已提交'));
  row.appendChild(el('span', 'monitoring-action-meta',
    safeText(command.action, 'service action') + ' · ' +
    safeText(command.status, 'queued')));
  host.appendChild(row);
}

function renderActionPanel(host, service, client, onRefresh) {
  clear(host);
  host.appendChild(el('h3', null, '受控动作'));
  var allowed = Array.isArray(service.allowed_actions)
    ? service.allowed_actions.map(boundedAction).filter(Boolean).slice(0, 2) : [];
  var notice = el('div', 'monitoring-action-notice');
  host.appendChild(notice);
  var buttons = el('div', 'monitoring-action-buttons');
  host.appendChild(buttons);
  if (!allowed.length) {
    notice.textContent = '当前服务未开放受控动作';
    return;
  }
  function setNotice(text, className) {
    notice.setAttribute('class', 'monitoring-action-notice' + (className ? ' ' + className : ''));
    notice.textContent = text;
  }
  function wait(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }
  function commandAccepted(response) {
    var command = response && response.command;
    if (!command || typeof command !== 'object') return false;
    renderCommandMeta(host, command);
    if (typeof onRefresh === 'function') {
      Promise.resolve(onRefresh()).catch(function () {});
    }
    return true;
  }
  async function pollApproval(grantId) {
    for (var attempt = 0; attempt < MAX_APPROVAL_POLLS; attempt += 1) {
      if (attempt) await wait(APPROVAL_POLL_DELAY_MS);
      var response = await client.getPlatformApproval(grantId);
      var grant = response && response.grant;
      if (!grant || typeof grant !== 'object') throw new Error('审批状态格式无效');
      if (grant.state === 'consumed') {
        setNotice('审批已执行', 'ok');
        return response;
      }
      if (grant.state === 'rejected' || grant.state === 'expired') {
        setNotice(grant.state === 'rejected' ? '审批已拒绝' : '审批已过期', 'bad');
        return response;
      }
    }
    setNotice('审批状态暂未收敛，请稍后刷新', 'warn');
    return null;
  }
  async function decide(grantId, decision, card) {
    var approval_decision = decision;
    card.querySelectorAll('button').forEach(function (button) { button.disabled = true; });
    try {
      var response = await client.decidePlatformApproval(grantId, approval_decision);
      if (approval_decision === 'reject') {
        setNotice('审批已拒绝', 'bad');
        return;
      }
      if (!commandAccepted(response)) await pollApproval(grantId);
    } catch (error) {
      setNotice('动作接口不可用，服务监控仍可用', 'bad');
    }
  }
  function showApproval(response) {
    var grant = response && response.grant;
    if (!grant || typeof grant !== 'object' || !grant.grant_id) {
      setNotice('审批响应格式无效', 'bad');
      return;
    }
    setNotice('等待明确审批决定', 'warn');
    var card = el('div', 'monitoring-approval-card');
    card.appendChild(el('span', null, '授权 ' + safeText(grant.grant_id, 'unknown') + ' · 一次性'));
    var approve = document.createElement('button');
    approve.type = 'button'; approve.className = 'button-primary'; approve.textContent = '批准重启';
    approve.addEventListener('click', function () { decide(grant.grant_id, 'approve', card); });
    var reject = document.createElement('button');
    reject.type = 'button'; reject.className = 'button-secondary'; reject.textContent = '拒绝';
    reject.addEventListener('click', function () { decide(grant.grant_id, 'reject', card); });
    card.appendChild(approve); card.appendChild(reject); host.appendChild(card);
  }
  allowed.forEach(function (action) {
    var button = document.createElement('button');
    button.type = 'button';
    button.className = action === 'restart' ? 'button-danger' : 'button-secondary';
    button.textContent = action === 'restart' ? '请求重启' : '检查服务';
    button.addEventListener('click', async function () {
      button.disabled = true;
      var awaitingApproval = false;
      try {
        var key = 'monitoring-' + action + '-' + Date.now().toString(36);
        var response = await client.requestPlatformServiceAction(service.service_id, action, key);
        if (response && response.approval_required) {
          awaitingApproval = true;
          showApproval(response);
        }
        else if (commandAccepted(response)) setNotice('动作已入队，等待节点回执', 'ok');
        else setNotice('动作响应格式无效', 'bad');
      } catch (error) {
        setNotice('动作接口不可用，服务监控仍可用', 'bad');
      } finally {
        button.disabled = awaitingApproval;
      }
    });
    buttons.appendChild(button);
  });
}

function renderDetail(host, detail, client, onRefresh) {
  clear(host);
  var service = detail.service && typeof detail.service === 'object' ? detail.service : {};
  var health = detail.health && typeof detail.health === 'object' ? detail.health : {};
  var heading = el('div', 'monitoring-detail-heading');
  var title = el('div');
  title.appendChild(el('h2', null, safeText(service.name, service.service_id || '服务详情')));
  title.appendChild(el('p', 'meta', safeText(service.service_id, 'unknown')));
  heading.appendChild(title);
  heading.appendChild(statusBadge(health.overall));
  host.appendChild(heading);
  renderDimensions(host, health);
  var facts = el('div', 'monitoring-detail-facts');
  facts.appendChild(el('span', null, '采集器：' + safeText(service.adapter, '—')));
  facts.appendChild(el('span', null, '节点：' + safeText(service.node_id, '—')));
  facts.appendChild(el('span', null, '评估：' + formatTime(health.evaluated_at)));
  host.appendChild(facts);
  renderEvidence(host, detail.evidence);
  var actions = el('section', 'monitoring-actions');
  renderActionPanel(actions, service, client, onRefresh);
  host.appendChild(actions);
}

function renderIncidents(host, incidents) {
  clear(host);
  host.appendChild(el('div', 'monitoring-section-heading', 'Incident 摘要'));
  if (!incidents.length) {
    host.appendChild(el('div', 'monitoring-empty', '暂无事件'));
    return;
  }
  var list = el('div', 'monitoring-incident-list');
  incidents.forEach(function (item) {
    var row = el('div', 'monitoring-incident-row');
    var copy = el('div', 'monitoring-incident-copy');
    copy.appendChild(el('strong', null, safeText(item.service_id, 'service')));
    copy.appendChild(el('span', 'monitoring-incident-meta',
      safeText(item.failure_class, 'health event') + ' · ' +
      formatTime(item.last_seen || item.updated_at)));
    row.appendChild(copy);
    row.appendChild(statusBadge(item.state === 'open' ? 'unhealthy' : 'healthy'));
    list.appendChild(row);
  });
  host.appendChild(list);
}

export function mountMonitoring(target, api) {
  clear(target);
  var client = api || {};
  var root = el('div', 'monitoring-view');
  var heading = el('div', 'monitoring-heading');
  var title = el('div');
  title.appendChild(el('h1', null, '服务监控'));
  title.appendChild(el('p', 'meta', '只读健康证据与事件摘要'));
  heading.appendChild(title);
  var refreshButton = document.createElement('button');
  refreshButton.type = 'button';
  refreshButton.setAttribute('class', 'monitoring-refresh');
  refreshButton.appendChild(uiIcon('refresh', { size: 14 }));
  refreshButton.appendChild(document.createTextNode('刷新'));
  heading.appendChild(refreshButton);
  root.appendChild(heading);

  var notice = el('div', 'monitoring-notice', '准备加载');
  root.appendChild(notice);
  var nodePanel = el('section', 'panel monitoring-nodes');
  root.appendChild(nodePanel);
  var summary = el('div', 'monitoring-summary');
  root.appendChild(summary);
  var columns = el('div', 'monitoring-grid');
  var servicePanel = el('section', 'panel monitoring-service-panel');
  servicePanel.appendChild(el('h2', null, '服务目录'));
  var serviceList = el('div', 'monitoring-service-list');
  servicePanel.appendChild(serviceList);
  var detailPanel = el('section', 'panel monitoring-detail-panel');
  detailPanel.appendChild(el('h2', null, '服务详情'));
  var detailHost = el('div', 'monitoring-detail-body');
  detailPanel.appendChild(detailHost);
  columns.appendChild(servicePanel);
  columns.appendChild(detailPanel);
  root.appendChild(columns);
  var incidentPanel = el('section', 'panel monitoring-incidents');
  root.appendChild(incidentPanel);
  target.appendChild(root);

  var services = [];
  var incidents = [];
  var selectedId = null;
  var detailRequest = 0;
  var refreshRequest = 0;
  var nodeRequest = 0;
  var disposed = false;
  var suspended = false;

  function setNotice(text, className) {
    notice.setAttribute('class', 'monitoring-notice' + (className ? ' ' + className : ''));
    notice.textContent = text;
  }

  function renderSummary() {
    clear(summary);
    var healthy = services.filter(function (row) {
      return row.health && row.health.overall === 'healthy';
    }).length;
    var open = incidents.filter(function (row) { return row.state === 'open'; }).length;
    [['服务', services.length], ['健康', healthy], ['未闭合事件', open]].forEach(function (item) {
      var card = el('div', 'monitoring-summary-item');
      card.appendChild(el('span', null, item[0]));
      card.appendChild(el('strong', null, String(item[1])));
      summary.appendChild(card);
    });
  }

  function renderServices() {
    clear(serviceList);
    if (!services.length) {
      serviceList.appendChild(el('div', 'monitoring-empty', '暂无已登记服务'));
      return;
    }
    services.forEach(function (row) {
      renderServiceRow(serviceList, row, selectedId, selectService);
    });
  }

  function showDetailError(error) {
    renderState(detailHost, 'monitoring-error', '服务详情加载失败：' + errorText(error));
  }

  async function selectService(serviceId) {
    if (disposed || suspended) return;
    selectedId = serviceId;
    renderServices();
    var requestId = detailRequest + 1;
    detailRequest = requestId;
    renderState(detailHost, 'monitoring-loading', '正在加载服务详情…');
    if (typeof client.getPlatformService !== 'function') {
      showDetailError(new Error('服务详情接口不可用'));
      return;
    }
    try {
      var data = await client.getPlatformService(serviceId);
      if (disposed || suspended || requestId !== detailRequest) return;
      if (!data || !data.service || typeof data.service !== 'object') {
        throw new Error('服务详情格式无效');
      }
      renderDetail(detailHost, data, client, function () { return selectService(serviceId); });
    } catch (error) {
      if (!disposed && !suspended && requestId === detailRequest) showDetailError(error);
    }
  }

  async function refreshNodes() {
    if (disposed || suspended || typeof client.getStatus !== 'function') return;
    var requestId = ++nodeRequest;
    try {
      var data = await client.getStatus();
      if (disposed || suspended || requestId !== nodeRequest) return;
      clear(nodePanel);
      nodePanel.appendChild(el('h2', null, '节点监控'));
      nodePanel.appendChild(el('p', 'meta', '来自节点最近一次上报；离线节点的指标仅供历史参考。'));
      var rows = el('div', 'monitoring-node-grid');
      (data.machines || []).forEach(function (machine) {
        var row = el('div', 'monitoring-node');
        var link = el('a', null, safeText(machine.machine)); link.href = pagePath('machine', machine.machine); row.appendChild(link);
        row.appendChild(el('span', 'meta', machine.online ? '在线' : '离线'));
        var system = machine.system || {};
        row.appendChild(el('p', 'meta', '负载 ' + safeText(system.load) + ' · 内存 ' + safeText(system.mem_used_mb) + ' / ' + safeText(system.mem_total_mb) + ' MB · 磁盘 ' + safeText(system.disk_used_pct)));
        row.appendChild(el('p', 'meta', '上报：' + safeText(machine.timestamp)));
        rows.appendChild(row);
      });
      if (!rows.childElementCount) rows.appendChild(el('p', 'meta', '暂无节点上报'));
      nodePanel.appendChild(rows);
    } catch (error) {
      if (!disposed && !suspended && requestId === nodeRequest) {
        renderState(nodePanel, 'monitoring-error', '节点监控加载失败：' + errorText(error));
      }
    }
  }

  async function refresh({quiet = false} = {}) {
    if (disposed || suspended) return;
    var requestId = ++refreshRequest;
    refreshNodes();
    if (!quiet) {
      refreshButton.disabled = true;
      setNotice('正在同步监控数据…');
      renderState(serviceList, 'monitoring-loading', '正在加载服务列表…');
      renderState(detailHost, 'monitoring-loading', '选择服务查看详情');
    }
    try {
      if (typeof client.getPlatformServices !== 'function' ||
          typeof client.getPlatformIncidents !== 'function') {
        throw new Error('服务监控接口不可用');
      }
      var data = await Promise.all([
        client.getPlatformServices(MAX_SERVICES),
        client.getPlatformIncidents(MAX_INCIDENTS),
      ]);
      if (disposed || suspended || requestId !== refreshRequest) return;
      services = validateServiceRows(data[0]);
      incidents = validateIncidents(data[1]);
      if (selectedId && !services.some(function (row) {
        return row.service.service_id === selectedId;
      })) selectedId = null;
      renderSummary();
      renderServices();
      renderIncidents(incidentPanel, incidents);
      setNotice('监控数据已更新', 'ok');
      if (selectedId) {
        await selectService(selectedId);
      } else if (services.length) {
        await selectService(services[0].service.service_id);
      } else {
        renderState(detailHost, 'monitoring-empty', '暂无服务详情');
      }
    } catch (error) {
      if (disposed || suspended || requestId !== refreshRequest) return;
      if (quiet) {
        setNotice('监控数据暂时不可用，仍显示上次结果', 'bad');
        return;
      }
      services = [];
      incidents = [];
      clear(summary);
      renderState(serviceList, 'monitoring-error', '服务列表加载失败：' + errorText(error));
      renderState(detailHost, 'monitoring-empty', '暂无服务详情');
      renderIncidents(incidentPanel, incidents);
      setNotice('监控数据不可用', 'bad');
    } finally {
      if (!disposed && !quiet) refreshButton.disabled = false;
    }
  }

  refreshButton.addEventListener('click', refresh);
  refresh();
  function teardown() {
    disposed = true;
    suspended = true;
    detailRequest += 1;
    refreshRequest += 1;
    nodeRequest += 1;
    clear(target);
  }
  teardown.suspend = function () {
    if (disposed || suspended) return;
    suspended = true;
    detailRequest += 1;
    refreshRequest += 1;
    nodeRequest += 1;
  };
  teardown.resume = function () {
    if (disposed || !suspended) return;
    suspended = false;
    refresh({quiet: true});
  };
  return teardown;
}
