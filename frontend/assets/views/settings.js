import {navigatePage, pagePath} from '../routes.js';
import {accountRequest} from '../api/accounts.js';
import {mountAccount} from './account.js';
import {mountMCPIntegrations} from './account/integrations.js';
import {mountIntegrationIdentities} from './account/identities.js';
import {mountWorkflowSummaries} from './account/workflow-summaries.js';
import {mountPlugins} from './plugins.js';
import {mountCatalog} from './settings/catalogs.js';
import {mountRuntime} from './settings/runtime.js';
import {el, field, panel} from './settings/forms.js';
import {uiIcon} from '../routes.js';

const SECTIONS = [
  ['models', '模型源与 API', 'API URL 模型 Key 超时 重试 协议', 'brain'],
  ['defaults', '默认配置', '模型 工作区 节点 默认', 'check-circle'],
  ['nodes', 'VM 与执行节点', '虚拟机 节点 能力 接入 凭据', 'cpu'],
  ['workspaces', '工作区与存储', '目录 路径 执行位置', 'layers'],
  ['hosts', '节点展示与心跳', 'VM 清单 YAML TTL', 'globe'],
  ['integrations', '集成与工作流', 'MCP 身份 摘要 工作流', 'git-branch'],
  ['plugins', '插件管理', 'GitHub CLI 官方插件 接入', 'box'],
  ['runtime', '系统与运行参数', '开关 并发 网络 浏览器 Komari 沙盒 保留时间', 'tool'],
  ['account', '账号与访问', '资料 密码 登录 会话 邀请 用户 权限', 'lock'],
  ['appearance', '外观', '主题 浅色 深色 系统', 'sun'],
];
const GROUPS = [
  ['工作空间', ['models', 'defaults', 'nodes', 'workspaces', 'hosts']],
  ['连接与扩展', ['integrations', 'plugins']],
  ['系统', ['runtime']],
  ['个人设置', ['account', 'appearance']],
];
export function mountSettings(root, {section = 'models', account = null} = {}) {
  let disposed = false, suspended = false, views = [], epoch = 0;
  const admin = !account || account.role === 'admin';
  const allowed = SECTIONS.filter(([id]) => admin || ['account', 'appearance'].includes(id));
  const selected = allowed.some(([id]) => id === section) ? section : allowed[0][0];
  root.classList.add('settings-page');
  const nav = document.createElement('nav'); nav.className = 'settings-navigation'; nav.setAttribute('aria-label', '设置分类');
  const navHeader = el('div', null, 'settings-nav-header');
  navHeader.append(el('strong', '设置'), el('span', '配置与偏好'));
  nav.append(navHeader);
  const search = field(nav, 'search', '搜索设置', '', {type: 'search'}); search.placeholder = '搜索设置';
  search.closest('label').classList.add('settings-search-field');
  const navList = el('div', null, 'settings-nav-list');
  const links = [];
  for (const [groupTitle, ids] of GROUPS) {
    const entries = allowed.filter(([id]) => ids.includes(id));
    if (!entries.length) continue;
    const group = el('div', null, 'settings-nav-group');
    group.append(el('h2', groupTitle, 'settings-nav-group-title'));
    for (const [id, title, keywords, icon] of entries) {
      const link = el('a'); link.href = pagePath('settings', id); link.dataset.section = id;
      const glyph = el('span'); glyph.className = 'settings-nav-icon'; glyph.append(uiIcon(icon, {size: 17}));
      link.append(glyph, el('span', title));
      if (id === selected) link.setAttribute('aria-current', 'page');
      group.append(link); links.push({link, text: title + ' ' + keywords});
    }
    navList.append(group);
  }
  nav.append(navList);
  const mobileSection = field(nav, 'mobile-section', '设置分类', selected, {
    options: allowed.map(([id, title]) => [id, title]),
  });
  mobileSection.classList.add('settings-mobile-section');
  mobileSection.addEventListener('change', () => navigatePage(pagePath('settings', mobileSection.value)));
  const noResults = el('p', '没有匹配的设置', 'settings-description'); noResults.hidden = true; nav.append(noResults);
  search.addEventListener('input', () => {
    const query = search.value.trim().toLocaleLowerCase();
    links.forEach(({link, text}) => { link.hidden = !text.toLocaleLowerCase().includes(query); });
    for (const group of nav.querySelectorAll('.settings-nav-group')) {
      group.hidden = !group.querySelector('a:not([hidden])');
    }
    noResults.hidden = links.some(({link}) => !link.hidden);
  });
  const body = el('div', null, 'settings-content');
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
