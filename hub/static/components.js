/* components.js — 纯函数渲染组件（无 DOM 操作，返回 HTML 字符串） */

function esc(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function renderHealthbar(data) {
  const total = data.total || 0;
  const online = data.online || 0;
  const hermes = data.hermes_count || 0;
  const okClass = online === total ? 'hb-ok' : online === 0 ? 'hb-bad' : 'hb-warn';
  return `
    <div class="healthbar">
      <div class="${okClass}"><span>在线</span> <b>${online}/${total}</b></div>
      <div><span>Agent 总数</span> <b>${data.agent_count || 0}</b></div>
      <div><span>活动任务</span> <b>${data.session_count || 0}</b></div>
      <div><span>Hermes 节点</span> <b>${hermes}</b></div>
    </div>
  `;
}

function renderMachineCard(m) {
  const dotClass = m.online ? 'on' : 'off';
  const hermesBadge = m.has_hermes
    ? `<span class="badge-hermes">Hermes ${esc(m.hermes_state || 'unknown')}</span>`
    : '';

  let body = '';
  if (!m.online) {
    body = `<div class="err">⚠️ ${esc(m.error || 'unreachable')}</div>`;
  } else {
    if (m.sessions && m.sessions.length > 0) {
      body += `<div style="font-size:12px;color:var(--text-2);margin-bottom:4px;">📋 活动任务 (${m.sessions.length})</div>`;
      m.sessions.forEach(s => {
        const displayName = s.display_name ? `<div>${esc(s.display_name)}</div>` : '';
        const updatedAt = (s.updated_at || '').substring(0, 16).replace('T', ' ');
        body += `
          <div class="agent-row">
            <div class="k">${esc(s.key)}</div>
            ${displayName}
            <div class="t">更新: ${esc(updatedAt)}</div>
          </div>
        `;
      });
    } else {
      body += `<div class="meta">无活动会话</div>`;
    }

    if (m.agent_summaries && m.agent_summaries.length > 0) {
      body += `<div style="font-size:12px;color:var(--text-2);margin-bottom:4px;">📦 agent 连接器</div>`;
      m.agent_summaries.forEach(a => {
        let statusHtml = '';
        if (a.status === 'ok') {
          statusHtml = '<span class="st-ok">● ok</span>';
        } else if (a.status === 'absent') {
          statusHtml = '<span class="st-absent">○ 未部署</span>';
        } else {
          statusHtml = `<span class="st-err">● ${esc(a.detail || a.status)}</span>`;
        }
        body += `
          <div class="agent-row" style="padding:4px 8px;margin:4px 0;">
            <span class="k">${esc(a.type)}</span>
            ${statusHtml}
          </div>
        `;
      });
    }

    const sys = m.system || {};
    const diskPct = parseInt(String(sys.disk_used_pct || 0).replace('%', '')) || 0;
    const barClass = diskPct > 90 ? 'danger' : diskPct > 70 ? 'warn' : '';
    body += `
      <div class="stat">
        <span>📦 agent: <b>${m.agent_count || 0}</b></span>
        <span>📈 负载: <b>${esc(sys.load) || 0}</b></span>
        <span>💾 磁盘: <b>${diskPct}%</b></span>
        <span>⏱️ 运行: <b>${esc(sys.uptime || '?')}</b></span>
      </div>
      <div class="bar"><i class="${barClass}" style="width:${diskPct}%"></i></div>
    `;
  }

  return `
    <div class="card" id="card-${esc(m.machine)}">
      <h2>
        <span class="dot ${dotClass}"></span>
        <a href="/machine/${esc(m.machine)}">${esc(m.machine)}</a>
        ${hermesBadge}
      </h2>
      <div class="meta">${esc(m.desc || '')}</div>
      ${body}
    </div>
  `;
}

function renderEventItem(e) {
  let rawTs = e.ts || '';
  if (typeof rawTs === 'number') {
    rawTs = new Date(rawTs * 1000).toISOString();
  }
  const ts = String(rawTs).substring(0, 19).replace('T', ' ');
  let cls = '';
  let icon = '•';
  if (e.event === 'state_changed') {
    cls = 'ok';
    icon = '✓';
  } else if (e.event === 'scan_error') {
    cls = 'bad';
    icon = '✗';
  }
  return `
    <div class="event-item ${cls}">
      <div class="t">${esc(ts)}</div>
      <div>${icon} ${esc(e.machine || 'fleet')}: ${esc(e.summary || e.event || 'event')}</div>
    </div>
  `;
}
