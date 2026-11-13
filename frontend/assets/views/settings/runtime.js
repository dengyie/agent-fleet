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
      const section = panel(content, kind === 'hosts' ? 'VM 展示与心跳配置' : '系统与运行参数', '保存写入服务器配置文件；当前进程继续使用已加载配置，需要按部署流程重启 Hub 后生效。页面不会自动重启服务。');
      section.append(el('p', data.path, 'settings-path'));
      const form = el('form', null, 'settings-form'); section.append(form);
      const controls = new Map();
      if (kind === 'hosts') {
        controls.set('content', field(form, 'content', '节点清单（YAML）', data.content, {type: 'textarea', help: '配置节点展示顺序、描述、stale_after_s 与上报信息；执行能力请在「VM 与执行节点」中设置。'}));
        controls.get('content').rows = 22;
      } else {
        const groups = [
          ['平台功能', spec => spec.type === 'bool' && spec.key.startsWith('AGENT_FLEET_PLATFORM_')],
          ['监控与会话', spec => spec.type === 'bool' && !spec.key.startsWith('AGENT_FLEET_PLATFORM_')],
          ['运行间隔与容量', spec => ['number', 'integer'].includes(spec.type)],
          ['外部连接与路径', spec => !['bool', 'number', 'integer'].includes(spec.type)],
        ].map(([title, matches]) => {
          const details = el('details', null, 'settings-runtime-group');
          details.append(el('summary', title));
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
            help: '当前生效：' + (Array.isArray(spec.effective) ? JSON.stringify(spec.effective) : String(spec.effective ?? '未设置')) + (spec.environment_override ? ' · 环境变量覆盖，文件设置不会覆盖该环境变量。' : '')});
          if (spec.min !== null) { control.min = spec.min; control.max = spec.max; control.step = spec.type === 'integer' ? '1' : 'any'; }
          controls.set(spec.key, {control, spec, original: spec.type === 'bool' ? control.checked : control.value});
        }
      }
      const submit = el('button', '保存服务器配置', 'button-primary'); submit.type = 'submit'; form.append(submit, button('重新加载', load));
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
          if (disposed) return;
          if (await load()) status.textContent = '已保存。需要重启 Hub 后生效。' + (result.backup_path ? ' 原配置备份：' + result.backup_path : '');
        } catch (error) { if (!disposed) status.textContent = errorText(error) + '；请重新加载核对服务器状态。'; }
        finally { submit.disabled = false; }
      });
      if (data.paths) {
        const paths = panel(content, '数据与部署位置', '路径变更需要迁移数据，请按部署流程处理。以下展示当前运行位置。');
        for (const [key, value] of Object.entries(data.paths)) paths.append(el('p', key + '：' + value, 'settings-path'));
      }
      return true;
    } catch (error) { if (!disposed && ticket === generation) { status.replaceChildren(el('span', errorText(error)), button('重试', load)); } }
    finally { if (!disposed && ticket === generation) content.inert = false; }
  }
  load(); return () => { disposed = true; generation++; };
}
