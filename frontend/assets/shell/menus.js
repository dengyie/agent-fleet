// Native popovers own light-dismiss, Escape and top-layer rendering. Keep the
// keyboard/focus contract shared by the theme and account menus.
export function mountMenu(trigger, popover) {
  const items = () => [...popover.querySelectorAll('[role^="menuitem"]')].filter(item => !item.disabled && !item.hidden);
  const open = () => popover.matches(':popover-open');
  const onBeforeToggle = event => {
    trigger.setAttribute('aria-expanded', String(event.newState === 'open'));
    if (event.newState !== 'open') return;
    const options = items();
    const selected = options.find(item => item.getAttribute('aria-checked') === 'true') || options[0];
    // Native popover autofocus runs synchronously while opening. The later
    // toggle event must not steal focus back after an Arrow/Home/End key.
    options.forEach(item => item.toggleAttribute('autofocus', item === selected));
  };
  const onToggle = () => {
    trigger.setAttribute('aria-expanded', String(open()));
  };
  const onKey = event => {
    if (!open()) return;
    if (event.key === 'Escape') {
      // Navigation has its own Escape handler for the mobile drawer.
      event.stopPropagation(); event.preventDefault();
      popover.hidePopover(); trigger.focus();
    } else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      event.preventDefault();
      const options = items();
      const current = options.indexOf(document.activeElement);
      const index = event.key === 'Home' ? 0 : event.key === 'End' ? options.length - 1
        : (current + (event.key === 'ArrowDown' ? 1 : -1) + options.length) % options.length;
      options[index]?.focus();
    } else if (event.key === 'Tab') {
      popover.hidePopover(); trigger.focus();
    }
  };
  const onTriggerKey = event => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      popover.showPopover();
    }
  };
  const onSelect = event => {
    const selected = event.target.closest('[role^="menuitem"]');
    // Keep pending logout visible with its status until it completes.
    if (!selected || selected.id === 'operator-logout') return;
    popover.hidePopover(); trigger.focus();
  };
  popover.addEventListener('toggle', onToggle);
  popover.addEventListener('beforetoggle', onBeforeToggle);
  popover.addEventListener('keydown', onKey);
  popover.addEventListener('click', onSelect);
  trigger.addEventListener('keydown', onTriggerKey);
  return () => {
    popover.hidePopover();
    popover.removeEventListener('toggle', onToggle);
    popover.removeEventListener('beforetoggle', onBeforeToggle);
    popover.removeEventListener('keydown', onKey);
    popover.removeEventListener('click', onSelect);
    trigger.removeEventListener('keydown', onTriggerKey);
  };
}
