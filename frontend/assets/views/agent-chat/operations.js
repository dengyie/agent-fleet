import {pagePath, uiIcon} from '../../routes.js';
import {sessionControls} from '../../ui/session-capabilities.js';
import {sessionAccessLabel} from '../../ui/session-labels.js';
import {el, button, link, errorText} from './dom.js';
import {operationStatus} from './state.js';

export const CONTROL_LABELS = Object.freeze({pause_session: '暂停会话', resume_session: '恢复会话',
  terminate_session: '终止会话', quarantine_session: '隔离会话', cancel_attempt: '取消任务',
  apply_local_profile: '应用本机配置'});

// The Hub declares actions; machine observations only supply registered choices.
// Keep the panel mounted so polling cannot replace drafts or the focused select.
export function mountNativeOperations(host, {api, onControl}) {
  let current = {}, disposed = false, suspended = false, active = false;
  let profiles = [], loadedKey = '', requestedKey = '', generation = 0, controller, loading = false;
  let profileError = '';
  const heading = el('h2', 'assistant-section-title', '会话操作');
  const access = el('p', 'native-access meta'), availability = el('p', 'meta');
  const feedback = el('p', 'native-operation-feedback meta'); feedback.setAttribute('role', 'status');
  availability.setAttribute('role', 'status');
  const links = el('div', 'native-operation-links');
  const nodeLink = link('管理此节点', '#'), timelineLink = link('原始记录与授权', '#');
  links.append(nodeLink, timelineLink);
  const delivery = el('section', 'native-operation-section');
  delivery.append(el('h3', null, '消息与上下文注入'), el('p', 'meta', '在对话输入框发送指令或补充上下文，消息会投递到当前原生会话。投递回执与 Agent 回复分别显示。'));
  const profileSection = el('section', 'native-operation-section');
  const label = el('label', 'native-profile-field', '本机模型配置');
  const select = el('select'); select.setAttribute('aria-label', '本机模型配置'); label.append(select);
  const profileStatus = el('p', 'meta'); profileStatus.setAttribute('role', 'status');
  const refresh = button('刷新本机配置', () => loadProfiles(true));
  refresh.prepend(uiIcon('refresh', {size: 14}));
  const apply = button('应用本机配置', () => {
    const controls = sessionControls(current.session, current.features);
    const selected = profiles.find(row => row.profile_id === select.value);
    if (!current.authorityVerified || current.busy || !controls.applyLocalProfile || !selected || loading || suspended) return;
    onControl('apply_local_profile', {profile_id: selected.profile_id}, '应用本机配置 · ' + selected.label);
  });
  const profileActions = el('div', 'native-operation-buttons'); profileActions.append(apply, refresh);
  profileSection.append(el('h3', null, '本机 Provider / 模型配置'), el('p', 'meta', '使用节点已登记的配置。网页只传递配置标识，切换结果以节点回执为准。'), label, profileStatus, profileActions);
  const lifecycle = el('section', 'native-operation-section'); lifecycle.append(el('h3', null, '运行控制'));
  const controlsHost = el('div', 'native-operation-buttons'); lifecycle.append(controlsHost);
  const confirmation = el('dialog', 'conversation-dialog'); confirmation.setAttribute('aria-label', '确认会话控制');
  const confirmationText = el('p'); let confirming = '';
  const confirm = button('确认执行', () => {
    const action = confirming; confirmation.close(); confirming = '';
    if (current.authorityVerified && !current.busy && sessionControls(current.session, current.features).actions.includes(action)) onControl(action);
  }, 'button-danger');
  confirmation.append(el('h2', null, '确认会话控制'), confirmationText,
    button('取消', () => { confirming = ''; confirmation.close(); }), confirm);
  host.append(heading, access, availability, feedback, links, delivery, profileSection, lifecycle, confirmation);
  const actionButtons = new Map();
  for (const [action, title] of Object.entries(CONTROL_LABELS)) {
    if (action === 'apply_local_profile') continue;
    const control = button(title, () => {
      if (!current.authorityVerified || current.busy || !sessionControls(current.session, current.features).actions.includes(action)) return;
      if (['terminate_session', 'quarantine_session', 'cancel_attempt'].includes(action)) {
        confirming = action;
        confirmationText.textContent = '对 ' + current.session.machine_id + ' 的当前会话执行“' + title + '”。' +
          ({terminate_session: '这会终止会话进程，可能无法继续。', quarantine_session: '这会隔离会话并影响后续运行。', cancel_attempt: '这会取消当前任务。'}[action]);
        confirmation.showModal();
      } else onControl(action);
    });
    actionButtons.set(action, control); controlsHost.append(control);
  }

  function key() { return JSON.stringify([current.session?.machine_id, current.session?.agent_family]); }
  function paint() {
    if (disposed) return;
    const {session, features, authorityVerified, busy} = current;
    const controls = sessionControls(session, features);
    const latest = current.operations?.at(-1);
    feedback.hidden = !latest;
    feedback.textContent = latest ? (latest.description || CONTROL_LABELS[latest.action] || '消息投递') + ' · ' +
      operationStatus(latest) + (latest.error ? '：' + latest.error : '') : '';
    access.textContent = session ? sessionAccessLabel(session) : '绑定原生会话后可查看操作';
    availability.textContent = !session ? '启动任务与原生会话绑定后，这里会显示节点实际支持的操作。' :
      !authorityVerified ? '权限状态暂不可确认，请重新连接后再操作。' :
      controls.appendUserTurn ? '可以向当前会话发送消息与上下文。' : controls.deliveryLabel || '当前会话仅可阅读；请在节点页检查纳管状态与可用能力。';
    links.hidden = !session;
    if (session) {
      nodeLink.href = pagePath('machine', session.machine_id);
      timelineLink.href = pagePath('session', session.session_id);
    }
    for (const [action, node] of actionButtons) {
      if (!controls.actions.includes(action)) node.remove();
      else if (!node.isConnected) controlsHost.append(node);
      node.disabled = !authorityVerified || busy;
    }
    lifecycle.hidden = ![...actionButtons.keys()].some(action => controls.actions.includes(action));
    if (confirmation.open && (!authorityVerified || busy || !controls.actions.includes(confirming))) confirmation.close();
    profileSection.hidden = !controls.applyLocalProfile;
    const ready = loadedKey === key();
    select.disabled = !authorityVerified || busy || loading || !ready || !profiles.length;
    apply.disabled = select.disabled || !select.value;
    refresh.disabled = !authorityVerified || busy || loading;
    profileStatus.textContent = profileError || (loading ? '正在读取节点登记配置…' : !ready ? '打开操作面板后读取节点配置。' :
      profiles.length ? '已读取 ' + profiles.length + ' 个匹配配置。' : '此节点尚未上报当前 Agent 的本机配置。');
    profileStatus.className = profileError ? 'err' : 'meta';
  }
  async function loadProfiles(fresh = false) {
    if (disposed || suspended || loading || !active || !current.authorityVerified ||
        !sessionControls(current.session, current.features).applyLocalProfile || (!fresh && requestedKey === key())) return;
    const request = ++generation, requested = key(), machine = current.session.machine_id, family = current.session.agent_family;
    requestedKey = requested; loading = true; profileError = ''; controller = new AbortController(); paint();
    try {
      const detail = await api.getMachine(machine, {signal: controller.signal});
      if (disposed || suspended || request !== generation || requested !== key()) return;
      if (detail.machine !== machine) throw new Error('节点配置与当前会话不一致');
      profiles = (detail.current?.local_profiles || []).filter(row => row.family === family);
      const selected = select.value; select.replaceChildren();
      for (const row of profiles) {
        const option = el('option', null, row.label + (row.current ? ' · 节点当前配置' : ''));
        option.value = row.profile_id; select.append(option);
      }
      select.value = profiles.find(row => row.profile_id === selected)?.profile_id || profiles.find(row => row.current)?.profile_id || profiles[0]?.profile_id || '';
      loadedKey = requested;
    } catch (error) {
      if (disposed || suspended || request !== generation) return;
      loadedKey = ''; profileError = '配置读取失败：' + errorText(error);
    } finally { if (!disposed && request === generation) { loading = false; paint(); } }
  }
  select.addEventListener('change', paint);
  function stop() { generation++; controller?.abort(); loading = false; if (loadedKey !== key()) requestedKey = ''; if (confirmation.open) confirmation.close(); }
  return {
    update(value) {
      const previous = key(); current = value;
      if (previous !== key()) { stop(); profiles = []; loadedKey = ''; requestedKey = ''; profileError = ''; select.replaceChildren(); }
      paint(); if (active) loadProfiles();
    },
    activate() { active = true; loadProfiles(); },
    suspend() { suspended = true; stop(); },
    resume() { suspended = false; if (active) loadProfiles(); },
    dispose() { disposed = true; stop(); },
  };
}
