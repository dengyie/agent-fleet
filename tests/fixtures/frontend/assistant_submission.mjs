// Execute the actual ES modules against a disposable local Hub. This minimal
// DOM models only mounting, form controls and event handlers, not layout.
import { pathToFileURL } from 'node:url';

class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.attributes = {}; this.listeners = {}; this.value = ''; this._text = ''; this.disabled = false; }
  appendChild(child) { if (child.parentNode) child.parentNode.removeChild(child); child.parentNode = this; this.children.push(child); return child; }
  insertBefore(child, before) { if (child.parentNode) child.parentNode.removeChild(child); child.parentNode = this; this.children.splice(this.children.indexOf(before), 0, child); return child; }
  getAttribute(key) { return this.attributes[key]; }
  get open() { return 'open' in this.attributes; }
  set open(value) { if (value) this.attributes.open = ''; else delete this.attributes.open; }
  close() { delete this.attributes.open; }
  removeChild(child) { this.children.splice(this.children.indexOf(child), 1); child.parentNode = null; }
  get options() { return this.children; }
  get firstChild() { return this.children[0] || null; }
  querySelector(selector) {
    if (typeof selector !== 'string' || !selector.startsWith('.')) return null;
    const className = selector.slice(1);
    if (String(this.className || '').split(/\s+/).includes(className)) return this;
    for (const child of this.children) {
      const match = child.querySelector(selector);
      if (match) return match;
    }
    return null;
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(key, fn) { this.listeners[key] = fn; }
}
globalThis.document = {
  createElement: tag => new Element(tag), createElementNS: (_, tag) => new Element(tag),
  createTextNode: text => { const node = new Element('#text'); node.textContent = text; return node; },
};
globalThis.DOMParser = class { parseFromString() { return {documentElement: new Element('svg')}; } };
const [origin, modulePath, scenario] = process.argv.slice(2);
globalThis.window = { dispatchEvent() {}, FleetConfig: {apiBaseUrl: origin + '/api'}, history: {replaceState() {}} };
const realFetch = globalThis.fetch;
const requests = [];
const replies = [];
globalThis.fetch = async (url, options) => {
  const response = await realFetch(url, options);
  if (String(url).endsWith('/turns')) {
    requests.push(JSON.parse(options.body));
    const body = await response.clone().json();
    replies.push(body);
    if (scenario === 'retry' && requests.length === 1) {
      await response.text();
      throw new Error('connection lost after Hub committed');
    }
  }
  return response;
};
const {mountAssistant} = await import(pathToFileURL(modulePath));
const root = new Element('main');
mountAssistant(root);
function find(predicate, node = root) {
  if (predicate(node)) return node;
  for (const child of node.children) { const result = find(predicate, child); if (result) return result; }
}
async function until(predicate) {
  for (let i = 0; i < 200; i++) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 10));
  }
  throw new Error('UI did not settle: ' + root.textContent);
}
await until(() => find(n => n.className === 'assistant-run-status')?.textContent === '就绪');
const model = find(n => n.attributes['aria-label'] === '模型');
const composer = find(n => n.className === 'assistant-composer');
const input = find(n => n.tagName === 'textarea', composer);
const submit = () => composer.listeners.submit({preventDefault() {}});
model.value = 'model-selected';
input.value = 'Submit one turn';
await submit();
if (scenario === 'retry') {
  // Changes to controls while resolving uncertain acceptance must not change
  // the pending request. A real user sees these controls disabled.
  model.value = 'model-default';
  await submit();
}
await until(() => replies.length === (scenario === 'retry' ? 2 : 1));
console.log(JSON.stringify({requests, replies, text: root.textContent}));
process.exit(0);
