import { pagePath } from '../routes.js';

const labels = {user_message: '用户消息', assistant_message: '助手消息', tool_call: '工具调用', tool_result: '工具结果'};

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function snippet(row) {
  const content = element('p', 'session-search-snippet');
  const characters = Array.from(row.snippet);
  let end = 0;
  for (const [start, stop] of row.highlight) {
    content.append(document.createTextNode(characters.slice(end, start).join("")));
    content.append(element('mark', '', characters.slice(start, stop).join("")));
    end = stop;
  }
  content.append(document.createTextNode(characters.slice(end).join("")));
  return content;
}

export function mountSessionSearch(root, client) {
  let disposed = false;
  let requestId = 0;
  let cursor = null;
  let filters = null;
  const form = element('form', 'panel session-search-form');
  const controls = new Map();
  const fields = [
    ['q', '搜索内容', 'search', '输入 2–128 个字符'],
    ['machine', '机器', 'text', '全部机器'],
    ['project', '项目', 'text', '全部项目'],
    ['agent', 'Agent', 'text', '全部 Agent'],
    ['from', '开始时间', 'datetime-local', ''],
    ['to', '结束时间', 'datetime-local', ''],
  ];
  for (const [name, title, type, placeholder] of fields) {
    const label = element('label', name === 'q' ? 'session-search-query' : '');
    label.append(element('span', '', title));
    const input = element('input');
    input.name = name;
    input.type = type;
    input.placeholder = placeholder;
    input.maxLength = name === 'q' ? 128 : 256;
    if (name === 'q') { input.required = true; input.minLength = 2; }
    label.append(input);
    form.append(label);
    controls.set(name, input);
  }
  const search = element('button', 'button-primary', '搜索');
  search.type = 'submit';
  form.append(search);
  const status = element('p', 'meta', '搜索脱敏会话中的消息和工具记录。');
  status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');
  const results = element('ol', 'session-search-results');
  const next = element('button', '', '下一页');
  next.type = 'button';
  next.hidden = true;
  root.replaceChildren(form, status, results, next);

  async function run(after = null) {
    const current = ++requestId;
    search.disabled = true;
    next.disabled = true;
    status.textContent = '正在搜索…';
    try {
      const data = await client.searchSessions({...filters, limit: 50, after});
      if (disposed || current !== requestId) return;
      results.replaceChildren();
      for (const row of data.items) {
        const item = element('li', 'panel session-search-result');
        const link = element('a', '', labels[row.kind] + ' · #' + row.sequence);
        const query = new URLSearchParams({sequence: String(row.sequence)});
        link.href = pagePath('session', row.session_id) + '?' + query.toString();
        item.append(link, snippet(row));
        item.append(element('p', 'meta', [row.session_id, row.machine_id, row.project_id, row.agent_family, row.emitted_at].filter(Boolean).join(' · ')));
        results.append(item);
      }
      cursor = data.next_cursor;
      status.textContent = data.items.length ? '本页 ' + data.items.length + ' 条结果' : '没有匹配的会话记录。';
      next.hidden = !cursor;
    } catch (error) {
      if (disposed || current !== requestId) return;
      results.replaceChildren();
      cursor = null;
      next.hidden = true;
      status.textContent = error.code === 'search_disabled' ? '会话搜索尚未启用。'
        : error.code === 'invalid_filter' ? '请检查筛选条件和时间范围。'
        : error.code === 'invalid_query' ? '请输入 2–128 个字符。'
        : '暂时无法搜索会话，请重试。';
    } finally {
      if (!disposed && current === requestId) { search.disabled = false; next.disabled = false; }
    }
  }

  function submit(event) {
    event.preventDefault();
    filters = {};
    for (const [name, input] of controls) {
      const value = input.value.trim();
      if (!value) continue;
      if (name === 'from' || name === 'to') {
        const date = new Date(value);
        if (!Number.isFinite(date.getTime())) { status.textContent = '请输入有效的时间。'; return; }
        filters[name] = date.toISOString();
      } else filters[name] = value;
    }
    if (Array.from(filters.q || '').length < 2 || Array.from(filters.q || '').length > 128) {
      status.textContent = '请输入 2–128 个字符。';
      return;
    }
    if (filters.from && filters.to && filters.from > filters.to) {
      status.textContent = '开始时间不能晚于结束时间。';
      return;
    }
    run();
  }
  const more = () => { if (cursor) run(cursor); };
  form.addEventListener('submit', submit);
  next.addEventListener('click', more);
  return () => {
    disposed = true;
    requestId += 1;
    form.removeEventListener('submit', submit);
    next.removeEventListener('click', more);
  };
}
