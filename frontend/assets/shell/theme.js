const key = 'agent-fleet-theme';
const media = window.matchMedia('(prefers-color-scheme: dark)');
let choice = 'system';
try { choice = localStorage.getItem(key) || choice; } catch (_) { /* Storage may be disabled. */ }
if (!['light', 'dark', 'system'].includes(choice)) choice = 'system';
function apply() {
  document.documentElement.dataset.resolvedTheme = choice === 'system' ? (media.matches ? 'dark' : 'light') : choice;
  document.querySelectorAll('[data-theme]').forEach(button => {
    button.setAttribute('aria-pressed', String(button.dataset.theme === choice));
  });
}
export function setTheme(value) {
  choice = value;
  try { localStorage.setItem(key, choice); } catch (_) { /* Theme still works for this page. */ }
  apply();
}
apply();
media.addEventListener('change', apply);
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('[data-theme]').forEach(button => button.addEventListener('click', () => setTheme(button.dataset.theme)));
  apply();
});
