import {uiIcon, pagePath} from '../routes.js';
import {sessionControls} from '../ui/session-capabilities.js';
import {conversationTargets, mountTargetPicker} from './conversation-target.js';
import {el, button, link, errorText} from './agent-chat/dom.js';
import {nativeDirectoryPath} from './agent-chat/paths.js';
import {createNativeChatService, createMessageJournal, createTaskJournal} from './agent-chat/service.js';
import {createNativeReader} from './agent-chat/live.js';
import {mergeNativeEvents, sessionTitle, agentName, AGENT_LABELS, TERMINAL_RECEIPTS, RECEIPT_LABELS, operationStatus, uncertainWrite} from './agent-chat/state.js';
import {createNativeMessages} from './agent-chat/messages.js';
import {mountNativeInspector} from './agent-chat/inspector.js';
import {CONTROL_LABELS} from './agent-chat/operations.js';
import {renderQuestions, disposeQuestions, suspendQuestions} from './session-questions.js';

const newToken = () => globalThis.crypto?.randomUUID?.() || 'native-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2);

export function mountAgentChat(target, {sessionId, taskId, machine, store, api, account, identitySignal, onIdentity}) {
  const service = createNativeChatService(api);
  let disposed = false, suspended = false, submitting = false, targetChoice = null, frozenTask = null;
  let session = null, task = null, events = [], liveCursor = null, beforeCursor = null, hasOlder = false, olderRequested = false;
  let readError = '', operations = [], readVersion = 0, authorityVerified = false;
  let journal, taskJournal;
  function restoreMessage() {
    let storage;
    try { if (account?.id) storage = window.sessionStorage; } catch (_) {}
    journal = createMessageJournal(storage, JSON.stringify(['fleet-native-message', window.FleetConfig?.apiBaseUrl || '/api', account?.id, sessionId]));
    const saved = journal.read();
    if (saved) operations.push({...saved, recovering: true, action: 'append_user_turn', status: saved.command_id ? 'queued' : 'unknown', label: '正在核对原发送'});
  }
  if (sessionId) restoreMessage();
  else if (!taskId && account?.id) {
    try {
      taskJournal = createTaskJournal(window.sessionStorage, JSON.stringify(['fleet-native-task', window.FleetConfig?.apiBaseUrl || '/api', account.id]));
      frozenTask = taskJournal.read();
      if (frozenTask) operations.push({text:frozenTask.instruction, action:'create_task', status:'failed', label:'启动结果待确认；可恢复原请求'});
    } catch (_) { taskJournal = createTaskJournal(null, ''); }
  }
  const deliveryCards = new Map();
  let renderedQuestionKey = '';
  const questionState = {disposed: false, suspended: false};
  const root = el('div', 'assistant-view native-chat-view'); target.replaceChildren(root);
  const heading = el('div', 'assistant-heading'), headingCopy = el('div');
  const title = el('h1', null, '新建电脑 Agent 对话');
  headingCopy.append(title, el('p', 'meta', '在你的电脑上运行，保留原生会话')); heading.append(headingCopy);
  const status = el('span', 'assistant-run-status', '准备就绪'); status.setAttribute('role', 'status'); heading.append(status); root.append(heading);
  const chat = el('section', 'assistant-chat'), transcript = el('div', 'assistant-transcript');
  const thread = el('div', 'assistant-thread native-thread'); thread.setAttribute('data-chat-scroll', ''); thread.setAttribute('aria-label', '电脑 Agent 对话'); thread.tabIndex = 0;
  const intro = el('div', 'assistant-intro'); intro.append(uiIcon('monitor', {size: 28}), el('h2', null, '让电脑上的 Agent 开始工作'), el('p', null, '选择电脑、项目与 Agent，然后发送你的目标。'));
  const pickerHost = el('section', 'native-target-picker'); pickerHost.setAttribute('aria-label', '新会话运行目标');
  const loading = el('p', 'native-loading meta', '正在加载会话…'); loading.setAttribute('role', 'status');
  const historyNotice = el('p', 'native-history-notice meta');
  const messageHost = el('div', 'assistant-messages'), pendingHost = el('div', 'native-pending');
  const questions = el('section', 'native-questions');
  const previousPage = button('加载更早记录', () => { olderRequested = true; paint(); reader.wake(); }); previousPage.hidden = true;
  const anchor = el('auto-scroll-anchor');
  thread.append(intro, pickerHost, loading, historyNotice, previousPage, messageHost, questions, pendingHost, anchor);
  transcript.append(thread); chat.append(transcript);
  const dock = el('div', 'composer-dock'), notice = el('div', 'native-chat-notice'); notice.setAttribute('role', 'status');
  const form = el('form', 'assistant-composer'), prompt = el('chat-prompt-input'), input = el('textarea');
  input.rows = 2; input.maxLength = 2000; input.setAttribute('aria-label', '给电脑 Agent 的消息'); input.placeholder = '描述你希望 Agent 完成的工作…';
  const actions = el('div', 'composer-tools'), destination = el('span', 'native-composer-target meta');
  const send = el('button'); send.type = 'submit'; send.setAttribute('aria-label', '发送给电脑 Agent'); send.append(uiIcon('arrow-up', {size: 17}), el('span', null, '发送'));
  actions.append(destination, send); prompt.append(input, actions); form.append(prompt);
  dock.append(notice, form, el('p', 'composer-hint', 'Enter 发送 · Shift + Enter 换行 · 切换页面后任务继续运行')); chat.append(dock); root.append(chat);
  const sourceLinks = conversationTargets('computer'); intro.prepend(sourceLinks);
  const inspector = mountNativeInspector(root, heading, {api, onControl: control, service});
  const renderMessages = createNativeMessages(messageHost);
  let picker = null;
  const directoryLink = link('打开已有电脑会话', nativeDirectoryPath(), 'native-directory-link'); intro.append(directoryLink);

  function features() { return store?.getState().status?.features || {}; }
  function pendingOperation() { return operations.find(op => op.status === 'submitting' || op.status === 'unknown' || op.command_id && !TERMINAL_RECEIPTS.has(op.status)); }
  function questionKey() {
    return JSON.stringify([events.filter(event => event.kind === 'interaction_request' || event.kind === 'interaction_response'),
      session?.current_state, session?.status, session?.allowed_actions, features().append_user_turn, authorityVerified,
      [...(questionState.questions?.entries() || [])].map(([id, draft]) => [id, [...draft.selected], draft.busy, draft.status, draft.closed, draft.command])]);
  }
  function paint({preserveScroll = false} = {}) {
    if (disposed) return;
    const controls = sessionControls(session, features());
    const pending = pendingOperation();
    title.textContent = session ? sessionTitle(session, events) : task ? '正在连接 ' + agentName(task.agent_type) + ' 会话' : '新建电脑 Agent 对话';
    status.textContent = readError ? '连接暂不可用' : session ? AGENT_LABELS[session.current_state] || '状态待确认' : task ? ({queued:'等待节点启动', leased:'节点已领取', running:'等待绑定原生会话', succeeded:'任务已完成', failed:'启动失败', cancelled:'已取消', expired:'启动已过期', paused:'任务已暂停'}[task.state] || task.state) : submitting ? '正在提交' : '准备就绪';
    status.dataset.state = readError ? 'unknown' : session?.current_state || task?.state || 'idle';
    intro.hidden = !!sessionId || !!taskId || submitting;
    pickerHost.hidden = !!sessionId || !!taskId;
    loading.hidden = !((sessionId && !session || taskId && !task) && !readError);
    historyNotice.textContent = hasOlder ? '已显示最近记录，可向上加载更早内容。新消息会持续同步。' : '';
    previousPage.hidden = !hasOlder; previousPage.disabled = olderRequested;
    previousPage.textContent = olderRequested ? '正在加载更早记录…' : '加载更早记录';
    const count = renderMessages(events);
    if (session && !count && !readError) historyNotice.textContent ||= '尚未同步到消息。节点产生内容后会自动更新。';
    const visibleOperations = operations.slice(-5);
    for (const [op, entry] of deliveryCards) if (!visibleOperations.includes(op)) { entry.card.remove(); deliveryCards.delete(op); }
    for (const op of visibleOperations) {
      const previous = deliveryCards.get(op), signature = JSON.stringify(op);
      if (previous?.signature === signature) continue;
      const done = TERMINAL_RECEIPTS.has(op.status) || op.status === 'reviewed' || op.status === 'task_queued';
      const card = el(done ? 'details' : 'article', 'native-delivery-card');
      if (done && previous?.card.open) card.open = true;
      card.append(el(done ? 'summary' : 'strong', null, (op.text ? '本次发送' : '会话控制') + ' · ' + operationStatus(op)), el('p', 'native-delivery-text', op.text || op.description || op.action));
      card.append(el('p', 'meta', operationStatus(op)));
      if (op.error) card.append(el('p', 'meta', op.error));
      if (op.status === 'unknown') {
        card.append(link('查看原始记录确认投递', sessionId ? pagePath('session', sessionId) : taskId ? pagePath('task', taskId) : nativeDirectoryPath()));
        if (op.client_token) card.append(button('恢复原发送', () => deliverMessage(op)));
        else card.append(button('已核对，继续编辑', () => { op.status = 'reviewed'; op.label = '已由用户核对，未重新发送'; paint(); }));
      }
      if (previous) previous.card.replaceWith(card);
      else pendingHost.append(card);
      deliveryCards.set(op, {card, signature});
    }
    // Questions own their drafts and original command IDs; reuse their lifecycle.
    if (session && renderedQuestionKey !== questionKey()) {
      questions.replaceChildren();
      renderQuestions(questions, {events, session, api, state: questionState, paint,
        enabled: authorityVerified && features().append_user_turn === true});
      renderedQuestionKey = questionKey();
    }
    const busy = submitting || !!pending || !!frozenTask;
    input.readOnly = busy || !!taskId && !sessionId;
    const canSend = sessionId ? authorityVerified && controls.appendUserTurn : !taskId && targetChoice?.ready;
    input.disabled = !!sessionId && !controls.appendUserTurn && !busy;
    send.disabled = busy || !canSend;
    picker?.setLocked(busy);
    destination.textContent = session ? [agentName(session.agent_family), session.machine_id].filter(Boolean).join(' · ') : [agentName(targetChoice?.agent_type), targetChoice?.project].filter(Boolean).join(' · ');
    notice.replaceChildren();
    if (readError) notice.append(el('p', 'err', '读取暂时失败：' + readError), button('重新连接', () => reader.wake()));
    const last = operations.at(-1);
    if (last?.status === 'failed') notice.append(el('p', 'err', last.error || '操作失败，请查看投递记录。'));
    if (sessionId && session && !controls.appendUserTurn && !pending) notice.append(el('p', 'meta', controls.deliveryLabel || '当前会话只可阅读。可用控制以节点确认的权限为准。'));
    if (frozenTask && !submitting) notice.append(el('p', 'meta', '启动提交结果待确认，重试将沿用原启动标识和内容。'), button('查询或重试同一启动', submit));
    const resultTaskId = task?.task_id || events.find(event =>
      ['session_start', 'session_metadata'].includes(event.kind) && event.payload?.task_id)?.payload.task_id;
    inspector.update({session, task, resultTaskId, selection: targetChoice, operations, busy, authorityVerified, features: features()});
    const follow = anchor.isAtBottom !== false;
    anchor.toggleAttribute('is-streaming', !!session && session.current_state === 'running');
    if (follow && !suspended && !preserveScroll) anchor.scrollToBottom(true);
  }

  async function read(signal, current) {
    if (!sessionId && !taskId) return;
    const version = readVersion;
    if (!sessionId) {
      const result = await service.readTask(taskId, signal);
      if (!current() || version !== readVersion) return;
      task = result; readError = '';
      if (result.session_id) {
        sessionId = result.session_id; readVersion++; onIdentity?.({sessionId});
        restoreMessage();
        window.dispatchEvent(new Event('fleet-native-sessions-updated'));
        reader.wake();
      }
      paint(); return;
    }
    const id = sessionId;
    const mode = liveCursor === null ? 'tail' : olderRequested ? 'before' : 'after';
    const position = mode === 'tail' ? {tail: true} : mode === 'before' ? {beforeCursor} : {afterCursor: liveCursor};
    const result = await service.readSession(id, {position, signal});
    if (!current() || version !== readVersion || sessionId !== id) return;
    session = result.session || session; authorityVerified = result.authorityVerified;
    readError = result.error ? errorText(result.error) : '';
    if (result.page) {
      // History grows only on explicit page requests. Live reads retain that
      // window size; trimming updates the older boundary so it is reloadable.
      const budget = mode === 'before' ? events.length + result.page.events.length : Math.max(500, events.length);
      const merged = mergeNativeEvents(events, result.page.events, budget);
      events = merged.events;
      if (mode !== 'before') liveCursor = result.page.next_cursor;
      if (mode !== 'after') hasOlder = result.page.has_more;
      if (merged.trimmed) hasOlder = true;
      beforeCursor = events[0]?.cursor || result.page.before_cursor;
      if (mode === 'before') olderRequested = false;
      if (mode === 'after' && result.page.has_more) reader.wake();
    }
    // Status checks never resend messages or control commands.
    for (const op of operations.filter(op => op.command_id && !TERMINAL_RECEIPTS.has(op.status))) {
      try {
        const receipt = await service.receipt(op.command_id, id, signal);
        if (!current() || version !== readVersion) return;
        op.status = receipt.status; op.label = RECEIPT_LABELS[receipt.status] || receipt.status;
        op.error = ['failed', 'expired', 'rejected', 'cancelled'].includes(receipt.status) ? receipt.reason || '' : '';
        if (op.client_token && TERMINAL_RECEIPTS.has(op.status)) journal.clear();
      } catch (error) { if (!current()) return; op.error = '命令已入队，回执暂不可读，将继续查询。'; }
    }
    const scrollTop = thread.scrollTop, scrollHeight = thread.scrollHeight;
    paint({preserveScroll: mode === 'before'});
    if (mode === 'before') thread.scrollTop = scrollTop + thread.scrollHeight - scrollHeight;
    if (result.error) throw result.error;
  }
  const reader = createNativeReader({read, onError(error) { authorityVerified = false; readError = errorText(error); olderRequested = false; paint({preserveScroll: true}); }});

  async function deliverMessage(op) {
    if (disposed || submitting) return;
    submitting = true; op.status = 'submitting'; op.error = ''; op.label = '正在发送…';
    journal.write(op); paint();
    try {
      const data = await service.send(sessionId, op);
      if (disposed) return;
      if (!data.command_id || data.session_id !== sessionId) throw new Error('消息投递回执无效，请恢复原发送确认');
      op.command_id = data.command_id; op.status = data.status === 'pending' ? 'queued' : data.status;
      op.label = RECEIPT_LABELS[op.status] || '已入队，等待节点投递';
      if (TERMINAL_RECEIPTS.has(op.status)) journal.clear(); else journal.write(op);
      reader.wake();
    } catch (error) {
      if (disposed) return;
      // Even a 409 during recovery can be a token conflict. Preserve the
      // original operation rather than enabling a replacement by accident.
      op.status = uncertainWrite(error) || op.recovering ? 'unknown' : 'failed';
      op.error = errorText(error); op.label = op.status === 'unknown' ? '发送结果待确认' : '请求未被接受';
      if (op.status === 'failed') { journal.clear(); input.value = op.text; }
    } finally {
      op.recovering = true; submitting = false; if (!disposed) paint();
    }
  }

  async function submit(event) {
    event?.preventDefault();
    if (disposed || submitting || pendingOperation()) return;
    const text = frozenTask?.instruction || input.value.trim();
    if (!text || [...text].length > 2000) return;
    if (sessionId && (!authorityVerified || !sessionControls(session, features()).appendUserTurn)) return;
    if (!sessionId && (taskId || !frozenTask && !targetChoice?.ready)) return;
    if (sessionId) {
      const op = {text, client_token: newToken(), action: 'append_user_turn', status: 'submitting'};
      operations = operations.slice(-39); operations.push(op); input.value = ''; input.dispatchEvent(new Event('input'));
      await deliverMessage(op); return;
    }
    const recoveringTask = !!frozenTask;
    submitting = true;
    const op = {text, action: sessionId ? 'append_user_turn' : 'create_task', status: 'submitting', label: '正在发送…'};
    operations = operations.slice(-39); operations.push(op); input.value = ''; input.dispatchEvent(new Event('input')); paint();
    try {
      if (!sessionId) {
        frozenTask ||= Object.freeze({machine: targetChoice.machine, project: targetChoice.project,
          agent_type: targetChoice.agent_type, instruction: text, client_token: newToken()});
        taskJournal?.write(frozenTask);
        const data = await service.create(frozenTask);
        if (disposed) return;
        task = data.task; taskId = task.task_id; frozenTask = null; taskJournal?.clear();
        op.status = 'task_queued'; op.label = '启动请求已入队';
        onIdentity?.({taskId});
        window.dispatchEvent(new Event('fleet-native-sessions-updated'));
      }
      reader.wake();
    } catch (error) {
      if (disposed) return;
      op.error = errorText(error);
      if (uncertainWrite(error) || recoveringTask) {
        op.status = frozenTask ? 'failed' : 'unknown';
        op.label = '发送结果待确认，请先查看原始会话。';
      } else {
        if (!sessionId) { frozenTask = null; taskJournal?.clear(); }
        op.status = 'failed'; op.label = '请求未被接受'; input.value = text;
      }
      // Failed/unknown submissions stay visible in the receipt inspector.
      notice.replaceChildren(el('p', 'err', op.error));
    } finally { submitting = false; if (!disposed) paint(); }
  }
  async function control(action, payload, label) {
    const controls = sessionControls(session, features());
    if (disposed || suspended || submitting || pendingOperation() || !authorityVerified || !controls.actions.includes(action)) return;
    if (action === 'apply_local_profile' && (!controls.applyLocalProfile || !payload?.profile_id)) return;
    submitting = true;
    const op = {action, description: label || CONTROL_LABELS[action], status: 'submitting', label: '控制请求提交中…'}; operations = operations.slice(-39); operations.push(op); paint();
    try {
      const result = await service.control(sessionId, action, payload);
      if (disposed) return;
      if (!result.command_id || result.session_id !== sessionId) throw new Error('控制回执无效');
      op.command_id = result.command_id; op.status = result.status === 'pending' ? 'queued' : result.status;
      reader.wake();
    } catch (error) {
      if (disposed) return;
      op.status = uncertainWrite(error) ? 'unknown' : 'failed'; op.label = '控制结果待确认'; op.error = errorText(error);
    } finally { submitting = false; if (!disposed) paint(); }
  }
  form.addEventListener('submit', submit);
  let lastKey = '';
  const unsubscribe = store.subscribe(() => {
    const state = store.getState(), summary = state.sessionSummaries[sessionId];
    const key = JSON.stringify([summary, state.tasks[taskId], state.connection, state.status?.features]);
    if (key !== lastKey) { lastKey = key; reader.wake(); }
  });
  const visibility = () => {
    questionState.suspended = document.hidden || suspended;
    if (questionState.suspended) { authorityVerified = false; reader.stop(); suspendQuestions(questionState); paint({preserveScroll: true}); }
    else { reader.start(); renderedQuestionKey = ''; paint(); }
  };
  const focus = () => reader.wake();
  document.addEventListener('visibilitychange', visibility); window.addEventListener('focus', focus);
  const dispose = () => {
    if (disposed) return; disposed = true; questionState.disposed = true;
    reader.dispose(); unsubscribe(); picker?.dispose(); inspector.dispose(); disposeQuestions(questionState);
    document.removeEventListener('visibilitychange', visibility); window.removeEventListener('focus', focus);
    identitySignal?.removeEventListener('abort', dispose);
  };
  dispose.suspend = () => { suspended = true; authorityVerified = false; questionState.suspended = true; reader.stop(); suspendQuestions(questionState); inspector.suspend(); paint({preserveScroll: true}); };
  dispose.resume = () => { suspended = false; questionState.suspended = false; inspector.resume(); visibility(); };
  identitySignal?.addEventListener('abort', dispose, {once: true});
  if (!sessionId && !taskId) picker = mountTargetPicker(pickerHost, {api, store, machine, onChange(choice) { targetChoice = choice; paint(); }});
  paint(); visibility(); return dispose;
}
