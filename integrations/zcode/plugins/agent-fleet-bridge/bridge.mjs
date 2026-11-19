/** ZCode owns dispatch and queueing. This bridge stores receipts, never inputs. */
import { createServer } from 'node:http';
import { randomBytes, createHash, timingSafeEqual } from 'node:crypto';
import { mkdir, readFile, writeFile, rename } from 'node:fs/promises';
import { join } from 'node:path';

const identifier = /^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$/;
const MAX_BODY = 4 * 1024 * 1024;
const safeErrorCodes = new Set(['not-granted', 'not-declared', 'invalid-input', 'forbidden',
  'unavailable', 'host-unavailable', 'disposed', 'incompatible', 'cancelled', 'limit-exceeded', 'internal']);
const knownRejections = new Set(['not-granted', 'not-declared', 'invalid-input', 'forbidden', 'incompatible']);
const fault = (code, status = 400) => Object.assign(new Error(code), { code, status });

async function atomicJson(path, value) {
  const temporary = path + '.' + randomBytes(8).toString('hex') + '.tmp';
  await writeFile(temporary, JSON.stringify(value) + '\n', { mode: 0o600 });
  await rename(temporary, path);
}
async function body(req) {
  let length = 0;
  const parts = [];
  for await (const part of req) {
    length += part.length;
    if (length > MAX_BODY) throw fault('body_too_large', 413);
    parts.push(part);
  }
  try {
    const value = JSON.parse(Buffer.concat(parts).toString('utf8'));
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw Error();
    return value;
  } catch { throw fault('invalid_json'); }
}
function validId(value) { return typeof value === 'string' && identifier.test(value); }
function reply(res, status, value) {
  if (!res.destroyed) {
    res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
    res.end(JSON.stringify(value));
  }
}

export async function startBridge(ctx) {
  const dataPath = ctx.extension.dataPath;
  if (!dataPath) throw fault('missing_data_directory');
  const receiptDir = join(dataPath, 'receipts');
  await mkdir(receiptDir, { recursive: true, mode: 0o700 });
  const token = randomBytes(32).toString('hex');
  const inflight = new Map();
  let closed = false;

  async function load(id) {
    if (!validId(id)) throw fault('invalid_request_id');
    try { return JSON.parse(await readFile(join(receiptDir, id + '.json'), 'utf8')); }
    catch (error) {
      if (error.code === 'ENOENT') throw fault('receipt_not_found', 404);
      throw fault('receipt_unreadable', 503);
    }
  }

  async function submit(input) {
    const { session_id, request_id, text } = input;
    if (Object.keys(input).some(key => !['session_id', 'request_id', 'text'].includes(key)) ||
        !validId(request_id) || !validId(session_id) || typeof text !== 'string' ||
        !text.trim() || text.length > 1_000_000) throw fault('invalid_message');
    const digest = createHash('sha256').update(JSON.stringify([session_id, text])).digest('hex');
    if (inflight.has(request_id)) {
      await inflight.get(request_id);
      const existing = await load(request_id);
      if (existing.digest !== digest) throw fault('idempotency_conflict', 409);
      return existing;
    }
    const work = (async () => {
      const path = join(receiptDir, request_id + '.json');
      const record = { request_id, session_id, digest, status: 'unknown', created_at: new Date().toISOString() };
      try {
        // Reserve before any remote side effect. A crash leaves unknown, never permission to replay.
        await writeFile(path, JSON.stringify(record) + '\n', { flag: 'wx', mode: 0o600 });
      } catch (error) {
        if (error.code !== 'EEXIST') throw error;
        const existing = await load(request_id);
        if (existing.digest !== digest) throw fault('idempotency_conflict', 409);
        return existing;
      }
      try {
        record.receipt = await ctx.sessions.sendToSession({ sessionId: session_id, requestId: request_id, prompt: text });
        record.status = record.receipt.admission;
      } catch (error) {
        record.error_code = safeErrorCodes.has(error.code) ? error.code : 'dispatch_unknown';
        record.status = knownRejections.has(error.code) ? 'rejected' : 'unknown';
      }
      await atomicJson(path, record);
      return record;
    })();
    inflight.set(request_id, work);
    try { return await work; } finally { inflight.delete(request_id); }
  }

  async function route(req, res) {
    const supplied = Buffer.from(req.headers.authorization || '');
    const expected = Buffer.from('Bearer ' + token);
    if (supplied.length !== expected.length || !timingSafeEqual(supplied, expected))
      throw fault('unauthorized', 401);
    // No CORS support or browser-origin requests, including authenticated ones.
    if (req.headers.origin) throw fault('origin_not_allowed', 403);
    const url = new URL(req.url, 'http://127.0.0.1');
    let result;
    if (req.method === 'GET' && url.pathname === '/health') {
      result = { protocol: 1, plugin: ctx.extension.id, transport: 'zcode-extension', active: !closed };
    } else if (req.method === 'GET' && url.pathname === '/visible') {
      result = { sessions: await ctx.workbench.visibleSessions() };
    } else if (req.method === 'GET' && url.pathname === '/sessions') {
      result = await ctx.sessions.listSessions({ ...(url.searchParams.has('cursor') ? { cursor: url.searchParams.get('cursor') } : {}), limit: 200 });
    } else if (req.method === 'POST' && url.pathname === '/send') {
      result = await submit(await body(req));
    } else if (req.method === 'GET' && url.pathname.startsWith('/receipts/')) {
      result = await load(url.pathname.slice('/receipts/'.length));
      if (result.receipt?.operation && result.status !== 'rejected') {
        result.observation = await ctx.sessions.waitSessions({ operations: [result.receipt.operation], mode: 'all', timeoutMs: 0 });
      }
    } else if (req.method === 'GET' && url.pathname === '/messages') {
      const sessionId = url.searchParams.get('session_id');
      if (!validId(sessionId)) throw fault('invalid_session_id');
      result = await ctx.sessions.readSession({ sessionId, view: 'messages', limit: 200,
        ...(url.searchParams.has('cursor') ? { cursor: url.searchParams.get('cursor') } : {}) });
    } else if (req.method === 'POST' && url.pathname === '/request-grants') {
      result = await ctx.grants.request();
    } else throw fault('not_found', 404);
    reply(res, 200, result);
  }
  const server = createServer((req, res) => {
    route(req, res).catch(error => reply(res, error.status || 503,
      { error: error.status || safeErrorCodes.has(error.code) ? error.code : 'bridge_unavailable' }));
  });
  server.requestTimeout = 30_000;
  server.headersTimeout = 10_000;
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  const endpoint = { protocol: 1, url: `http://127.0.0.1:${server.address().port}`, token,
    plugin: ctx.extension.id, pid: process.pid, active: true };
  const endpointFile = join(dataPath, 'bridge.json');
  async function close() {
    if (closed) return;
    closed = true;
    await atomicJson(endpointFile, { protocol: 1, active: false });
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
    await Promise.allSettled([...inflight.values()]);
  }
  try { await atomicJson(endpointFile, endpoint); }
  catch (error) { server.closeAllConnections(); server.close(); throw error; }
  return { endpoint, close };
}
