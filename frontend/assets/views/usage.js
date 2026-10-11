/* Owner-scoped usage: all prices arrive as validated decimal strings. */
import { uiIcon } from '../routes.js';

const DIMENSIONS = {day: '日期（UTC）', project: '项目', machine: '机器', agent: 'Agent', model: '模型'};
const SOURCES = {provider: '供应商报告', configured: '配置估算', unknown: '未知'};

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function count(value) {
  return value === null ? '未知' : BigInt(value).toLocaleString('zh-CN');
}

function total(items, field) {
  if (items.some(item => item[field] === null)) return null;
  return items.reduce((sum, item) => sum + BigInt(item[field]), 0n).toString();
}

function renderResults(host, result) {
  host.replaceChildren();
  if (!result.items.length) {
    host.append(node('p', 'usage-empty', '此范围内没有已结算的用量记录。'));
    return;
  }
  const summary = node('div', 'usage-summary');
  for (const [label, field] of [['模型请求', 'requests'], ['总 Token', 'total_tokens'], ['含未知数据的请求', 'unknown_count']]) {
    const card = node('div', 'panel');
    card.append(node('span', 'meta', label), node('strong', '', count(total(result.items, field))));
    summary.append(card);
  }
  host.append(summary);
  const wrapper = node('div', 'usage-table-wrap panel');
  wrapper.tabIndex = 0;
  wrapper.setAttribute('role', 'region');
  wrapper.setAttribute('aria-label', '用量明细，可横向滚动');
  const table = node('table', 'usage-table');
  table.append(node('caption', '', '按结算时间聚合 · UTC · 费用币种 USD'));
  const head = node('thead');
  const headings = node('tr');
  const columns = [...result.group_by.map(key => [key, DIMENSIONS[key]]),
    ['requests', '请求'], ['input_tokens', '输入 Token'], ['output_tokens', '输出 Token'],
    ['total_tokens', '总 Token'], ['estimated_cost', '费用'], ['cost_source', '费用来源'], ['unknown_count', '未知请求']];
  for (const [, label] of columns) {
    const th = node('th', '', label); th.scope = 'col'; headings.append(th);
  }
  head.append(headings); table.append(head);
  const body = node('tbody');
  for (const item of result.items) {
    const row = node('tr');
    for (const [key] of columns) {
      let value = item[key];
      if (key === 'estimated_cost') value = value === null ? '未知' : '$' + value;
      else if (key === 'cost_source') value = SOURCES[value];
      else if (!DIMENSIONS[key]) value = count(value);
      row.append(node('td', value === '未知' || value === null ? 'usage-unknown' : '', value ?? '未知'));
    }
    body.append(row);
  }
  table.append(body); wrapper.append(table); host.append(wrapper);
}

export function mountUsage(container, client) {
  const page = node('section', 'usage-page');
  page.append(node('p', 'meta', '查看已结算模型请求的用量。配置估算仅供参考；未知费用或 Token 会保留为未知。项目对应运行工作区，未记录的 Agent 与机器显示未知。'));
  const form = node('form', 'usage-filters panel');
  const inputs = {};
  function field(key, label, type = 'text') {
    const wrap = node('label', 'usage-field', label);
    const input = node('input'); input.type = type; input.name = key;
    input.maxLength = key === 'model' ? 160 : 128;
    if (type === 'text') input.placeholder = '全部';
    if (type === 'date') input.required = true;
    wrap.append(input); form.append(wrap); inputs[key] = input;
    return input;
  }
  const today = new Date();
  const end = Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate());
  field('from', '开始日期（UTC）', 'date').value = new Date(end - 6 * 86400000).toISOString().slice(0, 10);
  field('to', '结束日期（含当日）', 'date').value = new Date(end).toISOString().slice(0, 10);
  for (const key of ['project', 'machine', 'agent', 'model']) field(key, DIMENSIONS[key] + '筛选');
  const grouping = node('label', 'usage-field', '分组方式');
  const select = node('select'); select.name = 'group_by';
  for (const [key, label] of [...Object.entries(DIMENSIONS), ['day,project,machine,agent,model', '全部维度']]) {
    const option = node('option', '', label); option.value = key; select.append(option);
  }
  grouping.append(select); form.append(grouping);
  const submit = node('button', 'button-primary', '查询用量'); submit.type = 'submit';
  submit.prepend(uiIcon('refresh', {size: 16})); form.append(submit);
  page.append(form);
  const status = node('p', 'usage-status'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
  const results = node('div', 'usage-results'); page.append(status, results); container.replaceChildren(page);
  let disposed = false, generation = 0;
  async function load(event) {
    if (event) event.preventDefault();
    const current = ++generation;
    const from = Date.parse(inputs.from.value + 'T00:00:00Z');
    const to = Date.parse(inputs.to.value + 'T00:00:00Z') + 86400000;
    if (!Number.isFinite(from) || !Number.isFinite(to) || to <= from || to - from > 90 * 86400000) {
      status.textContent = '请选择最多 90 天的有效日期范围。'; results.replaceChildren(); return;
    }
    const params = {from: new Date(from).toISOString(), to: new Date(to).toISOString(), group_by: select.value};
    for (const key of ['project', 'machine', 'agent', 'model']) params[key] = inputs[key].value.trim();
    submit.disabled = true; status.textContent = '正在加载用量…'; results.replaceChildren();
    try {
      const result = await client.getUsageSummary(params);
      if (disposed || current !== generation) return;
      renderResults(results, result);
      status.textContent = '已加载 ' + result.items.length + ' 组用量；时间采用 UTC。';
    } catch (error) {
      if (disposed || current !== generation) return;
      status.textContent = error.status === 404 || error.status === 503 ? '用量看板暂时不可用。' : (error.detail || '用量加载失败，请重试。');
    } finally {
      if (!disposed && current === generation) submit.disabled = false;
    }
  }
  form.addEventListener('submit', load);
  load();
  return () => { disposed = true; generation++; form.removeEventListener('submit', load); };
}
