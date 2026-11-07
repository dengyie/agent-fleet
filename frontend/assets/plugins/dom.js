export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}
export function button(text, action, className = '') {
  const node = el('button', className, text);
  node.type = 'button';
  node.addEventListener('click', action);
  return node;
}
export function field(text, input) {
  const label = el('label', 'plugin-field');
  label.append(el('span', '', text), input);
  return label;
}
export function errorText(error) { return error?.detail || error?.message || '请求失败，请重试'; }
