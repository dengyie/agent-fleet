import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';

const [servicePath] = process.argv.slice(2);
const {createAssistantService} = await import(pathToFileURL(servicePath));
const calls = [];
const api = new Proxy({}, {
  get(_target, method) {
    return (...args) => { calls.push([method, ...args]); return Promise.resolve({method}); };
  }
});
const service = createAssistantService(api);

await service.appendTurn('conversation-1', {text: 'hello', client_token: 'token-1'});
assert.deepEqual(calls, [['appendConversationTurn', 'conversation-1', {text: 'hello', client_token: 'token-1'}]]);
const selectedIds = ['memory-1'];
const payload = service.createTurnRequest({
  text: '  summarize  ', clientToken: 'token-2', modelProfileId: 'profile-1',
  memoryContext: {memoryIds: selectedIds, revisions: {'memory-1': 3}, maxItems: 8, maxBytes: 8192}
});
selectedIds.push('later-change');
assert.deepEqual(payload, {
  text: 'summarize', client_token: 'token-2', overrides: {model_profile_id: 'profile-1'},
  memory_context: {enabled: true, memory_ids: ['memory-1'], revisions: {'memory-1': 3}, max_items: 8, max_bytes: 8192}
});
assert.equal(calls.length, 1, 'building a request must not touch the transport');
console.log('assistant service boundary passed');
