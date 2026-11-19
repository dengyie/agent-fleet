import {getRuntimeSettings, saveRuntimeSettings} from '../../api/settings.js';
import {el, button, field, panel, errorText} from './forms.js';

export function mountRuntime(root, kind = 'runtime') {
  let disposed = false, generation = 0;
  const status = el('p', '正在读取配置…'); status.setAttribute('role', 'status'); root.append(status);
  const content = el('div'); root.append(content);
  async function load() {
    const ticket = ++generation; content.inert = true;
    try {
      const data = await getRuntimeSettings(kind);
      if (disposed || ticket !== generation) return;
      content.replaceChildren(); status.textContent = '';
      const section = panel(content, kind === 'hosts' ? 'VM 展示与心跳配置' : '系统与运行参数', '管理服务器配置、功能开关与运行方式。', kind === 'hosts' ? 'globe' : 'tool');
      section.append(el('p', '保存后需重启 Hub 生效。当前服务会继续使用已加载的配置，页面不会自动重启。', 'settings-notice'));
      section.append(el('p', data.path, 'settings-path'));
      const form = el('form', null, 'settings-form'); section.append(form);
      const controls = new Map();
      if (kind === 'hosts') {
        controls.set('content', field(form, 'content', '节点清单（YAML）', data.content, {type: 'textarea', help: '配置节点展示顺序、描述、stale_after_s 与上报信息；执行能力请在「VM 与执行节点」中设置。'}));
        controls.get('content').rows = 22;
      } else {
        const groups = [
          ['平台功能', '配置助手、工作流与节点的可用能力', spec => spec.type === 'bool' && spec.key.startsWith('AGENT_FLEET_PLATFORM_')],
          ['监控与会话', '管理状态采集、会话与监控功能', spec => spec.type === 'bool' && !spec.key.startsWith('AGENT_FLEET_PLATFORM_')],
          ['运行间隔与容量', '调整轮询频率、并发与保留时间', spec => ['number', 'integer'].includes(spec.type)],
          ['外部连接与路径', '管理网络地址、目录与外部服务连接', spec => !['bool', 'number', 'integer'].includes(spec.type)],
        ].map(([title, description, matches]) => {
          const details = el('details', null, 'settings-runtime-group');
          const summary = el('summary'); summary.append(el('span', title), el('small', description)); details.append(summary);
          const fields = el('div', null, 'settings-runtime-fields');
          details.append(fields);
          form.append(details);
          return {matches, fields};
        });
        for (const spec of data.settings) {
          let value = spec.saved ?? spec.effective;
          if (spec.type === 'bool') value = spec.saved === null ? !!spec.effective : ['1', 'true', 'yes', 'on'].includes(spec.saved.toLowerCase());
          else if (spec.type === 'array') {
            if (spec.saved !== null && spec.key.endsWith('ALLOWED_ORIGINS')) value = spec.saved.split(',').filter(Boolean);
            else if (spec.saved !== null) { try { value = JSON.parse(spec.saved); } catch (_) { value = []; } }
            value = JSON.stringify(value || [], null, 2);
          }
          const group = groups.find(({matches}) => matches(spec));
          const control = field(group?.fields || form, spec.key, spec.label, value ?? '', {type: spec.type === 'bool' ? 'checkbox' : ['number', 'integer'].includes(spec.type) ? 'number' : spec.type === 'array' ? 'textarea' : spec.type === 'url' ? 'url' : 'text',
            help: '当前生效：' + (spec.type === 'bool' ? spec.effective ? '已开启' : '已关闭' : Array.isArray(spec.effective) ? JSON.stringify(spec.effective) : String(spec.effective ?? '未设置')) + (spec.environment_override ? ' · 环境变量覆盖，文件设置不会覆盖该环境变量。' : '')});
          if (spec.min !== null) { control.min = spec.min; control.max = spec.max; control.step = spec.type === 'integer' ? '1' : 'any'; }
          controls.set(spec.key, {control, spec, original: spec.type === 'bool' ? control.checked : control.value});
        }
      }
      const submit = el('button', '保存服务器配置', 'button-primary'); submit.type = 'submit';
      const actions = el('div', null, 'settings-form-actions'); actions.append(button('重新加载', load), submit); form.append(actions);
      form.addEventListener('submit', async event => {
        event.preventDefault(); submit.disabled = true; status.textContent = '正在保存…';
        try {
          let body;
          if (kind === 'hosts') body = {digest: data.digest, content: controls.get('content').value};
          else {
            const values = {};
            for (const [key, {control, spec, original}] of controls) {
              const raw = spec.type === 'bool' ? control.checked : control.value;
              if (raw === original) continue;
              values[key] = spec.type === 'bool' ? raw : spec.type === 'array' ? JSON.parse(raw) : ['number', 'integer'].includes(spec.type) ? Number(raw) : raw;
            }
            if (!Object.keys(values).length) { status.textContent = '配置没有变化'; return; }
            body = {digest: data.digest, values};
          }
          const result = await saveRuntimeSettings(kind, body);
          if (disposed || ticket !== generation || !form.isConnected) return;
          if (await load()) status.textContent = '已保存。需要重启 Hub 后生效。' + (result.backup_path ? ' 原配置备份：' + result.backup_path : '');
        } catch (error) { if (!disposed && ticket === generation && form.isConnected) status.textContent = errorText(error) + '；请重新加载核对服务器状态。'; }
        finally { submit.disabled = false; }
      });
      if (data.paths) {
        const paths = panel(content, '数据与部署位置', '以下是当前运行位置。路径变更涉及数据迁移，请按部署流程处理。');
        const locations = el('dl', null, 'settings-locations');
        for (const [key, value] of Object.entries(data.paths)) locations.append(el('dt', key), el('dd', value));
        paths.append(locations);
      }
      return true;
    } catch (error) { if (!disposed && ticket === generation) { status.replaceChildren(el('span', errorText(error)), button('重试', load)); } }
    finally { if (!disposed && ticket === generation) content.inert = false; }
  }
  load(); return () => { disposed = true; generation++; };
}
