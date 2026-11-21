/* Pure transcript projection. Cursor is Hub ingestion identity; vendor sequence
 * and text equality cannot identify messages across independent streams. */
export const TERMINAL_RECEIPTS = new Set(['succeeded', 'already_finished', 'failed', 'expired', 'rejected', 'cancelled']);
export const RECEIPT_LABELS = Object.freeze({queued: '已入队', pending: '已入队', delivered: '节点已接收',
  accepted: '节点已接受', executing: '正在投递', succeeded: '已交付到原会话', already_finished: '操作已结束',
  failed: '投递失败', expired: '投递已过期', rejected: '投递被拒绝', cancelled: '已取消'});
export const AGENT_LABELS = Object.freeze({running: 'Agent 正在执行', idle: '等待下一条消息', completed: '本轮已结束',
  waiting_for_user: '等待你回答', waiting_for_approval: '等待审批', failed: 'Agent 执行失败', stopped: '已停止', unknown: '状态待确认'});

export function mergeNativeEvents(current, incoming, max = 500) {
  const rows = new Map(current.map(row => [row.cursor, row]));
  for (const row of incoming) rows.set(row.cursor, row);
  const events = [...rows.values()].sort((a, b) => BigInt(a.cursor) < BigInt(b.cursor) ? -1 : 1);
  return {events: events.slice(-max), trimmed: events.length > max};
}

export function nativeMessages(events) {
  const messages = [], fragments = new Map(), tools = new Map();
  for (const event of events) {
    const p = event.payload || {}, stream = event.stream_id || '';
    if (event.kind === 'user_message' || event.kind === 'assistant_message') {
      const chunkKey = stream + ':' + event.kind;
      let row = fragments.get(chunkKey);
      if (row) { row.text += p.text || ''; row.complete = p.is_complete !== false; }
      else {
        row = {key: event.cursor, kind: event.kind, text: p.text || '', complete: p.is_complete !== false, sequence: event.sequence};
        messages.push(row);
      }
      if (p.is_complete === false) fragments.set(chunkKey, row);
      else fragments.delete(chunkKey);
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
    } else if (event.kind === 'capture_gap') {
      messages.push({key: event.cursor, kind: 'notice', text: '记录存在采集缺口：' + (p.reason || '详情见原始记录')});
    } else if (event.kind === 'process_exit' && p.exit_code !== undefined && p.exit_code !== 0) {
      messages.push({key: event.cursor, kind: 'notice', text: 'Agent 进程退出，退出码 ' + p.exit_code});
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
  const first = events.find(event => event.kind === 'user_message' && event.payload?.text);
  return first?.payload.text.slice(0, 60) || [session?.agent_family || '电脑 Agent', session?.machine_id].filter(Boolean).join(' · ');
}

// Unknown admissions require explicit recovery of the frozen operation.
// Commands with an ID use read-only receipt polling.
export function uncertainWrite(error) {
  return !error?.status || error.status >= 500 || error.kind === 'parse' || error.kind === 'contract';
}
