import {getSettings, saveSetting, saveDefaults, provisionNode} from '../../api/settings.js';
import {el, button, field, panel, jsonValue, errorText} from './forms.js';

const info = {
  models: ['模型源与 API', '新增模型源', 'profile_id', '模型配置 ID'],
  nodes: ['VM 与执行节点', '新增执行节点', 'node_id', '节点 ID'],
  workspaces: ['工作区与存储', '新增工作区', 'workspace_id', '工作区 ID'],
};
export function mountCatalog(root, kind) {
  let disposed = false, catalog, currentOwner = '', sequence = 0;
  const context = document.createElement('details'); context.className = 'settings-owner-scope';
  const summary = el('summary', '为其他账号配置');
  const scopeForm = el('div', null, 'settings-owner-form');
  const owner = field(scopeForm, 'owner', '账号 ID');
  const status = el('p', '正在加载…'); status.setAttribute('role', 'status'); root.append(status);
  const content = el('div', null, 'settings-catalog'); root.append(content);
  scopeForm.prepend(el('p', '默认配置当前管理员账号。切换账号不会复制或迁移配置。', 'settings-description'));
  scopeForm.append(button('加载账号配置', () => { currentOwner = owner.value.trim(); load(); }));
  context.append(summary, scopeForm); root.prepend(context);
  async function load() {
    const ticket = ++sequence;
    content.inert = true;
    try {
      const data = await getSettings(currentOwner);
      if (disposed || ticket !== sequence) return;
      catalog = data; status.textContent = ''; content.replaceChildren();
      if (kind === 'defaults') renderDefaults(); else renderCatalog();
      return true;
    } catch (error) { if (!disposed && ticket === sequence) status.textContent = errorText(error); }
    finally { if (!disposed && ticket === sequence) content.inert = false; }
  }
  function renderDefaults() {
    const section = panel(content, '新对话默认配置', '只影响之后解析的运行配置。已有对话的覆盖配置和已提交任务保持原有绑定。');
    const form = el('form', null, 'settings-form'); section.append(form);
    const controls = {};
    for (const [name, title, rows, id, label] of [
      ['model_profile_id', '默认模型', catalog.models, 'profile_id', 'model'],
      ['workspace_id', '默认工作区', catalog.workspaces, 'workspace_id', 'name'],
      ['execution_node_id', '默认执行节点', catalog.nodes, 'node_id', 'label'],
    ]) controls[name] = field(form, name, title, catalog.defaults[name], {options: [['', '未指定'], ...rows.filter(row => row.enabled).map(row => [row[id], row[label] + ' · ' + row[id]])]});
    const submit = el('button', '保存默认配置', 'button-primary'); submit.type = 'submit'; form.append(submit);
    form.addEventListener('submit', async event => {
      event.preventDefault(); submit.disabled = true;
      try {
        await saveDefaults(Object.fromEntries(Object.entries(controls).map(([key, control]) => [key, control.value || null])), catalog.defaults.revision, currentOwner);
        if (await load()) status.textContent = '默认配置已保存';
      } catch (error) { status.textContent = errorText(error); }
      finally { submit.disabled = false; }
    });
  }
  function renderCatalog() {
    const [title, addLabel, idKey, idTitle] = info[kind];
    const section = panel(content, title, kind === 'models' ? 'API URL、模型与凭据在此管理；保存影响未来任务。已排队任务及登记工作流使用已冻结的配置，登记模板需重新发布才会切换模型源。' : kind === 'nodes' ? '登记已有 VM 或物理机的执行节点及能力。节点进程连接 Hub 并上报心跳后才可执行；登记不会创建或启动云主机。' : '设置工作目录和默认执行节点；路径位于实际执行节点。保存不会创建、搬迁或删除目录。');
    const toolbar = el('div', null, 'settings-toolbar'); section.append(toolbar);
    toolbar.append(button(addLabel, () => editor(null), 'button-primary'), button('重新加载', load));
    const list = el('div', null, 'settings-records'); section.append(list);
    for (const row of catalog[kind]) {
      const label = kind === 'models' ? row.model : kind === 'nodes' ? row.label : row.name;
      const entry = button('', () => editor(row), 'settings-record');
      entry.append(el('strong', label), el('span', row[idKey] + ' · ' + (row.enabled ? '已启用' : '已停用')));
      if (kind === 'models') entry.append(el('small', row.provider_config.endpoint || row.provider_config.base_url || row.provider));
      if (kind === 'nodes') entry.append(el('small', row.last_seen_at ? '最近心跳：' + new Date(row.last_seen_at * 1000).toLocaleString() : '尚无心跳'));
      list.append(entry);
    }
    if (!list.childElementCount) list.append(el('p', '暂无配置，点击上方按钮新增。', 'settings-description'));
    const editorRoot = el('div'); section.append(editorRoot);
    function editor(row) {
      editorRoot.replaceChildren();
      const form = el('form', null, 'settings-form'); editorRoot.append(form);
      form.append(el('h3', row ? '编辑 ' + row[idKey] : addLabel));
      const id = field(form, idKey, idTitle, row?.[idKey], {required: true, readOnly: !!row});
      id.pattern = '[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}';
      const controls = {};
      let value;
      if (kind === 'models') {
        controls.provider = field(form, 'provider', '模型协议', (row?.provider === 'openai-compatible' ? 'openai_compatible' : row?.provider) || 'openai_compatible', {options: [['openai_compatible', 'OpenAI 兼容 API'], ['deterministic', '本地确定性测试模型']]});
        controls.model = field(form, 'model', '模型名称', row?.model, {required: true});
        const config = row?.provider_config || {};
        controls.base_url = field(form, 'url', 'API URL', config.endpoint || config.base_url || '', {type: 'url', help: '填写 API Base URL（如 https://api.example.com/v1），或完整的 /chat/completions 地址。'});
        controls.api_key = field(form, 'api_key', 'API Key', '', {type: 'password', help: row?.secret_configured ? '已配置。留空保留原凭据；填写新值即轮换。' : '首次接入请填写；保存后不会回显。'});
        const advanced = el('details', null, 'settings-advanced');
        advanced.append(el('summary', '高级参数'));
        form.append(advanced);
        controls.timeout = field(advanced, 'timeout', '请求超时（秒）', config.timeout_s ?? 60, {type: 'number'}); controls.timeout.min = 1; controls.timeout.max = 600;
        controls.retries = field(advanced, 'retries', '最大重试次数', config.max_retries ?? 0, {type: 'number', help: '建议保持 0；不确定是否已受理的请求不会自动重放。'}); controls.retries.min = 0; controls.retries.max = 5;
        controls.stream = field(advanced, 'stream', '启用流式响应', config.stream === true, {type: 'checkbox'});
        controls.headers = field(advanced, 'headers', '附加请求头（JSON）', JSON.stringify(config.headers || {}, null, 2), {type: 'textarea', help: '支持 user-agent、accept-language、x-client-name、openai-organization；认证信息使用 API Key。'});
        controls.pricing = field(advanced, 'pricing', '用量单价（JSON，可留空）', config.pricing ? JSON.stringify(config.pricing, null, 2) : '', {type: 'textarea', help: 'USD / 百万 token，示例：{"currency":"USD","input":"1","output":"2","cache_read":"0","cache_write":"0"}。'});
        controls.capabilities = field(advanced, 'capabilities', '能力声明（JSON）', JSON.stringify(row?.capabilities || {}, null, 2), {type: 'textarea', help: '例如 {"streaming": true, "tools": true}。'});
      } else if (kind === 'nodes') {
        controls.label = field(form, 'label', '节点名称', row?.label, {required: true});
      } else {
        controls.name = field(form, 'name', '工作区名称', row?.name, {required: true});
        controls.root_path = field(form, 'root_path', '工作目录绝对路径', row?.root_path, {required: true});
        controls.node = field(form, 'default_node_id', '默认执行节点', row?.default_node_id, {options: [['', '使用账号默认节点'], ...catalog.nodes.filter(n => n.enabled).map(n => [n.node_id, n.label])]});
      }
      if (kind === 'nodes') controls.capabilities = field(form, 'capabilities', '能力声明（JSON）', JSON.stringify(row?.capabilities || {}, null, 2), {type: 'textarea', help: '例如 {"workspace.exec": true}。只填写节点实际支持的能力。'});
      const enabled = field(form, 'enabled', '启用此配置', row?.enabled ?? true, {type: 'checkbox'});
      const submit = el('button', '保存配置', 'button-primary'); submit.type = 'submit';
      const result = el('p'); result.setAttribute('role', 'status');
      form.append(submit, button('取消编辑', () => editorRoot.replaceChildren()), result);
      if (kind === 'nodes' && row?.enabled) {
        form.append(button('签发／轮换节点接入凭据', async event => {
          if (!window.confirm('轮换后节点必须使用新凭据重新连接，确认继续？')) return;
          event.currentTarget.disabled = true;
          try {
            const issued = await provisionNode(row.node_id, currentOwner);
            if (disposed || !form.isConnected) return;
            result.textContent = '接入凭据仅显示一次，请保存到节点配置：' + issued.credential;
          } catch (error) { result.textContent = errorText(error); }
          finally { if (form.isConnected) event.target.disabled = false; }
        }));
      }
      form.addEventListener('submit', async event => {
        event.preventDefault(); submit.disabled = true; result.textContent = '正在保存…';
        try {
          if (kind === 'models') {
            const provider_config = {timeout_s: Number(controls.timeout.value), max_retries: Number(controls.retries.value), stream: controls.stream.checked, headers: jsonValue(controls.headers)};
            if (controls.base_url.value.trim()) provider_config[/\/chat\/completions\/?$/.test(controls.base_url.value.trim()) ? 'endpoint' : 'base_url'] = controls.base_url.value.trim();
            if (controls.pricing.value.trim()) provider_config.pricing = jsonValue(controls.pricing);
            value = {provider: controls.provider.value, model: controls.model.value.trim(), provider_config, capabilities: jsonValue(controls.capabilities)};
          } else if (kind === 'nodes') value = {label: controls.label.value.trim(), capabilities: jsonValue(controls.capabilities)};
          else value = {name: controls.name.value.trim(), root_path: controls.root_path.value.trim(), backend: row?.backend || 'directory', default_node_id: controls.node.value || null};
          await saveSetting(kind, id.value.trim(), {expected_digest: row?.digest || '', value: {...value, enabled: enabled.checked}, ...(controls.api_key?.value ? {api_key: controls.api_key.value} : {})}, currentOwner);
          if (disposed) return;
          if (await load()) status.textContent = '配置已保存，将用于之后的新任务';
        } catch (error) { if (!disposed) result.textContent = errorText(error) + (error.status === 409 ? '；请点击重新加载，核对后再编辑。' : ''); }
        finally { submit.disabled = false; }
      });
      form.scrollIntoView({block: 'nearest'});
    }
  }
  load();
  return () => { disposed = true; sequence++; root.querySelectorAll('input[type=password]').forEach(input => { input.value = ''; }); };
}
