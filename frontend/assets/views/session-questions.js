/* Structured questions: bounded DOM, persistent drafts and signed-command receipts. */
const MAX_QUESTION_DRAFTS = 100;
const TERMINAL_RECEIPTS = new Set(['succeeded', 'failed', 'expired', 'rejected', 'cancelled']);
const INPUT_REJECTIONS = new Set(['invalid_interaction_answer', 'invalid_json', 'invalid_message']);
const CLOSED_QUESTIONS = new Set(['interaction_answered', 'interaction_expired', 'session_not_managed']);

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  if (className) node.className = className;
  return node;
}

export function renderQuestions(host, {events, session, api, state, paint, enabled}) {
  state.questions ||= new Map();
  const answered = new Set(events.filter(e => e.kind === 'interaction_response').map(e => e.payload.request_id));
  const visible = new Set(events.filter(e => e.kind === 'interaction_request').map(e => e.payload.request_id));
  for (const [id, draft] of state.questions) {
    if (answered.has(id)) draft.closed = true;
    const unresolved = draft.busy || (!draft.closed && (draft.submitted || (draft.command && !TERMINAL_RECEIPTS.has(draft.status))));
    if (visible.has(id) || unresolved) continue;
    draft.disposed = true;
    clearTimeout(draft.timer);
    state.questions.delete(id);
  }
  const canAnswer = enabled && session.managed && session.capabilities?.includes('interactive_questions') &&
    !['closed', 'failed'].includes(session.status) && !['completed', 'failed', 'stopped'].includes(session.current_state);
  const rendered = new Set();
  for (const event of events) {
    if (event.kind !== 'interaction_request') continue;
    const q = event.payload;
    if (rendered.has(q.request_id)) continue;
    rendered.add(q.request_id);
    let draft = state.questions.get(q.request_id);
    if (!draft) {
      draft = {selected: new Set(), text: '', busy: false, status: '', command: '', token: crypto.randomUUID()};
      if (state.questions.size < MAX_QUESTION_DRAFTS) state.questions.set(q.request_id, draft);
    }
    const retained = state.questions.has(q.request_id);
    const card = el('section', '', 'session-question');
    card.dataset.requestId = q.request_id;
    card.append(el('h3', q.prompt));
    const unavailable = !retained || !canAnswer || answered.has(q.request_id) || draft.closed || Boolean(draft.command);
    const locked = unavailable || draft.busy || Boolean(draft.submitted);
    let statusText = canAnswer ? '等待回答' : '只读问题';
    if (answered.has(q.request_id)) statusText = '已回答';
    if (!retained) statusText = '问题状态已达保留上限，请先完成待确认提交。';
    const status = el('p', draft.status || statusText, 'meta');
    status.setAttribute('role', 'status');
    const submit = async () => {
      if (unavailable || draft.busy || !api.answerSessionQuestion) return;
      if (!draft.submitted) {
        draft.submitted = Object.freeze({request_id: q.request_id,
          selected: Object.freeze(q.options.filter(o => draft.selected.has(o.id)).map(o => o.id)),
          text: draft.text.trim(), client_token: draft.token});
      }
      draft.busy = true;
      draft.status = '提交中';
      paint();
      try {
        const result = await api.answerSessionQuestion(session.session_id,
          {...draft.submitted, selected: [...draft.submitted.selected]});
        if (state.disposed) return;
        draft.command = result.command_id;
        draft.submitted = null;
        draft.status = result.status === 'pending' ? 'queued' : result.status;
        if (api.getControlReceipt) poll(api, state, draft, paint, 0);
      } catch (error) {
        if (state.disposed) return;
        draft.status = '提交结果尚未确认，请重试同一答案。';
        if (error?.status === 400 && INPUT_REJECTIONS.has(error.code)) {
          draft.submitted = null;
          draft.token = crypto.randomUUID();
          draft.status = '回答未通过校验，请修改后提交。';
        }
        if (CLOSED_QUESTIONS.has(error?.code)) {
          draft.closed = true;
          draft.submitted = null;
          draft.status = error.code === 'interaction_answered' ? '已回答' : '问题已失效';
        }
      } finally {
        draft.busy = false;
        if (!state.disposed) paint();
      }
    };
    const choices = el('div', '', 'session-question-options');
    for (const option of q.options) {
      const button = el('button', option.label);
      button.type = 'button';
      button.disabled = locked;
      button.setAttribute('aria-pressed', String(draft.selected.has(option.id)));
      button.addEventListener('click', () => {
        if (locked || draft.busy || draft.submitted) return;
        if (q.question_type !== 'multi') {
          draft.selected = new Set([option.id]);
          submit();
          return;
        }
        if (draft.selected.has(option.id)) draft.selected.delete(option.id);
        else draft.selected.add(option.id);
        paint();
      });
      choices.append(button);
    }
    card.append(choices);
    if (q.allow_custom || q.question_type === 'text') {
      const text = el('textarea');
      text.rows = 3;
      text.maxLength = 2000;
      text.value = draft.text;
      text.disabled = locked;
      text.setAttribute('aria-label', '自定义回答');
      text.addEventListener('input', () => { if (!locked && !draft.submitted) draft.text = text.value; });
      card.append(text);
    }
    if (q.question_type === 'multi' || q.question_type === 'text' || q.allow_custom) {
      const confirm = el('button', q.question_type === 'multi' ? '确认 ' + draft.selected.size + ' 项' : '提交回答');
      confirm.type = 'button';
      confirm.disabled = locked;
      confirm.addEventListener('click', submit);
      card.append(confirm);
    }
    if (draft.submitted && !unavailable && !draft.busy) {
      const retry = el('button', '重试同一答案');
      retry.type = 'button';
      retry.addEventListener('click', submit);
      card.append(retry);
    }
    card.append(status);
    if (draft.command) card.append(el('p', draft.command, 'meta'));
    host.append(card);
  }
}

async function poll(api, state, draft, paint, count) {
  if (state.disposed || draft.disposed || count >= 30) return;
  try {
    const receipt = await api.getControlReceipt(draft.command);
    if (state.disposed || draft.disposed) return;
    draft.status = receipt.status;
    paint();
    if (!['queued', 'pending', 'delivered', 'accepted', 'executing'].includes(receipt.status)) return;
  } catch (error) {
    if (state.disposed || draft.disposed) return;
    draft.status = '回执暂不可用';
    paint();
  }
  draft.timer = setTimeout(() => poll(api, state, draft, paint, count + 1), 1000);
}

export function disposeQuestions(state) {
  for (const draft of state.questions?.values() || []) {
    draft.disposed = true;
    clearTimeout(draft.timer);
  }
  state.questions?.clear();
}
