import {navigatePage, pagePath} from '../routes.js';
import {accountRequest} from '../api/accounts.js';
import {mountAccount} from './account.js';
import {mountMCPIntegrations} from './account/integrations.js';
import {mountIntegrationIdentities} from './account/identities.js';
import {mountWorkflowSummaries} from './account/workflow-summaries.js';
import {mountPlugins} from './plugins.js';
import {mountCatalog} from './settings/catalogs.js';
import {mountRuntime} from './settings/runtime.js';
import {mountAppearance} from './settings/appearance.js';
import {el, button, field, panel, errorText} from './settings/forms.js';
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
export function mountSettings(root, {section = 'models', account = null, identitySignal, animateEntrance = true, onClose = () => navigatePage(pagePath('assistant'))} = {}) {
  let disposed = false, suspended = false, closing = false, closeTimer = null, views = [], epoch = 0;
  let backdropPointer = null;
  let integrationState = 'idle', integrationStatus = null;
  const admin = !account || account.role === 'admin';
  const allowed = SECTIONS.filter(([id]) => admin || ['account', 'appearance'].includes(id));
  const selected = allowed.some(([id]) => id === section) ? section : allowed[0][0];
  root.classList.add('settings-page');
  const dialog = document.createElement('dialog'); dialog.className = 'settings-dialog';
  dialog.classList.toggle('is-section-navigation', !animateEntrance);
  dialog.setAttribute('aria-label', '设置');
  const close = document.createElement('button'); close.type = 'button'; close.className = 'icon-button settings-close';
  close.setAttribute('aria-label', '关闭设置'); close.title = '关闭设置 · Esc'; close.append(uiIcon('x', {size: 18}));
  const requestClose = () => {
    if (disposed || closing) return;
    closing = true;
    // Preserve an unfinished entrance so reversing the motion never snaps to its endpoint.
    const current = getComputedStyle(dialog);
    dialog.style.setProperty('--settings-exit-opacity', current.opacity);
    dialog.style.setProperty('--settings-exit-transform', current.transform);
    dialog.style.setProperty('--settings-backdrop-exit-opacity', getComputedStyle(dialog, '::backdrop').opacity);
    dialog.classList.add('is-closing');
    const duration = Number.parseFloat(getComputedStyle(dialog).getPropertyValue('--settings-close-duration')) || 180;
    closeTimer = window.setTimeout(onClose, duration + 24);
  };
  close.addEventListener('click', requestClose);
  dialog.addEventListener('cancel', event => {
    if (event.target !== dialog) return;
    event.preventDefault(); requestClose();
  });
  const outsideDialog = event => {
    const bounds = dialog.getBoundingClientRect();
    // A nested modal can extend beyond settings; its bubbled events are not
    // backdrop gestures for this dialog.
    return event.target === dialog && (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom);
  };
  dialog.addEventListener('pointerdown', event => {
    backdropPointer = event.isPrimary && event.button === 0 && outsideDialog(event)
      ? {id: event.pointerId, x: event.clientX, y: event.clientY} : null;
  });
  dialog.addEventListener('pointermove', event => {
    if (backdropPointer && (event.pointerId !== backdropPointer.id
        || Math.hypot(event.clientX - backdropPointer.x, event.clientY - backdropPointer.y) > 8)) backdropPointer = null;
  });
  dialog.addEventListener('pointerup', event => {
    const start = backdropPointer; backdropPointer = null;
    // Both ends must be on the backdrop. Selecting text or scrolling across
    // the edge must not discard the user's interaction with the dialog.
    if (start && start.id === event.pointerId && event.button === 0 && outsideDialog(event)
        && Math.hypot(event.clientX - start.x, event.clientY - start.y) <= 8) requestClose();
  });
  dialog.addEventListener('pointercancel', () => { backdropPointer = null; });
  const layout = document.createElement('div'); layout.className = 'settings-layout';
  dialog.append(close, layout); root.append(dialog);
  const nav = document.createElement('nav'); nav.className = 'settings-navigation'; nav.setAttribute('aria-label', '设置分类');
  const navHeader = el('div', null, 'settings-nav-header');
  navHeader.append(el('strong', '设置'), el('span', '配置与偏好'));
  nav.append(navHeader);
  const searchField = el('div', null, 'settings-search-field'); nav.append(searchField);
  const search = field(searchField, 'search', '搜索设置', '', {type: 'search'}); search.placeholder = '搜索设置';
  const clearSearch = button('', () => { search.value = ''; filterSections(); search.focus({preventScroll: true}); }, 'icon-button settings-search-clear');
  clearSearch.setAttribute('aria-label', '清除设置搜索'); clearSearch.title = '清除搜索'; clearSearch.append(uiIcon('x', {size: 14})); clearSearch.hidden = true;
  searchField.append(clearSearch);
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
  const noResults = el('p', '', 'settings-search-status'); noResults.setAttribute('role', 'status'); noResults.hidden = true; nav.append(noResults);
  function filterSections() {
    const query = search.value.trim().toLocaleLowerCase();
    links.forEach(({link, text}) => { link.hidden = !text.toLocaleLowerCase().includes(query); });
    for (const group of nav.querySelectorAll('.settings-nav-group')) {
      group.hidden = !group.querySelector('a:not([hidden])');
    }
    const count = links.filter(({link}) => !link.hidden).length;
    noResults.hidden = !query;
    noResults.textContent = count ? '找到 ' + count + ' 个设置分类' : '没有匹配的设置';
    clearSearch.hidden = !search.value;
  }
  search.addEventListener('input', filterSections);
  search.addEventListener('keydown', event => {
    if (event.isComposing || event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.key === 'Escape' && search.value) {
      event.preventDefault(); event.stopPropagation(); search.value = ''; filterSections();
    } else if (event.key === 'Enter') {
      event.preventDefault(); links.find(({link}) => !link.hidden)?.link.click();
    } else if (event.key === 'ArrowDown') {
      event.preventDefault(); links.find(({link}) => !link.hidden)?.link.focus();
    }
  });
  navList.addEventListener('keydown', event => {
    if (event.isComposing || event.metaKey || event.ctrlKey || event.altKey || !['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
    const visible = links.filter(({link}) => !link.hidden).map(({link}) => link);
    const current = visible.indexOf(document.activeElement);
    if (current < 0) return;
    event.preventDefault();
    const index = event.key === 'Home' ? 0 : event.key === 'End' ? visible.length - 1
      : (current + (event.key === 'ArrowDown' ? 1 : -1) + visible.length) % visible.length;
    visible[index].focus();
  });
  const body = el('div', null, 'settings-content');
  layout.append(nav, body);
  const focusCategory = () => {
    const selectedLink = links.find(({link}) => link.dataset.section === selected)?.link;
    const control = mobileSection.getClientRects().length ? mobileSection : selectedLink?.hidden ? search : selectedLink;
    control?.focus({preventScroll: true});
  };
  dialog.showModal();
  if (!animateEntrance) focusCategory();
  let extensionRoot = body;
  if (['account', 'plugins', 'integrations'].includes(selected)) {
    const [, title, , icon] = SECTIONS.find(([id]) => id === selected);
    const descriptions = {account: '管理个人资料、登录设备和账号访问。', plugins: '连接常用工具，管理插件与节点接入。', integrations: '管理外部连接、集成身份和工作流摘要。'};
    const section = panel(body, title, descriptions[selected], icon);
    extensionRoot = el('div', null, 'settings-section-body'); section.append(extensionRoot);
  }
  if (['models', 'nodes', 'workspaces', 'defaults'].includes(selected)) views.push(mountCatalog(body, selected, {identitySignal}));
  else if (['runtime', 'hosts'].includes(selected)) views.push(mountRuntime(body, selected));
  else if (selected === 'account') {
    if (account) views.push(mountAccount(extensionRoot));
    else extensionRoot.append(el('p', '当前使用操作员认证模式。账号资料与邀请管理需要启用账号登录。', 'settings-notice'));
  } else if (selected === 'plugins') views.push(mountPlugins(extensionRoot));
  else if (selected === 'integrations') loadIntegrations();
  else if (selected === 'appearance') views.push(mountAppearance(body));
  async function loadIntegrations() {
    if (disposed || integrationState === 'loading' || integrationState === 'loaded') return;
    integrationState = 'loading';
    const ticket = ++epoch;
    if (!integrationStatus) {
      integrationStatus = el('p'); integrationStatus.setAttribute('role', 'status'); extensionRoot.append(integrationStatus);
    }
    const status = integrationStatus;
    status.textContent = '正在读取可用集成…';
    try {
      const data = await accountRequest('me');
      if (disposed || ticket !== epoch) return;
      const features = data.features || {}; let count = 0;
      if (features.mcp_catalog_management) { views.push(mountMCPIntegrations(extensionRoot, {suspended, registrationEnabled: features.mcp_catalog_registration === true, ownerId: data.user.id})); count++; }
      if (features.fleet_identity_management) { views.push(mountIntegrationIdentities(extensionRoot, {suspended})); count++; }
      if (features.workflow_summary_management) { views.push(mountWorkflowSummaries(extensionRoot, {suspended, ownerId: data.user.id})); count++; }
      status.textContent = count ? '' : '尚未启用集成管理。可在系统与运行参数中配置相关功能开关，重启后生效。';
      const link = el('a', '配置集成运行开关', 'button-secondary'); link.href = pagePath('settings', 'runtime'); extensionRoot.append(link);
      integrationState = 'loaded';
    } catch (error) {
      if (disposed || ticket !== epoch) return;
      integrationState = 'failed';
      status.replaceChildren(el('span', errorText(error)), button('重试加载集成', loadIntegrations));
    }
  }
  const dispose = () => { disposed = true; backdropPointer = null; window.clearTimeout(closeTimer); epoch++; views.forEach(view => view?.()); views = []; dialog.close(); dialog.remove(); root.classList.remove('settings-page'); };
  dispose.suspend = () => {
    suspended = true;
    backdropPointer = null;
    window.clearTimeout(closeTimer);
    closeTimer = null;
    closing = false;
    dialog.classList.remove('is-closing');
    for (const property of ['--settings-exit-opacity', '--settings-exit-transform', '--settings-backdrop-exit-opacity']) dialog.style.removeProperty(property);
    dialog.close();
    views.forEach(view => view?.suspend?.());
  };
  dispose.resume = ({animateEntrance = true} = {}) => {
    suspended = false;
    dialog.classList.toggle('is-section-navigation', !animateEntrance);
    if (!dialog.open) dialog.showModal();
    if (!animateEntrance) focusCategory();
    views.forEach(view => view?.resume?.());
    if (integrationState === 'failed') loadIntegrations();
  };
  return dispose;
}
