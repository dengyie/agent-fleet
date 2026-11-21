import {pagePath} from '../../routes.js';
import {mountInspector} from '../assistant/workspace.js';
import {el, button, link} from './dom.js';
import {RECEIPT_LABELS, AGENT_LABELS} from './state.js';

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
  function update({session, task, selection, operations, canControl, busy, features}) {
    const next = JSON.stringify([session, task, selection, canControl, busy, features]);
    if (next !== contextKey) {
      contextKey = next; context.replaceChildren(el('h2', 'assistant-section-title', '运行上下文'));
      function fact(label, value) {
        const row = el('div', 'native-context-row'); row.append(el('span', null, label), el('strong', null, value || '尚未提供')); context.append(row);
      }
      fact('电脑节点', session?.machine_id || task?.machine || selection?.machine);
      fact('Agent', session?.agent_family || task?.agent_type || selection?.agent_type);
      fact('项目', task?.project || selection?.project);
      fact('状态', session ? AGENT_LABELS[session.current_state] || session.current_state : task?.state || '等待发送');
      fact('采集质量', {exact: '精确记录', structured: '结构化记录', best_effort: '尽力采集'}[session?.capture_quality]);
      context.append(el('p', 'meta', '仅展示节点已产生的消息和工具记录。部分 Agent 在整条消息完成后同步内容。'));
      if (session) {
        context.append(link('查看原始时间线与接力', pagePath('session', session.session_id)));
        context.append(el('p', 'meta', '模型沿用电脑上的配置。本机配置详情可在原始会话面板查看。'));
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
    }
  }
  return {update, suspend() { if (confirmation.open) confirmation.close(); ui.suspend(); }, resume() { ui.resume(); }, dispose() { if (confirmation.open) confirmation.close(); ui.dispose(); }};
}
