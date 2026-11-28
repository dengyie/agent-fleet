import {uiIcon, pagePath} from '../routes.js';
import {conversationTargets} from './conversation-target.js';
import {nativeChatPath} from './agent-chat/paths.js';
import {el, button, link, errorText} from './agent-chat/dom.js';
import {sessionTitle, agentName, AGENT_LABELS} from './agent-chat/state.js';
import {sessionControls} from '../ui/session-capabilities.js';

export function mountSessions(target, {api, store, machine: initialMachine, identitySignal}) {
  let disposed = false, suspended = false, generation = 0, controller, rows = [];
  let cursor = null, hasMore = false, busy = false;
  const root = el('div', 'native-directory'); target.replaceChildren(root);
  root.append(conversationTargets('sessions'));
  const heading = el('header', 'native-directory-heading'), copy = el('div');
  copy.append(el('h1', null, '电脑上的 Agent 会话'), el('p', 'meta', '从已同步的会话中查看记录，按当前权限继续工作。'));
  heading.append(copy, link('新建电脑对话', nativeChatPath(), 'button-primary')); root.append(heading);
  const filters = el('div', 'native-directory-filters');
  const machine = el('select'), family = el('select'), capability = el('select'), search = el('input');
  machine.setAttribute('aria-label', '筛选电脑'); family.setAttribute('aria-label', '筛选 Agent');
  search.type = 'search'; search.placeholder = '筛选会话名、节点、Agent 或模型'; search.setAttribute('aria-label', '筛选已载入会话');
  function option(select, value, text) { const row = el('option', null, text); row.value = value; select.append(row); }
  option(machine, '', '全部电脑'); option(family, '', '全部 Agent');
  capability.setAttribute('aria-label', '筛选会话能力');
  for (const [value, title] of [['', '全部会话'], ['message', '可继续对话'], ['control', '可控制'], ['read', '仅可阅读']]) option(capability, value, title);
  const refresh = button('刷新', load); refresh.prepend(uiIcon('refresh', {size: 14}));
  const node = link('节点与活动实例', '#'); node.hidden = true;
  filters.append(machine, family, capability, search, refresh, link('搜索消息正文', pagePath('session-search')), node); root.append(filters);
  const status = el('p', 'native-directory-status meta'); status.setAttribute('role', 'status');
  const list = el('div', 'native-session-list'); list.setAttribute('aria-label', '电脑会话列表'); root.append(status, list);
  const more = button('加载更多会话', () => load(true)); more.hidden = true; root.append(more);
  function paint() {
    if (disposed) return;
    more.hidden = !hasMore; more.disabled = busy;
    more.textContent = busy ? '正在加载…' : '加载更多会话';
    list.replaceChildren();
    const query = search.value.trim().toLocaleLowerCase();
    const features = store.getState().status?.features;
    const visible = rows.filter(row => {
      const controls = sessionControls(row, features);
      return (!capability.value || (capability.value === 'message' ? controls.appendUserTurn : capability.value === 'control' ? controls.canControl : !controls.canControl)) &&
      (!family.value || row.agent_family === family.value) &&
      [row.machine_id, row.agent_family, row.session_id, row.native_title,
        row.title_preview, row.provider, row.model].join(' ').toLocaleLowerCase().includes(query);
    });
    for (const row of visible) {
      const card = link('', nativeChatPath({sessionId: row.session_id}), 'native-session-card');
      const glyph = el('span', 'native-session-glyph'); glyph.append(uiIcon('code', {size: 18}));
      const text = el('div', 'native-session-copy');
      text.append(el('strong', null, sessionTitle(row)), el('span', 'meta',
        (row.native_title ? '原生会话名' : row.title_preview ? '首条用户消息' : '未提供会话名') + ' · ' + row.session_id));
      const client = [agentName(row.agent_family), row.adapter, row.client_version].filter(Boolean).join(' · ');
      const model = [row.provider, row.model].filter(Boolean).join(' / ');
      if (client || model) text.append(el('span', 'meta', [client, model].filter(Boolean).join(' · ')));
      const time = row.updated_at || row.started_at;
      if (time && Number.isFinite(Date.parse(time))) text.append(el('time', 'meta', '最近同步 ' + new Date(time).toLocaleString()));
      const state = el('div', 'native-session-state');
      state.append(el('span', 'native-session-state-label', AGENT_LABELS[row.current_state] || '历史记录'));
      const canSend = sessionControls(row, store.getState().status?.features).appendUserTurn;
      const controls = sessionControls(row, features);
      state.append(el('span', 'meta', canSend ? '继续对话' : controls.canControl ? '阅读与控制' : '查看记录'), uiIcon('chevron-right', {size: 15}));
      card.append(glyph, text, state); list.append(card);
    }
    if (!visible.length) {
      const empty = el('section', 'native-directory-empty'); empty.append(uiIcon('layers', {size: 28}),
        el('h2', null, rows.length ? '没有匹配的已载入会话' : '尚未同步到可读会话'),
        el('p', 'meta', rows.length ? '试试其它筛选条件，或搜索消息正文。' : '连接电脑并开启会话采集后，记录会出现在这里。'));
      if (machine.value) empty.append(link('查看节点连接与活动实例', pagePath('machine', machine.value)));
      list.append(empty);
    }
  }
  async function load(append = false) {
    if (disposed || suspended) return;
    append = append === true;
    if (append && (busy || !hasMore)) return;
    const request = ++generation; controller?.abort(); controller = new AbortController();
    const requestedCursor = append ? cursor : null;
    const requestedCapability = capability.value;
    busy = true; more.disabled = true;
    refresh.disabled = true; list.setAttribute('aria-busy', 'true'); status.textContent = '正在读取已同步会话…';
    node.hidden = !machine.value; if (machine.value) node.href = pagePath('machine', machine.value);
    if (!rows.length) list.replaceChildren(...Array.from({length: 4}, () => el('div', 'native-session-skeleton')));
    try {
      let result, next = requestedCursor, found = [];
      // A bounded backend page may contain no matching authority. Continue a
      // finite number of pages so archive backfill cannot hide old controls.
      // Any remaining history stays available through the load-more action.
      for (let pass = 0; pass < (requestedCapability ? 5 : 1); pass++) {
        result = await api.listSessions({machine: machine.value, limit: requestedCapability ? 1000 : 100,
          cursor: next, ...(requestedCapability ? {capability: requestedCapability} : {})}, {signal: controller.signal});
        if (disposed || suspended || request !== generation) return;
        if (result.has_more && result.next_cursor === next) throw new Error('会话目录游标未推进，请刷新重试');
        found.push(...result.sessions);
        next = result.next_cursor;
        if (found.length || !result.has_more) break;
      }
      rows = [...new Map([...(append ? rows : []), ...found].map(row => [row.session_id, row])).values()];
      cursor = result.next_cursor; hasMore = result.has_more;
      const selected = family.value; family.replaceChildren(); option(family, '', '全部 Agent');
      for (const value of [...new Set(rows.map(row => row.agent_family).filter(Boolean))].sort()) option(family, value, value);
      family.value = [...family.options].some(row => row.value === selected) ? selected : '';
      status.textContent = '已载入 ' + rows.length + ' 条会话' + (hasMore ? '，可继续加载。' : '，已到目录末尾。') + '能力筛选会查询后续目录；Agent 和文本筛选仅作用于已载入记录。刷新可查看新同步会话。';
      paint();
    } catch (error) {
      if (disposed || suspended || request !== generation) return;
      status.textContent = '读取失败：' + errorText(error); if (!rows.length) list.replaceChildren();
    } finally {
      if (!disposed && request === generation) { busy = false; refresh.disabled = false; list.setAttribute('aria-busy', 'false'); paint(); }
    }
  }
  async function initialize() {
    try {
      const result = store.getState().status || await api.getStatus();
      if (disposed) return;
      for (const row of result.machines) option(machine, row.machine, [row.machine, row.system?.platform].filter(Boolean).join(' · '));
      if ([...machine.options].some(row => row.value === initialMachine)) machine.value = initialMachine;
    } catch (_) { /* Directory is independently available when telemetry fails. */ }
    load();
  }
  function resetDirectory() { rows = []; cursor = null; hasMore = false; more.hidden = true; load(); }
  machine.addEventListener('change', resetDirectory); family.addEventListener('change', paint); capability.addEventListener('change', resetDirectory); search.addEventListener('input', paint);
  const focus = () => load(); window.addEventListener('focus', focus);
  const dispose = () => { if (disposed) return; disposed = true; generation++; controller?.abort(); window.removeEventListener('focus', focus); identitySignal?.removeEventListener('abort', dispose); };
  dispose.suspend = () => { suspended = true; generation++; controller?.abort(); };
  dispose.resume = () => { suspended = false; load(); };
  identitySignal?.addEventListener('abort', dispose, {once: true}); initialize(); return dispose;
}
