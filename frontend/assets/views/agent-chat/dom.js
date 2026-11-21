export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
export function button(text, onClick, className = 'button-secondary') {
  const node = el('button', className, text);
  node.type = 'button'; node.addEventListener('click', onClick);
  return node;
}
export function errorText(error) {
  return error?.detail || error?.message || error?.code || '暂时无法连接，请重试';
}
export function link(text, href, className = 'button-secondary') {
  const node = el('a', className, text); node.href = href; return node;
}
