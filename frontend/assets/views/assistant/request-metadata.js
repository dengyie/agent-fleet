import { uiIcon } from '../../routes.js';

const TOKENS = [
  ['input_tokens', '输入 Token（包含缓存）', 'arrow-up'],
  ['cache_read_tokens', '缓存读取 Token（输入的子集）', 'database-search'],
  ['reasoning_tokens', '推理 Token（输出的子集）', 'brain'],
  ['output_tokens', '输出 Token（包含推理）', 'arrow-down'],
  ['cache_write_tokens', '缓存写入 Token（输入的子集）', 'database-plus'],
];
const STATES = {running: '请求中', succeeded: '已完成', failed: '请求失败', unknown: '结果未知'};
const number = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? String(value) : '—';
const node = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };
const money = cost => cost && /^(\d+)(\.\d+)?$/.test(cost.amount) && cost.currency === 'USD' ? '$' + cost.amount : '费用未提供';

function chip(label, value, icon) {
  const n = node('span', 'request-chip');
  if (icon) n.appendChild(uiIcon(icon, {size: 12}));
  n.appendChild(node('span', 'request-chip-value', value));
  n.title = label + '：' + value;
  n.setAttribute('aria-label', n.title);
  return n;
}
function detail(host, label, value) {
  host.appendChild(node('dt', null, label)); host.appendChild(node('dd', null, value));
}
function createRequest() {
  const root = node('details', 'request-metadata');
  const summary = node('summary', 'request-summary');
  const body = node('div', 'request-detail');
  root.appendChild(summary); root.appendChild(body);
  root._parts = {summary, body};
  return root;
}
function updateRequest(root, request) {
  // Keep details/summary identity and the user's expanded state across polls.
  const {elapsed_ms, ...stable} = request;
  root._request = request;
  root._elapsed = typeof elapsed_ms === 'number' ? elapsed_ms : 0;
  root._received = performance.now();
  // SSE and polling can serialize the same DTO with different object key order.
  // Compare values canonically so unchanged snapshots retain their child nodes.
  const signature = JSON.stringify(stable, (_, value) =>
    value && typeof value === 'object' && !Array.isArray(value)
      ? Object.fromEntries(Object.keys(value).sort().map(key => [key, value[key]]))
      : value);
  if (root._signature === signature) return;
  root._signature = signature;
  root.setAttribute('data-request-id', request.request_id);
  root.setAttribute('data-state', request.status);
  const {summary, body} = root._parts;
  while (summary.firstChild) summary.removeChild(summary.firstChild);
  while (body.firstChild) body.removeChild(body.firstChild);
  summary.setAttribute('aria-label', '请求参数与元数据 · 步骤 ' + request.step + ' · 请求 ' + request.request_index);
  summary.appendChild(chip('模型', request.model || request.requested_model || '未提供', 'cpu'));
  const usage = request.usage || {};
  const tokens = node('span', 'request-tokens');
  TOKENS.forEach(([key, label, icon]) => {
    let value = usage[key];
    if (key === 'input_tokens' && [value, usage.cache_read_tokens, usage.cache_write_tokens].every(n => typeof n === 'number')) {
      value = Math.max(0, value - usage.cache_read_tokens - usage.cache_write_tokens); label = '未缓存输入 Token';
    }
    if (key === 'output_tokens' && typeof value === 'number' && typeof usage.reasoning_tokens === 'number') {
      value = Math.max(0, value - usage.reasoning_tokens); label = '可见输出 Token（不含推理）';
    }
    tokens.appendChild(chip(label, number(value), icon));
  });
  summary.appendChild(tokens);
  const duration = chip('请求耗时', request.duration_ms == null ? (request.status === 'running' ? '请求中' : '耗时未确认') : number(request.duration_ms) + 'ms');
  duration.className += ' request-duration'; summary.appendChild(duration); root._duration = duration.firstChild;
  summary.appendChild(chip(request.cost?.source === 'configured' ? '按配置单价估算（点击查看明细）' : '上游报告费用（点击查看明细）',
    (request.cost?.source === 'configured' ? '≈' : '') + money(request.cost)));
  const date = new Date(request.started_at * 1000);
  const validDate = Number.isFinite(date.getTime());
  const stamp = chip('请求开始时间', validDate ? date.toLocaleTimeString([], {hour12: false}) : '时间未知');
  stamp.title = validDate ? date.toLocaleString() + ' · ' + date.toISOString() : '时间未知'; summary.appendChild(stamp);
  const state = node('span', 'request-state', STATES[request.status] || '结果未知'); summary.appendChild(state);
  const chevron = uiIcon('chevron-down', {size: 13}); summary.appendChild(chevron);
  const list = node('dl', 'request-fields');
  detail(list, '请求编号', request.request_id);
  detail(list, '运行尝试 / 步骤 / HTTP 请求', [request.attempt, request.step, request.request_index].join(' / '));
  detail(list, '请求模型', request.requested_model || '未提供');
  detail(list, '响应模型', request.response_model || '上游未提供，摘要使用请求模型');
  detail(list, '提供方', request.provider || '未提供');
  TOKENS.forEach(([key, label]) => detail(list, label, number(usage[key])));
  detail(list, '总 Token', number(usage.total_tokens));
  detail(list, '耗时', request.duration_ms == null ? '等待请求结束后确认' : number(request.duration_ms) + 'ms');
  detail(list, '开始时间', validDate ? date.toLocaleString() + ' · ' + date.toISOString() : '未提供');
  detail(list, 'HTTP 状态', request.http_status == null ? '未收到' : String(request.http_status));
  detail(list, '响应编号', request.response_id || '未提供');
  detail(list, '结束原因', request.finish_reason || request.provider_error || '未提供');
  detail(list, '流式请求', request.parameters?.stream ? '是' : '否');
  if (request.parameters?.timeout_s) detail(list, '网络超时', request.parameters.timeout_s + 's');
  body.appendChild(list);
  body.appendChild(node('p', 'request-explanation', '— 表示上游未提供。缓存属于输入，推理属于输出，总量不重复相加。耗时包含模型响应及读取解析，不包含排队和工具执行。'));
  const cost = request.cost;
  body.appendChild(node('strong', 'request-cost-total', money(cost)));
  body.appendChild(node('p', 'request-explanation', !cost ? (request.cost_unavailable_reason ? '当前请求包含独立计价的缓存时长或媒体类型，现有单价不能准确估算费用。' : '上游未报告费用，或缺少配置单价/完整 Token 明细，无法计算费用。') : cost.source === 'provider' ? '费用来自上游响应，最终账单以上游为准。' : '费用按请求时冻结的 USD / 百万 Token 单价估算，包含推理输出；最终账单以上游为准。'));
  const names = {input: '未缓存输入', output: '输出（包含推理）', cache_read: '缓存读取', cache_write: '缓存写入'};
  (cost?.items || []).forEach(item => body.appendChild(node('p', 'request-cost-line',
    (item.includes_unspecified_cache ? '输入（含同价未拆分缓存）' : names[item.category] || item.category) + '：' + item.tokens + ' × $' + item.rate_per_million + ' / 1M = $' + item.amount)));
}

// Sum each displayed category independently: cache and reasoning are subsets,
// and missing provider accounting must never silently become zero.
function displayedTokens(request, key) {
  const usage = request.usage || {};
  let value = usage[key];
  if (key === 'input_tokens' && [value, usage.cache_read_tokens, usage.cache_write_tokens].every(n => typeof n === 'number'))
    value = Math.max(0, value - usage.cache_read_tokens - usage.cache_write_tokens);
  if (key === 'output_tokens' && typeof value === 'number' && typeof usage.reasoning_tokens === 'number')
    value = Math.max(0, value - usage.reasoning_tokens);
  return value;
}
function sumKnown(values, suffix = '') {
  const known = values.filter(value => typeof value === 'number' && Number.isFinite(value) && value >= 0);
  if (!known.length) return '—';
  return (known.length === values.length ? '' : '≥') + known.reduce((total, value) => total + value, 0) + suffix;
}
function sumCost(requests) {
  const amounts = requests.map(r => r.cost).filter(c => c?.currency === 'USD' && /^(\d+)(\.\d+)?$/.test(c.amount));
  if (!amounts.length) return '费用未提供';
  const scale = Math.max(...amounts.map(c => (c.amount.split('.')[1] || '').length));
  const total = amounts.reduce((sum, c) => {
    const [whole, fraction = ''] = c.amount.split('.');
    return sum + BigInt(whole + fraction.padEnd(scale, '0'));
  }, 0n).toString().padStart(scale + 1, '0');
  const exact = scale ? total.slice(0, -scale) + '.' + total.slice(-scale) : total;
  return (amounts.length < requests.length ? '≥' : '') + (amounts.some(c => c.source === 'configured') ? '≈' : '') + '$' + exact;
}
function aggregateValue(value, known, count, suffix = '') {
  return !known ? '—' : (known < count ? '≥' : '') + number(value) + suffix;
}
function updateGroup(group, requests, totals, omitted) {
  const signature = JSON.stringify({requests,totals,omitted}, (key, value) => key === 'elapsed_ms' ? undefined :
    value && typeof value === 'object' && !Array.isArray(value)
      ? Object.fromEntries(Object.keys(value).sort().map(k => [k, value[k]])) : value);
  if (signature === group._signature) return;
  group._signature = signature;
  const summary = group.querySelector(':scope > summary');
  summary.replaceChildren();
  const models = totals?.models || [...new Set(requests.map(r => r.model || r.requested_model || '未提供'))];
  const modelCount = totals?.model_count ?? models.length;
  const count = totals?.count ?? requests.length;
  summary.append(chip('模型', modelCount === 1 ? models[0] : modelCount + ' 个模型', 'cpu'));
  summary.append(chip('本轮模型请求次数', count + ' 次请求'));
  const tokens = node('span', 'request-tokens');
  TOKENS.forEach(([key, label, icon]) => tokens.append(chip((key === 'input_tokens' ? '输入 Token（已拆分缓存时扣除缓存）' : key === 'output_tokens' ? '输出 Token（已拆分推理时扣除推理）' : label) + '合计（≥ 表示有请求未提供）',
    totals ? aggregateValue(totals.display_usage[key], totals.known[key], count) : sumKnown(requests.map(r => displayedTokens(r, key))), icon)));
  summary.append(tokens);
  const duration = chip('模型请求耗时合计，不含工具执行；≥ 表示有耗时未确认', totals ? aggregateValue(totals.duration_ms, totals.duration_known, count, 'ms') : sumKnown(requests.map(r => r.duration_ms), 'ms'));
  duration.className += ' request-group-duration'; summary.append(duration);
  group._duration = duration.querySelector('.request-chip-value');
  const cost = totals ? (!totals.cost_known ? '费用未提供' :
    (totals.cost_known < count ? '≥' : '') + (totals.cost?.source === 'configured' ? '≈' : '') + money(totals.cost)) : sumCost(requests);
  summary.append(chip('费用合计；≥ 表示部分请求费用未提供；≈ 表示包含估算', cost));
  const running = totals ? totals.states.running || 0 : requests.filter(r => r.status === 'running').length;
  const failed = totals ? (totals.states.failed || 0) + (totals.states.unknown || 0) : requests.filter(r => ['failed', 'unknown'].includes(r.status)).length;
  const status = running ? '请求中' : failed ? failed + ' 次失败或未确认' : totals?.complete === false ? '部分统计' : '已完成';
  summary.append(node('span', 'request-state', status), uiIcon('chevron-down', {size:13}));
  group.dataset.state = running ? 'running' : failed ? 'unknown' : 'succeeded';
  summary.setAttribute('aria-label', '本轮模型请求汇总 · ' + count + ' 次请求，展开查看逐次详情');
  let notice = group.querySelector('.request-retention-notice');
  if (omitted) {
    if (!notice) { notice = node('p', 'request-retention-notice request-explanation'); group.insertBefore(notice, group.querySelector('.request-group-items')); }
    notice.textContent = '已省略 ' + omitted + ' 次较早的请求明细，当前保留 ' + requests.length + ' 次。' +
      (totals?.complete === false ? '历史完整统计无法恢复，≥ 表示已知部分。' : '汇总包含全部请求。');
  } else notice?.remove();
  group._totals = totals;
}
export function renderRequestMetadata(host, requests, totals = null, omitted = 0) {
  requests = requests || [];
  if (!host._requestRows) host._requestRows = new Map();
  if (!requests.length && !totals?.count) {
    host.replaceChildren(); host._requestRows.clear(); host._requestGroup = null; return;
  }
  if (!host._requestGroup) {
    const group = node('details', 'request-group');
    group.append(node('summary', 'request-summary'), node('div', 'request-group-items'));
    host.append(group); host._requestGroup = group;
  }
  const group = host._requestGroup;
  updateGroup(group, requests, totals, omitted);
  const container = group.querySelector('.request-group-items');
  const keep = new Set();
  requests.forEach((request, index) => {
    const id = request.request_id; keep.add(id);
    let row = host._requestRows.get(id);
    if (!row) { row = createRequest(); host._requestRows.set(id, row); }
    updateRequest(row, request);
    if (container.children[index] !== row) container.insertBefore(row, container.children[index] || null);
  });
  host._requestRows.forEach((row, id) => { if (!keep.has(id)) { row.remove(); host._requestRows.delete(id); } });
}
export function tickRequestMetadata(host) {
  let active = false;
  const durations = [];
  const liveElapsed = [];
  host._requestRows?.forEach(row => {
    if (row._request.status !== 'running') { durations.push(row._request.duration_ms); return; }
    active = true;
    const elapsed = Math.max(0, Math.round(row._elapsed + performance.now() - row._received));
    durations.push(elapsed);
    liveElapsed.push(elapsed);
    const text = '≈' + elapsed + 'ms';
    if (row._duration.textContent !== text) row._duration.textContent = text;
  });
  if (active && host._requestGroup?._duration) {
    const totals = host._requestGroup._totals;
    const text = '≈' + (totals ? aggregateValue(totals.duration_ms + liveElapsed.reduce((sum, value) => sum + value, 0),
      totals.duration_known + liveElapsed.length, totals.count, 'ms') : sumKnown(durations, 'ms'));
    if (host._requestGroup._duration.textContent !== text) host._requestGroup._duration.textContent = text;
  }
  return active;
}
