import * as api from '../api/muse.js';
import {el, action, renderMuseMessage, renderApproval, renderMusePendingMessage,
  renderMuseThinkingMessage, createBrowserActivity, updateBrowserActivity} from './muse/cards.js';
import {eventSequence, eventRole, eventText, eventIsStreaming, latestSequence,
  mergeEvents, visibleMessages, browserTaskIsActive, reduceBrowserActivity} from './muse/model.js';

export function mountMuse(target) {
  let disposed = false, suspended = false, connected = false, busy = false, timer, epoch = 0;
  let sessions = [], settings = {auto_approve_sessions: [], auto_approve_all: false}, activePane = null, layout = 'single';
  let accounts = [], accountId = null, accountLabel = '', connectionRevision, settingsRevision, openedDefault = false;
  let importTargetId = null;
  const panes = new Map();
  const root = document.createElement('div'); root.className = 'muse-workspace';
  const toolbar = el('div', 'muse-toolbar');
  const identity = el('div', 'muse-identity');
  identity.append(el('span', 'muse-wordmark', 'Muse'), el('span', 'meta', '你的独立智能工作区'));
  const badge = el('span', 'muse-connection', '正在检查登录态'); badge.setAttribute('role', 'status');
  const accountSelect = el('select', 'muse-account-select'); accountSelect.setAttribute('aria-label', 'Muse 账号'); accountSelect.hidden = true;
  const addAccount = action('添加账号', 'plus', () => {
    importTargetId = 'muse-' + crypto.randomUUID().replaceAll('-', '').slice(0, 12);
    accountLabelInput.value = '';
    loginPanel.hidden = false; accountLabelInput.focus();
  });
  const manage = action('管理登录态', 'lock', () => { loginPanel.hidden = !loginPanel.hidden; });
  const split = action('并排窗口', 'layers', () => {
    layout = layout === 'single' ? 'split' : 'single';
    split.setAttribute('aria-pressed', String(layout === 'split'));
    split.lastChild.textContent = layout === 'split' ? '单窗口' : '并排窗口';
    showPanes();
  });
  toolbar.append(identity, badge, accountSelect, addAccount, split, manage);
  const notice = el('p', 'muse-notice'); notice.setAttribute('role', 'status'); notice.hidden = true;
  const loginPanel = el('section', 'muse-login'); loginPanel.hidden = true;
  loginPanel.append(el('h2', '', '连接你的 Muse'), el('p', 'meta',
    '导入 muse.ai 的 Cookie JSON、Playwright 登录态文件，或粘贴 Cookie。连接后不需要保持浏览器开启。'));
  const loginForm = el('form', 'muse-login-form');
  const accountLabelInput = el('input'); accountLabelInput.type = 'text'; accountLabelInput.maxLength = 80;
  accountLabelInput.placeholder = '账号名称（可选）'; accountLabelInput.setAttribute('aria-label', 'Muse 账号名称');
  const credential = el('textarea'); credential.rows = 3; credential.autocomplete = 'off';
  credential.spellcheck = false; credential.setAttribute('aria-label', 'Muse 登录态');
  credential.placeholder = '粘贴 Muse 登录态';
  const file = el('input'); file.type = 'file'; file.accept = '.json,application/json'; file.setAttribute('aria-label', '选择登录态文件');
  file.addEventListener('change', async () => {
    const selected = file.files[0];
    if (!selected) return;
    if (selected.size > 200000) { showError(new Error('登录态文件不能超过 200 KB。')); return; }
    credential.value = await selected.text();
  });
  const importButton = el('button', 'button-primary', '导入并连接'); importButton.type = 'submit';
  const disconnect = action('断开连接', 'x', async () => {
    const selectedAccount = accountId, selectedConnection = connectionRevision, generation = epoch;
    disconnect.disabled = true;
    try {
      const result = await api.museDisconnect(selectedAccount, selectedConnection); accounts = result.accounts || accounts;
      if (disposed || generation !== epoch || accountId !== selectedAccount) { renderAccounts(); return; }
      accountId = accounts[0]?.account_id || null; clearAccountView(); renderAccounts();
      credential.value = ''; file.value = ''; showConnected(); renderSessions(); approvalsBox.replaceChildren(); audit.replaceChildren();
    } catch (error) { showError(error); }
    finally { disconnect.disabled = false; }
  });
  loginForm.append(accountLabelInput, credential, file, importButton, disconnect); loginPanel.append(loginForm);
  loginForm.addEventListener('submit', async event => {
    event.preventDefault(); if (!credential.value.trim()) return;
    const selectedAccount = importTargetId || accountId, generation = epoch;
    importButton.disabled = true; notice.hidden = true;
    try {
      const result = await api.museImport(credential.value, selectedAccount, accountLabelInput.value.trim());
      accounts = result.accounts || accounts;
      if (disposed) return;
      if (generation !== epoch) {
        renderAccounts(); return;
      }
      importTargetId = null; epoch++; connected = true; accountId = result.account_id; accountLabel = result.label || '';
      settings = result.settings; settingsRevision = result.settings_revision; sessions = result.sessions || [];
      connectionRevision = result.connection_revision; accounts = result.accounts || accounts; openedDefault = false;
      autoAll.checked = settings.auto_approve_all === true;
      // A re-import may select another Muse account. Do not retain its previous
      // transcript, pane drafts or approval state.
      clearPanes(); approvalSignature = auditSignature = '';
      loginPanel.hidden = true;
      credential.value = ''; file.value = ''; accountLabelInput.value = ''; showConnected(); renderAccounts(); renderSessions();
      if (sessions.length) { openedDefault = true; openSession(sessions.find(s => s.is_primary) || sessions[0]); }
      schedule(0);
    } catch (error) { showError(error); }
    finally { importButton.disabled = false; }
  });
  const body = el('div', 'muse-body');
  const rail = el('aside', 'muse-sessions'); rail.setAttribute('aria-label', 'Muse 对话');
  const newChat = action('新建子会话', 'git-branch', () => openSession({session_id: crypto.randomUUID(), title: '新子会话', is_thread: true}, true));
  const search = el('input'); search.type = 'search'; search.placeholder = '搜索 Muse 对话'; search.setAttribute('aria-label', '搜索 Muse 对话');
  search.addEventListener('input', renderSessions);
  const list = el('div', 'muse-session-list');
  rail.append(newChat, search, list);
  const area = el('div', 'muse-conversations');
  const tabs = el('div', 'muse-tabs'); tabs.setAttribute('role', 'tablist'); tabs.setAttribute('aria-label', '已打开的对话');
  const deck = el('div', 'muse-deck');
  const empty = el('div', 'muse-empty');
  empty.append(el('h2', '', '让 Muse 继续你的工作'), el('p', 'meta', '导入登录态后，打开已有对话或开始一个新的子会话。'));
  deck.append(empty); area.append(tabs, deck); body.append(rail, area);
  const permissions = el('details', 'muse-permissions');
  const permissionHeading = el('summary', '', '权限请求与操作记录');
  const permissionHelp = el('p', 'meta', '默认仅手动审批。单会话授权只作用于所选对话；每次自动批准都会留存记录。');
  const fullAccess = el('label', 'muse-auto-all');
  const autoAll = el('input'); autoAll.type = 'checkbox';
  autoAll.setAttribute('aria-label', '自动同意全部权限请求'); autoAll.disabled = true;
  const fullAccessText = el('span');
  fullAccessText.append(el('strong', '', '完全访问'), el('small', '', '自动同意此账号下全部会话和后台请求的权限申请'));
  fullAccess.append(autoAll, fullAccessText);
  const approvalsBox = el('div', 'muse-approvals');
  const audit = el('div', 'muse-audit');
  permissions.append(permissionHeading, permissionHelp, fullAccess, approvalsBox, audit);
  root.append(toolbar, notice, loginPanel, body, permissions); target.replaceChildren(root);

  function clearPanes() {
    for (const pane of panes.values()) {
      clearTimeout(pane.replyTimer); pane.replyTimer = null; pane.browserActivityModel = null;
    }
    panes.clear(); activePane = null; empty.hidden = false; deck.replaceChildren(empty); tabs.replaceChildren();
  }
  function clearAccountView() {
    epoch++; connected = false; sessions = []; settings = {auto_approve_sessions: [], auto_approve_all: false}; connectionRevision = null; settingsRevision = null;
    clearPanes();
    autoAll.checked = false; autoAll.disabled = true;
    delete autoAll.dataset.saving;
    openedDefault = false;
    approvalSignature = auditSignature = ''; approvalsBox.replaceChildren(); audit.replaceChildren();
  }
  function renderAccounts() {
    accountSelect.replaceChildren();
    for (const account of accounts) {
      const option = document.createElement('option'); option.value = account.account_id; option.textContent = account.label || account.account_id;
      option.selected = account.account_id === accountId; accountSelect.append(option);
    }
    accountSelect.hidden = accounts.length === 0;
  }
  accountSelect.addEventListener('change', () => {
    if (accountSelect.value === accountId) return;
    importTargetId = null;
    accountId = accountSelect.value; clearAccountView(); showConnected(); schedule(0);
  });

  let noticeSource = null;
  function showError(error, source = 'general') {
    if (disposed) return;
    notice.textContent = error.detail || error.message || 'Muse 暂时不可用'; notice.hidden = false;
    noticeSource = source;
    if (error.code === 'muse_login_expired') { badge.textContent = '登录态已过期'; loginPanel.hidden = false; }
  }
  function clearError(source) {
    if (noticeSource !== source) return;
    notice.textContent = ''; notice.hidden = true; noticeSource = null;
  }
  function showConnected() {
    badge.textContent = connected ? '已导入登录态' : '未连接'; badge.dataset.connected = String(connected);
    newChat.disabled = split.disabled = !connected; disconnect.hidden = !connected;
    autoAll.disabled = !connected || autoAll.dataset.saving === 'true';
    for (const pane of panes.values()) pane.auto.disabled = pane.draft || !connected || settings.auto_approve_all === true;
    if (!connected) loginPanel.hidden = false;
    root.dataset.connected = String(connected);
  }
  function renderSessions() {
    list.replaceChildren(); const query = search.value.toLocaleLowerCase();
    for (const session of sessions.filter(s => (s.title || '').toLocaleLowerCase().includes(query))) {
      const item = action('', session.is_primary ? 'sparkles' : 'git-branch', () => openSession(session));
      item.className = 'muse-session'; item.dataset.sessionId = session.session_id;
      const text = el('span'); text.append(el('strong', '', session.title || '未命名会话'),
        el('small', '', (session.is_primary ? '主会话' : session.is_thread ? '子会话' : '执行会话') + ' · ' + (session.status || '就绪')));
      item.append(text); if (activePane === session.session_id) item.setAttribute('aria-current', 'true');
      list.append(item);
    }
    if (!list.children.length) list.append(el('p', 'meta', connected ? '暂无匹配的会话' : '连接后显示你的对话'));
  }
  function showPanes() {
    deck.dataset.layout = layout;
    for (const [id, pane] of panes) {
      const wasHidden = pane.host.hidden;
      pane.host.hidden = layout === 'single' && id !== activePane;
      pane.tab.setAttribute('aria-selected', String(id === activePane));
      pane.host.dataset.active = String(id === activePane);
      if (pane.host.hidden) {
        clearTimeout(pane.replyTimer); pane.replyTimer = null;
      } else if (wasHidden && pane.awaitingReply && !pane.loading && !suspended) loadHistory(pane);
    }
    empty.hidden = panes.size > 0; renderSessions();
  }
  function closePane(pane) {
    clearTimeout(pane.replyTimer);
    panes.delete(pane.id); pane.host.remove(); pane.tab.remove();
    pane.browserActivityModel = null;
    if (activePane === pane.id) activePane = panes.keys().next().value || null;
    if (!panes.size) deck.append(empty);
    showPanes();
  }
  function openSession(session, draft = false) {
    const id = session.session_id;
    if (!panes.has(id)) {
      if (panes.size >= 6) { showError(new Error('最多同时打开 6 个对话窗口，请先关闭一个。')); return; }
      const host = el('section', 'muse-pane'); host.dataset.sessionId = id;
      const tab = action(session.title || '未命名会话', '', () => { activePane = id; showPanes(); });
      tab.setAttribute('role', 'tab'); tab.id = 'muse-tab-' + id; host.setAttribute('role', 'tabpanel'); host.setAttribute('aria-labelledby', tab.id);
      const header = el('header', 'muse-pane-header');
      const heading = el('h2', '', session.title || '未命名会话');
      const close = action('关闭', 'x', () => closePane(pane)); close.setAttribute('aria-label', '关闭对话窗口');
      const autoLabel = el('label', 'muse-auto-label');
      const auto = el('input'); auto.type = 'checkbox'; auto.setAttribute('aria-label', '自动审批此会话');
      auto.checked = settings.auto_approve_sessions.includes(id); auto.disabled = draft;
      autoLabel.append(auto, document.createTextNode('自动审批'));
      header.append(heading, autoLabel, close);
      const browserActivity = createBrowserActivity();
      const scroll = el('div', 'muse-transcript'); scroll.dataset.chatScroll = ''; scroll.tabIndex = 0; scroll.setAttribute('aria-label', '对话内容');
      const older = action('加载更早消息', 'arrow-up', () => loadHistory(pane, true)); older.hidden = true;
      const messages = el('div', 'muse-messages');
      messages.append(el('p', 'muse-empty', draft ? '发送第一条消息，开始一个独立的 Muse 子会话。' : '正在加载对话…'));
      const anchor = el('auto-scroll-anchor'); scroll.append(older, messages, anchor);
      const state = el('p', 'muse-pane-state'); state.setAttribute('role', 'status');
      const continueInput = action('已核对，继续输入', 'arrow-right', () => {
        clearTimeout(pane.replyTimer); pane.replyTimer = null;
        pane.pending = null; pane.input.value = ''; pane.input.readOnly = false;
        pane.replyTo = null; pane.reply.hidden = true; pane.send.textContent = '发送';
        pane.delivery = {phase: 'idle'}; pane.awaitingReply = false;
        continueInput.hidden = true; pane.state.textContent = '原操作保留为结果未知。可以输入下一条消息。';
        renderDelivery(pane);
        pane.input.focus();
      });
      continueInput.hidden = true; continueInput.className = 'muse-delivery-recovery';
      continueInput.title = '先检查会话中是否已有消息，再继续输入；这不会撤回或重新发送原消息。';
      const reply = el('div', 'muse-reply-target'); reply.hidden = true;
      const form = el('form', 'muse-composer');
      const prompt = el('chat-prompt-input'); const input = el('textarea'); input.rows = 2; input.maxLength = 30000;
      input.placeholder = '交给 Muse 的任务…'; input.setAttribute('aria-label', '发送给 Muse'); prompt.append(input);
      const controls = el('div', 'muse-composer-controls');
      controls.append(el('span', 'meta', 'Enter 发送 · Shift + Enter 换行'));
      const cancel = action('停止', 'square', async () => {
        const selectedAccount = accountId, selectedConnection = connectionRevision, generation = epoch;
        cancel.disabled = true;
        try { const result = await api.museCancel(id, selectedAccount, selectedConnection);
          if (!disposed && generation === epoch && accountId === selectedAccount) {
            clearTimeout(pane.replyTimer); pane.replyTimer = null;
            pane.awaitingReply = false; pane.delivery = {phase: 'idle'}; pane.input.readOnly = false; pane.send.disabled = false;
            state.textContent = result.cancelled ? '已请求 Muse 停止' : receiptText(result); renderDelivery(pane);
          } }
        catch (error) { if (!disposed && generation === epoch && accountId === selectedAccount) showError(error); }
        finally { cancel.disabled = false; }
      });
      const send = el('button', 'button-primary', '发送'); send.type = 'submit'; controls.append(cancel, send);
      form.append(reply, prompt, controls); host.append(header, browserActivity, scroll, state, continueInput, form);
      const pane = {id, host, tab, heading, scroll, messages, older, anchor, input, send, state, reply, auto,
        continueInput, browserActivity, browserActivityModel: null, events: [], nodes: new Map(), draft, loading: false, pending: null,
        delivery: {phase: 'idle'}, awaitingReply: false, replyTo: null};
      panes.set(id, pane); tabs.append(tab); deck.append(host);
      host.addEventListener('focusin', () => {
        activePane = id;
        for (const [key, p] of panes) p.tab.setAttribute('aria-selected', String(key === id));
      });
      auto.addEventListener('change', async () => {
        const selectedAccount = accountId, selectedConnection = connectionRevision;
        const selectedSettings = settingsRevision, generation = epoch, enabled = auto.checked;
        auto.disabled = true;
        try { const result = await api.museSettings(selectedAccount, selectedConnection, selectedSettings, id, enabled);
          if (!disposed && generation === epoch && accountId === selectedAccount) {
            settings = result.settings; settingsRevision = result.settings_revision;
            autoAll.checked = settings.auto_approve_all === true;
            showConnected();
          } }
        catch (error) {
          if (!disposed && generation === epoch && accountId === selectedAccount) {
            auto.checked = settings.auto_approve_sessions.includes(id); showError(error);
          }
        }
        finally { auto.disabled = pane.draft || !connected || settings.auto_approve_all === true; }
      });
      form.addEventListener('submit', event => { event.preventDefault(); sendMessage(pane); });
      if (!draft) loadHistory(pane);
    }
    activePane = id; showPanes();
  }
  function receiptText(result) {
    if (result.state === 'confirmed') return 'Muse 已确认接收';
    if (result.state === 'rejected') return result.result?.detail || 'Muse 未接受此操作';
    if (result.state === 'retryable') return 'Muse 环境尚未就绪，可以用同一个请求标识重试。';
    if (result.state === 'dispatching') return '同一请求仍在派发中，请稍后核对；不会重复发送。';
    return '投递结果尚未确认。请检查历史消息；此请求不会被自动重复发送。';
  }

  function renderDelivery(pane) {
    pane.messages.querySelectorAll('[data-transient-message]').forEach(node => node.remove());
    const delivery = pane.delivery;
    if (delivery && delivery.phase !== 'idle' || pane.awaitingReply) {
      pane.messages.querySelectorAll('.muse-empty').forEach(node => node.remove());
    }
    if (delivery && delivery.phase !== 'idle' && !delivery.userConfirmed) {
      pane.messages.append(renderMusePendingMessage({
        phase: delivery.userPhase || delivery.phase,
        requestId: delivery.requestId,
        text: delivery.text,
        label: delivery.userLabel,
      }));
    }
    if (pane.awaitingReply) {
      pane.messages.append(renderMuseThinkingMessage({
        phase: delivery.phase === 'executing' ? 'executing' : delivery.phase === 'streaming' ? 'streaming' : 'thinking',
        label: delivery.assistantLabel,
      }));
      pane.anchor.setAttribute('is-streaming', '');
      pane.anchor.scrollToBottom();
    } else if (!delivery || delivery.phase === 'idle') {
      pane.anchor.removeAttribute('is-streaming');
    }
  }

  function finishDelivery(pane) {
    clearTimeout(pane.replyTimer);
    pane.replyTimer = null;
    pane.awaitingReply = false;
    pane.delivery = {phase: 'idle'};
    pane.input.readOnly = false;
    pane.send.disabled = false;
    pane.send.textContent = '发送';
    pane.state.textContent = 'Muse 已回复';
    pane.continueInput.hidden = true;
    pane.sentUnknown = false;
    renderDelivery(pane);
  }

  function reconcileDelivery(pane, events, messages) {
    const delivery = pane.delivery;
    if (!delivery || delivery.phase === 'idle') return;
    const baseline = Number.isFinite(delivery.baselineSeq) ? delivery.baselineSeq : 0;
    const recent = (events || []).filter(event => {
      const sequence = eventSequence(event);
      return sequence === null || sequence > baseline;
    });
    const matchingUser = (messages || []).filter(event => eventRole(event) === 'user' &&
      (eventSequence(event) === null || eventSequence(event) > baseline) &&
      eventText(event).trim() === String(delivery.text || '').trim())
      .sort((a, b) => (eventSequence(a) || 0) - (eventSequence(b) || 0)).pop();
    if (matchingUser) delivery.userConfirmed = true;
    if (!pane.awaitingReply) { renderDelivery(pane); return; }
    const userSequence = matchingUser && eventSequence(matchingUser);
    const responseEvents = recent.filter(event => userSequence === null || eventSequence(event) === null || eventSequence(event) > userSequence);
    const assistant = responseEvents.reduce((latest, event) => {
      if (eventRole(event) !== 'assistant' || event.event_name === 'delta.presentation') return latest;
      if (!latest) return event;
      const sequence = eventSequence(event), latestSeq = eventSequence(latest);
      return sequence !== null && (latestSeq === null || sequence > latestSeq) ? event : latest;
    }, null);
    const streaming = responseEvents.some(eventIsStreaming);
    const executing = browserTaskIsActive(responseEvents);
    if (assistant && !eventIsStreaming(assistant)) {
      finishDelivery(pane);
      return;
    }
    delivery.phase = executing ? 'executing' : streaming || assistant ? 'streaming' : 'thinking';
    delivery.assistantLabel = executing ? 'Muse 正在执行任务…' : streaming || assistant ? 'Muse 正在输出…' : 'Muse 正在思考…';
    pane.state.textContent = delivery.assistantLabel;
    renderDelivery(pane);
  }

  function scheduleReplyPoll(pane) {
    clearTimeout(pane.replyTimer);
    pane.replyTimer = null;
    if (!pane.awaitingReply || disposed || suspended || !panes.has(pane.id) || pane.host.hidden) return;
    pane.replyTimer = setTimeout(() => {
      pane.replyTimer = null;
      loadHistory(pane);
    }, 900);
  }

  async function sendMessage(pane) {
    if (pane.send.disabled || !pane.input.value.trim()) return;
    const generation = epoch;
    if (!pane.pending) pane.pending = {text: pane.input.value, id: crypto.randomUUID(), replyTo: pane.replyTo};
    const baselineSeq = pane.delivery && pane.delivery.requestId === pane.pending.id && Number.isFinite(pane.delivery.baselineSeq)
      ? pane.delivery.baselineSeq : latestSequence(pane.events);
    const previousDelivery = pane.delivery;
    pane.delivery = {
      phase: 'sending', userPhase: 'sending', requestId: pane.pending.id, text: pane.pending.text,
      baselineSeq, userConfirmed: false,
      userLabel: '正在发送…', assistantLabel: '',
    };
    // A retry reuses the same idempotency key. Keep a previously confirmed
    // history match while the transport receipt is being refreshed so the UI
    // does not briefly regress from a real user message to a pending copy.
    if (previousDelivery?.requestId === pane.pending.id && previousDelivery.userConfirmed) {
      pane.delivery.userConfirmed = true;
    }
    pane.awaitingReply = false;
    pane.send.disabled = true; pane.continueInput.hidden = true; pane.input.readOnly = true; pane.state.textContent = '正在发送…';
    renderDelivery(pane);
    try {
      const result = await api.museSend(pane.id, pane.pending.text, pane.pending.id, pane.pending.replyTo, accountId, connectionRevision);
      if (disposed || epoch !== generation) return;
      pane.state.textContent = receiptText(result);
      if (result.state === 'confirmed') {
        pane.pending = null; pane.input.value = ''; pane.input.readOnly = true; pane.draft = false; pane.auto.disabled = false;
        pane.replyTo = null; pane.reply.hidden = true; pane.awaitingReply = true;
        pane.delivery.phase = 'thinking'; pane.delivery.userPhase = 'sent';
        pane.delivery.userLabel = '已发送，等待 Muse 回复…'; pane.delivery.assistantLabel = 'Muse 正在思考…';
        pane.send.textContent = '等待回复…'; renderDelivery(pane);
        await loadHistory(pane); schedule(0);
      } else if (result.state === 'rejected') {
        pane.pending = null; pane.input.readOnly = false; pane.send.textContent = '发送';
        pane.delivery = {phase: 'idle'}; renderDelivery(pane);
      } else if (result.state === 'retryable') {
        pane.input.readOnly = false; pane.send.textContent = '重试发送'; pane.continueInput.hidden = true;
        pane.delivery.phase = pane.delivery.userPhase = 'retryable'; pane.delivery.userLabel = '发送失败，可重试'; renderDelivery(pane);
      } else if (result.state === 'dispatching') {
        pane.send.textContent = '核对投递结果'; pane.continueInput.hidden = true;
        pane.sentUnknown = true; pane.delivery.phase = pane.delivery.userPhase = 'unknown';
        pane.delivery.userLabel = '正在核对投递结果…'; renderDelivery(pane); schedule(1000); await loadHistory(pane);
      } else {
        pane.send.textContent = '核对投递结果'; pane.continueInput.hidden = false;
        pane.sentUnknown = true; pane.delivery.phase = pane.delivery.userPhase = 'unknown';
        pane.delivery.userLabel = '投递结果待核对'; renderDelivery(pane); await loadHistory(pane);
      }
    } catch (error) {
      if (!disposed && epoch === generation) {
        pane.state.textContent = '连接中断。可用同一个请求标识核对投递结果。'; pane.send.textContent = '核对投递结果';
        pane.continueInput.hidden = false; pane.sentUnknown = true;
        pane.delivery.phase = pane.delivery.userPhase = 'unknown'; pane.delivery.userLabel = '连接中断，投递结果待核对';
        renderDelivery(pane); showError(error); await loadHistory(pane);
      }
    } finally { pane.send.disabled = pane.awaitingReply; }
  }
  async function loadHistory(pane, older = false) {
    if (pane.loading || (pane.draft && !pane.sentUnknown) || disposed || suspended) return;
    const generation = epoch; pane.loading = true; pane.older.disabled = true;
    const atBottom = pane.scroll.scrollHeight - pane.scroll.scrollTop - pane.scroll.clientHeight < 90;
    const height = pane.scroll.scrollHeight;
    try {
      const finiteSeq = pane.events.map(e => e.seq).filter(Number.isFinite);
      const before = older && finiteSeq.length ? Math.min(...finiteSeq) : undefined;
      const after = !older && finiteSeq.length ? (pane.catchup?.after ?? Math.max(...finiteSeq)) : undefined;
      const continuation = !older ? pane.catchup?.before : undefined;
      const result = await api.museHistory(pane.id, accountId, connectionRevision, before, after, continuation);
      if (disposed || generation !== epoch || !panes.has(pane.id)) return;
      const incomingEvents = result.chat_events || [];
      pane.events = mergeEvents(pane.events, incomingEvents);
      pane.browserActivityModel = reduceBrowserActivity(pane.browserActivityModel, incomingEvents,
        {allowUnordered: !older, allowEqual: !older});
      updateBrowserActivity(pane.browserActivity, pane.browserActivityModel, {accountId, connectionRevision});
      if (older || !pane.loaded) pane.older.hidden = !result.has_more_before;
      if (!older && result.has_more_after) {
        pane.catchup = {after, before: result.continuation_before_seq}; schedule(0);
      } else if (!older) pane.catchup = null;
      pane.loaded = true;
      const messages = visibleMessages(pane.events);
      const visibleKeys = new Set(messages.map(message => message.key));
      for (const [key, previous] of pane.nodes) if (!visibleKeys.has(key)) { previous.node.remove(); pane.nodes.delete(key); }
      if (!messages.length) pane.messages.replaceChildren(el('p', 'muse-empty', '此会话还没有消息。'));
      else {
        if (!pane.nodes.size) pane.messages.replaceChildren();
        for (const message of messages) {
          const signature = JSON.stringify(message), previous = pane.nodes.get(message.key);
          if (previous?.signature === signature) continue;
          const node = renderMuseMessage(message, event => {
            pane.replyTo = event.message_id; pane.reply.hidden = false; pane.reply.replaceChildren(
              el('span', '', '回复：' + String(event.display_text || event.payload?.content || '消息').slice(0, 90)),
              action('取消引用', 'x', () => { pane.replyTo = null; pane.reply.hidden = true; })); pane.input.focus();
          }, {accountId, connectionRevision});
          if (previous) previous.node.replaceWith(node);
          pane.nodes.set(message.key, {signature, node});
        }
        // Leave unchanged nodes attached: reconnecting Web Components can
        // rebuild their children and discard selection or expanded content.
        let cursor = pane.messages.firstChild;
        for (const message of messages) {
          const node = pane.nodes.get(message.key).node;
          if (node !== cursor) pane.messages.insertBefore(node, cursor);
          cursor = node.nextSibling;
        }
      }
      if (older) pane.scroll.scrollTop += pane.scroll.scrollHeight - height;
      else if (atBottom) { pane.anchor.setAttribute('is-streaming', ''); pane.anchor.scrollToBottom(); }
      reconcileDelivery(pane, pane.events, messages);
      renderDelivery(pane);
    } catch (error) {
      if (!disposed && generation === epoch) {
        pane.state.textContent = pane.awaitingReply ? '等待 Muse 回复时，状态刷新失败…' : (error.detail || error.message);
        renderDelivery(pane);
      }
    } finally {
      pane.loading = false; pane.older.disabled = false;
      scheduleReplyPoll(pane);
    }
  }
  let approvalSignature = '', auditSignature = '';
  autoAll.addEventListener('change', async () => {
    const selectedAccount = accountId, selectedConnection = connectionRevision;
    const selectedSettings = settingsRevision, generation = epoch, enabled = autoAll.checked;
    autoAll.disabled = true; autoAll.dataset.saving = 'true';
    try {
      const result = await api.museSettings(selectedAccount, selectedConnection, selectedSettings, null, null, enabled);
      if (!disposed && generation === epoch && accountId === selectedAccount) {
        settings = result.settings; settingsRevision = result.settings_revision;
        autoAll.checked = settings.auto_approve_all === true;
        showConnected();
      }
    } catch (error) {
      if (!disposed && generation === epoch && accountId === selectedAccount) {
        autoAll.checked = settings.auto_approve_all === true; showError(error);
      }
    } finally {
      if (!disposed && generation === epoch && accountId === selectedAccount) {
        delete autoAll.dataset.saving; autoAll.disabled = !connected;
      }
    }
  });
  async function refresh() {
    if (disposed || suspended || busy) return;
    busy = true; const generation = epoch;
    try {
      const status = await api.museStatus(accountId);
      if (disposed || generation !== epoch) return;
      if (connectionRevision !== undefined && connectionRevision !== status.connection_revision) {
        accounts = status.accounts || accounts; accountId = status.account_id || accountId; accountLabel = status.label || accountLabel;
        clearAccountView(); connectionRevision = status.connection_revision; connected = status.connected;
        settings = status.settings || {auto_approve_sessions: [], auto_approve_all: false};
        settingsRevision = status.settings_revision; autoAll.checked = settings.auto_approve_all === true;
        renderAccounts(); showConnected(); renderSessions(); return;
      }
      connectionRevision = status.connection_revision;
      connected = status.connected; settings = status.settings || {auto_approve_sessions: [], auto_approve_all: false};
      settingsRevision = status.settings_revision; autoAll.checked = settings.auto_approve_all === true;
      accounts = status.accounts || accounts; accountId = status.account_id || accountId; accountLabel = status.label || accountLabel;
      renderAccounts(); showConnected();
      const signature = JSON.stringify(status.audit);
      if (signature !== auditSignature) {
        auditSignature = signature; audit.replaceChildren(el('h3', '', '最近操作'));
        for (const record of (status.audit || []).slice(0, 12)) audit.append(el('p', 'meta',
          new Date(record.created * 1000).toLocaleString() + ' · ' +
          ({auto_approve: '自动审批', approval: '手动审批', send: '发送消息', cancel: '停止生成'}[record.action] || record.action) + ' · ' +
          ({confirmed: '已确认', rejected: '已拒绝', unknown: '结果未知'}[record.state] || record.state)));
      }
      if (!connected) { clearError('poll'); return; }
      const [sessionResult, approvalResult] = await Promise.allSettled([
        api.museSessions(accountId, connectionRevision), api.museApprovals(accountId, connectionRevision)]);
      if (disposed || generation !== epoch) return;
      if (sessionResult.status === 'fulfilled') {
        sessions = sessionResult.value.sessions || []; renderSessions(); badge.textContent = '已连接';
        for (const session of sessions) {
          const pane = panes.get(session.session_id);
          if (pane) { pane.heading.textContent = pane.tab.textContent = session.title || '未命名会话';
            pane.draft = false; pane.auto.disabled = settings.auto_approve_all === true;
            pane.auto.checked = settings.auto_approve_sessions.includes(pane.id); }
        }
        if (!openedDefault && sessions.length) { openedDefault = true; openSession(sessions.find(s => s.is_primary) || sessions[0]); }
      } else showError(sessionResult.reason, 'poll');
      if (approvalResult.status === 'fulfilled') {
        const result = approvalResult.value, pending = result.pending_approvals || result.pending || [];
        const signature = JSON.stringify(pending);
        permissionHeading.textContent = pending.length ? '权限请求（' + pending.length + '）与操作记录' : '权限请求与操作记录';
        if (signature !== approvalSignature) {
          approvalSignature = signature; approvalsBox.replaceChildren();
          const selectedAccount = accountId, selectedConnection = connectionRevision, selectedGeneration = epoch;
          pending.forEach(item => approvalsBox.append(renderApproval(item, async (approval, decision) => {
            if (disposed || selectedGeneration !== epoch || accountId !== selectedAccount) return;
            try { const response = await api.museDecide(approval.approval_id, decision, approval.message_revision || 0, selectedAccount, selectedConnection);
              if (disposed || selectedGeneration !== epoch || accountId !== selectedAccount) return;
              notice.textContent = receiptText(response); notice.hidden = false; schedule(0); }
            catch (error) { if (!disposed && selectedGeneration === epoch && accountId === selectedAccount) showError(error); }
          })));
          if (pending.length) permissions.open = true;
          else approvalsBox.append(el('p', 'meta', '没有待处理的权限请求'));
        }
      } else showError(approvalResult.reason, 'poll');
      if (sessionResult.status === 'fulfilled' && approvalResult.status === 'fulfilled') clearError('poll');
      await Promise.all([...panes.values()].filter(p => !p.host.hidden && !p.awaitingReply).map(p => loadHistory(p)));
    } catch (error) { showError(error, 'poll'); }
    finally {
      busy = false;
      const needsReplyPolling = [...panes.values()].some(pane => pane.awaitingReply);
      schedule(needsReplyPolling ? 4000 : [...panes.values()].some(pane => pane.catchup) ? 0 : 4000);
    }
  }
  function schedule(delay) {
    clearTimeout(timer); if (!disposed && !suspended) timer = setTimeout(refresh, delay);
  }
  refresh();
  const dispose = () => { disposed = true; epoch++; clearTimeout(timer); clearPanes(); credential.value = ''; target.replaceChildren(); };
  dispose.suspend = () => { suspended = true; clearTimeout(timer); for (const pane of panes.values()) { clearTimeout(pane.replyTimer); pane.replyTimer = null; } };
  dispose.resume = () => { suspended = false; schedule(0); };
  return dispose;
}
