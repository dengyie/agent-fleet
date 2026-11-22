import {pagePath} from '../../routes.js';
import {mountInspector} from '../assistant/workspace.js';
import {el, button, link} from './dom.js';
import {RECEIPT_LABELS, AGENT_LABELS, sessionTitle} from './state.js';

const ACTIONS = Object.freeze({pause_session: '暂停会话', resume_session: '恢复会话',
  terminate_session: '终止会话', quarantine_session: '隔离会话'});

export function mountNativeInspector(root, heading, {onControl}) {
  const inspector = el('dialog', 'assistant-inspector'); inspector.setAttribute('aria-label', '电脑会话工作面板');
  const context = el('section', 'native-context'), receipts = el('section', 'native-receipts');
  root.append(inspector);
  const ui = mountInspector(root, heading, inspector, [
    {id: 'native-context', title: '会话', sections: [context]},
    {id: 'native-receipts', title: '投递记录', sections: [receipts]},
  ]);
  const confirmation = el('dialog', 'conversation-dialog');
  confirmation.setAttribute('aria-label', '确认会话控制');
  const confirmationText = el('p'), confirm = button('确认执行', () => {
    const action = confirm.dataset.action; confirmation.close(); onControl(action);
  }, 'button-danger');
  const cancel = button('取消', () => confirmation.close());
  confirmation.append(el('h2', null, '确认会话控制'), confirmationText, cancel, confirm); root.append(confirmation);
  let contextKey = '', receiptKey = '';
  let durationTimer = null;
  function update({session, task, selection, operations, canControl, busy, features}) {
    const next = JSON.stringify([session, task, selection, canControl, busy, features]);
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
        const actions = canControl ? session.allowed_actions || [] : [];
        for (const [action, title] of Object.entries(ACTIONS)) {
          if (!actions.includes(action)) continue;
          const actionButton = button(title, () => {
            if (action === 'terminate_session' || action === 'quarantine_session') {
              confirmationText.textContent = '将对 ' + session.machine_id + ' 的当前会话执行“' + title + '”。' +
                (action === 'terminate_session' ? '这会终止会话进程，不能保证之后可继续。' : '这会隔离会话，影响后续运行。');
              confirm.dataset.action = action; confirmation.showModal();
            } else onControl(action);
          });
          actionButton.disabled = busy; context.append(actionButton);
        }
      } else if (task) context.append(link('查看启动任务', pagePath('task', task.task_id)));
    }
    const nextReceipts = JSON.stringify(operations);
    if (nextReceipts !== receiptKey) {
      receiptKey = nextReceipts; receipts.replaceChildren(el('h2', 'assistant-section-title', '本页投递记录'));
      receipts.append(el('p', 'meta', '命令投递成功表示已交给原会话，模型回复以对话记录为准。'));
      if (!operations.length) receipts.append(el('p', 'meta', '还没有从本页发出的操作。'));
      for (const op of [...operations].reverse()) {
        const detail = el('details', 'native-operation-detail');
        detail.append(el('summary', null, (op.text?.slice(0, 48) || ACTIONS[op.action] || '启动会话') + ' · ' + (RECEIPT_LABELS[op.status] || op.label)));
        const body = el('p', 'meta'); body.textContent = [op.command_id, op.error].filter(Boolean).join(' · '); detail.append(body); receipts.append(detail);
      }
      updateDurations();
    }
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
  return {update, suspend() { if (confirmation.open) confirmation.close(); clearInterval(durationTimer); ui.suspend(); }, resume() { ui.resume(); setDurationTimer(); }, dispose() { if (confirmation.open) confirmation.close(); clearInterval(durationTimer); ui.dispose(); }};
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
