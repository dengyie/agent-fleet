import { uiIcon } from '../../routes.js';
import { el } from './panels.js';

// Layout only: API requests, permissions and rendered panel content stay with
// mountAssistant. Hiding a panel never unmounts its ongoing run or form state.
export function mountInspector(root, heading, inspector, groups) {
  inspector.id = 'assistant-inspector';
  const tools = el('div', 'assistant-heading-actions');
  const toggle = el('button', 'button-secondary inspector-toggle');
  toggle.type = 'button';
  toggle.setAttribute('aria-label', '工作面板');
  toggle.setAttribute('aria-controls', inspector.id);
  toggle.appendChild(uiIcon('layers', { size: 15 }));
  toggle.appendChild(el('span', null, '工作面板'));
  tools.appendChild(toggle);
  heading.appendChild(tools);

  const header = el('div', 'inspector-header');
  const label = el('div', 'inspector-label');
  label.appendChild(uiIcon('layers', { size: 17 }));
  label.appendChild(el('strong', null, '工作面板'));
  const close = el('button', 'icon-button inspector-close');
  close.type = 'button'; close.setAttribute('aria-label', '收起工作面板');
  close.appendChild(uiIcon('x', { size: 16 }));
  header.appendChild(label); header.appendChild(close); inspector.appendChild(header);
  const tabs = el('div', 'inspector-tabs'); tabs.setAttribute('role', 'tablist'); tabs.setAttribute('aria-label', '工作面板');
  inspector.appendChild(tabs);
  const entries = groups.map(({id, title, sections}) => {
    const button = el('button', 'inspector-tab', title); button.type = 'button'; button.id = 'inspector-tab-' + id;
    const panel = el('div', 'inspector-panel'); panel.id = 'inspector-panel-' + id;
    button.setAttribute('role', 'tab'); button.setAttribute('aria-controls', panel.id);
    panel.setAttribute('role', 'tabpanel'); panel.setAttribute('aria-labelledby', button.id); panel.tabIndex = 0;
    sections.forEach(section => panel.appendChild(section));
    tabs.appendChild(button); inspector.appendChild(panel);
    return {id, button, panel};
  });
  const narrow = window.matchMedia?.('(max-width: 1100px)');
  let open = !narrow?.matches;
  function setOpen(value, restoreFocus = false) {
    open = value; inspector.hidden = !open;
    root.setAttribute('data-inspector-open', String(open));
    toggle.setAttribute('aria-expanded', String(open));
    const modal = open && !!narrow?.matches;
    const chat = root.querySelector?.('.assistant-chat');
    if (chat) chat.inert = modal;
    heading.inert = modal;
    inspector.setAttribute('role', modal ? 'dialog' : 'complementary');
    inspector.setAttribute('aria-modal', String(modal));
    if (restoreFocus) toggle.focus();
  }
  function select(id, reveal = false) {
    for (const entry of entries) {
      const active = entry.id === id;
      entry.button.setAttribute('aria-selected', String(active));
      entry.button.tabIndex = active ? 0 : -1;
      entry.panel.hidden = !active;
    }
    if (reveal) { setOpen(true); if (narrow?.matches) entries.find(entry => entry.id === id).button.focus(); }
  }
  entries.forEach((entry, index) => {
    entry.button.addEventListener('click', () => select(entry.id));
    entry.button.addEventListener('keydown', event => {
      let next;
      if (event.key === 'ArrowRight') next = (index + 1) % entries.length;
      if (event.key === 'ArrowLeft') next = (index + entries.length - 1) % entries.length;
      if (event.key === 'Home') next = 0;
      if (event.key === 'End') next = entries.length - 1;
      if (next === undefined) return;
      event.preventDefault(); select(entries[next].id); entries[next].button.focus();
    });
  });
  toggle.addEventListener('click', () => { setOpen(!open); if (open) entries.find(entry => !entry.panel.hidden).button.focus(); });
  close.addEventListener('click', () => setOpen(false, true));
  inspector.addEventListener('keydown', event => {
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); setOpen(false, true); }
    if (event.key === 'Tab' && narrow?.matches && open) {
      const focusable = [...inspector.querySelectorAll('button, a, input, select, textarea, summary, [tabindex="0"]')].filter(node => !node.disabled && node.tabIndex >= 0 && node.getClientRects().length);
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  const onResize = () => setOpen(!narrow.matches, narrow.matches && inspector.contains(document.activeElement));
  narrow?.addEventListener('change', onResize);
  select(entries[0].id); setOpen(open);
  return { select, dispose: () => narrow?.removeEventListener('change', onResize) };
}
