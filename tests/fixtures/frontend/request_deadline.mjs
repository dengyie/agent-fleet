import assert from 'node:assert/strict';
import http from 'node:http';

const server = http.createServer((req, res) => {
  res.writeHead(req.url.endsWith('error') ? 503 : 200, {'Content-Type': 'application/json'});
  res.flushHeaders();
  if (req.url.endsWith('fast')) res.end('{"ok":true}');
  else {
    const timer = setTimeout(() => res.end('{"ok":true}'), 900);
    res.on('close', () => clearTimeout(timer));
  }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
globalThis.window = {FleetConfig: {apiBaseUrl: origin + '/api'},
  sessionStorage: {getItem: () => ''}, location: {href: origin + '/', origin}};
try {
  const {platformRequest} = await import('../../../frontend/assets/api/client.js');
  for (const path of ['/slow', '/error']) {
    await assert.rejects(platformRequest(path, {timeoutMs: 150}),
      error => error.kind === 'timeout' && error.code === 'request_timeout');
  }
  assert.deepEqual(await platformRequest('/fast', {timeoutMs: 1000}), {ok: true});
} finally {
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
}
