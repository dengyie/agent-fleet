import {getTheme, setTheme} from '../../shell/theme.js';
import {uiIcon} from '../../routes.js';
import {el, panel} from './forms.js';

export function mountAppearance(root) {
  const section = panel(root, '外观主题', '选择适合你的配色。偏好保存在当前浏览器，与右上角的主题菜单同步。', 'sun');
  const choices = el('fieldset', null, 'settings-theme-choices');
  choices.append(el('legend', '配色方案'));
  const radios = [];
  for (const [value, title, description, icon] of [
    ['light', '浅色', '清爽明亮，适合日间使用', 'sun'],
    ['dark', '深色', '柔和暗调，适合低光环境', 'moon'],
    ['system', '自动', '跟随设备的外观设置', 'monitor'],
  ]) {
    const label = el('label', null, 'settings-theme-card'); label.dataset.scheme = value;
    const input = el('input'); input.type = 'radio'; input.name = 'settings-theme'; input.value = value;
    input.setAttribute('aria-label', title); input.checked = getTheme() === value;
    input.addEventListener('change', () => { if (input.checked) setTheme(value); });
    const preview = el('span', null, 'settings-theme-preview'); preview.setAttribute('aria-hidden', 'true');
    const sidebar = el('span', null, 'settings-theme-sidebar'); sidebar.append(el('i'), el('i'), el('i'));
    const chat = el('span', null, 'settings-theme-chat'); chat.append(el('i'), el('i'), el('i'));
    preview.append(sidebar, chat);
    const caption = el('span', null, 'settings-theme-caption'); caption.append(uiIcon(icon, {size: 16}), el('strong', title));
    label.append(input, preview, caption, el('small', description)); choices.append(label); radios.push(input);
  }
  section.append(choices, el('p', '自动模式会随系统切换，无需手动调整。', 'settings-notice'));
  const syncTheme = () => radios.forEach(input => { input.checked = input.value === getTheme(); });
  window.addEventListener('fleet-theme-changed', syncTheme);
  return () => window.removeEventListener('fleet-theme-changed', syncTheme);
}
