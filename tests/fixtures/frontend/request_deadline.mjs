import assert from 'node:assert/strict';
import http from 'node:http';

// Self-bounded: report the stage that stalled instead of letting the parent
// pytest subprocess timeout hide where a slow CI runner got stuck.
let stage = 'startup';
const watchdog = setTimeout(() => {
  console.error(`request_deadline: stalled at stage "${stage}"`);
  process.exit(2);
}, 12000);
watchdog.unref();

const server = http.createServer((req, res) => {
  res.writeHead(req.url.endsWith('error') ? 503 : 200, {'Content-Type': 'application/json'});
  res.flushHeaders();
  if (req.url.endsWith('fast')) res.end('{"ok":true}');
  else {
    const timer = setTimeout(() => res.end('{"ok":true}'), 900);
    res.on('close', () => clearTimeout(timer));
  }
});
stage = 'listen';
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
globalThis.window = {FleetConfig: {apiBaseUrl: origin + '/api'},
  sessionStorage: {getItem: () => ''}, location: {href: origin + '/', origin}};
try {
  stage = 'import';
  const {platformRequest} = await import('../../../frontend/assets/api/client.js');
  for (const path of ['/slow', '/error']) {
    stage = `deadline ${path}`;
    await assert.rejects(platformRequest(path, {timeoutMs: 150}),
      error => error.kind === 'timeout' && error.code === 'request_timeout');
  }
  stage = 'fast';
  assert.deepEqual(await platformRequest('/fast', {timeoutMs: 1000}), {ok: true});
  // A runtime whose body read ignores the abort signal must still honour the
  // deadline (the read is raced against the signal, not trusted to observe it).
  stage = 'stuck body';
  const realFetch = globalThis.fetch;
  globalThis.fetch = async () => ({status: 200, ok: true, json: () => new Promise(() => {})});
  try {
    await assert.rejects(platformRequest('/stuck', {timeoutMs: 100}),
      error => error.kind === 'timeout' && error.code === 'request_timeout');
  } finally {
    globalThis.fetch = realFetch;
  }
} finally {
  stage = 'teardown';
  server.closeAllConnections();
  // Keep-alive sockets from the fetch pool must not keep the process alive.
  await Promise.race([
    new Promise(resolve => server.close(resolve)),
    new Promise(resolve => setTimeout(resolve, 1000).unref()),
  ]);
}
process.exit(0);
