import {pagePath, uiIcon} from '../routes.js';
import {nativeChatPath, nativeDirectoryPath} from './agent-chat/paths.js';
import {el, button, link, errorText} from './agent-chat/dom.js';

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
  let targets = null, locked = false, loadingMachines = false;
  const fields = el('div', 'native-target-fields');
  function field(name) {
    const label = el('label', 'native-target-field');
    const select = el('select'); select.setAttribute('aria-label', name);
    label.append(el('span', null, name), select); fields.append(label); return select;
  }
  const machine = field('电脑节点'), project = field('项目'), agent = field('Agent');
  const notice = el('p', 'native-target-notice meta'); notice.setAttribute('role', 'status');
  const retry = button('刷新节点与目录', () => loadMachines(true));
  host.append(fields, notice, retry);
  function option(select, value, text, disabled = false) {
    const row = el('option', null, text); row.value = value; row.disabled = disabled; select.append(row);
  }
  function changed() {
    const selected = targets?.agents[agent.value];
    const ready = !!targets && targets.machine === machine.value && !!project.value &&
      targets.projects.includes(project.value) && !!selected && selected.installed !== false;
    onChange({ready, machine: machine.value, project: project.value,
      agent_type: agent.value, installed: selected?.installed, observed_at: targets?.observed_at});
  }
  function locks(notify = true) {
    machine.disabled = locked || loadingMachines || !machine.value;
    project.disabled = locked || !targets?.projects.length;
    agent.disabled = locked || !targets || !Object.keys(targets.agents).length;
    retry.disabled = locked || loadingMachines; if (notify) changed();
  }
  async function loadTargets() {
    const request = ++generation;
    controller?.abort(); controller = new AbortController(); targets = null;
    project.replaceChildren(); agent.replaceChildren(); locks();
    if (!machine.value) { notice.textContent = '暂无在线节点，请连接电脑后刷新节点与目录。'; return; }
    notice.textContent = '正在读取登记项目和 Agent 安装状态…';
    try {
      const result = await api.getTaskTargets(machine.value, {signal: controller.signal});
      if (disposed || request !== generation) return;
      if (result.machine !== machine.value) throw new Error('节点目录与所选电脑不一致');
      targets = result;
      for (const name of result.projects) option(project, name, name);
      for (const [family, state] of Object.entries(result.agents)) {
        option(agent, family, family + (state.installed === false ? ' · 未安装' : state.installed === null ? ' · 安装状态待确认' : ''), state.installed === false);
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
      machine.replaceChildren();
      for (const row of rows) option(machine, row.machine,
        [row.machine, row.system?.platform, row.online ? '在线' : '离线'].filter(Boolean).join(' · '), !row.online);
      machine.value = rows.find(row => row.online && row.machine === selected)?.machine || rows.find(row => row.online)?.machine || '';
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
  project.addEventListener('change', changed); agent.addEventListener('change', changed);
  loadMachines();
  return {setLocked(value) { locked = value; locks(false); }, dispose() { disposed = true; generation++; controller?.abort(); }};
}
