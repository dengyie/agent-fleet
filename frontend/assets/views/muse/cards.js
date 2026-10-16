import {uiIcon} from '../../routes.js';
import {museResource} from '../../api/muse.js';

export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = String(text);
  return node;
}
export function action(label, icon, callback) {
  const button = el('button', 'button-secondary'); button.type = 'button';
  if (icon) button.append(uiIcon(icon, {size: 16}));
  button.append(document.createTextNode(label));
  button.addEventListener('click', callback);
  return button;
}
function safeUrl(value) {
  try {
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}
function link(title, value) {
  const url = safeUrl(value);
  if (!url) return el('span', 'meta', title || '资源');
  const node = el('a', 'muse-resource-link', title || new URL(url).hostname);
  node.href = url; node.target = '_blank'; node.rel = 'noopener noreferrer';
  return node;
}
function details(value, title = '查看卡片数据') {
  const node = el('details', 'muse-card-details');
  node.append(el('summary', '', title), el('pre', '', JSON.stringify(value, null, 2)));
  return node;
}
function markdown(text) {
  const node = el('stream-markdown'); node.setAttribute('content', String(text || '')); return node;
}

function resourceControl(path, title, preview = false, context = {}) {
  const box = el('div', 'muse-resource');
  const status = el('p', 'meta'); status.setAttribute('role', 'status');
  const button = action(title || (preview ? '查看截图' : '下载文件'), preview ? 'image' : 'paperclip', async () => {
    button.disabled = true; status.textContent = '正在读取…';
    try {
      const file = await museResource(path, context.accountId, context.connectionRevision);
      status.textContent = '';
      if (preview && ['image/png', 'image/jpeg', 'image/gif', 'image/webp'].includes(file.mime)) {
        const image = el('img', 'muse-image'); image.alt = title || 'Muse 截图';
        image.src = 'data:' + file.mime + ';base64,' + file.data_base64;
        box.replaceChildren(image, status);
      } else {
        const bytes = Uint8Array.from(atob(file.data_base64), c => c.charCodeAt(0));
        const url = URL.createObjectURL(new Blob([bytes], {type: 'application/octet-stream'}));
        const anchor = el('a'); anchor.href = url; anchor.download = file.filename; anchor.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      }
    } catch (error) { status.textContent = error.detail || error.message; }
    finally { button.disabled = false; }
  });
  box.append(button, status); return box;
}

function eventCards(event) {
  const payload = event.payload || {};
  if (event.event_name === 'delta.presentation') return [payload];
  return [
    ...(Array.isArray(payload.content) ? payload.content : []),
    ...(Array.isArray(event.resources) ? event.resources : []),
    ...(Array.isArray(payload.tool_calls) ? payload.tool_calls : []),
  ];
}

function browserTask(card) {
  if (!card || typeof card !== 'object') return false;
  const data = card.data || card;
  return (card.kind || card.type || data.type) === 'browser_task';
}

export function latestBrowserTask(events) {
  let latest = null;
  for (const event of events) {
    const payload = event.payload || {};
    if ((event.visibility && event.visibility !== 'public') ||
        (payload.visibility && payload.visibility !== 'public')) continue;
    for (const card of eventCards(event)) if (browserTask(card)) latest = card;
  }
  return latest;
}

export function createBrowserActivity() {
  const panel = el('details', 'muse-browser-activity');
  panel.dataset.browserActivity = '';
  panel.open = true;
  const summary = el('summary');
  summary.append(uiIcon('globe', {size: 16}), el('strong', '', 'Muse 云端浏览器'));
  summary.append(el('span', 'muse-browser-activity-status', '等待活动'));
  const content = el('div', 'muse-browser-activity-content');
  panel.append(summary, content);
  updateBrowserActivity(panel, []);
  return panel;
}

export function updateBrowserActivity(panel, events, context = {}) {
  const card = latestBrowserTask(events);
  const signature = card ? JSON.stringify(card) : 'empty';
  if (panel.dataset.signature === signature) return;
  panel.dataset.signature = signature;
  const status = panel.querySelector('.muse-browser-activity-status');
  const content = panel.querySelector('.muse-browser-activity-content');
  content.replaceChildren();
  if (!card) {
    status.textContent = '等待活动';
    content.append(el('p', 'meta', 'Muse 尚未在此会话返回浏览器活动。'));
    return;
  }

  const data = card.data || card;
  status.textContent = data.display_status || data.display_state || data.status || '状态未知';
  const title = data.activity_title || card.fallback_text;
  if (title) content.append(el('p', 'muse-browser-activity-title', title));
  const site = data.current_site || data.latest_tab;
  if (site && typeof site === 'object') {
    const siteLink = link(site.page_title || site.title || site.domain || '当前页面', site.url);
    siteLink.classList.add('muse-browser-activity-site');
    content.append(siteLink);
  }
  if (data.step_count != null) content.append(el('p', 'meta', `已执行 ${data.step_count} 步`));
  const screenshot = data.latest_screenshot?.url;
  if (typeof screenshot === 'string' && screenshot.startsWith('sandbox://workspace/')) {
    content.append(resourceControl(screenshot, '查看最新浏览器截图', true, context));
  }
  if (!content.children.length) content.append(el('p', 'meta', 'Muse 尚未提供更多浏览器活动信息。'));
}

export function renderCard(card, context = {}) {
  if (!card || typeof card !== 'object') return el('p', '', String(card));
  const data = card.data || card;
  const kind = card.kind || card.type || data.type || 'card';
  const box = el('section', 'muse-card'); box.dataset.cardType = kind;
  const title = data.title || data.name || data.filename || card.title;
  if (['tool_use', 'tool_call', 'tool_result'].includes(kind)) {
    const badge = el('tool-call-badge');
    badge.setAttribute('name', title || '工具调用');
    badge.setAttribute('status', ['completed', 'success', 'succeeded'].includes(data.status) ? 'success' :
      ['failed', 'error'].includes(data.status) ? 'error' : 'running');
    if (data.input || data.arguments) badge.setAttribute('args', JSON.stringify(data.input || data.arguments));
    if (data.output || data.result) badge.setAttribute('output', typeof (data.output || data.result) === 'string' ? data.output || data.result : JSON.stringify(data.output || data.result));
    box.append(badge); return box;
  }
  const names = {browser_task: '浏览器任务', task: '任务', plan: '执行计划', file_ref: '文件', file: '文件',
    image: '图片', image_url: '图片', sources: '引用来源', artifact: '作品', widget: '交互卡片', code: '代码'};
  const heading = el('div', 'muse-card-heading');
  heading.append(uiIcon(kind === 'browser_task' ? 'globe' : 'layers', {size: 16}), el('strong', '', title || names[kind] || kind));
  box.append(heading);
  if (kind === 'browser_task') {
    box.append(el('p', 'muse-card-status', data.display_status || data.display_state || data.status || ''),
      el('p', '', data.activity_title || card.fallback_text || ''));
    if (data.current_site || data.latest_tab) {
      const site = data.current_site || data.latest_tab;
      box.append(link(site.page_title || site.title || site.domain, site.url));
    }
    if (data.step_count != null) box.append(el('p', 'meta', `已执行 ${data.step_count} 步`));
    if (data.latest_screenshot?.url?.startsWith('sandbox://workspace/')) {
      box.append(resourceControl(data.latest_screenshot.url, '查看浏览器截图', true, context));
    }
  } else if (kind === 'code') {
    box.append(el('pre', '', data.code || data.text || ''));
  } else if (Array.isArray(data.sources)) {
    data.sources.forEach(source => { const row = el('div', 'muse-source'); row.append(link(source.title, source.url));
      if (source.snippet) row.append(el('p', 'meta', source.snippet)); box.append(row); });
  } else {
    const text = data.description || data.summary || data.text || data.content;
    if (typeof text === 'string') box.append(markdown(text));
    if (Array.isArray(data.steps)) {
      const list = el('ol');
      data.steps.forEach(step => list.append(el('li', '', typeof step === 'string' ? step : (step.title || step.description || '') + (step.status ? ' · ' + step.status : ''))));
      box.append(list);
    }
    const url = data.url || data.href || (typeof data.image_url === 'string' ? data.image_url : data.image_url?.url);
    if (url && !url.startsWith('sandbox://')) box.append(link(data.filename || data.title || '打开资源', url));
    if (data.path) box.append(el('code', 'muse-file-path', data.path));
    const filePath = data.path || url || card.src;
    if (typeof filePath === 'string' && /^(sandbox:\/\/)?\/?workspace\//.test(filePath)) {
      box.append(resourceControl(filePath, data.filename || '查看文件', ['image', 'image_url'].includes(kind), context));
    }
    // External images are opt-in: do not load tracking URLs automatically.
    if (['image', 'image_url'].includes(kind) && safeUrl(url)) {
      const show = action('显示图片', 'eye', () => {
        const image = el('img', 'muse-image'); image.alt = data.alt || title || 'Muse 图片';
        image.referrerPolicy = 'no-referrer'; image.src = safeUrl(url); image.loading = 'lazy';
        show.replaceWith(image);
      }); box.append(show);
    }
  }
  box.append(details(card));
  return box;
}

export function mergeEvents(existing, events) {
  const result = new Map(existing.map(e => [String(e.seq ?? e.message_id), e]));
  for (const event of events) result.set(String(event.seq ?? event.message_id), event);
  return [...result.values()].sort((a, b) => (a.seq || 0) - (b.seq || 0));
}

export function visibleMessages(events) {
  const result = new Map();
  for (const event of events) {
    if (event.visibility && event.visibility !== 'public') continue;
    const payload = event.payload || {};
    if (payload.visibility && payload.visibility !== 'public') continue;
    const key = event.event_name === 'delta.presentation' ? 'card:' + (payload.id || event.message_id) : 'message:' + (event.message_id || event.seq);
    result.set(key, {key, ...event});
  }
  return [...result.values()];
}

export function renderMuseMessage(event, onReply, context = {}) {
  const payload = event.payload || {}, role = event.role || payload.role || 'assistant';
  const article = el('article', 'muse-message muse-message-' + (role === 'user' ? 'user' : 'assistant'));
  article.dataset.messageId = event.message_id || '';
  const head = el('div', 'muse-message-meta');
  head.append(el('strong', '', role === 'user' ? '你' : 'Muse'));
  if (event.occurred_at_ms) head.append(el('time', '', new Date(event.occurred_at_ms).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})));
  article.append(head);
  if (event.event_name === 'delta.presentation') article.append(renderCard(payload, context));
  else {
    const content = event.display_text || payload.display_text || payload.content || '';
    if (typeof content === 'string' && content) {
      article.append(markdown(content));
      // Muse uses sandbox links in Markdown for generated files; the shared
      // Markdown component deliberately permits only HTTP links.
      const paths = new Set();
      for (const match of content.matchAll(/!?\[([^\]]*)\]\((sandbox:\/\/workspace\/[^\s)]+)\)/g)) {
        if (paths.has(match[2])) continue; paths.add(match[2]);
        article.append(resourceControl(match[2], match[1] || '查看文件', match[0].startsWith('!'), context));
      }
    }
    else if (Array.isArray(content)) content.forEach(item => article.append(item.type === 'text' ? markdown(item.text) : renderCard(item, context)));
    for (const item of [...(event.resources || []), ...(payload.tool_calls || [])]) article.append(renderCard(item, context));
    if (!content && !(event.resources || []).length) article.append(details(payload, '查看消息详情'));
  }
  if (onReply && event.message_id) {
    const button = action('引用回复', 'arrow-left', () => onReply(event)); button.className = 'muse-reply-button'; article.append(button);
  }
  return article;
}

export function renderApproval(item, onDecision) {
  const display = item.display || {}, card = el('section', 'muse-card muse-approval');
  card.dataset.approvalId = item.approval_id;
  card.append(el('strong', '', display.summary_title || display.display_name || item.display_name || '等待授权'),
    el('p', '', display.purpose_summary || display.rich_explanation || item.rich_explanation || item.short_explanation || ''),
    el('p', 'meta', display.scope_summary || item.host || ''));
  for (const row of display.detail_rows || []) card.append(el('p', 'meta', (row.label || '') + ' ' + (row.value || '')));
  const actions = el('div', 'muse-actions');
  for (const [decision, label] of [['allow_once', '允许这一次'], ['deny', '拒绝']]) {
    actions.append(action(label, decision === 'deny' ? 'x' : 'check', async () => {
      actions.querySelectorAll('button').forEach(b => b.disabled = true);
      try { await onDecision(item, decision); }
      finally { actions.querySelectorAll('button').forEach(b => b.disabled = false); }
    }));
  }
  card.append(actions, details(item, '权限请求详情'));
  return card;
}
