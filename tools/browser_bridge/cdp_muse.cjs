'use strict';
// Only fixed DOM operations on one logged-in Muse main chat are exposed.
// No caller selectors, URLs, JavaScript, cookies, storage, or private API calls.
const ENDPOINT = 'http://127.0.0.1:9222';
const error = code => Object.assign(new Error(code), {code});
const fail = code => { throw error(code); };
const MAX_BYTES = 2 * 1024 * 1024;

function validateRequest(request) {
  if (!request || typeof request !== 'object' || Array.isArray(request)) fail('request_invalid');
  const keys = request.op === 'send' ? ['op', 'target_id', 'cursor', 'prompt'] : ['op', 'target_id'];
  if (!['snapshot', 'send'].includes(request.op) || Object.keys(request).some(k => !keys.includes(k))) fail('operation_forbidden');
  if (request.target_id !== undefined && (typeof request.target_id !== 'string' || !/^[\w-]{1,128}$/.test(request.target_id))) fail('target_invalid');
  if (request.op === 'send' && (!request.target_id || typeof request.cursor !== 'string' || request.cursor.length > 256
      || typeof request.prompt !== 'string' || !request.prompt.trim() || Array.from(request.prompt).length > 10000
      || /[\x00-\x08\x0b\x0c\x0e-\x1f]/.test(request.prompt))) fail('request_invalid');
}

function selectTarget(targets, targetId) {
  if (!Array.isArray(targets)) fail('devtools_invalid_response');
  const matches = targets.filter(t => t.type === 'page' && t.url === 'https://muse.ai/' && (!targetId || t.id === targetId));
  if (matches.length !== 1) fail(matches.length ? 'muse_tabs_ambiguous' : 'muse_tab_not_found');
  const target = matches[0];
  const url = new URL(target.webSocketDebuggerUrl);
  if (url.protocol !== 'ws:' || url.hostname !== '127.0.0.1' || url.port !== '9222'
      || url.username || url.password || url.search || url.hash || url.pathname !== `/devtools/page/${target.id}`) fail('devtools_endpoint_invalid');
  return target;
}

async function discover(targetId) {
  const response = await fetch(`${ENDPOINT}/json/list`, {redirect: 'error', signal: AbortSignal.timeout(3000)});
  if (!response.ok) fail('devtools_unavailable');
  const reader = response.body.getReader();
  let bytes = 0; const chunks = [];
  for (;;) {
    const {done, value} = await reader.read();
    if (done) break;
    bytes += value.length;
    if (bytes > MAX_BYTES) { await reader.cancel(); fail('devtools_invalid_response'); }
    chunks.push(Buffer.from(value));
  }
  return selectTarget(JSON.parse(Buffer.concat(chunks).toString('utf8')), targetId);
}

async function connect(target) {
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  try {
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(error('cdp_timeout')), 3000);
      ws.addEventListener('open', () => { clearTimeout(timer); resolve(); }, {once: true});
      ws.addEventListener('error', () => { clearTimeout(timer); reject(error('cdp_unavailable')); }, {once: true});
    });
  } catch (cause) { ws.close(); throw cause; }
  let sequence = 0;
  return {close: () => ws.close(), call: (method, params) => new Promise((resolve, reject) => {
    const id = ++sequence;
    const finish = (cause, result) => {
      clearTimeout(timer); ws.removeEventListener('message', receive); ws.removeEventListener('close', closed);
      if (cause) reject(cause); else resolve(result);
    };
    const closed = () => finish(error('cdp_disconnected'));
    const receive = event => {
      if (typeof event.data !== 'string' || Buffer.byteLength(event.data) > MAX_BYTES) return finish(error('cdp_response_too_large'));
      let reply; try { reply = JSON.parse(event.data); } catch { return finish(error('cdp_invalid_response')); }
      if (reply.id === id) finish(reply.error ? error('cdp_error') : null, reply.result);
    };
    const timer = setTimeout(() => finish(error('cdp_timeout')), 3000);
    ws.addEventListener('message', receive); ws.addEventListener('close', closed);
    ws.send(JSON.stringify({id, method, params}));
  })};
}

// Each evaluation checks the current origin and main-chat DOM again. Navigation
// or switching into a side chat invalidates the operation.
const GUARD = `
  if (location.href !== 'https://muse.ai/') return {error:'page_changed'};
  const logs = [...document.querySelectorAll('[role="log"][data-hatch-chat-column="true"]')];
  const fields = [...document.querySelectorAll('[data-hatch-composer-chrome] textarea')];
  if (logs.length !== 1 || fields.length !== 1) return {error:'muse_dom_unavailable'};
  const log = logs[0], field = fields[0];
  const items = [...log.querySelectorAll('[data-message-item]')];
  const cursor = items.at(-1)?.getAttribute('data-message-id') || '';
`;
const SNAPSHOT = `(() => { ${GUARD}
  return {url:'https://muse.ai/', ready:!field.disabled && !field.readOnly,
    draft:field.value.length > 0, messages:items.slice(-100).map(item => {
      const body = item.getAttribute('data-message-role') === 'user'
        ? item.querySelector('p.whitespace-pre-wrap')
        : item.querySelector('[data-hatch-assistant-message-body]');
      return {id:item.getAttribute('data-message-id'), role:item.getAttribute('data-message-role'),
        text:Array.from(body?.innerText || '').slice(0,12288).join(''), busy:!!item.querySelector('[aria-busy="true"]')};
    })};
})()`;

async function evaluate(call, expression) {
  const result = await call('Runtime.evaluate', {expression, returnByValue:true});
  if (result.exceptionDetails || !result.result || typeof result.result.value !== 'object' || result.result.value === null) fail('muse_dom_unavailable');
  const value = result.result.value;
  if (value.error) fail(/^[a-z_]{1,64}$/.test(value.error) ? value.error : 'muse_dom_unavailable');
  return value;
}

async function operate(call, request, targetId) {
  validateRequest(request);
  if (request.op === 'snapshot') return {...await evaluate(call, SNAPSHOT), target_id:targetId};
  // JSON encodes prompt data; callers cannot add executable expressions.
  const data = JSON.stringify({prompt:request.prompt, cursor:request.cursor});
  await evaluate(call, `(() => { ${GUARD}
    const data = ${data};
    if (cursor !== data.cursor) return {error:'conversation_changed'};
    if (field.value) return {error:'composer_has_draft'};
    if (field.disabled || field.readOnly) return {error:'composer_unavailable'};
    if (log.querySelector('[aria-busy="true"]')) return {error:'agent_busy'};
    field.focus();
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(field,data.prompt);
    field.dispatchEvent(new Event('input',{bubbles:true}));
    return {prepared:true};
  })()`);
  // React updates the send control after the input event. Re-check all mutable
  // state immediately before clicking the observed, fixed send button.
  let sent = false;
  for (let attempt = 0; attempt < 10 && !sent; attempt++) {
    const result = await evaluate(call, `(() => { ${GUARD}
      const data = ${data};
      if (cursor !== data.cursor) return {error:'conversation_changed'};
      if (field.value !== data.prompt) return {error:'composer_changed'};
      if (field.disabled || field.readOnly) return {error:'composer_unavailable'};
      if (log.querySelector('[aria-busy="true"]')) return {error:'agent_busy'};
      const root = field.closest('[data-hatch-composer-chrome]');
      const buttons = [...root.querySelectorAll('button')].filter(b =>
        ['发送','发送消息','Send','Send message'].includes(b.getAttribute('aria-label')));
      if (buttons.length !== 1 || buttons[0].disabled) return {sent:false};
      buttons[0].click(); return {sent:true};
    })()`);
    sent = result.sent === true;
    if (!sent) await new Promise(resolve => setTimeout(resolve, 100));
  }
  if (!sent) fail('send_control_unavailable');
  return {sent:true};
}

async function main(request) {
  validateRequest(request);
  const target = await discover(request.target_id);
  const connection = await connect(target);
  try { return await operate(connection.call, request, target.id); }
  finally { connection.close(); }
}

if (require.main === module) {
  (async () => {
    let text = '';
    process.stdin.setEncoding('utf8');
    for await (const chunk of process.stdin) {
      text += chunk;
      if (Buffer.byteLength(text) > 64 * 1024) fail('request_too_large');
    }
    return main(JSON.parse(text));
  })().then(result => process.stdout.write(JSON.stringify({ok:true,result}) + '\n'),
    cause => process.stdout.write(JSON.stringify({ok:false,error:cause.code || 'bridge_error'}) + '\n'));
}
module.exports = {main, operate, selectTarget, validateRequest};
