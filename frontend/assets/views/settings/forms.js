export function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = text;
  if (className) node.className = className;
  return node;
}
export function button(text, action, className = 'button-secondary') {
  const node = el('button', text, className); node.type = 'button';
  node.addEventListener('click', action); return node;
}
export function field(parent, name, title, value = '', {type = 'text', options, help, required = false, readOnly = false} = {}) {
  const label = el('label', null, 'settings-field'); label.append(el('span', title));
  const control = el(options ? 'select' : type === 'textarea' ? 'textarea' : 'input');
  control.name = name; control.setAttribute('aria-label', title);
  if (options) for (const [key, text] of options) { const option = el('option', text); option.value = key; control.append(option); }
  else if (type !== 'textarea') control.type = type;
  if (type === 'checkbox') control.checked = value === true;
  else control.value = value ?? '';
  if (type === 'password') { control.autocomplete = 'new-password'; control.spellcheck = false; }
  control.required = required; control.readOnly = readOnly;
  if (type === 'textarea') { control.rows = 4; control.spellcheck = false; }
  label.append(control); if (help) label.append(el('small', help)); parent.append(label); return control;
}
export function panel(parent, title, description) {
  const node = el('section', null, 'panel settings-panel'); node.append(el('h2', title));
  if (description) node.append(el('p', description, 'settings-description')); parent.append(node); return node;
}
export function jsonValue(field) {
  const value = JSON.parse(field.value || '{}');
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('请输入 JSON 对象');
  return value;
}
export function errorText(error) { return error.detail || error.message || '保存失败，请重新加载检查当前状态'; }
