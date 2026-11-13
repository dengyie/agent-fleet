import {pagePath} from '../routes.js';
import {accountRequest} from '../api/accounts.js';
import {mountAccount} from './account.js';
import {mountMCPIntegrations} from './account/integrations.js';
import {mountIntegrationIdentities} from './account/identities.js';
import {mountWorkflowSummaries} from './account/workflow-summaries.js';
import {mountPlugins} from './plugins.js';
import {mountCatalog} from './settings/catalogs.js';
import {mountRuntime} from './settings/runtime.js';
import {el, field, panel} from './settings/forms.js';

const SECTIONS = [
  ['models', '模型源与 API', 'API URL 模型 Key 超时 重试 协议'],
  ['defaults', '默认配置', '模型 工作区 节点 默认'],
  ['nodes', 'VM 与执行节点', '虚拟机 节点 能力 接入 凭据'],
  ['workspaces', '工作区与存储', '目录 路径 执行位置'],
  ['hosts', '节点展示与心跳', 'VM 清单 YAML TTL'],
  ['integrations', '集成与工作流', 'MCP 身份 摘要 工作流'],
  ['plugins', '插件管理', 'GitHub CLI 官方插件 接入'],
  ['runtime', '系统与运行参数', '开关 并发 网络 浏览器 Komari 沙盒 保留时间'],
  ['account', '账号与访问', '资料 密码 登录 会话 邀请 用户 权限'],
  ['appearance', '外观', '主题 浅色 深色 系统'],
];
export function mountSettings(root, {section = 'models', account = null} = {}) {
  let disposed = false, suspended = false, views = [], epoch = 0;
  const admin = !account || account.role === 'admin';
  const allowed = SECTIONS.filter(([id]) => admin || ['account', 'appearance'].includes(id));
  const selected = allowed.some(([id]) => id === section) ? section : allowed[0][0];
  root.classList.add('settings-page');
  const nav = document.createElement('nav'); nav.className = 'settings-navigation'; nav.setAttribute('aria-label', '设置分类');
  const search = field(nav, 'search', '搜索设置', '', {type: 'search'}); search.placeholder = '搜索模型、VM、API…';
  const links = [];
  for (const [id, title, keywords] of allowed) {
    const link = el('a', title); link.href = pagePath('settings', id);
    if (id === selected) link.setAttribute('aria-current', 'page');
    nav.append(link); links.push({link, text: title + ' ' + keywords});
  }
  const noResults = el('p', '没有匹配的设置', 'settings-description'); noResults.hidden = true; nav.append(noResults);
  search.addEventListener('input', () => {
    const query = search.value.trim().toLocaleLowerCase();
    links.forEach(({link, text}) => { link.hidden = !text.toLocaleLowerCase().includes(query); });
    noResults.hidden = links.some(({link}) => !link.hidden);
  });
  const body = el('div', null, 'settings-content');
  body.append(el('p', '所有配置集中在这里。日常任务仍在主助手和工作页面完成。', 'settings-intro'));
  root.append(nav, body);
  if (['models', 'nodes', 'workspaces', 'defaults'].includes(selected)) views.push(mountCatalog(body, selected));
  else if (['runtime', 'hosts'].includes(selected)) views.push(mountRuntime(body, selected));
  else if (selected === 'account') {
    if (account) views.push(mountAccount(body));
    else panel(body, '账号与访问', '当前使用操作员认证模式。账号资料与邀请管理需要启用账号登录。');
  } else if (selected === 'plugins') views.push(mountPlugins(body));
  else if (selected === 'integrations') loadIntegrations();
  else if (selected === 'appearance') {
    const section = panel(body, '外观主题', '外观只保存在当前浏览器，与侧栏主题选择同步。');
    const choice = field(section, 'theme', '配色方案', document.querySelector('[data-theme][aria-pressed="true"]')?.dataset.theme || 'system', {options: [['system', '跟随系统'], ['light', '浅色'], ['dark', '深色']]});
    choice.addEventListener('change', () => document.querySelector('[data-theme="' + choice.value + '"]')?.click());
  }
  async function loadIntegrations() {
    const ticket = ++epoch;
    const status = el('p', '正在读取可用集成…'); status.setAttribute('role', 'status'); body.append(status);
    try {
      const data = await accountRequest('me');
      if (disposed || ticket !== epoch) return;
      const features = data.features || {}; let count = 0;
      if (features.mcp_catalog_management) { views.push(mountMCPIntegrations(body, {suspended, registrationEnabled: features.mcp_catalog_registration === true, ownerId: data.user.id})); count++; }
      if (features.fleet_identity_management) { views.push(mountIntegrationIdentities(body, {suspended})); count++; }
      if (features.workflow_summary_management) { views.push(mountWorkflowSummaries(body, {suspended, ownerId: data.user.id})); count++; }
      status.textContent = count ? '' : '尚未启用集成管理。可在系统与运行参数中配置相关功能开关，重启后生效。';
      const link = el('a', '配置集成运行开关', 'button-secondary'); link.href = pagePath('settings', 'runtime'); body.append(link);
    } catch (error) { if (!disposed) status.textContent = error.detail || '集成配置加载失败，请重新进入此分类重试'; }
  }
  const dispose = () => { disposed = true; epoch++; views.forEach(view => view?.()); views = []; root.classList.remove('settings-page'); };
  dispose.suspend = () => { suspended = true; views.forEach(view => view?.suspend?.()); };
  dispose.resume = () => { suspended = false; views.forEach(view => view?.resume?.()); };
  return dispose;
}
