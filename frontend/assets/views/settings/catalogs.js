import {getSettings, saveSetting, saveDefaults, enrollWorkspaceNode} from '../../api/settings.js';
import {el, button, field, panel, jsonValue, errorText} from './forms.js';
import {uiIcon} from '../../routes.js';

const info = {
  models: ['模型源与 API', '新增模型源', 'profile_id', '模型配置 ID'],
  nodes: ['VM 与执行节点', '新增执行节点', 'node_id', '节点 ID'],
  workspaces: ['工作区与存储', '新增工作区', 'workspace_id', '工作区 ID'],
};
const icons = {models: 'brain', nodes: 'cpu', workspaces: 'layers'};
export function mountCatalog(root, kind, {identitySignal} = {}) {
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
    catalog = null;
    content.replaceChildren();
    status.textContent = '正在加载…';
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
    const ticket = sequence, formOwner = currentOwner;
    const section = panel(content, '新对话默认配置', '为新对话选择常用模型、工作区和执行节点。已有对话和已提交任务保留原有配置。', 'check-circle');
    const form = el('form', null, 'settings-form'); section.append(form);
    const controls = {};
    for (const [name, title, rows, id, label] of [
      ['model_profile_id', '默认模型', catalog.models, 'profile_id', 'model'],
      ['workspace_id', '默认工作区', catalog.workspaces, 'workspace_id', 'name'],
      ['execution_node_id', '默认执行节点', catalog.nodes, 'node_id', 'label'],
    ]) controls[name] = field(form, name, title, catalog.defaults[name], {options: [['', '未指定'], ...rows.filter(row => row.enabled).map(row => [row[id], row[label] + ' · ' + row[id]])]});
    const submit = el('button', '保存默认配置', 'button-primary'); submit.type = 'submit';
    const actions = el('div', null, 'settings-form-actions'); actions.append(submit); form.append(actions);
    form.addEventListener('submit', async event => {
      event.preventDefault(); submit.disabled = true;
      try {
        await saveDefaults(Object.fromEntries(Object.entries(controls).map(([key, control]) => [key, control.value || null])), catalog.defaults.revision, formOwner);
        if (disposed || ticket !== sequence || !form.isConnected) return;
        if (await load()) status.textContent = '默认配置已保存';
      } catch (error) { if (!disposed && ticket === sequence && form.isConnected) status.textContent = errorText(error); }
      finally { submit.disabled = false; }
    });
  }
  function renderCatalog() {
    const ticket = sequence, formOwner = currentOwner;
    const [title, addLabel, idKey, idTitle] = info[kind];
    const section = panel(content, title, kind === 'models' ? '接入兼容 OpenAI Chat Completions 的第三方模型服务。每个模型配置独立的 Base URL、API Key 和模型 ID；密钥保存在服务端私密文件中，读取时不会回显。保存后用于新任务，已排队任务保留原配置。' : kind === 'nodes' ? '连接已有 VM 或物理机，配置执行能力。节点连接 Hub 并上报心跳后即可使用；登记不会创建或启动云主机。' : '管理工作目录和默认执行节点。目录位于实际执行节点；保存不会创建、搬迁或删除文件。', icons[kind]);
    const toolbar = el('div', null, 'settings-toolbar'); section.append(toolbar);
    const count = el('span', catalog[kind].length + ' 项配置', 'settings-count');
    const add = button(addLabel, () => editor(null), 'button-primary'); add.append(uiIcon('arrow-right', {size: 16}));
    const reload = button('重新加载', load); reload.prepend(uiIcon('refresh', {size: 15}));
    toolbar.append(count, reload, add);
    const list = el('div', null, 'settings-records'); section.append(list);
    for (const row of catalog[kind]) {
      const label = kind === 'models' ? row.model : kind === 'nodes' ? row.label : row.name;
      const entry = button('', () => editor(row), 'settings-record');
      entry.dataset.recordId = row[idKey]; entry.setAttribute('aria-pressed', 'false');
      const glyph = el('span', null, 'settings-record-icon'); glyph.append(uiIcon(icons[kind], {size: 18}));
      const copy = el('span', null, 'settings-record-copy');
      if (kind === 'models') {
        copy.append(el('strong', row.provider_config.provider_name || (row.provider === 'deterministic' ? '本地测试模型' : 'OpenAI 兼容服务')));
        copy.append(el('small', '模型：' + row.model + ' · ' + row[idKey]));
        copy.append(el('small', row.provider_config.endpoint || row.provider_config.base_url || '未设置 Base URL'));
        copy.append(el('small', row.secret_configured ? 'API Key 已配置' : '未配置 API Key'));
      } else {
        copy.append(el('strong', label), el('small', row[idKey]));
        if (kind === 'nodes') copy.append(el('small', row.last_seen_at ? '最近心跳：' + new Date(row.last_seen_at * 1000).toLocaleString() : '尚无心跳'));
        if (kind === 'workspaces') copy.append(el('small', row.root_path));
      }
      const state = el('span', row.enabled ? '已启用' : '已停用', 'settings-record-state'); state.dataset.enabled = String(row.enabled);
      entry.append(glyph, copy, state, uiIcon('chevron-right', {size: 16}));
      list.append(entry);
    }
    if (!list.childElementCount) {
      const empty = el('div', null, 'settings-empty'); empty.append(uiIcon(icons[kind], {size: 26}), el('strong', '尚未添加配置'), el('p', '点击「' + addLabel + '」，开始连接你的' + (kind === 'models' ? '模型服务。' : kind === 'nodes' ? '执行节点。' : '工作目录。'))); list.append(empty);
    }
    const editorRoot = el('div'); section.append(editorRoot);
    function clearEditor() { editorRoot.replaceChildren(); list.hidden = false; list.querySelectorAll('.settings-record').forEach(entry => entry.setAttribute('aria-pressed', 'false')); }
    function editor(row) {
      clearEditor();
      list.hidden = !catalog[kind].length;
      list.querySelectorAll('.settings-record').forEach(entry => entry.setAttribute('aria-pressed', String(entry.dataset.recordId === row?.[idKey])));
      const form = el('form', null, 'settings-form settings-editor'); editorRoot.append(form);
      const heading = el('div', null, 'settings-editor-heading');
      heading.append(el('h3', row ? '编辑 ' + row[idKey] : kind === 'models' ? '添加第三方模型' : addLabel), el('p', row ? '更新服务连接信息；不填写新 Key 会保留当前凭据。' : kind === 'models' ? '依次填写服务商、Base URL、API Key 和服务商实际使用的模型 ID。' : '填写基本信息，高级选项可按需配置。', 'settings-description')); form.append(heading);
      let id;
      const controls = {};
      let value;
      if (kind === 'models') {
        controls.provider = field(form, 'provider', '模型协议', (row?.provider === 'openai-compatible' ? 'openai_compatible' : row?.provider) || 'openai_compatible', {options: [['openai_compatible', 'OpenAI 兼容 API'], ['deterministic', '本地确定性测试模型']]});
        const config = row?.provider_config || {};
        controls.provider_name = field(form, 'provider_name', '服务商名称', config.provider_name || '', {required: !row && controls.provider.value !== 'deterministic', help: '例如 OpenRouter、硅基流动或自建网关。'});
        controls.base_url = field(form, 'url', 'Base URL', config.endpoint || config.base_url || '', {type: 'url', required: !row && controls.provider.value !== 'deterministic', help: '服务商提供的 API 根地址，例如 https://api.example.com/v1。'});
        controls.api_key = field(form, 'api_key', 'API Key', '', {type: 'password', required: !row?.secret_configured && (row?.enabled ?? true) && controls.provider.value !== 'deterministic', help: row?.secret_configured ? '当前凭据已保存。留空以保留；填写新 Key 会替换当前凭据。' : '仅写入服务端私密凭据文件，不会在后续读取中回显。'});
        controls.base_url.placeholder = 'https://api.example.com/v1';
        controls.api_key.placeholder = row?.secret_configured ? '留空以保留当前 Key' : '粘贴服务商提供的 API Key';
        controls.provider_name.closest('label').classList.add('settings-field-wide');
        controls.base_url.closest('label').classList.add('settings-field-wide');
        controls.api_key.closest('label').classList.add('settings-field-wide');
        const keyControl = controls.api_key;
        const keyActions = el('span', null, 'settings-secret-input');
        keyControl.parentElement.insertBefore(keyActions, keyControl);
        keyActions.append(keyControl);
        const revealKey = button('', () => {
          const visible = keyControl.type === 'password';
          keyControl.type = visible ? 'text' : 'password';
          revealKey.setAttribute('aria-label', visible ? '隐藏 API Key' : '显示 API Key');
          revealKey.title = visible ? '隐藏 API Key' : '显示 API Key';
        }, 'icon-button settings-secret-toggle');
        revealKey.setAttribute('aria-label', '显示 API Key'); revealKey.title = '显示 API Key'; revealKey.append(uiIcon('eye', {size: 16}));
        keyActions.append(revealKey);
        controls.model = field(form, 'model', '模型 ID', row?.model, {required: true, help: '填写服务商 API 实际接受的名称，通常可在模型列表或 API 文档中找到。'});
        controls.model.placeholder = '例如 vendor/model-name';
        controls.model.closest('label').classList.add('settings-field-wide');
        const guide = el('div', null, 'settings-model-source-guide');
        guide.append(uiIcon('globe', {size: 17}), el('span', '支持自建网关和第三方 OpenAI 兼容 API。仅提供 Anthropic Messages 或其他非兼容协议的地址不能用于此配置。'));
        form.insertBefore(guide, controls.provider.closest('label'));
        controls.provider.addEventListener('change', () => {
          const required = controls.provider.value !== 'deterministic' && enabled.checked;
          controls.provider_name.required = controls.provider.value !== 'deterministic' && !row;
          controls.base_url.required = required;
          controls.api_key.required = required && !row?.secret_configured;
        });
        const advanced = el('details', null, 'settings-advanced');
        advanced.append(el('summary', '高级参数'));
        form.append(advanced);
        controls.anthropic = field(advanced, 'anthropic', 'Claude Code 接管 Base URL', config.agent_endpoints?.anthropic || '', {type: 'url', help: '可选。网关须支持 Anthropic Messages；通常填写站点根地址。复用此模型的 API Key，供主助手接管 VM 上已结束的原会话。'});
        controls.responses = field(advanced, 'responses', 'Codex 接管 Base URL', config.agent_endpoints?.responses || '', {type: 'url', help: '可选。网关须支持 Responses；通常以 /v1 结尾。复用此模型的 API Key。仅支持 Chat Completions 的地址不能用于 Codex。'});
        controls.timeout = field(advanced, 'timeout', '请求超时（秒）', config.timeout_s ?? 60, {type: 'number'}); controls.timeout.min = 1; controls.timeout.max = 600;
        controls.retries = field(advanced, 'retries', '最大重试次数', config.max_retries ?? 3, {type: 'number', help: '建议 3 次；503、限流等临时错误会自动重试，并实时显示进度。已执行的工具不会重放。'}); controls.retries.min = 0; controls.retries.max = 5;
        controls.stream = field(advanced, 'stream', '启用流式响应', row ? config.stream === true : true, {type: 'checkbox'});
        controls.headers = field(advanced, 'headers', '附加请求头（JSON）', JSON.stringify(config.headers || {}, null, 2), {type: 'textarea', help: '支持 user-agent、accept-language、x-client-name、openai-organization；认证信息使用 API Key。'});
        controls.pricing = field(advanced, 'pricing', '用量单价（JSON，可留空）', config.pricing ? JSON.stringify(config.pricing, null, 2) : '', {type: 'textarea', help: 'USD / 百万 token，示例：{"currency":"USD","input":"1","output":"2","cache_read":"0","cache_write":"0"}。'});
        controls.capabilities = field(advanced, 'capabilities', '能力声明（JSON）', JSON.stringify(row?.capabilities || {}, null, 2), {type: 'textarea', help: '例如 {"streaming": true, "tools": true}。'});
        id = field(advanced, idKey, '模型配置 ID', row?.[idKey] || 'model-' + (globalThis.crypto?.randomUUID?.().slice(0, 12) || Date.now().toString(36)), {required: true, readOnly: !!row, help: 'Fleet 内部引用 ID，用于默认模型选择；可以与服务商模型 ID 不同。'});
        id.pattern = '[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}';
        if (!row) id.placeholder = '例如 openrouter-gpt-4o';
      } else if (kind === 'nodes') {
        id = field(form, idKey, idTitle, row?.[idKey], {required: true, readOnly: !!row});
        id.pattern = '[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}';
        controls.label = field(form, 'label', '节点名称', row?.label, {required: true});
      } else {
        id = field(form, idKey, idTitle, row?.[idKey], {required: true, readOnly: !!row});
        id.pattern = '[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}';
        controls.name = field(form, 'name', '工作区名称', row?.name, {required: true});
        controls.root_path = field(form, 'root_path', '工作目录绝对路径', row?.root_path, {required: true});
        controls.node = field(form, 'default_node_id', '默认执行节点', row?.default_node_id, {options: [['', '使用账号默认节点'], ...catalog.nodes.filter(n => n.enabled).map(n => [n.node_id, n.label])]});
      }
      if (kind === 'nodes') controls.capabilities = field(form, 'capabilities', '能力声明（JSON）', JSON.stringify(row?.capabilities || {'workspace.list': true, 'workspace.read': true, 'workspace.write': true, 'workspace.exec': true}, null, 2), {type: 'textarea', help: '工作区节点默认支持列目录、读写文件和执行命令。仅声明实际安装的能力。'});
      const enabled = field(form, 'enabled', '启用此配置', row?.enabled ?? true, {type: 'checkbox'});
      if (kind === 'models') enabled.addEventListener('change', () => {
        const compatible = controls.provider.value !== 'deterministic';
        controls.base_url.required = compatible;
        controls.api_key.required = compatible && enabled.checked && !row?.secret_configured;
      });
      const submit = el('button', '保存配置', 'button-primary'); submit.type = 'submit';
      const result = el('p'); result.setAttribute('role', 'status');
      const actions = el('div', null, 'settings-form-actions');
      actions.append(result, button('取消编辑', () => { clearEditor(); add.focus({preventScroll: true}); }), submit); form.append(actions);
      if (kind === 'nodes' && row?.enabled) {
        const setup = el('details', null, 'settings-advanced');
        setup.append(el('summary', '部署或更新 VM 执行配置'));
        setup.append(el('p', '先保存节点，并在「工作区与存储」中将工作目录绑定到此节点。这里生成接入文件；安装文件并重启 VM 上的 Node 后，执行模式和目录才会生效。下载会轮换此节点凭据，旧进程需同步更新。', 'settings-description'));
        const mode = field(setup, 'execution_mode', '执行模式', 'vm_direct', {options: [['vm_direct', 'VM 直接执行（推荐）'], ['sandbox', '自定义沙盒启动器']]});
        const launcher = field(setup, 'sandbox_launcher', '沙盒启动参数（JSON）', '[]', {type: 'textarea', help: '仅自定义沙盒填写 argv 数组；VM 直接执行留为 []。VM 模式可运行 shell 和解释器，文件与网络范围由 VM 账号决定。'});
        launcher.closest('label').hidden = true;
        mode.addEventListener('change', () => { launcher.closest('label').hidden = mode.value !== 'sandbox'; });
        setup.append(el('pre', 'python3 -m tools.plugins.node --enroll enrollment.json --state-directory /var/lib/fleet-node/state\npython3 -m tools.plugins.node --config /var/lib/fleet-node/state/node.json', 'settings-description settings-node-command'));
        setup.append(el('p', '首次安装使用上述命令。更新时先停止 Node，保留 commands.db 命令日志，备份并替换接入文件生成的三个配置文件，再启动 Node。不要删除命令日志；它防止已执行命令重复运行。', 'settings-description'));
        const download = button('下载接入配置', async () => {
          if (!window.confirm('将轮换节点凭据。请保存下载文件，并在 VM 上安装后重启 Node，确认继续？')) return;
          download.disabled = true;
          try {
            const issued = await enrollWorkspaceNode(row.node_id, {hub_url: window.location.origin,
              execution_mode: mode.value, sandbox_launcher: mode.value === 'sandbox' ? JSON.parse(launcher.value) : []}, formOwner);
            // The server has already rotated the credential. Deliver this
            // one-time result even if navigation replaced/disposed the editor.
            if (identitySignal?.aborted) return;
            const url = URL.createObjectURL(new Blob([JSON.stringify(issued.enrollment, null, 2) + '\n'], {type: 'application/json'}));
            const link = el('a'); link.href = url; link.download = row.node_id + '-enrollment.json';
            document.body.append(link); link.click(); link.remove();
            setTimeout(() => URL.revokeObjectURL(url), 1000);
            if (!disposed && ticket === sequence && form.isConnected) result.textContent = '接入配置已下载。请妥善保管凭据，在 VM 安装并重启 Node；出现心跳后再选择为默认执行节点。';
          } catch (error) { if (!disposed && ticket === sequence && form.isConnected) result.textContent = errorText(error); }
          finally { download.disabled = false; }
        });
        setup.append(download); form.insertBefore(setup, actions);
      }
      form.addEventListener('submit', async event => {
        event.preventDefault(); submit.disabled = true; result.textContent = '正在保存…';
        try {
          if (kind === 'models') {
            const provider_config = {timeout_s: Number(controls.timeout.value), max_retries: Number(controls.retries.value), stream: controls.stream.checked, headers: jsonValue(controls.headers)};
            if (controls.provider_name.value.trim()) provider_config.provider_name = controls.provider_name.value.trim();
            if (controls.base_url.value.trim()) provider_config[/\/chat\/completions\/?$/.test(controls.base_url.value.trim()) ? 'endpoint' : 'base_url'] = controls.base_url.value.trim();
            if (controls.pricing.value.trim()) provider_config.pricing = jsonValue(controls.pricing);
            const agent_endpoints = Object.fromEntries(['anthropic', 'responses'].filter(key => controls[key].value.trim()).map(key => [key, controls[key].value.trim()]));
            if (Object.keys(agent_endpoints).length) provider_config.agent_endpoints = agent_endpoints;
            value = {provider: controls.provider.value, model: controls.model.value.trim(), provider_config, capabilities: jsonValue(controls.capabilities)};
          } else if (kind === 'nodes') value = {label: controls.label.value.trim(), capabilities: jsonValue(controls.capabilities)};
          else value = {name: controls.name.value.trim(), root_path: controls.root_path.value.trim(), backend: row?.backend || 'directory', default_node_id: controls.node.value || null};
          await saveSetting(kind, id.value.trim(), {expected_digest: row?.digest || '', value: {...value, enabled: enabled.checked}, ...(controls.api_key?.value ? {api_key: controls.api_key.value} : {})}, formOwner);
          if (disposed || ticket !== sequence || !form.isConnected) return;
          if (await load()) status.textContent = '配置已保存，将用于之后的新任务';
        } catch (error) { if (!disposed && ticket === sequence && form.isConnected) result.textContent = errorText(error) + (error.status === 409 ? '；请点击重新加载，核对后再编辑。' : ''); }
        finally { submit.disabled = false; }
      });
      heading.scrollIntoView({block: 'nearest'});
      (kind === 'models' ? controls.provider_name : id).focus({preventScroll: true});
    }
  }
  load();
  const clearSecrets = () => root.querySelectorAll('input[name="api_key"]').forEach(input => { input.value = ''; });
  const dispose = () => { disposed = true; sequence++; clearSecrets(); };
  dispose.suspend = clearSecrets;
  return dispose;
}
