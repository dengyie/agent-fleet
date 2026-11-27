/* Pure transcript projection. Cursor is Hub ingestion identity; vendor sequence
 * and text equality cannot identify messages across independent streams. */
export const TERMINAL_RECEIPTS = new Set(['succeeded', 'already_finished', 'failed', 'expired', 'rejected', 'cancelled']);
export const AGENT_NAMES = Object.freeze({codex:'Codex', claude_code:'Claude Code', hermes:'Hermes',
  pi:'Pi', zcode:'ZCode', agy:'Google Antigravity'});
export const agentName = family => AGENT_NAMES[family] || family || '电脑 Agent';
export const RECEIPT_LABELS = Object.freeze({queued: '已入队', pending: '已入队', delivered: '节点已接收',
  accepted: '节点已接受', executing: '正在投递', succeeded: '已交付到原会话', already_finished: '操作已结束',
  failed: '投递失败', expired: '投递已过期', rejected: '投递被拒绝', cancelled: '已取消'});
export function operationStatus(operation) {
  if (operation.status === 'succeeded' && operation.action !== 'append_user_turn') return '节点已执行';
  return RECEIPT_LABELS[operation.status] || operation.label || '等待确认';
}
const OPERATION_FAILURES = Object.freeze({
  escape_unverified: '节点尚未确认原 Agent 进程已完全退出，暂不能继续执行。请在工作面板查看可用的终止操作，并以节点回执确认停止；若仍失败，请检查该节点。',
  liveness_unknown: '节点暂时无法确认原 Agent 进程状态。请先检查节点连接与会话状态，并查看原操作回执，避免重复执行。',
});
export function operationFailure(reason) {
  return Object.hasOwn(OPERATION_FAILURES, reason) ? OPERATION_FAILURES[reason] + '（' + reason + '）' : reason || '';
}
export const AGENT_LABELS = Object.freeze({running: 'Agent 正在执行', idle: '等待下一条消息', completed: '本轮已结束',
  waiting_for_user: '等待你回答', waiting_for_approval: '等待审批', failed: 'Agent 执行失败', stopped: '已停止', unknown: '状态待确认'});
const STATE_REASONS = Object.freeze({session_started:'会话已启动', user_prompt_submitted:'已收到用户消息',
  tool_started:'开始调用工具', tool_finished:'工具已返回', session_ended:'本轮会话结束',
  agent_stopped:'Agent 已停止', approval_required:'需要你审批', user_input_required:'需要你回答',
  explicit_state:'节点状态已确认', source_unavailable:'节点暂时无法继续提供执行记录'});

// Group only adjacent public activity in the same stream. Never move later
// steps above a message/tool, infer private reasoning, or merge independent streams.
export function nativeTranscript(events) {
  const rows = [], latest = new Map();
  for (const row of nativeMessages(events)) {
    const prior = latest.get(row.stream_id);
    if (prior) { prior.active = false; prior.title = '执行过程'; }
    if (row.kind !== 'activity') { rows.push(row); continue; }
    let group = rows.at(-1);
    if (group?.kind !== 'thinking' || group.stream_id !== row.stream_id) {
      group = {key:row.key, kind:'thinking', stream_id:row.stream_id, content:'', active:false, title:'执行过程'};
      rows.push(group);
    }
    group.content += (group.content ? '\n' : '') + row.text;
    if (row.detail) group.content += ' · ' + row.detail;
    if (row.active !== undefined) group.active = row.active;
    group.title = group.active ? 'Agent 正在执行' : '执行过程';
    latest.set(row.stream_id, group);
  }
  return rows;
}

export function nativeCaptureNotice(session) {
  const protocol = session?.agent_evidence?.protocol;
  if (session?.agent_family !== 'zcode' || protocol?.name !== 'desktop_bridge' || protocol.state !== 'observed') return '';
  return 'ZCode 桌面会话在 Agent 空闲后同步正文与工具记录，执行期间请等待同步。记录按采集顺序展示，不代表完整历史。';
}

export function mergeNativeEvents(current, incoming, max = 500) {
  const rows = new Map(current.map(row => [row.cursor, row]));
  for (const row of incoming) rows.set(row.cursor, row);
  const events = [...rows.values()].sort((a, b) => BigInt(a.cursor) < BigInt(b.cursor) ? -1 : 1);
  return {events: events.slice(-max), trimmed: events.length > max};
}

export function nativeMessages(events) {
  const messages = [], fragments = new Map(), identifiedMessages = new Map(), tools = new Map();
  const interactions = new Map(), pending = new Map();
  function track(stream, row, active) {
    if (!pending.has(stream)) pending.set(stream, new Set());
    if (active) pending.get(stream).add(row);
    else pending.get(stream).delete(row);
  }
  function endStream(stream, interrupted) {
    for (const row of pending.get(stream) || []) {
      if (row.kind === 'tool') row.status = 'unknown';
      else { row.complete = true; row.interrupted = interrupted; }
    }
    pending.delete(stream);
    fragments.delete(stream + ':assistant_message');
  }
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
      row.interrupted = false;
      if (kind === 'assistant_message') track(stream, row, !row.complete);
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
      track(stream, row, row.status === 'running');
      row.emitted_at = event.emitted_at || row.emitted_at || '';
    } else if (event.kind === 'capture_gap') {
      messages.push({key: event.cursor, kind: 'notice', text: '记录存在采集缺口：' + (p.reason || 'source_unavailable'), emitted_at: event.emitted_at});
    } else if (event.kind === 'session_start') {
      messages.push({key: event.cursor, kind: 'activity',
        text: ['原生会话开始', agentName(p.agent_family), p.adapter].filter(Boolean).join(' · '), emitted_at: event.emitted_at});
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
      if (['completed', 'failed', 'stopped'].includes(p.state)) endStream(stream, p.state !== 'completed');
      messages.push({key: event.cursor, kind: 'activity', stream_id:stream,
        text: AGENT_LABELS[p.state] || 'Agent 状态更新',
        detail: Object.hasOwn(STATE_REASONS, p.reason) ? STATE_REASONS[p.reason] : '',
        active: p.state === 'running', emitted_at: event.emitted_at});
    } else if (event.kind === 'capture_quality_changed') {
      messages.push({key: event.cursor, kind: 'activity',
        text: ['采集质量', p.old_quality, p.new_quality].filter(Boolean).join(' · '), emitted_at: event.emitted_at});
    } else if (event.kind === 'process_spawn') {
      messages.push({key: event.cursor, kind: 'activity',
        text: ['Agent 进程启动', p.process_family].filter(Boolean).join(' · '), emitted_at: event.emitted_at});
    } else if (event.kind === 'step_start' || event.kind === 'step_end') {
      messages.push({key: event.cursor, kind: 'activity',
        text: [p.step_name, event.kind === 'step_end' ? p.status : '开始'].filter(Boolean).join(' · '),
        active:event.kind === 'step_start', emitted_at: event.emitted_at});
    } else if (event.kind === 'supervisor_state_changed') {
      messages.push({key: event.cursor, kind: 'activity',
        text: ['接力状态', p.old_state, p.new_state].filter(Boolean).join(' · '), emitted_at: event.emitted_at});
    } else if (event.kind === 'policy_signal') {
      messages.push({key: event.cursor, kind: 'activity',
        text: ['策略提示', p.severity, p.reason].filter(Boolean).join(' · '), emitted_at: event.emitted_at});
    } else if (event.kind === 'session_close') {
      endStream(stream, p.reason !== 'completed' || p.exit_code !== undefined && p.exit_code !== 0);
      messages.push({key: event.cursor, kind: 'activity', active:false,
        text: ['会话结束', p.reason, p.exit_code === undefined ? '' : '退出码 ' + p.exit_code].filter(Boolean).join(' · '),
        emitted_at: event.emitted_at});
    } else if (event.kind === 'process_exit') {
      messages.push({key: event.cursor, kind: 'activity', active:false,
        text: 'Agent 进程退出' + (p.exit_code === undefined ? '' : ' · 退出码 ' + p.exit_code),
        emitted_at: event.emitted_at});
    }
  }
  // Every visible row keeps its stream identity for display-only grouping.
  const streams = new Map(events.map(event => [event.cursor, event.stream_id || '']));
  for (const row of messages) if (row.stream_id === undefined) {
    row.stream_id = streams.get(row.key) || '';
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
  return first?.payload.text.slice(0, 60) || [agentName(session?.agent_family), session?.machine_id].filter(Boolean).join(' · ');
}

// Unknown admissions require explicit recovery of the frozen operation.
// Commands with an ID use read-only receipt polling.
export function uncertainWrite(error) {
  return !error?.status || error.status >= 500 || error.kind === 'parse' || error.kind === 'contract';
}
