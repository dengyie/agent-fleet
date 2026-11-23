// Each native continuation owns an immutable, command-bound result.
import {el, button, errorText} from './dom.js';

export function mountTurnResults(root, service) {
  let sessionId = '', version = 0, controller, disposed = false, suspended = false;
  let cursor, hasMore = false;
  const details = new Map();
  const load = button('查看续聊成果', () => refresh());
  const older = button('加载更早成果', () => refresh(true)); older.hidden = true;
  const status = el('p', 'meta'); status.setAttribute('role', 'status');
  const body = el('div', 'native-turn-result-body');
  const heading = el('h2', 'assistant-section-title', '每轮续聊成果');
  const note = el('p', 'meta', '按本轮开始前的工作区快照计算变更；原任务结果单独保留。仅已验证的托管工作区可采集。');
  root.append(heading, note, load, status, body, older);
  function cancel() {
    version++; controller?.abort(); controller = null; load.disabled = false; older.disabled = false;
    for (const request of details.values()) { request.controller.abort(); request.action.disabled = false; }
    details.clear();
  }
  function block(parent, title, text) {
    const detail = el('details', 'native-result-detail');
    detail.append(el('summary', null, title), el('pre', null, text)); parent.append(detail);
  }
  async function detail(row, content, action) {
    if (disposed || suspended || details.has(row.command_id)) return;
    const epoch = version, id = sessionId;
    const request = {controller: new AbortController(), action};
    details.set(row.command_id, request);
    action.disabled = true;
    try {
      const result = await service.readContinuationResult(id, row.command_id, request.controller.signal);
      if (disposed || suspended || version !== epoch || sessionId !== id || details.get(row.command_id) !== request) return;
      content.replaceChildren();
      if (result.collection_state === 'unavailable') content.append(el('p', 'meta', '本轮成果未能采集，执行状态与采集状态分别记录。'));
      if (result.diff_patch) block(content, '本轮 diff' + (result.diff_truncated ? '（已截断）' : ''), result.diff_patch);
      else if (result.collection_state !== 'unavailable') content.append(el('p', 'meta', '本轮没有文件变更。'));
      if (result.redacted) content.append(el('p', 'meta', '敏感内容已脱敏。'));
      const summary = result.test_summary;
      if (summary) {
        content.append(el('p', 'meta', [summary.framework, ...['passed', 'failed', 'skipped', 'errors'].filter(key => key in summary).map(key => key + ': ' + summary[key])].filter(Boolean).join(' · ')));
        if (summary.failed_names?.length) block(content, '失败测试', summary.failed_names.join('\n'));
      } else content.append(el('p', 'meta', '本轮未生成新的结构化测试摘要。'));
    } catch (error) {
      if (!disposed && !suspended && version === epoch) content.replaceChildren(el('p', 'err', '成果读取失败：' + errorText(error)));
    } finally {
      if (details.get(row.command_id) === request) { details.delete(row.command_id); action.disabled = false; }
    }
  }
  async function refresh(before = false) {
    if (!sessionId || disposed || suspended || before && !hasMore) return;
    if (!before) { cancel(); body.replaceChildren(); }
    if (!controller) controller = new AbortController();
    const epoch = version, id = sessionId;
    load.disabled = true; older.disabled = true; status.textContent = '正在读取续聊成果…';
    try {
      const page = await service.readContinuationResults(id, {before: before ? cursor : undefined, signal: controller.signal});
      if (disposed || suspended || version !== epoch || sessionId !== id) return;
      for (const row of page.results) {
        const item = el('details', 'native-result-detail'), content = el('div');
        const state = {succeeded: '执行完成', failed: '执行失败', cancelled: '已取消', unknown: '执行结果不确定'}[row.execution_state];
        const action = button('读取本轮详情', () => detail(row, content, action));
        item.append(el('summary', null, row.finished_at + ' · ' + state + ' · ' + row.duration_s.toFixed(1) + ' 秒'),
          el('p', 'meta', row.command_id), action, content);
        if (row.diff_stat) block(content, '本轮变更统计', row.diff_stat);
        body.append(item);
      }
      hasMore = page.has_more; cursor = page.before_cursor; older.hidden = !hasMore;
      status.textContent = body.childElementCount ? '已读取本会话的续聊成果。' : '尚未上报续聊成果；旧客户端和未验证工作区可能没有成果记录。';
      load.textContent = '刷新续聊成果';
    } catch (error) {
      if (!disposed && !suspended && version === epoch) status.textContent = '续聊成果读取失败：' + errorText(error);
    } finally { if (version === epoch) { load.disabled = false; older.disabled = false; } }
  }
  return {
    update(id) { if (id === sessionId) return; cancel(); sessionId = id || ''; cursor = undefined; hasMore = false; older.hidden = true; body.replaceChildren(); load.hidden = !sessionId; load.textContent = '查看续聊成果'; status.textContent = sessionId ? '按需读取每轮续聊成果。' : ''; },
    suspend() { suspended = true; cancel(); }, resume() { suspended = false; },
    dispose() { disposed = true; cancel(); body.replaceChildren(); },
  };
}
