import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
if (!globalThis.crypto) globalThis.crypto = webcrypto;
class Element {
  children = []; listeners = {}; dataset = {}; disabled = false; textContent = '';
  constructor(tag) { this.tagName = tag; }
  append(...children) { this.children.push(...children); }
  setAttribute(name, value) { this[name] = value; }
  addEventListener(name, listener) { this.listeners[name] = listener; }
  click() { if (!this.disabled) this.listeners.click?.(); }
  find(predicate) { return predicate(this) ? this : this.children.map(child => child.find(predicate)).find(Boolean); }
}
globalThis.document = {createElement: tag => new Element(tag)};
const {renderQuestions, disposeQuestions, suspendQuestions} = await import(process.argv[2]);
const scenario = process.argv[3];
const host = new Element('root');
const state = {disposed:false};
const session = {session_id:'session-a',managed:true,status:'running',current_state:'waiting_for_user',capabilities:['interactive_questions'],allowed_actions:['append_user_turn']};
let id = 'q-1', calls = [];
const api = {answerSessionQuestion: async (sessionId, body) => {
  calls.push(structuredClone(body));
  if (scenario === 'bounded_submissions' || calls.length === 1) {
    const error = new Error('fixture error');
    error.status = scenario === 'validation_retry' ? 400 : 0;
    error.code = scenario === 'validation_retry' ? 'invalid_interaction_answer' : 'network_error';
    throw error;
  }
  return {command_id:'command-a', status:'queued'};
}};
function paint() {
  host.children = [];
  renderQuestions(host, {events:[{kind:'interaction_request', payload:{request_id:id, question_type:'single', prompt:'Choose',
    options:[{id:'a',label:'First'},{id:'b',label:'Second'}],allow_custom:false}}],session,api,state,paint,enabled:true});
}
const button = text => host.find(element => element.tagName === 'button' && element.textContent === text);
const flush = () => new Promise(resolve => setImmediate(resolve));
if (scenario === 'receipt_lifecycle') {
  const timers = new Map(); let nextTimer = 0, reads = 0;
  globalThis.setTimeout = callback => { timers.set(++nextTimer, callback); return nextTimer; };
  globalThis.clearTimeout = timer => timers.delete(timer);
  api.answerSessionQuestion = async () => ({command_id:'command-a', status:'queued'});
  api.getControlReceipt = async () => ({status:++reads > 35 ? 'succeeded' : 'executing'});
  paint(); button('First').click(); await flush();
  for (let i = 0; i < 40; i++) {
    const queued = [...timers.values()]; timers.clear();
    queued.forEach(callback => callback()); await flush();
  }
  assert.equal(state.questions.get(id).status, 'succeeded', 'long-running question receipts keep polling to a terminal result');
  assert.equal(reads, 36);
  assert.equal(timers.size, 0, 'terminal receipt releases polling timers');
  id = 'suspend-question';
  let releaseFirst, currentReads = 0;
  const oldReceipt = new Promise(resolve => { releaseFirst = resolve; });
  api.getControlReceipt = async () => ++currentReads === 1 ? oldReceipt : {status:'succeeded'};
  paint(); button('First').click(); await flush();
  state.suspended = true; suspendQuestions(state);
  assert.equal(timers.size, 0);
  state.suspended = false; paint(); await flush();
  assert.equal(state.questions.get(id).status, 'succeeded');
  releaseFirst({status:'executing'}); await flush();
  assert.equal(state.questions.get(id).status, 'succeeded', 'late response cannot undo resumed question receipt');
  assert.equal(timers.size, 0);
}
paint();
if (scenario === 'network_retry') {
  button('First').click(); await flush();
  assert.equal(button('Second').disabled, true, 'uncertain submit must freeze the editable answer');
  assert.ok(button('重试同一答案'), 'all question types must expose an explicit same-answer retry');
  button('Second').click();
  button('重试同一答案').click(); await flush();
  assert.equal(calls.length, 2);
  assert.deepEqual(calls[1], calls[0], 'retry payload and client_token must be identical');
}
if (scenario === 'validation_retry') {
  button('First').click(); await flush();
  assert.equal(button('Second').disabled, false, 'definitive input rejection permits a corrected answer');
  button('Second').click(); await flush();
  assert.notEqual(calls[1].client_token, calls[0].client_token, 'a changed answer receives a fresh token');
  assert.deepEqual(calls[1].selected, ['b']);
}
if (scenario === 'bounded_pages') {
  button('First').click(); await flush();
  for (let index = 2; index <= 205; index++) { id = 'q-' + index; paint(); }
  assert.ok(state.questions.size <= 100, 'paging must not grow draft state without a bound');
  id = 'q-1'; paint();
  assert.ok(button('重试同一答案'), 'paging retains an unresolved submission');
  button('重试同一答案').click(); await flush();
  assert.deepEqual(calls[1], calls[0]);
}
if (scenario === 'bounded_submissions') {
  for (let index = 1; index <= 105; index++) {
    id = 'q-' + index; paint(); button('First').click(); await flush();
  }
  assert.ok(state.questions.size <= 100, 'unresolved submissions must be bounded without evicting their frozen answers');
  assert.ok(calls.length <= 100, 'capacity blocks new submissions until a result is resolved');
  id = 'q-1'; paint(); assert.ok(button('重试同一答案'));
}
state.disposed = true; disposeQuestions(state);
