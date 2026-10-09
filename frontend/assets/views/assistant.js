import { updateRunRequests, tickMessageRequests } from './assistant/panels.js';
/* Persistent assistant workspace: bounded DOM rendering and cursor recovery. */
import { uiIcon, pagePath } from '../routes.js';
import {
  getPlatformDefaults, getConversations,
  getPlatformArtifacts,
  getPlatformArtifactPreview,
  getPlatformMemories, searchPlatformMemories,
  createConversation, getConversation,
  appendConversationTurn, getRun, getRunEvents, cancelRun, getUnknownCommands,
  createPlatformLegacyTask, getPlatformLegacyTask,
  submitCommandReconcile, requestCommandPostcheck, getCommandPostcheck,
  createExecutionWindow, listExecutionWindows, getExecutionWindow, reconnectExecutionWindow,
  attachExecutionWindow, acquireExecutionWindowWriter,
  renewExecutionWindowWriter, releaseExecutionWindowWriter,
  closeExecutionWindow, getExecutionWindowEvents,
} from '../api/platform.js';

import { mountInspector } from './assistant/workspace.js';
import { el, clear, showError, renderMessages, renderEvents, renderExecutionWindow, renderArtifacts, renderLegacyProjection, renderUnknownCommands, memoryReference, renderMemoryItems, renderSelectedMemoryItems } from './assistant/panels.js';
function token() { if (typeof crypto !== 'undefined' && crypto.randomUUID) return crypto.randomUUID(); return 'turn-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2); }

export function mountAssistant(target, options) {
  options = options || {}; clear(target);
  // The shell supplies its verified identity; legacy operators have no account.
  var canAdminister = !options.account || options.account.role === 'admin';
  var root = el('div', 'assistant-view');
  var heading = el('div', 'assistant-heading'); var title = el('div'); var conversationTitle = el('h1', null, '新对话'); title.appendChild(conversationTitle); title.appendChild(el('p', 'meta', 'Agent Fleet · 主助手')); heading.appendChild(title); var status = el('span', 'assistant-run-status', '准备中'); heading.appendChild(status); root.appendChild(heading);
  var controls = el('div', 'assistant-controls'); var model = document.createElement('select'); var workspace = document.createElement('select'); model.setAttribute('aria-label', '模型'); workspace.setAttribute('aria-label', '工作区'); model.id = 'assistant-model'; workspace.id = 'assistant-workspace'; var modelLabel = el('label', null, '模型'); modelLabel.setAttribute('for', model.id); controls.appendChild(modelLabel); controls.appendChild(model); var workspaceLabel = el('label', null, '工作区'); workspaceLabel.setAttribute('for', workspace.id); controls.appendChild(workspaceLabel); controls.appendChild(workspace); root.appendChild(controls);
  var runModel = el('p', 'assistant-run-model meta', ''); runModel.hidden = true;
  model.title = '选择下一次发送使用的模型';
  var memorySection = el('section', 'assistant-memory-context'); memorySection.appendChild(el('h2', 'assistant-section-title', '记忆上下文'));
  var memoryControls = el('div', 'assistant-memory-controls'); var memoryEnabled = document.createElement('input'); memoryEnabled.type = 'checkbox'; memoryEnabled.id = 'assistant-memory-enabled';
  var memoryToggleLabel = el('label', 'assistant-memory-toggle'); memoryToggleLabel.appendChild(memoryEnabled); memoryToggleLabel.appendChild(el('span', null, '本回合使用显式记忆')); memoryControls.appendChild(memoryToggleLabel);
  var memoryQuery = document.createElement('input'); memoryQuery.type = 'search'; memoryQuery.maxLength = 512; memoryQuery.placeholder = '搜索记忆'; memoryQuery.setAttribute('aria-label', '搜索记忆'); memoryControls.appendChild(memoryQuery);
  var memorySearch = document.createElement('button'); memorySearch.type = 'button'; memorySearch.className = 'button-secondary'; memorySearch.textContent = '搜索'; memoryControls.appendChild(memorySearch); memorySection.appendChild(memoryControls);
  var memoryHost = el('div', 'assistant-memory-items'); memorySection.appendChild(memoryHost);
  var selectedMemoryHost = el('div', 'assistant-memory-selected-list'); memorySection.appendChild(el('h3', 'assistant-memory-subtitle', '已选记忆')); memorySection.appendChild(selectedMemoryHost);
  var memoryBudget = el('div', 'assistant-memory-budget meta'); memorySection.appendChild(memoryBudget); root.appendChild(memorySection);
  var historySection = el('section', 'assistant-history-section'); var historyHeading = el('div', 'assistant-history-heading'); historyHeading.appendChild(el('h2', 'assistant-section-title', '最近对话')); var newConversation = document.createElement('a'); newConversation.className = 'button-secondary'; newConversation.href = pagePath('assistant'); newConversation.textContent = '新对话'; historyHeading.appendChild(newConversation); historySection.appendChild(historyHeading); var historyHost = el('div', 'assistant-history'); historySection.appendChild(historyHost);
  var messageHost = el('div', 'assistant-messages'); var artifactSection = el('section', 'assistant-artifact-section'); artifactSection.appendChild(el('h2', 'assistant-section-title', '工作区产物')); var artifactHost = el('div', 'assistant-artifacts'); artifactSection.appendChild(artifactHost); var eventHost = el('div', 'assistant-events');
  var windowSection = el('section', 'assistant-execution-window'); windowSection.appendChild(el('h2', 'assistant-section-title', '默认执行窗口')); var windowHost = el('div', 'assistant-window-host'); windowSection.appendChild(windowHost);
  var legacySection = el('section', 'assistant-legacy-section'); legacySection.appendChild(el('h2', 'assistant-section-title', '关联旧任务')); var legacyToggle = document.createElement('button'); legacyToggle.type = 'button'; legacyToggle.className = 'button-secondary'; legacyToggle.textContent = '打开旧任务关联'; legacySection.appendChild(legacyToggle); var legacyForm = el('div', 'assistant-legacy-form'); legacyForm.hidden = true;
  function legacyField(label, type, maxLength) { var wrapper = el('label', 'assistant-legacy-field'); wrapper.appendChild(el('span', null, label)); var field = document.createElement(type === 'textarea' ? 'textarea' : 'input'); if (type !== 'textarea') field.type = type; if (maxLength) field.maxLength = maxLength; field.required = true; wrapper.appendChild(field); legacyForm.appendChild(wrapper); return field; }
  var legacyMachine = legacyField('机器', 'text', 64); var legacyAgent = legacyField('Agent 类型', 'text', 32); var legacyProject = legacyField('项目', 'text', 64); var legacyInstruction = legacyField('任务说明', 'textarea', 2000); var legacyConfirm = document.createElement('input'); legacyConfirm.type = 'checkbox'; var confirmLabel = el('label', 'assistant-legacy-confirm'); confirmLabel.appendChild(legacyConfirm); confirmLabel.appendChild(el('span', null, '确认执行')); legacyForm.appendChild(confirmLabel); var legacySubmit = document.createElement('button'); legacySubmit.type = 'button'; legacySubmit.className = 'button-secondary'; legacySubmit.textContent = '提交旧任务'; legacyForm.appendChild(legacySubmit); var legacyStatus = el('div', 'assistant-legacy-status meta'); var legacyDetails = el('div', 'assistant-legacy-details'); legacyForm.appendChild(legacyStatus); legacyForm.appendChild(legacyDetails); legacySection.appendChild(legacyForm);
  var unknownSection = el('section', 'assistant-unknown-section'); unknownSection.hidden = true; unknownSection.appendChild(el('h2', 'assistant-section-title', '待检查的未知命令')); var unknownHost = el('div', 'assistant-unknown-commands'); unknownSection.appendChild(unknownHost); root.appendChild(historySection); root.appendChild(messageHost); root.appendChild(artifactSection); root.appendChild(windowSection); root.appendChild(legacySection); root.appendChild(el('h2', 'assistant-section-title', '运行事件')); root.appendChild(eventHost); root.appendChild(unknownSection);
  var composer = el('form', 'assistant-composer'); var input = document.createElement('textarea'); input.rows = 2; input.maxLength = 32768; input.placeholder = '描述你的目标，让助手开始工作…'; var send = document.createElement('button'); send.type = 'submit'; send.appendChild(uiIcon('play', { size: 14 })); send.appendChild(document.createTextNode('运行')); composer.appendChild(input); composer.appendChild(send); var cancel = document.createElement('button'); cancel.type = 'button'; cancel.className = 'button-secondary'; cancel.appendChild(uiIcon('x', { size: 14 })); cancel.appendChild(document.createTextNode('取消运行')); cancel.hidden = true; composer.appendChild(cancel); root.appendChild(composer);
  var chat = el('section', 'assistant-chat');
  var thread = el('div', 'assistant-thread'); thread.setAttribute('data-chat-scroll', ''); thread.setAttribute('aria-label', '对话消息'); thread.tabIndex = 0;
  var initializationHost = el('div', 'assistant-initialization panel'); initializationHost.hidden = true; initializationHost.setAttribute('role', 'alert'); thread.appendChild(initializationHost);
  var intro = el('div', 'assistant-intro'); intro.appendChild(uiIcon('sparkles', {size: 30})); intro.appendChild(el('p', 'assistant-welcome-label', '你的想法，从这里开始')); intro.appendChild(el('h2', null, '今天，我们一起完成什么？')); intro.appendChild(el('p', null, '从一个问题到一份交付，把想法变成进展。'));
  var suggestions = el('div', 'assistant-suggestions');
  [
    ['code', '了解一个项目', '梳理结构，快速进入工作状态', '梳理工作区文件，生成一份项目摘要'],
    ['globe', '检查服务状态', '找到值得关注的问题', '检查服务健康，列出需要关注的问题'],
    ['layers', '整理工作成果', '把最近的进展变成交付说明', '整理最近的工作，生成交付说明']
  ].forEach(function (item) {
    var chip = el('button', 'assistant-suggestion'); chip.type = 'button'; chip.appendChild(uiIcon(item[0], {size: 19}));
    chip.appendChild(el('strong', null, item[1])); chip.appendChild(el('span', null, item[2]));
    chip.addEventListener('click', function () { if (!input.readOnly) { input.value = item[3]; input.dispatchEvent(new Event('input')); input.focus(); } }); suggestions.appendChild(chip);
  });
  intro.appendChild(suggestions); thread.appendChild(intro); thread.appendChild(messageHost);
  var scrollAnchor = el('auto-scroll-anchor'); thread.appendChild(scrollAnchor);
  var transcript = el('div', 'assistant-transcript'); transcript.appendChild(thread); chat.appendChild(transcript);
  var prompt = el('chat-prompt-input'); composer.insertBefore(prompt, input); prompt.appendChild(input);
  var composerTools = el('div', 'composer-tools'); composerTools.appendChild(controls);
  var composerActions = el('div', 'composer-actions'); composerActions.appendChild(cancel); composerActions.appendChild(send); composerTools.appendChild(composerActions); prompt.appendChild(composerTools);
  input.setAttribute('aria-label', '给助手的任务'); send.setAttribute('aria-label', '发送任务');
  var composerDock = el('div', 'composer-dock');
  var actionError = el('div', 'assistant-action-error'); actionError.hidden = true; actionError.setAttribute('role', 'alert'); composerDock.appendChild(actionError);
  composerDock.appendChild(composer); composerDock.appendChild(runModel); composerDock.appendChild(el('p', 'composer-hint', 'Enter 发送 · Shift + Enter 换行 · 任务会在后台继续运行')); chat.appendChild(composerDock);
  var inspector = el('dialog', 'assistant-inspector'); inspector.setAttribute('aria-label', '运行与产物');
  function disclosure(section, label, open) { var detail = el('details', 'assistant-disclosure'); detail.open = !!open; detail.appendChild(el('summary', null, label)); detail.appendChild(section); return detail; }
  var eventSection = el('section'); eventSection.appendChild(eventHost);
  var executionSections = [];
  if (canAdminister) executionSections.push(disclosure(windowSection, '执行窗口', true));
  executionSections.push(disclosure(eventSection, '运行记录', true));
  if (canAdminister) executionSections.push(unknownSection);
  var contextSections = [];
  if (canAdminister) contextSections.push(disclosure(memorySection, '记忆上下文', true));
  contextSections.push(disclosure(historySection, '最近对话', true));
  if (canAdminister) contextSections.push(disclosure(legacySection, '关联任务', false));
  clear(root); root.appendChild(heading); root.appendChild(chat); root.appendChild(inspector); target.appendChild(root);
  var inspectorUi = mountInspector(root, heading, inspector, [
    {id: 'artifacts', title: '产物', sections: [artifactSection]},
    {id: 'activity', title: '运行', sections: executionSections},
    {id: 'context', title: '上下文', sections: contextSections}
  ]);
  var conversationId = options.conversationId || null; var conversationWorkspaceId = null; var conversationArchived = false; var activeRunId = null; var latestRunId = null; var cursor = 0; var polling = false; var windowEventCursor = 0; var windowPolling = false; var windowState = { loading: false, error: null, window: null, mode: 'disconnected', events: [], holderId: token(), leaseToken: null, leaseExpiresAt: 0 };
  var disposed = false; var suspended = false; var timers = new Set();
  var windowPollAgain = false; var windowPollDelay = 1200;
  function later(fn, delay) { if (disposed || suspended) return; var timer = setTimeout(function () { timers.delete(timer); if (!disposed && !suspended) fn(); }, delay); timers.add(timer); }
  var requestClockActive = false;
  function startRequestClock() {
    if (requestClockActive || disposed || suspended) return;
    requestClockActive = true;
    function tick() {
      if (!disposed && !suspended && tickMessageRequests(messageHost)) later(tick, 250);
      else requestClockActive = false;
    }
    tick();
  }
  var pendingTurn = null; var submitting = false; var catalogReady = false; var modelCatalog = [];
  var artifactState = { loading: false, error: null, artifacts: [], workspaceId: null, previewById: {} };
  var memoryState = { loading: false, error: null, items: [], selected: {}, order: [], maxItems: 8, maxBytes: 8192, query: '' }; var memoryRequestSequence = 0;
  renderArtifacts(artifactHost, artifactState); renderMemoryItems(memoryHost, memoryState, toggleMemory); renderSelectedMemoryItems(selectedMemoryHost, memoryState, removeMemory); renderWindow();
  function setStatus(text) {
    var names = {queued: '排队中', running: '运行中', waiting_node: '等待节点', waiting_approval: '等待审批', waiting_task: '等待任务', cancelling: '正在取消', succeeded: '已完成', failed: '执行失败', unknown: '结果待确认', cancelled: '已取消'};
    status.textContent = names[text] || text;
    if (text === 'failed' || text === 'unknown') inspectorUi.select('activity', true);
    status.setAttribute('data-state', text); status.setAttribute('role', 'status');
    lockPendingTurn();
  }
  function showRunModel(run) {
    var profileId = run && run.config && run.config.model_profile_id;
    var profile = modelCatalog.find(function (row) { return row.profile_id === profileId; });
    runModel.textContent = profileId ? '本次模型：' + ((run.config && run.config.model) || (profile && profile.model) || profileId) : '';
    runModel.hidden = !profileId;
  }
  function clearActionError() { clear(actionError); actionError.hidden = true; }
  function showActionError(error) { clear(actionError); showError(actionError, error); actionError.hidden = false; }
  function lockPendingTurn() {
    var locked = submitting || !!pendingTurn;
    input.readOnly = locked || !!activeRunId || conversationArchived;
    input.placeholder = conversationArchived ? '此对话已归档，取消归档后可以继续发送' : '描述你的目标，让助手开始工作…';
    model.disabled = locked || !catalogReady; workspace.disabled = locked || !catalogReady || !!conversationId;
    memoryEnabled.disabled = locked;
    send.disabled = submitting || !!activeRunId || !catalogReady || conversationArchived;
    clear(send); send.appendChild(uiIcon('arrow-up', {size: 17})); send.appendChild(el('span', null, pendingTurn && !submitting ? '重试提交' : '发送'));
  }
  function setWindowState(next) {
    windowState = Object.assign({}, windowState, next || {});
    renderWindow();
  }
  function renderWindow() {
    renderExecutionWindow(windowHost, windowState, {
      onClose: closeWindow,
      onTakeControl: takeControl,
      onReleaseControl: returnControl,
      onRetryFrame: retryWindowFrame,
      onFrameError: markFrameError
    });
  }
  function windowError(error) {
    return (error && (error.detail || error.message || error.code)) || '执行窗口不可用';
  }
  async function pollExecutionWindow() {
    if (disposed || suspended || windowPolling || !windowState.window || !windowState.window.window_id) return;
    windowPolling = true;
    try {
      var windowId = windowState.window.window_id;
      var currentWindow = (await getExecutionWindow(windowId)).window;
      if (currentWindow && ['closed', 'expired'].indexOf(currentWindow.state) !== -1) {
        setWindowState({ window: currentWindow, mode: currentWindow.state, leaseToken: null });
        return;
      }
      if (currentWindow && currentWindow.state !== windowState.window.state) {
        setWindowState({ window: currentWindow });
      }
      var data = await getExecutionWindowEvents(windowId, windowEventCursor, 100);
      var events = Array.isArray(data.events) ? data.events : [];
      if (events.length) {
        windowEventCursor = typeof data.next_cursor === 'number' ? data.next_cursor : (events[events.length - 1].sequence || windowEventCursor);
        var frames = events.filter(function (event) {
          return event && event.kind === 'browser.frame' && event.payload &&
            typeof event.payload.artifact_id === 'string' && event.payload.artifact_id;
        });
        var latestFrame = frames.length ? frames[frames.length - 1].payload : null;
        if (latestFrame && (!windowState.frame || latestFrame.frame_seq > windowState.frame.frame_seq)) {
          setWindowState({ frame: latestFrame, frameError: null, frameRetryKey: '' });
        }
        setWindowState({ events: windowState.events.concat(events).slice(-100) });
      }
      if (windowState.mode === 'writable' && windowState.leaseToken) {
        try {
          var renewed = await renewExecutionWindowWriter(windowId, { holder_id: windowState.holderId, lease_token: windowState.leaseToken, ttl_s: 60 });
          if (!renewed.lease) throw new Error('writer_lease_invalid');
        } catch (renewError) {
          if (renewError && renewError.code === 'lease_conflict') setWindowState({ mode: 'read-only', leaseToken: null });
          else if (renewError && (renewError.code === 'lease_expired' || renewError.code === 'lease_not_found')) setWindowState({ mode: 'read-only', leaseToken: null });
          else if (renewError && (renewError.code === 'window_expired' || renewError.code === 'window_closed')) setWindowState({ mode: renewError.code === 'window_expired' ? 'expired' : 'closed', window: Object.assign({}, windowState.window, { state: renewError.code === 'window_expired' ? 'expired' : 'closed' }), leaseToken: null });
        }
      }
      windowPollDelay = 1200;
    } catch (error) {
      if (error && (error.code === 'not_found' || error.code === 'execution_windows_disabled')) {
        setWindowState({ window: null, mode: 'disconnected', error: null, leaseToken: null });
      } else {
        setWindowState({ error: windowError(error) });
        windowPollDelay = 2000;
      }
    } finally {
      windowPolling = false;
      if (!disposed && !suspended && windowState.window &&
          windowState.window.state !== 'closed' && windowState.window.state !== 'expired') {
        var delay = windowPollAgain ? 0 : windowPollDelay;
        windowPollAgain = false;
        windowPollDelay = 1200;
        later(pollExecutionWindow, delay);
      }
    }
  }
  var windowConnection = null;
  function connectExecutionWindow(runId, create) {
    if (!canAdminister) return;
    if (windowConnection) return windowConnection;
    windowConnection = doConnectExecutionWindow(runId, create).finally(function () { windowConnection = null; });
    return windowConnection;
  }
  async function doConnectExecutionWindow(runId, create) {
    if (!runId) return;
    setWindowState({ loading: true, error: null, mode: 'recovering', events: [],
      frame: null, frameError: null, frameRetryKey: '', leaseToken: null });
    windowEventCursor = 0;
    try {
      var created = create ? await createExecutionWindow(runId, { metadata: { surface: 'assistant' } }) : null;
      var current = created && created.window;
      var ticket = created && created.attach_ticket;
      if (!current) {
        var listed = await listExecutionWindows(runId, 20);
        current = Array.isArray(listed.windows) && listed.windows.length ? listed.windows[0] : null;
        if (!current) { setWindowState({ loading: false, window: null, mode: 'disconnected' }); return; }
        var reconnected = await reconnectExecutionWindow(current.window_id, { ttl_s: 300 });
        current = reconnected.window || current;
        ticket = reconnected.attach_ticket;
      }
      if (ticket) {
        var attached = await attachExecutionWindow(current.window_id, ticket);
        current = attached.window || current;
      }
      setWindowState({ loading: false, window: current, mode: 'attached' });
      pollExecutionWindow();
    } catch (error) {
      if (error && (error.code === 'not_found' || error.code === 'execution_windows_disabled')) setWindowState({ loading: false, window: null, mode: 'disconnected', error: null });
      else if (error && error.code === 'window_closed') setWindowState({ loading: false, window: current || windowState.window || null, mode: 'closed', error: null });
      else setWindowState({ loading: false, mode: 'error', error: windowError(error) });
    }
  }
  async function takeControl() {
    var current = windowState.window;
    if (!current || !current.window_id || windowState.controlPending) return;
    setWindowState({ controlPending: true, controlError: null });
    try {
      var acquired = await acquireExecutionWindowWriter(current.window_id, {
        holder_id: windowState.holderId, ttl_s: 60
      });
      setWindowState({ mode: 'writable', leaseToken: acquired.lease_token || null,
        leaseExpiresAt: (acquired.lease || {}).expires_at || 0, controlError: null });
    } catch (error) {
      if (error && error.code === 'lease_conflict') {
        setWindowState({ mode: 'read-only', leaseToken: null, controlError: '已有其他操作员接管' });
      } else {
        setWindowState({ controlError: windowError(error) });
      }
    } finally {
      setWindowState({ controlPending: false });
    }
  }
  async function returnControl() {
    var current = windowState.window;
    if (!current || !current.window_id || !windowState.leaseToken || windowState.controlPending) return;
    setWindowState({ controlPending: true, controlError: null });
    try {
      await releaseExecutionWindowWriter(current.window_id, {
        holder_id: windowState.holderId, lease_token: windowState.leaseToken
      });
      setWindowState({ mode: 'attached', leaseToken: null, leaseExpiresAt: 0, controlError: null });
    } catch (error) {
      setWindowState({ controlError: windowError(error) });
    } finally {
      setWindowState({ controlPending: false });
    }
  }
  function retryWindowFrame() {
    setWindowState({ frameError: null, frameRetryKey: token() });
  }
  function markFrameError(sequence) {
    if (windowState.frame && windowState.frame.frame_seq === sequence) {
      setWindowState({ frameError: '画面加载失败' });
    }
  }
  async function closeWindow() {
    var current = windowState.window;
    if (!current || !current.window_id) return;
    setWindowState({ mode: 'closing', error: null });
    try {
      if (windowState.leaseToken) {
        await releaseExecutionWindowWriter(current.window_id, { holder_id: windowState.holderId, lease_token: windowState.leaseToken });
      }
      var closed = await closeExecutionWindow(current.window_id);
      setWindowState({ loading: false, window: closed.window || Object.assign({}, current, { state: 'closed' }), mode: 'closed', leaseToken: null });
    } catch (error) { setWindowState({ mode: 'error', error: windowError(error) }); }
  }
  function renderMemoryState() {
    renderMemoryItems(memoryHost, memoryState, toggleMemory);
    renderSelectedMemoryItems(selectedMemoryHost, memoryState, removeMemory);
    var count = memoryState.order.length;
    memoryBudget.textContent = count && memoryEnabled.checked
      ? ('本回合将引用 ' + String(count) + '/' + String(memoryState.maxItems) + ' 条记忆 · 预算 ' + String(memoryState.maxBytes) + ' bytes')
      : count ? ('已选 ' + String(count) + ' 条记忆 · 未启用，不会提交') : '未启用记忆上下文';
  }
  function toggleMemory(reference, checked) {
    var selected = Object.assign({}, memoryState.selected); var order = memoryState.order.slice();
    if (checked) {
      if (order.length >= memoryState.maxItems) return;
      selected[reference.memory_id] = reference; if (order.indexOf(reference.memory_id) === -1) order.push(reference.memory_id);
    } else {
      delete selected[reference.memory_id]; order = order.filter(function (id) { return id !== reference.memory_id; });
    }
    memoryState = Object.assign({}, memoryState, { selected: selected, order: order }); renderMemoryState();
  }
  function selectedMemoryIds() { return memoryState.order.slice(0, memoryState.maxItems); }
  function removeMemory(memoryId) { toggleMemory({ memory_id: memoryId }, false); }
  async function refreshMemoryItems(query) {
    if (!canAdminister) return;
    var value = typeof query === 'string' ? query.trim() : ''; var sequence = ++memoryRequestSequence;
    memorySearch.disabled = true;
    memoryState = Object.assign({}, memoryState, { loading: true, error: null, query: value }); renderMemoryState();
    try {
      var data = value ? await searchPlatformMemories(value, 20) : await getPlatformMemories(50);
      if (sequence !== memoryRequestSequence) return;
      memorySearch.disabled = false;
      var references = (Array.isArray(data.memories) ? data.memories : []).map(memoryReference).filter(Boolean);
      memoryState = Object.assign({}, memoryState, { loading: false, error: null, items: references }); renderMemoryState();
    } catch (error) {
      if (sequence !== memoryRequestSequence) return;
      memorySearch.disabled = false;
      memoryState = Object.assign({}, memoryState, { loading: false, error: (error && (error.detail || error.message || error.code)) || '记忆上下文不可用', items: [] }); renderMemoryState();
    }
  }
  function renderConversationHistory(rows) {
    window.dispatchEvent(new CustomEvent('fleet-conversations-updated'));
    clear(historyHost);
    var conversations = Array.isArray(rows) ? rows.slice(0, 100) : [];
    if (!conversations.length) { historyHost.appendChild(el('div', 'assistant-history-empty meta', '暂无历史对话')); return; }
    conversations.forEach(function (conversation) {
      if (!conversation || typeof conversation.conversation_id !== 'string' || !conversation.conversation_id) return;
      var row = el('div', 'assistant-history-row' + (conversation.conversation_id === conversationId ? ' active' : ''));
      var link = document.createElement('a'); link.href = pagePath('conversation', conversation.conversation_id); link.className = 'assistant-history-link';
      link.textContent = conversation.title || conversation.last_message_preview || '未命名对话'; row.appendChild(link);
      var meta = el('span', 'assistant-history-meta'); var latest = conversation.latest_run;
      meta.textContent = (conversation.workspace_id ? '工作区 ' + conversation.workspace_id : '未绑定工作区') + (latest && latest.state ? ' · ' + latest.state : '');
      row.appendChild(meta); historyHost.appendChild(row);
    });
  }
  async function refreshConversationHistory() {
    try { var data = await getConversations(50); renderConversationHistory(data.conversations); }
    catch (error) { clear(historyHost); historyHost.appendChild(el('div', 'assistant-history-empty err', (error && (error.detail || error.message || error.code)) || '历史对话不可用')); }
  }
  function setArtifactState(next) { artifactState = Object.assign({}, artifactState, next); renderArtifacts(artifactHost, artifactState, previewArtifact); }
  async function previewArtifact(artifactId) {
    var current = artifactState.previewById && artifactState.previewById[artifactId];
    var nextPreviews = Object.assign({}, artifactState.previewById || {});
    if (current && current.open) { nextPreviews[artifactId] = Object.assign({}, current, { open: false }); setArtifactState({ previewById: nextPreviews }); return; }
    nextPreviews[artifactId] = { open: true, loading: true, error: null, data: null };
    setArtifactState({ previewById: nextPreviews });
    var workspaceId = artifactState.workspaceId;
    try {
      var response = await getPlatformArtifactPreview(artifactId, workspaceId);
      if (artifactState.workspaceId !== workspaceId) return;
      nextPreviews = Object.assign({}, artifactState.previewById || {});
      nextPreviews[artifactId] = { open: true, loading: false, error: null, data: response.preview || null };
      setArtifactState({ previewById: nextPreviews });
    } catch (error) {
      if (artifactState.workspaceId !== workspaceId) return;
      nextPreviews = Object.assign({}, artifactState.previewById || {});
      nextPreviews[artifactId] = { open: true, loading: false, error: (error && (error.detail || error.message || error.code)) || '预览加载失败', data: null };
      setArtifactState({ previewById: nextPreviews });
    }
  }
  async function refreshArtifacts(workspaceId) {
    if (!workspaceId) { setArtifactState({ loading: false, error: null, artifacts: [], workspaceId: null, previewById: {} }); return; }
    setArtifactState({ loading: true, error: null, artifacts: [], workspaceId: workspaceId, previewById: {} });
    try {
      var data = await getPlatformArtifacts(workspaceId, 100);
      if (conversationWorkspaceId !== workspaceId) return;
      setArtifactState({ loading: false, error: null, artifacts: data.artifacts || [], workspaceId: workspaceId, previewById: {} });
    } catch (error) {
      if (conversationWorkspaceId !== workspaceId) return;
      setArtifactState({ loading: false, error: (error && (error.detail || error.message || error.code)) || '产物加载失败', artifacts: [], workspaceId: workspaceId, previewById: {} });
    }
  }
  async function refreshUnknownCommands() {
    if (!canAdminister) return;
    try {
      var data = await getUnknownCommands(50); var commands = data.commands || [];
      unknownSection.hidden = commands.length === 0;
      renderUnknownCommands(unknownHost, commands, async function (commandId, outcome, button) {
        button.disabled = true;
        try {
          if (outcome === 'postcheck') {
            await requestCommandPostcheck(commandId);
            var latest = null;
            for (var attempt = 0; attempt < 8; attempt += 1) {
              latest = (await getCommandPostcheck(commandId)).postcheck;
              if (latest && latest.state !== 'pending') break;
              await new Promise(function (resolve) { setTimeout(resolve, 500); });
            }
            var postState = latest && latest.state ? latest.state : 'remains_unknown';
            setStatus('确定性检查结果：' + postState + '（原命令仍为未知）');
            await refreshUnknownCommands();
            return;
          }
          await submitCommandReconcile(commandId, { outcome: outcome, evidence: { source: 'operator', reference: 'assistant-ui:' + commandId } });
          setStatus('已记录人工证据（命令仍为未知）'); await refreshUnknownCommands();
        } catch (error) { showError(eventHost, error); button.disabled = false; }
      });
    } catch (error) { showError(eventHost, error); }
  }
  function fill(select, rows, valueKey, labelKey) { clear(select); (Array.isArray(rows) ? rows : []).filter(function (row) { return row.enabled !== false; }).forEach(function (row) { var option = document.createElement('option'); option.value = row[valueKey] || ''; option.textContent = row[labelKey] || option.value; select.appendChild(option); }); }
  async function refreshLegacy(runId) { if (!runId || legacyForm.hidden) return; try { var data = await getPlatformLegacyTask(runId); var link = data.legacy_task || data; legacyStatus.textContent = '状态：' + (link.state || 'unknown') + (link.task_state ? ' · ' + link.task_state : '') + (link.task_id ? ' · task ' + link.task_id : ''); renderLegacyProjection(legacyDetails, link); if (link.session_id) { var session = document.createElement('a'); session.href = pagePath('session', link.session_id); session.textContent = '打开会话'; legacyDetails.appendChild(session); } } catch (error) { legacyStatus.textContent = (error && error.code === 'legacy_task_not_found') ? '尚未关联旧任务' : ((error && (error.detail || error.message)) || '旧任务状态不可用'); clear(legacyDetails); } }
  async function refreshRunEvents(runId) { if (!runId) return; clear(eventHost); cursor = 0; try { var eventData = await getRunEvents(runId, 0); var events = eventData.events || []; if (events.length) { cursor = eventData.next_cursor || events[events.length - 1].sequence || 0; renderEvents(eventHost, events); } } catch (error) { showError(eventHost, error); } }
  async function refreshConversation() {
    if (!conversationId) return;
    var data = await getConversation(conversationId);
    var conversation = data.conversation || {};
    conversationArchived = Boolean(conversation.archived_at);
    lockPendingTurn();
    var firstMessage = (Array.isArray(conversation.messages) ? conversation.messages : []).find(function (message) { return message.role === 'user'; });
    conversationTitle.textContent = conversation.title || (firstMessage && firstMessage.content ? firstMessage.content.slice(0, 60) : '当前对话');
    conversationTitle.title = conversationTitle.textContent;
    renderMessages(messageHost, conversation.messages, conversation.runs || []); startRequestClock(); scrollAnchor.setAttribute('is-streaming', ''); intro.hidden = Boolean(conversation.messages && conversation.messages.length);
    var nextWorkspaceId = typeof conversation.workspace_id === 'string' ? conversation.workspace_id : null;
    if (conversationWorkspaceId !== nextWorkspaceId) {
      conversationWorkspaceId = nextWorkspaceId;
      await refreshArtifacts(conversationWorkspaceId);
    } else if (conversationWorkspaceId) await refreshArtifacts(conversationWorkspaceId);
    if (nextWorkspaceId) workspace.value = nextWorkspaceId;
    lockPendingTurn();
    var runs = conversation.runs || [];
    var last = runs[runs.length - 1];
    if (last) { setStatus(last.state); latestRunId = last.run_id; showRunModel(last); }
    if (latestRunId) {
      await refreshRunEvents(latestRunId);
      if (!windowState.window || windowState.window.run_id !== latestRunId) {
        await connectExecutionWindow(latestRunId, false);
      }
    }
    if (last && ['queued', 'running', 'waiting_node', 'waiting_approval', 'waiting_task', 'cancelling'].indexOf(last.state) !== -1) {
      activeRunId = last.run_id; setStatus(last.state); cancel.hidden = false; poll();
    }
    if (latestRunId) refreshLegacy(latestRunId);
  }
  async function poll() {
    if (disposed || suspended || polling || !activeRunId) return;
    polling = true;
    var runId = activeRunId;
    try {
      var runData = await getRun(runId);
      if (disposed || suspended) return;
      var run = runData.run || runData;
      showRunModel(run); setStatus(run.state || 'unknown');
      updateRunRequests(messageHost, run); startRequestClock();
      var eventData = await getRunEvents(runId, cursor);
      if (disposed || suspended) return;
      var events = eventData.events || [];
      if (events.length) {
        cursor = eventData.next_cursor || events[events.length - 1].sequence || cursor;
        renderEvents(eventHost, events);
      }
      if (['succeeded', 'failed', 'unknown', 'cancelled'].indexOf(run.state) !== -1) {
        cancel.hidden = true;
        // Keep the tracking identity until the durable answer is recovered.
        // A transient conversation GET failure must remain retryable.
        await refreshConversation();
        await refreshUnknownCommands();
        if (activeRunId === runId) activeRunId = null;
        lockPendingTurn();
      }
    } catch (error) {
      if (!disposed && !suspended) { showError(eventHost, error); setStatus('连接中断，稍后恢复'); }
    } finally {
      polling = false;
      if (!disposed && !suspended && activeRunId) later(poll, 1200);
    }
  }
  memorySearch.addEventListener('click', function () { refreshMemoryItems(memoryQuery.value); });
  memoryQuery.addEventListener('keydown', function (event) { if (event.key === 'Enter') { event.preventDefault(); refreshMemoryItems(memoryQuery.value); } });
  memoryEnabled.addEventListener('change', function () { renderMemoryState(); });
  composer.addEventListener('submit', async function (event) {
    event.preventDefault();
    if (submitting || activeRunId || !catalogReady || conversationArchived) return;
    var text = input.value.trim();
    if (!pendingTurn && !text) return;
    if (!pendingTurn) {
      var payload = { text: text, client_token: token() };
      if (model.value) payload.overrides = { model_profile_id: model.value };
      var selectedIds = selectedMemoryIds();
      if (canAdminister && memoryEnabled.checked && selectedIds.length) {
        payload.memory_context = {
          enabled: true, memory_ids: selectedIds,
          revisions: selectedIds.reduce(function (result, id) { result[id] = memoryState.selected[id].revision; return result; }, {}),
          max_items: memoryState.maxItems, max_bytes: memoryState.maxBytes
        };
      }
      pendingTurn = { payload: payload, workspaceId: workspace.value || undefined };
    }
    // An uncertain response keeps the exact token AND payload. A second
    // submit must reconcile the original operation, not create another Run.
    clearActionError(); submitting = true; lockPendingTurn(); setStatus('提交中');
    try {
      if (!conversationId) {
        var created = await createConversation({ workspace_id: pendingTurn.workspaceId });
        conversationId = created.conversation.conversation_id;
        if (window.history && window.history.replaceState) window.history.replaceState({}, '', pagePath('conversation', conversationId));
      }
      var turn = await appendConversationTurn(conversationId, pendingTurn.payload);
      pendingTurn = null; input.value = '';
      activeRunId = turn.run.run_id; latestRunId = activeRunId;
      showRunModel(turn.run); cursor = 0; cancel.hidden = false;
      // Run tracking starts independently of optional presentation requests.
      poll();
      connectExecutionWindow(activeRunId, true);
      refreshConversationHistory();
      // Conversation recovery runs in the tracker, so it cannot reset the
      // event cursor concurrently with an in-flight event request.
    } catch (error) {
      showActionError(error);
      if (activeRunId) setStatus('已提交，状态刷新失败');
      else {
        // An explicit validation/auth rejection did not accept this request.
        // Network, timeout, parse and 5xx failures leave acceptance uncertain.
        if (error && error.kind === 'http' && error.status >= 400 && error.status < 500) pendingTurn = null;
        setStatus(pendingTurn ? '提交结果待确认，请重试原请求' : '提交失败');
      }
    } finally { submitting = false; lockPendingTurn(); }
  });
  cancel.addEventListener('click', async function () { if (!activeRunId) return; clearActionError(); cancel.disabled = true; try { var result = await cancelRun(activeRunId); setStatus(result.state || 'cancelling'); } catch (error) { showActionError(error); } finally { cancel.disabled = false; } });
  legacyToggle.addEventListener('click', function () { legacyForm.hidden = !legacyForm.hidden; legacyToggle.textContent = legacyForm.hidden ? '打开旧任务关联' : '收起旧任务关联'; if (!legacyForm.hidden) refreshLegacy(latestRunId || activeRunId); });
  legacySubmit.addEventListener('click', async function () { var runId = activeRunId || latestRunId; if (!runId) { legacyStatus.textContent = '请先提交一次主助手运行'; return; } legacySubmit.disabled = true; legacyStatus.textContent = '提交中'; try { var result = await createPlatformLegacyTask(runId, { machine: legacyMachine.value.trim(), agent_type: legacyAgent.value.trim(), project: legacyProject.value.trim(), instruction: legacyInstruction.value.trim(), confirm: legacyConfirm.checked }); var link = result.legacy_task || result; legacyStatus.textContent = '已关联：' + (link.state || 'pending') + (link.task_id ? ' · ' + link.task_id : ''); renderLegacyProjection(legacyDetails, link); } catch (error) { legacyStatus.textContent = (error && (error.detail || error.message || error.code)) || '提交失败'; } finally { legacySubmit.disabled = false; } });
  lockPendingTurn();
  // Authentication, catalog loading and conversation recovery are distinct
  // failure states. Do not enable submission until the saved state is known.
  async function initialize() {
    clear(initializationHost); initializationHost.hidden = true;
    catalogReady = false; setStatus('加载中');
    var failureStatus = '平台配置加载失败';
    try {
      var data = await getPlatformDefaults();
      if (disposed) return;
      var defaults = data.defaults || {};
      modelCatalog = Array.isArray(data.models) ? data.models : [];
      fill(model, data.models, 'profile_id', 'model');
      fill(workspace, data.workspaces, 'workspace_id', 'name');
      if (defaults.model_profile_id && Array.from(model.options).some(function (o) { return o.value === defaults.model_profile_id; })) model.value = defaults.model_profile_id;
      if (defaults.workspace_id && Array.from(workspace.options).some(function (o) { return o.value === defaults.workspace_id; })) workspace.value = defaults.workspace_id;
      failureStatus = '会话恢复失败';
      await refreshConversation();
      if (disposed) return;
      catalogReady = !!model.value && !!workspace.value;
      if (!catalogReady) setStatus('请先配置可用模型和工作区');
      else if (!latestRunId) setStatus('就绪');
      lockPendingTurn();
      refreshConversationHistory();
      refreshUnknownCommands();
      refreshMemoryItems('');
    } catch (error) {
      if (disposed) return;
      if (error && error.status === 401) return; // The shared session guard redirects.
      setStatus(error && error.status === 403 ? '当前操作员无访问权限' : failureStatus);
      initializationHost.hidden = false;
      showError(initializationHost, error);
      var action = el('button', 'button-secondary', '重试加载');
      action.type = 'button';
      action.addEventListener('click', initialize);
      initializationHost.appendChild(action);
    }
  }
  initialize();
  function teardown() {
    disposed = true;
    inspectorUi.dispose();
    timers.forEach(clearTimeout);
    timers.clear();
  }
  teardown.suspend = function () {
    if (disposed || suspended) return;
    suspended = true;
    requestClockActive = false;
    timers.forEach(clearTimeout);
    timers.clear();
  };
  teardown.resume = function () {
    if (disposed || !suspended) return;
    suspended = false;
    if (activeRunId) poll();
    if (windowState.window && windowState.window.window_id) {
      if (windowPolling) windowPollAgain = true;
      else later(pollExecutionWindow, 0);
    }
    if (tickMessageRequests(messageHost)) startRequestClock();
  };
  return teardown;
}
