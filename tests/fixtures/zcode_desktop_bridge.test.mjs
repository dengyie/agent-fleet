import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { startBridge } from '../../integrations/zcode/plugins/agent-fleet-bridge/bridge.mjs';

async function fixture(t, send) {
  const dataPath = await mkdtemp(join(tmpdir(), 'fleet-zcode-test-'));
  const calls = [], observations = [];
  const ctx = {
    extension: { id: 'agent-fleet-bridge@agent-fleet', dataPath },
    workbench: { visibleSessions: async () => [{ sessionId: 'sess_original', focused: true, isDraft: false }] },
    sessions: {
      sendToSession: async input => {
        calls.push(input);
        if (send) return send(input);
        return { requestId: input.requestId, session: { kind: 'app', sessionId: input.sessionId },
          operation: { sessionId: input.sessionId, session: { kind: 'app', sessionId: input.sessionId }, commandId: 'cmd_original' },
          admission: 'accepted', delivery: 'queued' };
      },
      waitSessions: async input => { observations.push(input); return { status: 'timed_out', operations: [{ status: 'running', operation: input.operations[0] }] }; },
      listSessions: async () => ({ sessions: [] }),
      readSession: async input => ({ session: { sessionId: input.sessionId }, messages: [{ role: 'user', text: 'delivered' }], nextCursor: 'next' }),
    },
  };
  const bridge = await startBridge(ctx);
  t.after(() => bridge.close());
  async function request(path, payload, token = bridge.endpoint.token) {
    const response = await fetch(bridge.endpoint.url + path, {
      method: payload === undefined ? 'GET' : 'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      ...(payload === undefined ? {} : { body: JSON.stringify(payload) }),
    });
    return { status: response.status, body: await response.json() };
  }
  return { bridge, ctx, request, calls, observations, dataPath };
}
const message = { session_id: 'sess_original', request_id: 'fleet-command-1', text: 'Confirm receipt only.' };

test('targets the original conversation and deduplicates concurrent submissions', async t => {
  const f = await fixture(t);
  const responses = await Promise.all([f.request('/send', message), f.request('/send', message)]);
  assert.equal(f.calls.length, 1);
  assert.deepEqual(f.calls[0], { sessionId: 'sess_original', requestId: 'fleet-command-1', prompt: message.text });
  assert.deepEqual(responses[0], responses[1]);
  assert.equal(responses[0].body.receipt.delivery, 'queued');
  assert.equal((await f.request('/send', { ...message, text: 'different' })).status, 409);
  assert.equal(f.calls.length, 1);
  const ledger = await readFile(join(f.dataPath, 'receipts', message.request_id + '.json'), 'utf8');
  assert.ok(!ledger.includes(message.text));
});

test('receipt checks observe the original command and never resend', async t => {
  const f = await fixture(t);
  await f.request('/send', message);
  const result = await f.request('/receipts/' + message.request_id);
  assert.equal(result.body.observation.operations[0].status, 'running');
  assert.equal(f.observations[0].operations[0].commandId, 'cmd_original');
  assert.equal(f.calls.length, 1);
});

test('an ambiguous Host failure is durable and not retried on restart', async t => {
  const f = await fixture(t, async () => { throw Object.assign(new Error('private detail'), { code: 'host-unavailable' }); });
  const first = await f.request('/send', message);
  assert.equal(first.body.status, 'unknown');
  assert.ok(!JSON.stringify(first).includes('private detail'));
  await f.bridge.close();
  const bridge2 = await startBridge(f.ctx);
  t.after(() => bridge2.close());
  const response = await fetch(bridge2.endpoint.url + '/send', { method: 'POST',
    headers: { Authorization: `Bearer ${bridge2.endpoint.token}`, 'Content-Type': 'application/json' }, body: JSON.stringify(message) });
  assert.equal((await response.json()).status, 'unknown');
  assert.equal(f.calls.length, 1);
});

test('rejects unauthenticated, malformed and path-traversal requests before sending', async t => {
  const f = await fixture(t);
  assert.equal((await f.request('/send', message, 'wrong')).status, 401);
  assert.equal((await f.request('/send', { ...message, request_id: '../other' })).status, 400);
  assert.equal((await f.request('/send', { ...message, text: '   ' })).status, 400);
  assert.equal((await f.request('/send', { ...message, session_id: '' })).status, 400);
  assert.equal(f.calls.length, 0);
  assert.equal((await f.request('/visible')).body.sessions[0].sessionId, 'sess_original');
});

test('supports native message reads and pagination without using the send path', async t => {
  const f = await fixture(t);
  const result = await f.request('/messages?session_id=sess_original');
  assert.equal(result.body.session.sessionId, 'sess_original');
  assert.equal(result.body.messages[0].text, 'delivered');
  assert.equal(result.body.nextCursor, 'next');
  assert.equal(f.calls.length, 0);
});

test('successful durable receipt survives extension restart without duplicate dispatch', async t => {
  const f = await fixture(t);
  const first = await f.request('/send', message);
  await f.bridge.close();
  const second = await startBridge(f.ctx);
  t.after(() => second.close());
  const response = await fetch(second.endpoint.url + '/send', { method: 'POST',
    headers: { Authorization: `Bearer ${second.endpoint.token}`, 'Content-Type': 'application/json' }, body: JSON.stringify(message) });
  assert.deepEqual(await response.json(), first.body);
  assert.equal(f.calls.length, 1);
});

test('rejected admission is never reported as delivered or observed as a running operation', async t => {
  const f = await fixture(t, async input => ({ requestId: input.requestId, admission: 'rejected', delivery: 'none' }));
  const result = await f.request('/send', message);
  assert.equal(result.body.status, 'rejected');
  const receipt = await f.request('/receipts/' + message.request_id);
  assert.equal(receipt.body.receipt.delivery, 'none');
  assert.equal(f.observations.length, 0);
});

test('Host permission errors are explicit and receipts remain queryable', async t => {
  const f = await fixture(t, async () => { throw Object.assign(new Error('denied'), { code: 'not-granted' }); });
  const result = await f.request('/send', message);
  assert.equal(result.body.status, 'rejected');
  assert.equal(result.body.error_code, 'not-granted');
  assert.equal((await f.request('/receipts/' + message.request_id)).body.status, 'rejected');
});

test('closing the extension revokes the descriptor and releases the listening socket', async t => {
  const f = await fixture(t);
  await f.bridge.close();
  const descriptor = JSON.parse(await readFile(join(f.dataPath, 'bridge.json'), 'utf8'));
  assert.equal(descriptor.active, false);
  assert.equal(descriptor.token, undefined);
  await assert.rejects(fetch(f.bridge.endpoint.url + '/health'));
});
