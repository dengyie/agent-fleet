/* app.js — SSE 客户端 + 轮询降级 + 任务操作 */

const page = document.body.getAttribute('data-page') || '';

let sseSource = null;
let pollTimer = null;
let lastEventTs = 0;

function startSSE() {
  if (sseSource) return;

  const url = `/api/stream?since=${lastEventTs}`;
  sseSource = new EventSource(url);
  updateSSEDot(true);

  sseSource.addEventListener('machine_update', e => {
    try {
      const data = JSON.parse(e.data);
      lastEventTs = data.ts || Date.now() / 1000;
      handleMachineUpdate(data);
    } catch (err) {
      console.error('SSE parse error:', err);
    }
  });

  sseSource.addEventListener('fleet_event', e => {
    try {
      const data = JSON.parse(e.data);
      lastEventTs = data.ts || Date.now() / 1000;
      handleFleetEvent(data);
    } catch (err) {
      console.error('SSE parse error:', err);
    }
  });

  sseSource.addEventListener('task_update', e => {
    try {
      const data = JSON.parse(e.data);
      lastEventTs = Math.max(lastEventTs, data.ts || 0);
      if (page === 'task' && data.task_id === document.body.getAttribute('data-task-id')) {
        if (['succeeded', 'failed', 'cancelled', 'expired'].indexOf(data.state) !== -1) {
          location.reload(); /* 终态：重载拿结果 */
        } else {
          const flow = document.getElementById('stateflow');
          if (flow) {
            flow.querySelectorAll('.state-node').forEach(n => {
              n.classList.toggle('current', n.getAttribute('data-state') === data.state);
            });
          }
        }
      } else if (page === 'machine' || page === 'fleet') {
        refreshAll();
      }
    } catch (err) {
      console.error('SSE parse error:', err);
    }
  });

  sseSource.addEventListener('task_log', e => {
    try {
      const data = JSON.parse(e.data);
      lastEventTs = Math.max(lastEventTs, data.ts || 0);
      if (page === 'task' && data.task_id === document.body.getAttribute('data-task-id')) {
        const pending = document.getElementById('result-pending');
        if (pending) pending.textContent = '任务进行中…';
        const logEl = document.getElementById('task-log');
        FleetApp.logAppend(logEl, data.line, 500);
        if (logEl) {
          logEl.classList.add('flash');
          setTimeout(() => logEl.classList.remove('flash'), 600);
        }
      }
    } catch (err) {
      console.error('SSE parse error:', err);
    }
  });

  sseSource.onerror = () => {
    console.warn('SSE error, fallback to poll until reconnect');
    updateSSEDot(false);
    startPoll();
  };

  sseSource.onopen = () => {
    updateSSEDot(true);
    stopPoll();
  };
}

function stopSSE() {
  if (sseSource) {
    sseSource.close();
    sseSource = null;
  }
  updateSSEDot(false);
}

function startPoll() {
  if (pollTimer) return;
  pollTimer = setInterval(() => {
    fetch('/api/status', { cache: 'no-store' })
      .then(r => r.json())
      .then(data => {
        renderFleet(data);
      })
      .catch(err => console.error('Poll error:', err));
  }, 10000);
}

function stopPoll() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

function updateSSEDot(live) {
  const dot = document.querySelector('.sse-dot');
  if (dot) {
    dot.classList.toggle('live', live);
  }
}

function handleMachineUpdate(data) {
  const card = document.getElementById(`card-${data.machine}`);
  if (card) {
    card.classList.add('flash');
    setTimeout(() => card.classList.remove('flash'), 600);
  }

  appendEvent({
    event: 'state_changed',
    machine: data.machine,
    ts: data.ts,
    summary: Array.isArray(data.changes) && data.changes.length
      ? `更新: ${data.changes.join(', ')}`
      : '状态更新'
  });

  fetch('/api/status', { cache: 'no-store' })
    .then(r => r.json())
    .then(full => renderFleet(full))
    .catch(err => console.error('Refresh error:', err));
}

function handleFleetEvent(data) {
  appendEvent(data);
}

function appendEvent(data) {
  const stream = document.getElementById('event-stream');
  if (stream) {
    const html = renderEventItem(data);
    stream.insertAdjacentHTML('afterbegin', html);
    const items = stream.querySelectorAll('.event-item');
    if (items.length > 50) {
      items[items.length - 1].remove();
    }
  }
}

function renderFleet(data) {
  const rows = data.machines || data.rows || [];
  const grid = document.getElementById('machine-grid');

  // 更新健康条数字（SSR 结构为 <span class="hb-ok/...">在线<b id="hb-online">…）
  const online = rows.filter(m => m.online).length;
  const alerts = rows.length - online;
  const setNum = (id, val) => {
    const el = document.getElementById(id);
    if (el) el.textContent = val;
  };
  setNum('hb-online', online);
  setNum('hb-alerts', alerts);
  setNum('hb-total', rows.length);

  if (grid && typeof renderMachineCard === 'function') {
    grid.innerHTML = rows.map(renderMachineCard).join('');
  }
}

/* ---- 页面上下文刷新 ---- */
function refreshTaskCount() {
  const el = document.getElementById('hb-tasks');
  if (!el) return;
  fetch('/api/tasks?limit=200', { cache: 'no-store' })
    .then(r => r.json())
    .then(d => {
      if (d.ok) {
        el.textContent = d.tasks.length === 200 ? '200+' : String(d.tasks.length);
      }
    })
    .catch(() => {});
}

function refreshMachineTasks() {
  const machine = document.body.getAttribute('data-machine');
  const list = document.getElementById('task-list');
  if (!machine || !list) return;
  fetch('/api/tasks?machine=' + encodeURIComponent(machine) + '&limit=10', { cache: 'no-store' })
    .then(r => r.json())
    .then(d => {
      if (!d.ok) return;
      list.innerHTML = d.tasks.map(function (t) {
        return '<div class="task-row" data-task-id="' + esc(t.task_id) + '">' +
          '<a href="/task/' + encodeURIComponent(t.task_id) + '">' + esc(t.task_id) + '</a>' +
          '<span class="task-state st-' + esc(t.state) + '">' + esc(t.state) + '</span>' +
          '<span class="meta">' + esc(t.agent_type) + ' · ' + esc(t.project) + ' · ' +
          esc(String(t.instruction || '').slice(0, 60)) + ' · ' + esc(t.created_at) + '</span></div>';
      }).join('') || '<div class="meta">暂无任务</div>';
    })
    .catch(() => {});
}

function refreshAll() {
  if (page === 'machine') {
    refreshMachineTasks();
  } else if (page === 'fleet') {
    refreshTaskCount();
  }
}

function bindModalActions() {
  document.addEventListener('click', function (e) {
    const openBtn = e.target.closest('[data-open-modal]');
    if (openBtn) {
      const modal = document.getElementById(openBtn.getAttribute('data-open-modal'));
      if (modal) modal.classList.add('open');
      return;
    }
    const closeBtn = e.target.closest('[data-close-modal]');
    if (closeBtn) {
      const modal = document.getElementById(closeBtn.getAttribute('data-close-modal'));
      if (modal) modal.classList.remove('open');
      return;
    }
    const createBtn = e.target.closest('[data-create-task]');
    if (createBtn && typeof FleetApp.createTask === 'function') {
      FleetApp.createTask(String(createBtn.getAttribute('data-create-task') || ''));
    }
  });
}

/* ---- 任务操作 ---- */
function taskActionError(msg) {
  const el = document.getElementById('task-action-error');
  if (el) el.textContent = msg;
}

FleetApp.createTask = function (machine) {
  const errEl = document.getElementById('nt-error');
  const get = id => document.getElementById(id);
  const body = {
    machine: machine,
    agent_type: get('nt-agent') ? get('nt-agent').value : 'codex',
    project: get('nt-project') ? get('nt-project').value.trim() : '',
    instruction: get('nt-instruction') ? get('nt-instruction').value : '',
    client_token: (crypto.randomUUID ? crypto.randomUUID() : String(Date.now())),
  };
  fetch('/api/tasks', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(function (r) { return r.json().then(function (d) { return { status: r.status, d: d }; }); })
    .then(function (res) {
      if (res.d.ok) { window.location.href = '/task/' + encodeURIComponent(res.d.task.task_id); }
      else if (errEl) { errEl.textContent = res.d.detail || res.d.error || '创建失败'; }
    })
    .catch(function () { if (errEl) errEl.textContent = '网络错误'; });
};

FleetApp.cancelTask = function (taskId) {
  taskActionError('');
  fetch('/api/tasks/' + encodeURIComponent(taskId) + '/cancel', { method: 'POST' })
    .then(function (r) { return r.json().then(function (d) { return { status: r.status, d: d }; }); })
    .then(function (res) {
      if (res.d.ok) {
        if (page === 'task') { location.reload(); }
        else { refreshAll(); }
      } else {
        taskActionError(res.d.detail || res.d.error || '取消失败');
      }
    })
    .catch(function () { taskActionError('网络错误'); });
};

FleetApp.retryTask = function (taskId) {
  taskActionError('');
  fetch('/api/tasks/' + encodeURIComponent(taskId) + '/retry', { method: 'POST' })
    .then(function (r) { return r.json().then(function (d) { return { status: r.status, d: d }; }); })
    .then(function (res) {
      if (res.d.ok) { location.reload(); }
      else { taskActionError(res.d.detail || res.d.error || '重试失败'); }
    })
    .catch(function () { taskActionError('网络错误'); });
};

/* 日志追加：textContent 写入（天然转义），行数上限 500 */
FleetApp.logAppend = function (container, text, max) {
  if (!container || text == null) return;
  const cur = container.textContent;
  const sep = cur.length > 0 && !cur.endsWith('\n') ? '\n' : '';
  container.textContent = cur + sep + String(text);
  const capped = container.textContent.split('\n');
  if (capped.length > max) {
    container.textContent = capped.slice(-max).join('\n');
  }
};

/* esc 已在 components.js 全局定义，这里暴露到 FleetApp 便于调用 */
FleetApp.esc = esc;

document.addEventListener('DOMContentLoaded', () => {
  startSSE();
  bindModalActions();
  // 首次加载：fleet 页健康条任务数用真实任务计数替换 SSR 硬编码 0
  if (page === 'fleet') {
    refreshTaskCount();
  }
});
