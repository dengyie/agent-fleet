export function eventSequence(event) {
  if (!event || event.seq === null || event.seq === undefined || event.seq === '') return null;
  const value = Number(event.seq);
  return Number.isFinite(value) ? value : null;
}

export function latestSequence(events) {
  return (events || []).reduce((latest, event) => Math.max(latest, eventSequence(event) || 0), 0);
}

export function eventRole(event) {
  const payload = event && event.payload || {};
  return event && (event.role || payload.role) || '';
}

export function eventContent(event) {
  const payload = event && event.payload || {};
  return event && (event.display_text || payload.display_text || payload.content) || '';
}

export function eventText(event) {
  const content = eventContent(event);
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) return content.filter(item => item && item.type === 'text')
    .map(item => item.text || '').join('');
  return '';
}

export function eventIsStreaming(event) {
  const payload = event && event.payload || {};
  const markers = [event && event.status, event && event.state, event && event.phase,
    payload.status, payload.state, payload.phase, payload.stream_state];
  return markers.some(value => ['thinking', 'streaming', 'running', 'in_progress', 'generating'].includes(String(value || '').toLowerCase())) ||
    (typeof (event && event.event_name) === 'string' && /(^|[.:])stream/.test(event.event_name));
}

export function eventCards(event) {
  const payload = event && event.payload || {};
  if (event && event.event_name === 'delta.presentation') return [payload];
  return [
    ...(Array.isArray(payload.content) ? payload.content : []),
    ...(Array.isArray(event && event.resources) ? event.resources : []),
    ...(Array.isArray(payload.tool_calls) ? payload.tool_calls : []),
  ];
}

export function isBrowserTaskCard(card) {
  if (!card || typeof card !== 'object') return false;
  const data = card.data || card;
  return (card.kind || card.type || data.type) === 'browser_task';
}

function isPublicEvent(event) {
  const payload = event && event.payload || {};
  return (!event.visibility || event.visibility === 'public') &&
    (!payload.visibility || payload.visibility === 'public');
}

export function latestBrowserTask(events) {
  return reduceBrowserActivity(null, events)?.card || null;
}

export function browserTaskIsActive(events) {
  const card = latestBrowserTask(events);
  const data = card && (card.data || card);
  const status = String(data && (data.display_status || data.display_state || data.status) || '').toLowerCase();
  return !!status && !/(completed|complete|success|succeeded|stopped|failed|cancelled|canceled|done|已完成|成功|停止|失败|取消)/i.test(status);
}

export function reduceBrowserActivity(previous, incomingEvents, {allowUnordered = true, allowEqual = true} = {}) {
  let current = previous || null;
  for (const event of incomingEvents || []) {
    const sequence = eventSequence(event);
    const cards = eventCards(event);
    if (!isPublicEvent(event)) {
      if (!cards.some(isBrowserTaskCard)) continue;
      if (sequence === null) {
        if (allowUnordered) current = null;
      } else if (!current || current.order === null || sequence >= current.order) {
        current = {card: null, data: null, order: sequence, signature: 'hidden:' + sequence};
      }
      continue;
    }
    for (const card of cards) {
      if (!isBrowserTaskCard(card)) continue;
      if (sequence === null && (!allowUnordered || (current && current.order !== null))) continue;
      if (sequence !== null && current && current.order !== null &&
          (sequence < current.order || (sequence === current.order && !allowEqual))) continue;
      const data = card.data || card;
      current = {card, data, order: sequence, signature: JSON.stringify(card)};
    }
  }
  return current;
}

export function mergeEvents(existing, incoming) {
  const result = new Map((existing || []).map(event => [String(event.seq ?? event.message_id), event]));
  for (const event of incoming || []) result.set(String(event.seq ?? event.message_id), event);
  return [...result.values()].sort((a, b) => (a.seq || 0) - (b.seq || 0));
}

export function visibleMessages(events) {
  const result = new Map();
  for (const event of events || []) {
    if (!isPublicEvent(event)) continue;
    const payload = event.payload || {};
    const key = event.event_name === 'delta.presentation' ? 'card:' + (payload.id || event.message_id) : 'message:' + (event.message_id || event.seq);
    result.set(key, {key, ...event});
  }
  return [...result.values()];
}
