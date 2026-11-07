/* Official plugin catalog and registered contribution lifecycle. */
import * as defaultClient from '../api/plugins.js';
import {pluginContribution} from '../plugins/registry.js';
import {el, button, field, errorText} from '../plugins/dom.js';

export function mountPlugins(host, {client = defaultClient} = {}) {
  const catalog = document.createElement('div');
  catalog.className = 'plugin-catalog';
  const content = el('section', 'plugin-content');
  const status = el('p', 'plugin-message', '正在加载插件…');
  status.setAttribute('role', 'status');
  const enrollment = el('details', 'plugin-enrollment');
  const form = el('form', 'plugin-controls');
  const nodeId = el('input'); nodeId.required = true; nodeId.maxLength = 128;
  nodeId.setAttribute('aria-label', '新节点 ID'); nodeId.pattern = '[A-Za-z0-9][A-Za-z0-9_.-]{0,127}';
  const label = el('input'); label.maxLength = 128; label.setAttribute('aria-label', '节点名称');
  const create = el('button', 'button-primary', '生成接入文件'); create.type = 'submit';
  form.append(field('新节点 ID', nodeId), field('节点名称', label), create);
  const download = el('div', 'plugin-enrollment-result');
  enrollment.append(el('summary', '', '接入新节点'), form, download);
  host.replaceChildren(status, enrollment, catalog, content);
  let disposed = false, active = true, generation = 0, contribution, selected, downloadUrl;
  form.addEventListener('submit', async event => {
    event.preventDefault(); create.disabled = true;
    try {
      const data = await client.enrollPluginNode(nodeId.value.trim(), label.value.trim());
      if (disposed) return;
      const endpoint = new URL(window.FleetConfig?.apiBaseUrl || '/api', window.location.href).origin;
      const config = {...data.enrollment, hub_url: endpoint};
      if (downloadUrl) URL.revokeObjectURL(downloadUrl);
      downloadUrl = URL.createObjectURL(new Blob([JSON.stringify(config, null, 2) + '\n'], {type: 'application/json'}));
      const link = el('a', 'button-primary', '下载节点接入文件');
      link.href = downloadUrl; link.download = 'fleet-plugin-' + config.node_id + '.json';
      download.replaceChildren(link, el('p', '', '请保存此文件并传到目标节点。接入凭据仅在本次生成，已有节点不会被覆盖。'),
        el('code', '', 'python -m tools.plugins.node --enroll <接入文件> --state-directory <本机绝对路径>'));
      await load();
    } catch (error) { if (!disposed) status.textContent = errorText(error); }
    finally { create.disabled = false; }
  });
  async function load() {
    const ticket = ++generation;
    try {
      const data = await client.getPlugins();
      if (disposed || !active || ticket !== generation) return;
      if (!Array.isArray(data.plugins)) throw new Error('插件列表格式无效');
      create.disabled = data.enrollment_ready === false;
      status.textContent = data.enrollment_ready === false ?
        '平台命令签名尚未配置。请在 Hub 运行 python -m hub.plugins_setup，随后重启 Hub。' :
        data.plugins.length ? '' : '暂无可用插件';
      catalog.replaceChildren();
      for (const plugin of data.plugins) {
        const card = el('article', 'plugin-card');
        const open = button(plugin.name, () => select(plugin), 'plugin-title');
        const copy = el('div');
        copy.append(open, el('p', '', plugin.description), el('span', 'plugin-meta', '官方插件 · v' + plugin.version));
        const toggle = button(plugin.enabled ? '停用插件' : '启用插件', async () => {
          toggle.disabled = true;
          try {
            await client.configurePlugin(plugin.plugin_id, !plugin.enabled, plugin.revision);
            await load();
          } catch (error) {
            if (!disposed) status.textContent = errorText(error);
          } finally { toggle.disabled = false; }
        }, 'plugin-toggle');
        toggle.setAttribute('aria-pressed', String(plugin.enabled));
        card.append(copy, toggle);
        catalog.append(card);
      }
      const plugin = data.plugins.find(p => p.plugin_id === selected) || data.plugins[0];
      if (plugin) select(plugin);
    } catch (error) {
      if (!disposed && active && ticket === generation) {
        status.replaceChildren(el('span', '', errorText(error)), button('重试', load));
      }
    }
  }
  function select(plugin) {
    if (selected === plugin.plugin_id && contribution) {
      contribution.updatePlugin?.(plugin);
      return;
    }
    contribution?.();
    selected = plugin.plugin_id;
    const mount = pluginContribution(plugin.ui_module);
    content.replaceChildren();
    contribution = mount ? mount(content, {plugin, client}) : undefined;
    if (!mount) content.textContent = '当前版本尚未包含此插件的管理界面';
  }
  const dispose = () => { disposed = true; generation++; contribution?.(); if (downloadUrl) URL.revokeObjectURL(downloadUrl); };
  dispose.suspend = () => { active = false; generation++; contribution?.suspend?.(); };
  dispose.resume = () => { if (disposed) return; active = true; contribution?.resume?.(); load(); };
  load();
  return dispose;
}
