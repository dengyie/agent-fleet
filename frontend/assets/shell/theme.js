import {uiIcon} from '../routes.js';
import {mountMenu} from './menus.js';
const key = 'agent-fleet-theme';
const media = typeof window === 'undefined' ? null : window.matchMedia('(prefers-color-scheme: dark)');
const labels = {light: '浅色', dark: '深色', system: '自动 · 跟随系统'};
let choice = 'system';
try { choice = localStorage.getItem(key) || choice; } catch (_) { /* Storage may be disabled. */ }
if (!Object.hasOwn(labels, choice)) choice = 'system';
export function getTheme() { return choice; }
function apply() {
  document.documentElement.dataset.resolvedTheme = choice === 'system' ? (media.matches ? 'dark' : 'light') : choice;
  document.querySelectorAll('[data-theme]').forEach(button => {
    button.setAttribute('aria-checked', String(button.dataset.theme === choice));
  });
  const trigger = document.getElementById('theme-menu-trigger');
  if (trigger) {
    trigger.title = '外观主题：' + labels[choice];
    trigger.querySelector('[data-theme-icon]').replaceChildren(uiIcon({light: 'sun', dark: 'moon', system: 'monitor'}[choice], {size: 18}));
  }
}
export function setTheme(value) {
  if (!Object.hasOwn(labels, value)) return;
  choice = value;
  try { localStorage.setItem(key, choice); } catch (_) { /* Theme still works for this page. */ }
  apply();
  window.dispatchEvent(new Event('fleet-theme-changed'));
}
if (media) {
  apply();
  media.addEventListener('change', apply);
  window.addEventListener('storage', event => {
    if (event.key === key) setTheme(event.newValue || 'system');
  });
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-theme]').forEach(button => button.addEventListener('click', () => setTheme(button.dataset.theme)));
    mountMenu(document.getElementById('theme-menu-trigger'), document.getElementById('theme-menu'));
    apply();
  });
}
