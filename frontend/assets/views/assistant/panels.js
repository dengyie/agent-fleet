import { renderRequestMetadata, tickRequestMetadata } from './request-metadata.js';
import { uiIcon, pagePath } from '../../routes.js';
import { getPlatformArtifactContentUrl } from '../../api/platform.js';

export function el(tag, className, text) { var node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; }
export function clear(node) { while (node && node.firstChild) node.removeChild(node.firstChild); }
export function showError(host, error) { var text = (error && (error.detail || error.message || error.code)) || '平台请求失败'; if (error && error.requestId) text += ' · 请求编号 ' + error.requestId; host.appendChild(el('div', 'err', text)); }
export function renderMessages(host, messages, runs = []) {
  if (!host._messageRows) host._messageRows = new Map();
  const source = Array.isArray(messages) ? messages : [];
  const replies = new Map(source.filter(m => m.role !== 'user').map(m => [m.message_id, m]));
  const byTrigger = new Map(runs.map(run => [run.trigger_message_id, run]));
  const runReplies = new Set(runs.map(run => 'reply_' + run.run_id));
  const projected = [];
  source.forEach((message, index) => {
    if (message.role !== 'user' && runReplies.has(message.message_id)) return;
    projected.push({message, key: message.message_id || 'message-' + index});
    const run = byTrigger.get(message.message_id);
    if (!run) return;
    const key = 'reply_' + run.run_id;
    const reply = replies.get(key) || {role: 'assistant', content: run.result_text || '', message_id: key};
    projected.push({message: reply, key, run});
  });
  const keep = new Set();
  projected.forEach(({message, key, run}, index) => {
    keep.add(key);
    let row = host._messageRows.get(key);
    if (!row) {
      row = el('article', 'assistant-message ' + (message.role === 'user' ? 'user' : 'assistant'));
      row.appendChild(el('span', 'assistant-message-role', message.role === 'user' ? '你' : 'Agent Fleet'));
      const body = el('stream-markdown'); row.appendChild(body);
      const copy = el('button', 'message-copy'); copy.type = 'button'; copy.title = '复制消息'; copy.setAttribute('aria-label', '复制消息'); copy.appendChild(uiIcon('copy', {size: 14}));
      copy.addEventListener('click', async function () { try { await navigator.clipboard.writeText(row._content || ''); copy.setAttribute('aria-label', '已复制'); copy.title = '已复制'; } catch (_) { copy.title = '复制失败，请手动选择文本'; } });
      row.appendChild(copy);
      const requests = el('div', 'message-requests'); row.appendChild(requests);
      row._parts = {body, copy, requests}; host._messageRows.set(key, row);
    }
    row._runId = run?.run_id;
    const content = message.content || '';
    if (row._content !== content) {
      row._content = content; row._parts.body.textContent = content; row._parts.body.setAttribute('content', content);
    }
    row._parts.copy.hidden = !content;
    if (run) renderRequestMetadata(row._parts.requests, run.requests);
    if (host.children[index] !== row) host.insertBefore(row, host.children[index] || null);
  });
  host._messageRows.forEach((row, key) => { if (!keep.has(key)) { host.removeChild(row); host._messageRows.delete(key); } });
}
export function updateRunRequests(host, run) {
  host._messageRows?.forEach(row => {
    if (row._runId === run.run_id) renderRequestMetadata(row._parts.requests, run.requests);
  });
}
export function tickMessageRequests(host) {
  let active = false;
  host._messageRows?.forEach(row => { if (tickRequestMetadata(row._parts.requests)) active = true; });
  return active;
}
export function renderEvents(host, events) {
  (Array.isArray(events) ? events : []).forEach(function (event) {
    if (event.kind === 'provider_request_started' || event.kind === 'provider_request_finished') return;
    var payload = event.payload || {};
    if (event.kind === 'tool_call' || event.kind === 'tool_result') {
      var card = Array.from(host.children).find(function (node) { return node.getAttribute('data-command-id') === payload.command_id; });
      if (!card) { card = el('tool-call-badge'); card.setAttribute('data-command-id', payload.command_id || String(event.sequence)); host.appendChild(card); }
      card.setAttribute('name', payload.tool || payload.command_id || '工具执行');
      card.setAttribute('status', event.kind === 'tool_call' ? 'running' : payload.state === 'succeeded' ? 'success' : payload.state === 'unknown' ? 'unknown' : 'error');
      if (event.kind === 'tool_call') card.setAttribute('args', JSON.stringify(payload.argument_keys || []));
      if (event.kind === 'tool_result') card.setAttribute('output', JSON.stringify(payload));
    } else {
      var labels = {run_started: '开始运行', run_finished: '运行结束', run_failed: '运行失败', run_unknown: '运行结果待确认', memory_context_selected: '已载入记忆上下文'};
      var text = labels[event.kind] || event.kind;
      if (event.kind === 'run_finished') text += '：' + ({succeeded: '已完成', failed: '执行失败', unknown: '结果待确认', cancelled: '已取消'}[payload.state] || payload.state || '');
      if (payload.provider_error || payload.error_code) text += ' · ' + (payload.provider_error || payload.error_code);
      if (payload.upstream_code) text += ' · ' + payload.upstream_code;
      if (payload.provider_status) text += ' · HTTP ' + payload.provider_status;
      if (payload.step) text += ' · 步骤 ' + payload.step;
      host.appendChild(el('div', 'assistant-event', text));
    }
  });
  while (host.children.length > 200) host.removeChild(host.firstChild);
}
function renderExecutionWindowEvents(host, events) {
  clear(host);
  var rows = Array.isArray(events) ? events.slice(-100) : [];
  if (!rows.length) { host.appendChild(el('div', 'assistant-window-empty meta', '暂无窗口事件')); return; }
  rows.forEach(function (event) {
    if (!event || typeof event !== 'object') return;
    var payload = event.payload && typeof event.payload === 'object' ? event.payload : {};
    var text = event.kind || 'event';
    if (event.kind === 'status') text += ': ' + String(payload.state || payload.detail || '状态更新');
    else if (event.kind === 'text' || event.kind === 'output') text += ': ' + String(payload.text || '');
    else if (event.kind === 'notice') text += ': ' + String(payload.message || payload.code || '');
    else if (event.kind === 'input_ack') text += ': ' + (payload.accepted ? 'accepted' : 'rejected');
    host.appendChild(el('div', 'assistant-window-event', text));
  });
}
export function renderExecutionWindow(host, state, onClose) {
  clear(host);
  if (state.loading) { host.appendChild(el('div', 'assistant-window-status meta', '执行窗口恢复中')); return; }
  if (state.error) { host.appendChild(el('div', 'assistant-window-status err', state.error)); return; }
  if (!state.window) { host.appendChild(el('div', 'assistant-window-status meta', '尚未创建执行窗口')); return; }
  var row = el('div', 'assistant-window-summary');
  var copy = el('div', 'assistant-window-copy');
  copy.appendChild(el('strong', null, '执行窗口'));
  copy.appendChild(el('span', 'assistant-window-meta', String(state.window.state || 'unknown') + ' · ' + String(state.mode || 'unknown')));
  if (state.window.window_id) copy.appendChild(el('span', 'assistant-window-meta', state.window.window_id));
  row.appendChild(copy);
  var actions = el('div', 'assistant-window-actions');
  if (state.window.state !== 'closed' && state.window.state !== 'expired') {
    var close = document.createElement('button'); close.type = 'button'; close.className = 'button-secondary'; close.textContent = '关闭执行窗口';
    close.addEventListener('click', onClose); actions.appendChild(close);
  }
  row.appendChild(actions); host.appendChild(row);
  var events = el('div', 'assistant-window-events'); renderExecutionWindowEvents(events, state.events); host.appendChild(events);
}
function formatArtifactSize(size) {
  if (typeof size !== 'number' || !isFinite(size) || size < 0) return '大小未知';
  if (size < 1024) return String(size) + ' B';
  if (size < 1024 * 1024) return (size / 1024).toFixed(1) + ' KB';
  return (size / (1024 * 1024)).toFixed(1) + ' MB';
}
function previewableContentType(contentType) {
  return ['text/plain', 'text/markdown', 'text/csv', 'application/json'].indexOf(contentType) !== -1;
}
export function renderArtifacts(host, state, onPreview) {
  clear(host);
  if (state.loading) { host.appendChild(el('div', 'assistant-artifact-status meta', '加载中')); return; }
  if (state.error) { host.appendChild(el('div', 'assistant-artifact-status err', state.error)); return; }
  var artifacts = Array.isArray(state.artifacts) ? state.artifacts : [];
  if (!artifacts.length) {
    var empty = el('div', 'assistant-artifact-empty'); empty.appendChild(uiIcon('layers', {size: 28}));
    empty.appendChild(el('strong', null, '工作成果，会出现在这里'));
    empty.appendChild(el('p', null, '助手生成的文件将保存在工作区，你可以在这里预览和下载。'));
    host.appendChild(empty); return;
  }
  var rendered = 0;
  artifacts.forEach(function (artifact) {
    if (!artifact || typeof artifact !== 'object' || typeof artifact.artifact_id !== 'string' || !artifact.artifact_id || typeof artifact.name !== 'string' || !artifact.name) return;
    var href;
    try { href = getPlatformArtifactContentUrl(artifact.artifact_id, state.workspaceId); } catch (error) { return; }
    var row = el('div', 'assistant-artifact');
    var copy = el('div', 'assistant-artifact-copy');
    var artifactTitle = el('div', 'assistant-artifact-title'); artifactTitle.appendChild(uiIcon('code', {size: 16})); artifactTitle.appendChild(el('strong', null, artifact.name)); copy.appendChild(artifactTitle);
    copy.appendChild(el('span', 'assistant-artifact-meta', formatArtifactSize(artifact.size) + ' · ' + (artifact.content_type || '文件')));
    if (typeof artifact.sha256 === 'string' && artifact.sha256) copy.appendChild(el('span', 'assistant-artifact-digest', 'SHA-256 ' + artifact.sha256.slice(0, 16)));
    row.appendChild(copy);
    var actions = el('div', 'assistant-artifact-actions');
    var link = document.createElement('a'); link.className = 'button-secondary assistant-artifact-download'; link.href = href; link.download = artifact.name; link.rel = 'noopener'; link.textContent = '下载'; actions.appendChild(link);
    var preview = state.previewById && state.previewById[artifact.artifact_id];
    if (previewableContentType(artifact.content_type)) {
      var previewButton = document.createElement('button'); previewButton.type = 'button'; previewButton.className = 'button-secondary assistant-artifact-preview-toggle'; previewButton.textContent = preview && preview.open ? '收起预览' : '预览';
      previewButton.addEventListener('click', function () { if (typeof onPreview === 'function') onPreview(artifact.artifact_id); });
      actions.appendChild(previewButton);
    }
    row.appendChild(actions);
    if (preview && preview.open) {
      var previewHost = el('div', 'assistant-artifact-preview-wrap');
      if (preview.loading) previewHost.appendChild(el('div', 'assistant-artifact-status meta', '预览加载中'));
      else if (preview.error) previewHost.appendChild(el('div', 'assistant-artifact-status err', preview.error));
      else if (preview.data && typeof preview.data.text === 'string') {
        previewHost.appendChild(el('pre', 'assistant-artifact-preview', preview.data.text));
        if (preview.data.truncated) previewHost.appendChild(el('div', 'assistant-artifact-status meta', '预览已截断（最多 64 KiB）'));
      }
      row.appendChild(previewHost);
    }
    host.appendChild(row);
    rendered += 1;
  });
  if (!rendered) host.appendChild(el('div', 'assistant-artifact-status meta', '暂无产物'));
}
export function renderLegacyProjection(host, link) {
  clear(host);
  var projection = link && link.projection && typeof link.projection === 'object' ? link.projection : {};
  var summary = typeof projection.summary === 'string' ? projection.summary : '';
  if (summary) host.appendChild(el('p', null, '摘要：' + summary));
  var diff = projection.diff && typeof projection.diff === 'object' ? projection.diff : {};
  if (diff.stat || diff.has_patch) host.appendChild(el('p', null, '差异：' + (diff.stat || '有变更') + ' · ' + String(diff.patch_bytes || 0) + ' bytes'));
  var tests = projection.tests && typeof projection.tests === 'object' ? projection.tests : {};
  if (Object.keys(tests).length) host.appendChild(el('p', null, '测试：' + (tests.framework || '未指定') + ' · 通过 ' + String(tests.passed || 0) + ' · 失败 ' + String(tests.failed || 0) + ' · 跳过 ' + String(tests.skipped || 0)));
  var files = Array.isArray(projection.files) ? projection.files.slice(0, 50) : [];
  files.forEach(function (file) { if (!file || typeof file !== 'object') return; host.appendChild(el('div', 'assistant-legacy-file', String(file.path || '文件') + ' · ' + String(file.bytes || 0) + ' bytes')); });
}
export function renderUnknownCommands(host, commands, onReconcile) {
  clear(host);
  (Array.isArray(commands) ? commands : []).forEach(function (command) {
    var row = el('div', 'assistant-unknown-command');
    var meta = el('div', 'assistant-unknown-meta');
    meta.appendChild(el('strong', null, command.command_id || 'unknown command'));
    meta.appendChild(el('span', null, (command.action || 'command') + ' @ ' + (command.target_node || 'node')));
    var reason = command.result && command.result.reason ? command.result.reason : '执行结果未知';
    meta.appendChild(el('span', 'meta', reason)); row.appendChild(meta);
    var actions = el('div', 'assistant-unknown-actions');
    [['confirmed_succeeded', '人工确认成功'], ['confirmed_failed', '人工确认失败'], ['remains_unknown', '保持未知']].forEach(function (item) {
      var button = document.createElement('button'); button.type = 'button'; button.className = 'button-secondary'; button.textContent = item[1];
      button.addEventListener('click', function () { onReconcile(command.command_id, item[0], button); }); actions.appendChild(button);
    });
    if (command.action === 'tool.workspace.write' || command.action === 'service.inspect') {
      var check = document.createElement('button'); check.type = 'button'; check.className = 'button-secondary'; check.textContent = command.action === 'service.inspect' ? '检查服务' : '检查工作区';
      check.addEventListener('click', function () { onReconcile(command.command_id, 'postcheck', check); }); actions.appendChild(check);
    }
    row.appendChild(actions); host.appendChild(row);
  });
}
export function memoryReference(item) {
  if (!item || typeof item !== 'object' || typeof item.memory_id !== 'string' || !item.memory_id) return null;
  return {
    memory_id: item.memory_id,
    title: typeof item.title === 'string' ? item.title : '',
    kind: typeof item.kind === 'string' ? item.kind : '',
    revision: typeof item.revision === 'number' && isFinite(item.revision) ? item.revision : 0,
  };
}
export function renderMemoryItems(host, state, onToggle) {
  clear(host);
  if (state.loading) { host.appendChild(el('div', 'assistant-memory-status meta', '记忆上下文加载中')); return; }
  if (state.error) { host.appendChild(el('div', 'assistant-memory-status err', state.error)); return; }
  var rows = Array.isArray(state.items) ? state.items : [];
  if (!rows.length) { host.appendChild(el('div', 'assistant-memory-status meta', '暂无匹配记忆')); return; }
  rows.forEach(function (item) {
    var reference = memoryReference(item);
    if (!reference) return;
    var row = el('label', 'assistant-memory-row');
    var checkbox = document.createElement('input'); checkbox.type = 'checkbox';
    checkbox.checked = Boolean(state.selected[reference.memory_id]);
    checkbox.disabled = !checkbox.checked && Object.keys(state.selected).length >= state.maxItems;
    checkbox.addEventListener('change', function () { onToggle(reference, checkbox.checked); });
    row.appendChild(checkbox);
    var copy = el('span', 'assistant-memory-copy');
    copy.appendChild(el('strong', null, reference.title || reference.memory_id));
    copy.appendChild(el('span', 'assistant-memory-meta', (reference.kind || 'memory') + ' · revision ' + String(reference.revision)));
    row.appendChild(copy); host.appendChild(row);
  });
}
export function renderSelectedMemoryItems(host, state, onRemove) {
  clear(host);
  var selected = state.order.map(function (id) { return state.selected[id]; }).filter(Boolean);
  if (!selected.length) { host.appendChild(el('div', 'assistant-memory-status meta', '未选择记忆')); return; }
  selected.forEach(function (reference) {
    var row = el('div', 'assistant-memory-selected');
    var copy = el('span', 'assistant-memory-copy');
    copy.appendChild(el('strong', null, reference.title || reference.memory_id));
    copy.appendChild(el('span', 'assistant-memory-meta', reference.memory_id + ' · revision ' + String(reference.revision)));
    row.appendChild(copy);
    var remove = document.createElement('button'); remove.type = 'button'; remove.className = 'button-secondary'; remove.title = '移除记忆'; remove.appendChild(uiIcon('x', { size: 13 }));
    remove.addEventListener('click', function () { onRemove(reference.memory_id); });
    row.appendChild(remove); host.appendChild(row);
  });
}
