import {pagePath} from '../../routes.js';
import {mountInspector} from '../assistant/workspace.js';
import {el, button, link} from './dom.js';
import {operationStatus, AGENT_LABELS, sessionTitle} from './state.js';
import {mountNativeOperations, CONTROL_LABELS} from './operations.js';
import {mountHandoffPanel} from '../session-handoff.js';

export function mountNativeInspector(root, heading, {api, onControl}) {
  const inspector = el('dialog', 'assistant-inspector'); inspector.setAttribute('aria-label', '电脑会话工作面板');
  const context = el('section', 'native-context'), receipts = el('section', 'native-receipts');
  const operationsHost = el('section', 'native-operations'), handoffHost = el('section', 'native-handoff');
  const operationsPanel = mountNativeOperations(operationsHost, {api, onControl, dialogHost: root});
  let handoff, handoffId = '';
  root.append(inspector);
  const ui = mountInspector(root, heading, inspector, [
    {id: 'native-context', title: '会话', sections: [context]},
    {id: 'native-operations', title: '操作', sections: [operationsHost], onSelect: () => operationsPanel.activate()},
    {id: 'native-handoff', title: '接力', sections: [handoffHost], hidden: true},
    {id: 'native-receipts', title: '投递记录', sections: [receipts]},
  ]);
  let contextKey = '', receiptKey = '';
  let durationTimer = null;
  function update({session, task, selection, operations, authorityVerified, busy, features}) {
    operationsPanel.update({session, authorityVerified, busy, features, operations});
    if (session && handoffId !== session.session_id) {
      handoff?.(); handoffId = session.session_id;
      handoff = mountHandoffPanel(handoffHost, {api, sessionId: handoffId});
      ui.setVisible('native-handoff', true);
    }
    const next = JSON.stringify([session, task, selection]);
    if (next !== contextKey) {
      contextKey = next; context.replaceChildren(el('h2', 'assistant-section-title', '运行上下文'));
      function fact(label, value) {
        const row = el('div', 'native-context-row'); row.append(el('span', null, label), el('strong', null, value || '尚未提供')); context.append(row);
      }
      function durationFact(startedAt, finishedAt) {
        const row = el('div', 'native-context-row'); row.append(el('span', null, '持续时间'));
        const value = el('strong', 'native-duration');
        value.dataset.startedAt = startedAt || '';
        value.dataset.finishedAt = finishedAt || '';
        row.append(value); context.append(row);
      }
      fact('电脑节点', session?.machine_id || task?.machine || selection?.machine);
      fact('会话名', session ? sessionTitle(session) : task?.agent_type || selection?.agent_type);
      fact('客户端', [session?.agent_family || task?.agent_type || selection?.agent_type,
        session?.adapter].filter(Boolean).join(' · '));
      fact('客户端版本', session?.client_version);
      fact('项目', task?.project || selection?.project);
      fact('Provider', session?.provider);
      fact('模型', session?.model);
      fact('状态', session ? AGENT_LABELS[session.current_state] || session.current_state : task?.state || '等待发送');
      fact('采集质量', {exact: '精确记录', structured: '结构化记录', best_effort: '尽力采集'}[session?.capture_quality]);
      const startedAt = session?.native_started_at || session?.started_at;
      fact(session?.native_started_at ? '原生开始' : '首次采集', formatDate(startedAt));
      fact('最后活动', formatDate(session?.updated_at));
      if (session) durationFact(startedAt, session.finished_at || '');
      if (session) {
        context.append(link('查看原始时间线与接力', pagePath('session', session.session_id)));
        context.append(button('管理会话与本机配置', () => ui.select('native-operations')));
      } else if (task) context.append(link('查看启动任务', pagePath('task', task.task_id)));
    }
    const nextReceipts = JSON.stringify(operations);
    if (nextReceipts !== receiptKey) {
      receiptKey = nextReceipts; receipts.replaceChildren(el('h2', 'assistant-section-title', '本页投递记录'));
      receipts.append(el('p', 'meta', '命令投递成功表示已交给原会话，模型回复以对话记录为准。'));
      if (!operations.length) receipts.append(el('p', 'meta', '还没有从本页发出的操作。'));
      for (const op of [...operations].reverse()) {
        const detail = el('details', 'native-operation-detail');
        detail.append(el('summary', null, (op.text?.slice(0, 48) || op.description || CONTROL_LABELS[op.action] || '启动会话') + ' · ' + operationStatus(op)));
        const body = el('p', 'meta'); body.textContent = [op.command_id, op.error].filter(Boolean).join(' · '); detail.append(body); receipts.append(detail);
      }
    }
    updateDurations();
  }
  function updateDurations() {
    for (const value of context.querySelectorAll('.native-duration')) {
      const start = Date.parse(value.dataset.startedAt || '');
      const end = value.dataset.finishedAt ? Date.parse(value.dataset.finishedAt) : Date.now();
      value.textContent = Number.isFinite(start) && Number.isFinite(end) && end >= start
        ? formatDuration(end - start) : '未提供';
    }
  }
  function setDurationTimer() {
    clearInterval(durationTimer); durationTimer = setInterval(updateDurations, 1000);
  }
  setDurationTimer();
  return {update,
    suspend() { operationsPanel.suspend(); handoff?.suspend?.(); clearInterval(durationTimer); ui.suspend(); },
    resume() { ui.resume(); operationsPanel.resume(); handoff?.resume?.(); setDurationTimer(); },
    dispose() { operationsPanel.dispose(); handoff?.(); clearInterval(durationTimer); ui.dispose(); }};
}

function formatDate(value) {
  if (!value) return '';
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? new Date(timestamp).toLocaleString() : '';
}

function formatDuration(milliseconds) {
  const seconds = Math.floor(milliseconds / 1000);
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  return [hours ? hours + '小时' : '', minutes ? minutes + '分' : '', remainder + '秒']
    .filter(Boolean).join(' ');
}
