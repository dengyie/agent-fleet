import {el, button, link} from './dom.js';
import {pagePath} from '../../routes.js';
import {ACCOUNT_STATES} from '../../api/provider-account-contracts.js';
import {createNativeReader} from './live.js';

// Selection never changes a running session. A stale/missing selection stays
// blocked until explicitly changed; directory failure cannot select default auth.
export function mountProviderAccountPicker(host, {api, onChange}) {
  let machine = '', active = false, locked = false, rows = null, failed = false, selected = '';
  const label = el('label', 'native-target-field'), select = el('select');
  select.setAttribute('aria-label', 'Google 账号'); label.append(el('span', null, 'Google 账号'), select);
  const status = el('p', 'meta'); status.setAttribute('role', 'status');
  const manage = link('管理 Google 账号', pagePath('settings', 'provider-accounts'));
  const retry = button('刷新 Google 账号', () => reader.wake());
  host.append(label, status, retry, manage); host.hidden = true;
  function publish() {
    const row = rows?.find(row => row.account_id === selected);
    onChange({ready: !active || !!rows && !failed && (!selected || row?.state === 'ready' && row.enabled),
      ...(active && selected ? {provider_account_id: selected} : {})});
  }
  function paint() {
    select.replaceChildren();
    const option = (id, text, disabled = false) => { const item = el('option', null, text); item.value = id; item.disabled = disabled; select.append(item); };
    option('', '节点默认登录（不绑定独立账号）');
    for (const row of rows || []) option(row.account_id, row.name + ' · ' + ACCOUNT_STATES[row.state], row.state !== 'ready' || !row.enabled);
    if (selected && !rows?.some(row => row.account_id === selected)) option(selected, '所选账号当前不可用', true);
    select.value = selected; select.disabled = locked || !rows || failed;
    retry.disabled = locked;
    status.textContent = failed ? 'Google 账号目录读取失败，发送已暂停。请刷新后确认原账号；不会自动改用默认登录。' : !rows ? '正在读取此节点的 Google 账号…' :
      selected ? '本次任务及后续续聊固定使用所选账号。只有已启用且可用的账号能启动新任务。' :
        rows.length ? '可选择独立 Google 账号，或明确使用节点原有默认登录。' : '此节点尚无独立账号。可前往设置创建，或使用节点已有默认登录。';
    publish();
  }
  const reader = createNativeReader({interval: 5000, async read(signal, current) {
    if (!active) return;
    const id = machine, result = await api.listProviderAccounts(id, {signal});
    if (!current() || machine !== id) return;
    rows = result.accounts; failed = false; paint();
  }, onError() { failed = true; paint(); }});
  select.addEventListener('change', () => { selected = select.value; paint(); });
  return {
    update(nextMachine, family) {
      const nextActive = family === 'agy' && !!nextMachine;
      if (nextMachine === machine && nextActive === active) return;
      reader.stop(); machine = nextMachine; active = nextActive; rows = null; failed = false; selected = '';
      host.hidden = !active; paint(); if (active && !document.hidden) reader.start();
    },
    setLocked(value) { locked = value; select.disabled = locked || !rows || failed; retry.disabled = locked; },
    suspend() { reader.stop(); }, resume() { if (active) reader.start(); },
    dispose() { reader.dispose(); },
  };
}
