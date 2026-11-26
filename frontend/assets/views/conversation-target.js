import {pagePath, uiIcon} from '../routes.js';
import {nativeChatPath, nativeDirectoryPath} from './agent-chat/paths.js';
import {el, button, link, errorText} from './agent-chat/dom.js';
import {agentName} from './agent-chat/state.js';
import {mountProviderAccountPicker} from './agent-chat/account-picker.js';

export function conversationTargets(active = 'assistant') {
  const nav = el('nav', 'conversation-targets'); nav.setAttribute('aria-label', '对话来源');
  for (const [id, title, icon, href] of [
    ['assistant', '主助手', 'sparkles', pagePath('assistant')],
    ['computer', '电脑 Agent', 'monitor', nativeChatPath()],
    ['sessions', '已有会话', 'layers', nativeDirectoryPath()],
  ]) {
    const item = link('', href, 'conversation-target');
    item.append(uiIcon(icon, {size: 15}), el('span', null, title));
    if (active === id) item.setAttribute('aria-current', 'page');
    nav.append(item);
  }
  return nav;
}

// Selection only. Dispatch policy, native paths and CLI syntax stay on the Hub.
export function mountTargetPicker(host, {api, store, machine: initialMachine, onChange}) {
  let disposed = false, generation = 0, controller;
  let targets = null, locked = false, loadingMachines = false, machines = [];
  const fields = el('div', 'native-target-fields');
  function field(name) {
    const label = el('label', 'native-target-field');
    const select = el('select'); select.setAttribute('aria-label', name);
    label.append(el('span', null, name), select); fields.append(label); return select;
  }
  const machine = field('电脑节点'), project = field('项目'), agent = field('Agent');
  const notice = el('p', 'native-target-notice meta'); notice.setAttribute('role', 'status');
  const retry = button('刷新节点与目录', () => loadMachines(true));
  const shortcuts = el('div', 'native-target-shortcuts');
  const sessions = link('查看此节点会话', '#'), manage = link('管理此节点', '#');
  shortcuts.append(sessions, manage); shortcuts.hidden = true;
  const accountsHost = el('div', 'native-account-picker');
  let accountChoice = {ready: true};
  const accounts = mountProviderAccountPicker(accountsHost, {api, onChange(choice) { accountChoice = choice; changed(false); }});
  host.append(fields, accountsHost, notice, retry, shortcuts);
  function option(select, value, text, disabled = false) {
    const row = el('option', null, text); row.value = value; row.disabled = disabled; select.append(row);
  }
  function changed(updateAccounts = true) {
    if (updateAccounts) accounts.update(machine.value, agent.value);
    shortcuts.hidden = !machine.value;
    if (machine.value) {
      sessions.href = nativeDirectoryPath(machine.value);
      manage.href = pagePath('machine', machine.value);
    }
    const selected = targets?.agents[agent.value];
    const ready = !!targets && machines.some(row => row.machine === machine.value && row.online) && targets.machine === machine.value && !!project.value &&
      targets.projects.includes(project.value) && !!selected && selected.installed !== false;
    onChange({ready: ready && accountChoice.ready, machine: machine.value, project: project.value,
      ...(agent.value === 'agy' && accountChoice.provider_account_id ? {provider_account_id: accountChoice.provider_account_id} : {}),
      agent_type: agent.value, installed: selected?.installed, observed_at: targets?.observed_at});
  }
  function locks(notify = true) {
    machine.disabled = locked || loadingMachines || !machines.length;
    project.disabled = locked || !targets?.projects.length;
    agent.disabled = locked || !targets || !Object.keys(targets.agents).length;
    retry.disabled = locked || loadingMachines; if (notify) changed();
  }
  async function loadTargets() {
    const request = ++generation;
    controller?.abort(); controller = new AbortController(); targets = null;
    project.replaceChildren(); agent.replaceChildren(); locks();
    if (!machine.value) { notice.textContent = '暂无在线节点，请连接电脑后刷新节点与目录。'; return; }
    if (!machines.some(row => row.machine === machine.value && row.online)) {
      notice.textContent = '所选节点当前离线。仍可阅读已同步会话；恢复连接后刷新，或手动选择其它节点。'; return;
    }
    notice.textContent = '正在读取登记项目和 Agent 安装状态…';
    try {
      const result = await api.getTaskTargets(machine.value, {signal: controller.signal});
      if (disposed || request !== generation) return;
      if (result.machine !== machine.value) throw new Error('节点目录与所选电脑不一致');
      targets = result;
      for (const name of result.projects) option(project, name, name);
      for (const [family, state] of Object.entries(result.agents)) {
        option(agent, family, agentName(family) + (state.installed === false ? ' · 未安装' : state.installed === null ? ' · 安装状态待确认' : ''), state.installed === false);
      }
      agent.value = Object.keys(result.agents).find(name => result.agents[name].installed === true) ||
        Object.keys(result.agents).find(name => result.agents[name].installed !== false) || '';
      notice.textContent = !result.projects.length ? '此节点尚未登记可启动的项目，请在节点配置中登记。' :
        '使用电脑原有模型配置。安装状态来自最近上报，启动结果由节点确认。';
    } catch (error) {
      if (disposed || request !== generation || controller.signal.aborted) return;
      notice.textContent = error.status === 404 ? '当前服务尚未提供节点启动目录。已有会话仍可打开和阅读。' : errorText(error);
    } finally { if (!disposed && request === generation) locks(); }
  }
  async function loadMachines(fresh = false) {
    const request = ++generation, selected = machine.value || initialMachine;
    controller?.abort(); controller = new AbortController(); loadingMachines = true; targets = null;
    project.replaceChildren(); agent.replaceChildren(); locks();
    notice.textContent = '正在获取电脑节点…';
    try {
      const status = (!fresh && store?.getState().status) || await api.getStatus({signal: controller.signal});
      if (disposed || request !== generation) return;
      const rows = status.machines || [];
      machines = rows;
      machine.replaceChildren();
      if (selected && !rows.some(row => row.machine === selected)) option(machine, '', '所选节点不可用，请选择其它节点');
      for (const row of rows) option(machine, row.machine,
        [row.machine, row.system?.platform, row.online ? '在线' : '离线'].filter(Boolean).join(' · '), !row.online);
      machine.value = selected ? rows.find(row => row.machine === selected)?.machine || '' : rows.find(row => row.online)?.machine || '';
      loadingMachines = false;
      await loadTargets();
    } catch (error) {
      if (disposed || request !== generation || controller.signal.aborted) return;
      notice.textContent = errorText(error);
    } finally {
      if (!disposed && request === generation) { loadingMachines = false; locks(); }
    }
  }
  machine.addEventListener('change', loadTargets);
  project.addEventListener('change', () => changed()); agent.addEventListener('change', () => changed());
  loadMachines();
  return {setLocked(value) { locked = value; locks(false); accounts.setLocked(value); },
    suspend() { accounts.suspend(); }, resume() { accounts.resume(); },
    dispose() { disposed = true; generation++; controller?.abort(); accounts.dispose(); }};
}
