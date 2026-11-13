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
  removeAttribute(key) { delete this.attributes[key]; }
  toggleAttribute(key, force) {
    const enabled = force === undefined ? !(key in this.attributes) : Boolean(force);
    if (enabled) this.attributes[key] = ''; else delete this.attributes[key];
    return enabled;
  }
  remove() { this.parentNode?.removeChild(this); }
  addEventListener(key, fn) { this.listeners[key] = fn; }
  dispatchEvent(event) { this.listeners[event.type]?.(event); return true; }
}
globalThis.document = {
  createElement: tag => new Element(tag), createElementNS: (_, tag) => new Element(tag),
  createTextNode: text => { const node = new Element('#text'); node.textContent = text; return node; },
  hidden: false,
  addEventListener() {},
  removeEventListener() {},
};
globalThis.DOMParser = class { parseFromString() { return {documentElement: new Element('svg')}; } };
const [origin, modulePath, scenario] = process.argv.slice(2);
globalThis.window = Object.assign(new EventTarget(), {FleetConfig: {apiBaseUrl: origin + '/api'}, history: {replaceState() {}}});
const realFetch = globalThis.fetch;
const requests = [];
const replies = [];
const runPolls = [];
const replacedUrls = [];
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
globalThis.window.history.replaceState = (_, __, url) => replacedUrls.push(String(url));
globalThis.fetch = async (url, options) => {
  if (/\/runs\/[^/?]+(\/events)?(\?|$)/.test(String(url))) runPolls.push(String(url));
  const response = await realFetch(url, options);
  if (scenario === 'suspend' && /\/runs\/[^/?]+(\?|$)/.test(String(url)) && response.ok) {
    // Pin the Run as active so only suspend/resume can stop the tracker.
    const body = await response.json();
    if (body.run) body.run.state = 'running'; else body.state = 'running';
    return new Response(JSON.stringify(body), {status: response.status, headers: response.headers});
  }
  if (scenario === 'provider-503' && /\/runs\/[^/?]+(\?|$)/.test(String(url)) && response.ok) {
    const body = await response.json();
    body.state = 'unknown'; body.result_text = '模型调用结果待确认：transient_http（HTTP 503）';
    return new Response(JSON.stringify(body), {status: 200, headers: response.headers});
  }
  if (scenario === 'provider-503' && /\/conversations\/[^/?]+(\?|$)/.test(String(url)) && response.ok) {
    const body = await response.json();
    for (const run of body.conversation?.runs || []) {
      run.state = 'unknown'; run.result_text = '模型调用结果待确认：transient_http（HTTP 503）';
    }
    return new Response(JSON.stringify(body), {status: 200, headers: response.headers});
  }
  if (String(url).endsWith('/turns')) {
    requests.push(JSON.parse(options.body));
    const body = await response.clone().json();
    replies.push(body);
    if (scenario === 'retry' && requests.length === 1) {
      await response.text();
      throw new Error('connection lost after Hub committed');
    }
    if (scenario === 'retry-503' && requests.length === 1) {
      return new Response(JSON.stringify({error: 'temporarily_unavailable'}), {status: 503});
    }
  }
  if ((scenario === 'optimistic' || scenario === 'existing-optimistic') && /\/turns$/.test(String(url))) {
    await sleep(500);
  }
  return response;
};
const {mountAssistant} = await import(pathToFileURL(modulePath));
let conversationId = null;
if (scenario === 'existing-optimistic') {
  const response = await realFetch(origin + '/api/platform/v1/conversations', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({workspace_id: 'home'})
  });
  conversationId = (await response.json()).conversation.conversation_id;
}
const root = new Element('main');
const teardown = mountAssistant(root, {conversationId,
  onConversationCreated: id => window.history.replaceState(null, '', '/conversation/' + id)});
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
const submission = submit();
if (scenario === 'optimistic' || scenario === 'existing-optimistic') {
  await sleep(40);
  const messages = find(n => n.className === 'assistant-messages');
  if (!messages.textContent.includes('Submit one turn')) throw new Error('optimistic user message missing before submit response');
  if (!messages.textContent.includes('正在发送')) throw new Error('optimistic submission status missing in transcript before submit response');
  if (input.value !== '') throw new Error('composer should clear immediately');
  if (scenario === 'optimistic' && !replacedUrls.length) throw new Error('conversation route missing before append response');
}
await submission;
if (scenario === 'retry' || scenario === 'retry-503') {
  const messages = find(n => n.className === 'assistant-messages');
  if (!messages.textContent.includes('Submit one turn') || !messages.textContent.includes('提交结果待确认')) throw new Error('uncertain submission should keep message and status');
  // Changes to controls while resolving uncertain acceptance must not change
  // the pending request. A real user sees these controls disabled.
  model.value = 'model-default';
  await submit();
}
await until(() => replies.length === (scenario.startsWith('retry') ? 2 : 1));
if (scenario === 'provider-503') {
  await until(() => find(n => n.className === 'assistant-messages').textContent.includes('transient_http'));
  const button = find(n => n.className === 'button-secondary assistant-run-refresh');
  if (!button || button.hidden) throw new Error('unknown run needs read-only recovery action');
  await button.listeners.click();
  if (requests.length !== 1) throw new Error('refresh must never resubmit the model request');
} else if (scenario !== 'suspend') {
  const messages = find(n => n.className === 'assistant-messages');
  if (messages.children.filter(n => n.className === 'assistant-message user').length !== 1) throw new Error('accepted turn should replace, not duplicate optimistic message');
  if (!messages.textContent.includes('已发送，等待助手')) throw new Error('queued run status missing from transcript');
}
if (scenario === 'suspend') {
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  await until(() => runPolls.length >= 2);
  teardown.suspend();
  await sleep(300); // let an in-flight poll settle
  const suspendedAt = runPolls.length;
  await sleep(2600); // > 2 poll intervals of 1.2s
  const whileSuspended = runPolls.length - suspendedAt;
  teardown.resume();
  await until(() => runPolls.length > suspendedAt + whileSuspended);
  teardown();
  console.log(JSON.stringify({requests, replies, text: root.textContent, whileSuspended, resumed: true}));
  process.exit(0);
}
console.log(JSON.stringify({requests, replies, text: root.textContent, replacedUrls}));
process.exit(0);
