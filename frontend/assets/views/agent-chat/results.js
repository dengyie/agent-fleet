// Retained task snapshots through public HTTP only. No node filesystem access.
import {pagePath} from '../../routes.js';
import {el, button, link, errorText} from './dom.js';

export function mountNativeResults(root, service) {
  let identity = '', taskId = '', sessionId = '', controller = null;
  let disposed = false, suspended = false, loaded = false, version = 0;
  const heading = el('h2', 'assistant-section-title', '原任务结果');
  const note = el('p', 'meta', '这里展示原启动任务结束时保留的快照。续聊产生的修改和测试不会更新这份快照。');
  const load = button('查看原任务结果', () => refresh());
  const status = el('p', 'meta'); status.setAttribute('role', 'status');
  const body = el('div', 'native-result-body');
  root.append(heading, note, load, status, body);

  function cancel() { version++; controller?.abort(); controller = null; load.disabled = false; }
  function begin() {
    cancel(); controller = new AbortController();
    const signal = controller.signal, currentVersion = version;
    return {signal, current: () => !disposed && !suspended && version === currentVersion && !signal.aborted};
  }
  function textBlock(title, text, parent = body) {
    const detail = el('details', 'native-result-detail');
    detail.append(el('summary', null, title), el('pre', null, text)); parent.append(detail);
  }
  async function openFile(path, host, action) {
    if (disposed || suspended) return;
    const {signal, current} = begin(); action.disabled = true;
    try {
      const file = await service.readResultFile(taskId, sessionId, path, signal);
      if (!current()) return;
      host.replaceChildren();
      if (file.truncated || file.redacted) host.append(el('p', 'meta', [file.truncated ? '文件快照已截断' : '', file.redacted ? '敏感内容已脱敏' : ''].filter(Boolean).join(' · ')));
      host.append(el('pre', null, file.content));
    } catch (error) {
      if (current()) host.replaceChildren(el('p', 'err', '文件快照读取失败：' + errorText(error)));
    } finally { action.disabled = false; }
  }
  function render(result) {
    body.replaceChildren();
    const task = result.task, summary = task.result?.test_summary;
    body.append(link('打开原任务详情', pagePath('task', taskId)));
    status.textContent = '原任务状态：' + ({succeeded:'已完成',failed:'失败',cancelled:'已取消',running:'运行中',queued:'等待启动',leased:'节点已领取'}[task.state] || task.state);
    if (!task.result) body.append(el('p', 'meta', '原任务尚未上报结果；结束后可刷新。'));
    if (summary) {
      const tests = el('section', 'native-result-tests');
      tests.append(el('h3', null, '测试摘要'), el('p', null,
        [summary.framework, '通过 ' + (summary.passed ?? '未提供'), '失败 ' + (summary.failed ?? '未提供'),
          '跳过 ' + (summary.skipped ?? '未提供'), '错误 ' + (summary.errors ?? '未提供'),
          summary.duration_s == null ? '' : summary.duration_s + ' 秒'].filter(Boolean).join(' · ')));
      if (summary.failed_names?.length) textBlock('失败测试', summary.failed_names.join('\n'), tests);
      body.append(tests);
    } else body.append(el('p', 'meta', '未上报结构化测试摘要。'));
    if (task.result?.diff_stat) textBlock('变更统计', task.result.diff_stat);
    if (task.result?.log_summary) textBlock('任务日志摘要', task.result.log_summary);
    if (result.diff) {
      textBlock('保留的 diff' + (result.diff.truncated ? '（已截断）' : ''), result.diff.diff_patch);
    } else body.append(el('p', result.diffError ? 'err' : 'meta', result.diffError ? 'diff 读取失败：' + errorText(result.diffError) : '未保留 diff。'));
    const files = el('section', 'native-result-files'); files.append(el('h3', null, '保留的文件快照'));
    if (result.filesError) files.append(el('p', 'err', '文件目录读取失败：' + errorText(result.filesError)));
    else if (!result.files?.length) files.append(el('p', 'meta', '没有保留的文件快照。'));
    for (const file of result.files || []) {
      const detail = el('details', 'native-result-detail'), content = el('div');
      const action = button('读取文件快照', () => openFile(file.path, content, action));
      detail.append(el('summary', null, file.path + (file.truncated ? '（已截断）' : '') + (file.redacted ? '（已脱敏）' : '')), action, content);
      files.append(detail);
    }
    body.append(files);
  }
  async function refresh() {
    if (!taskId || disposed || suspended) return;
    const {signal, current} = begin(); load.disabled = true; status.textContent = '正在读取原任务快照…';
    body.replaceChildren(); loaded = false;
    try {
      const result = await service.readResults(taskId, sessionId, signal);
      if (!current()) return;
      loaded = true; render(result); load.textContent = '刷新原任务结果';
    } catch (error) {
      if (current()) status.textContent = '结果读取失败：' + errorText(error);
    } finally { if (current()) load.disabled = false; }
  }
  return {
    update(nextTaskId, nextSessionId) {
      const next = JSON.stringify([nextTaskId || '', nextSessionId || '']);
      if (next === identity) return;
      identity = next; cancel(); taskId = nextTaskId || ''; sessionId = nextSessionId || '';
      loaded = false; body.replaceChildren(); load.textContent = '查看原任务结果'; load.hidden = !taskId;
      status.textContent = taskId ? '按需读取原启动任务快照。' : '当前记录尚未提供原任务关联。可加载更早记录；从原生客户端导入的会话可能没有启动任务。';
    },
    suspend() { suspended = true; cancel(); if (!loaded && taskId) status.textContent = '读取已暂停，可重新查看原任务结果。'; },
    resume() { suspended = false; },
    dispose() { disposed = true; cancel(); body.replaceChildren(); },
  };
}
