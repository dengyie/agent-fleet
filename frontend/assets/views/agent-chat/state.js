/* Pure transcript projection. Cursor is Hub ingestion identity; vendor sequence
 * and text equality cannot identify messages across independent streams. */
export const TERMINAL_RECEIPTS = new Set(['succeeded', 'already_finished', 'failed', 'expired', 'rejected', 'cancelled']);
export const RECEIPT_LABELS = Object.freeze({queued: '已入队', pending: '已入队', delivered: '节点已接收',
  accepted: '节点已接受', executing: '正在投递', succeeded: '已交付到原会话', already_finished: '操作已结束',
  failed: '投递失败', expired: '投递已过期', rejected: '投递被拒绝', cancelled: '已取消'});
export function operationStatus(operation) {
  if (operation.status === 'succeeded' && operation.action !== 'append_user_turn') return '节点已执行';
  return RECEIPT_LABELS[operation.status] || operation.label || '等待确认';
}
export const AGENT_LABELS = Object.freeze({running: 'Agent 正在执行', idle: '等待下一条消息', completed: '本轮已结束',
  waiting_for_user: '等待你回答', waiting_for_approval: '等待审批', failed: 'Agent 执行失败', stopped: '已停止', unknown: '状态待确认'});

export function mergeNativeEvents(current, incoming, max = 500) {
  const rows = new Map(current.map(row => [row.cursor, row]));
  for (const row of incoming) rows.set(row.cursor, row);
  const events = [...rows.values()].sort((a, b) => BigInt(a.cursor) < BigInt(b.cursor) ? -1 : 1);
  return {events: events.slice(-max), trimmed: events.length > max};
}

export function nativeMessages(events) {
  const messages = [], fragments = new Map(), identifiedMessages = new Map(), tools = new Map();
  const interactions = new Map();
  for (const event of events) {
    const p = event.payload || {}, stream = event.stream_id || '';
    if (event.kind === 'user_message' || event.kind === 'assistant_message') {
      const kind = event.kind;
      const messageId = p.message_id || '';
      const mode = p.content_mode || 'delta';
      const chunkKey = stream + ':' + kind;
      const identityKey = messageId ? chunkKey + ':' + messageId : '';
      let row = messageId ? identifiedMessages.get(identityKey) : fragments.get(chunkKey);
      if (!row || (!messageId && row.complete)) {
        row = {key: event.cursor, kind, text: '', complete: false,
          sequence: event.sequence, emitted_at: event.emitted_at || ''};
        messages.push(row);
        if (messageId) identifiedMessages.set(identityKey, row);
      }
      if (mode === 'snapshot') row.text = p.text || '';
      else row.text += p.text || '';
      row.complete = p.is_complete !== false;
      row.updated_at = event.emitted_at || row.updated_at || '';
      if (!messageId) {
        if (row.complete) fragments.delete(chunkKey);
        else fragments.set(chunkKey, row);
      }
    } else if (event.kind === 'tool_call' || event.kind === 'tool_result') {
      const key = p.call_id ? stream + ':' + p.call_id : event.cursor;
      let row = tools.get(key);
      if (!row) {
        row = {key: event.cursor, kind: 'tool', name: p.tool_name || '工具', sequence: event.sequence};
        tools.set(key, row); messages.push(row);
      }
      row.args = p.arguments || row.args || '';
      row.output = p.result || row.output || '';
      row.label = toolLabel(row.name, row.args);
      const status = p.status || '';
      row.status = /^(error|failed|timeout|timed_out|cancelled)$/.test(status) ? 'error'
        : /^(ok|completed|success|succeeded|done)$/.test(status) ? 'success'
        : event.kind === 'tool_call' ? 'running' : 'unknown';
      row.emitted_at = event.emitted_at || row.emitted_at || '';
    } else if (event.kind === 'capture_gap') {
      messages.push({key: event.cursor, kind: 'notice', text: '记录存在采集缺口：' + (p.reason || 'source_unavailable'), emitted_at: event.emitted_at});
    } else if (event.kind === 'session_start') {
      messages.push({key: event.cursor, kind: 'activity',
        text: ['原生会话开始', p.agent_family, p.adapter].filter(Boolean).join(' · '), emitted_at: event.emitted_at});
    } else if (event.kind === 'session_metadata') {
      const facts = [p.model, p.provider].filter(Boolean).join(' / ');
      messages.push({key: event.cursor, kind: 'activity',
        text: ['会话信息更新', facts].filter(Boolean).join(' · '), emitted_at: event.emitted_at});
    } else if (event.kind === 'interaction_request') {
      interactions.set(p.request_id, p);
      messages.push({key: event.cursor, kind: 'activity',
        text: ['Agent 提问', p.prompt].filter(Boolean).join(' · '), emitted_at: event.emitted_at});
    } else if (event.kind === 'interaction_response') {
      const question = interactions.get(p.request_id);
      const labels = new Map((question?.options || []).map(option => [option.id, option.label]));
      const selected = Array.isArray(p.selected) ? p.selected.map(id => labels.get(id) || id) : [];
      messages.push({key: event.cursor, kind: 'activity',
        text: ['你的回答', ...selected, p.text].filter(Boolean).join(' · '), emitted_at: event.emitted_at});
    } else if (event.kind === 'agent_state_changed') {
      messages.push({key: event.cursor, kind: 'activity', text: AGENT_LABELS[p.state] || 'Agent 状态更新', emitted_at: event.emitted_at});
    } else if (event.kind === 'capture_quality_changed') {
      messages.push({key: event.cursor, kind: 'activity',
        text: ['采集质量', p.old_quality, p.new_quality].filter(Boolean).join(' · '), emitted_at: event.emitted_at});
    } else if (event.kind === 'process_spawn') {
      messages.push({key: event.cursor, kind: 'activity',
        text: ['Agent 进程启动', p.process_family].filter(Boolean).join(' · '), emitted_at: event.emitted_at});
    } else if (event.kind === 'step_start' || event.kind === 'step_end') {
      messages.push({key: event.cursor, kind: 'activity',
        text: [p.step_name, event.kind === 'step_end' ? p.status : '开始'].filter(Boolean).join(' · '),
        emitted_at: event.emitted_at});
    } else if (event.kind === 'supervisor_state_changed') {
      messages.push({key: event.cursor, kind: 'activity',
        text: ['接力状态', p.old_state, p.new_state].filter(Boolean).join(' · '), emitted_at: event.emitted_at});
    } else if (event.kind === 'policy_signal') {
      messages.push({key: event.cursor, kind: 'activity',
        text: ['策略提示', p.severity, p.reason].filter(Boolean).join(' · '), emitted_at: event.emitted_at});
    } else if (event.kind === 'session_close') {
      messages.push({key: event.cursor, kind: 'activity',
        text: ['会话结束', p.reason, p.exit_code === undefined ? '' : '退出码 ' + p.exit_code].filter(Boolean).join(' · '),
        emitted_at: event.emitted_at});
    } else if (event.kind === 'process_exit') {
      messages.push({key: event.cursor, kind: 'activity',
        text: 'Agent 进程退出' + (p.exit_code === undefined ? '' : ' · 退出码 ' + p.exit_code),
        emitted_at: event.emitted_at});
    }
  }
  return messages;
}

function toolLabel(name, args) {
  let summary = args;
  try {
    const value = JSON.parse(args);
    summary = value.command || value.cmd || value.path || value.file_path || value.url || value.query || '';
  } catch (_) { /* Plain tool arguments are already public display text. */ }
  return typeof summary === 'string' && summary.trim() ? name + ' · ' + summary.trim().split('\n')[0].slice(0, 100) : name;
}

export function sessionTitle(session, events = []) {
  if (session?.native_title) return session.native_title;
  if (session?.title_preview) return session.title_preview;
  const first = events.find(event => event.kind === 'user_message' && event.payload?.text);
  return first?.payload.text.slice(0, 60) || [session?.agent_family || '电脑 Agent', session?.machine_id].filter(Boolean).join(' · ');
}

// Unknown admissions require explicit recovery of the frozen operation.
// Commands with an ID use read-only receipt polling.
export function uncertainWrite(error) {
  return !error?.status || error.status >= 500 || error.kind === 'parse' || error.kind === 'contract';
}
