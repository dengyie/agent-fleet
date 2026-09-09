"""Contract tests for the standalone static frontend shell (Task 12 + Task 13).

The new ``frontend/`` tree is a pure static release that must be servable by a
plain static server with no Flask process and no build chain.  These source
contracts guard the Task 12 interfaces:

- ``frontend/index.html`` is a static entry with Fleet/machine/task mount
  points, a non-secret config script, and no Flask template expressions or
  server-rendered business data.
- ``frontend/config.js`` exposes only non-secret runtime configuration
  (``window.FleetConfig.apiBaseUrl`` defaulting to the same-origin ``/api``).
- ``frontend/routes.js`` provides ``apiPath(...)`` / ``pagePath(...)`` helpers
  that percent-encode each path segment and reject a raw ``/`` inside a segment.

Task 13 adds the API client and public contracts:

- ``frontend/api/client.js`` is the only HTTP entry: it owns every API path,
  encodes path segments, uses ``cache: "no-store"``, converts non-2xx and
  network/timeout failures to a stable ``ApiError``, and never adds ingest or
  runner headers.
- ``frontend/api/contracts.js`` exposes public DTO parsers
  (``parseStatus`` / ``parseMachine`` / ``parseEvents`` / ``parseTask`` /
  ``parseSseEvent`` / ``parseError``) that return allowlisted models or throw
  bounded ``ContractError``.

Task 14 adds the SSE lifecycle and state store:

- ``frontend/realtime/sse.js`` is the sole ``new EventSource`` owner with
  idempotent ``start()``, bounded poll fallback and terminal task refresh via
  an injected client callback.
- ``frontend/state/store.js`` is a DOM/client-agnostic ``FleetStore`` that
  applies contract-validated SSE events with ``Math.max`` stale fencing,
  bounded text logs (500 lines), bounded events, machine summary merges and
  listener notifications.

There is no JS test framework in this repo; like the existing
``tests/test_frontend_xss.py``, these tests assert against source text without
adding a dependency or build chain.  The executable fixture tests run the pure
contract parsers through ``node`` when it is available and are skipped when it
is not — Node is an optional local verification tool only, never a runtime or
release dependency.
"""

import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = REPO_ROOT / 'frontend'
INDEX_HTML = FRONTEND_DIR / 'index.html'
CONFIG_JS = FRONTEND_DIR / 'config.js'
ROUTES_JS = FRONTEND_DIR / 'routes.js'
CLIENT_JS = FRONTEND_DIR / 'api' / 'client.js'
CONTRACTS_JS = FRONTEND_DIR / 'api' / 'contracts.js'
SSE_JS = FRONTEND_DIR / 'realtime' / 'sse.js'
STORE_JS = FRONTEND_DIR / 'state' / 'store.js'


# Node-only fixture runner for the pure contract parsers.  Contracts are pure
# ES modules with no browser globals, so a single `node --input-type=module`
# invocation can import them from a file URL and assert against synthetic,
# non-secret payloads.  The Python test skips these when Node is unavailable.
NODE_FIXTURE_RUNNER = r"""
const contracts = await import(process.env.FLEET_CONTRACTS_URL);
const fixture = process.env.FLEET_FIXTURE;
let failures = 0;

function fail(label, message) {
  failures += 1;
  console.error('FAIL ' + label + ': ' + message);
}
function ok(label) {
  console.log('ok ' + label);
}
function isContractError(e) {
  return e instanceof contracts.ContractError;
}
function assertThrows(fn, label) {
  try {
    fn();
    fail(label, 'expected ContractError but succeeded');
  } catch (e) {
    if (isContractError(e)) ok(label);
    else fail(label, 'expected ContractError, got ' + String(e));
  }
}
function assertModel(fn, predicate, label) {
  try {
    const model = fn();
    if (predicate(model)) ok(label);
    else fail(label, 'model mismatch: ' + JSON.stringify(model));
  } catch (e) {
    fail(label, 'threw: ' + String(e && e.message));
  }
}

if (fixture === 'task_reject') {
  const baseTask = {
    task_id: 't-1', machine: 'mac', agent_type: 'codex',
    project: 'agent-fleet', state: 'queued', requested_by: 'op',
    created_at: 'c', expires_at: 'e', instruction: 'do it'
  };
  assertThrows(() => contracts.parseTask({ ok: true, task: Object.assign({}, baseTask, { attempt_id: 'a-1' }) }), 'task_reject.attempt_id');
  assertThrows(() => contracts.parseTask({ ok: true, task: Object.assign({}, baseTask, { nonce: 'n-1' }) }), 'task_reject.nonce');
  assertThrows(() => contracts.parseTask({ ok: true, task: Object.assign({}, baseTask, { client_token: 'tok' }) }), 'task_reject.client_token');
  assertThrows(() => contracts.parseTask({ ok: true, task: Object.assign({}, baseTask, { result: { exit_code: 0, attempt_id: 'a-2' } }) }), 'task_reject.result_attempt');
  assertThrows(() => contracts.parseTask({ ok: true, task: Object.assign({}, baseTask, { result: { exit_code: 0, raw_log: 'x' } }) }), 'task_reject.result_unknown');
  const clean = contracts.parseTask({ ok: true, task: baseTask });
  const keys = Object.keys(clean.task).sort();
  const expected = ['agent_type', 'created_at', 'expires_at', 'instruction', 'machine', 'project', 'requested_by', 'state', 'task_id'];
  if (JSON.stringify(keys) === JSON.stringify(expected)) ok('task_reject.clean_allowlist');
  else fail('task_reject.clean_allowlist', 'keys=' + keys.join(','));
  if (Object.prototype.hasOwnProperty.call(clean.task, 'result') === false) ok('task_reject.no_result_when_absent');
  else fail('task_reject.no_result_when_absent', 'result key present');
  const withNullResult = contracts.parseTask({ ok: true, task: Object.assign({}, baseTask, { result: null }) });
  if (withNullResult.task.result === null) ok('task_reject.null_result_preserved');
  else fail('task_reject.null_result_preserved', 'result=' + String(withNullResult.task.result));
  const withResult = contracts.parseTask({ ok: true, task: Object.assign({}, baseTask, { result: { exit_code: 0, log_summary: 'ok', duration_s: 1.5 } }) });
  if (withResult.task.result.exit_code === 0 && withResult.task.result.log_summary === 'ok') ok('task_reject.result_allowlist');
  else fail('task_reject.result_allowlist', JSON.stringify(withResult.task.result));
  const withSession = contracts.parseTask({ ok: true, task: Object.assign({}, baseTask, { session_id: 'sess_1' }) });
  if (withSession.task.session_id === 'sess_1') ok('task_reject.optional_session_id');
  else fail('task_reject.optional_session_id', JSON.stringify(withSession.task));
  const emptySession = contracts.parseTask({ ok: true, task: Object.assign({}, baseTask, { session_id: '' }) });
  if (Object.prototype.hasOwnProperty.call(emptySession.task, 'session_id') === false) ok('task_reject.empty_session_id_omitted');
  else fail('task_reject.empty_session_id_omitted', JSON.stringify(emptySession.task));
  const paused = contracts.parseTask({ ok: true, task: Object.assign({}, baseTask, { state: 'paused' }) });
  if (paused.task.state === 'paused') ok('task_reject.paused_state');
  else fail('task_reject.paused_state', JSON.stringify(paused.task));
  const withGate = contracts.parseTask({ ok: true, task: Object.assign({}, baseTask, {
    gate: { kind: 'confirm_before_dispatch', state: 'pending', requested_by: 'op',
            created_at: 'c' }
  }) });
  if (withGate.task.gate && withGate.task.gate.state === 'pending' &&
      withGate.task.gate.kind === 'confirm_before_dispatch') ok('task_reject.gate_allowlist');
  else fail('task_reject.gate_allowlist', JSON.stringify(withGate.task.gate));
  assertThrows(() => contracts.parseTask({ ok: true, task: Object.assign({}, baseTask, {
    result: { exit_code: 0, diff_patch: '+++ secret' } }) }), 'task_reject.result_diff_patch_body');
  const withSummary = contracts.parseTask({ ok: true, task: Object.assign({}, baseTask, {
    result: { exit_code: 0, test_summary: { framework: 'pytest', passed: 1, failed: 0,
      skipped: 0, errors: 0, duration_s: 0.1, failed_names: ['test_foo'], secret: 'x' },
      has_diff_patch: true, diff_patch_bytes: 12 } }) });
  if (withSummary.task.result.test_summary &&
      withSummary.task.result.test_summary.passed === 1 &&
      withSummary.task.result.test_summary.secret === undefined &&
      withSummary.task.result.has_diff_patch === true &&
      !Object.prototype.hasOwnProperty.call(withSummary.task.result, 'diff_patch')) {
    ok('task_reject.test_summary_and_patch_flag');
  } else {
    fail('task_reject.test_summary_and_patch_flag', JSON.stringify(withSummary.task.result));
  }
  const diff = contracts.parseTaskDiff({
    ok: true, task_id: 't-1', diff_patch: '@@ -1 +1 @@\n-a\n+b\n', truncated: false
  });
  if (diff.ok === true && diff.diff_patch.indexOf('+b') !== -1 && diff.truncated === false) {
    ok('task_reject.parse_task_diff');
  } else {
    fail('task_reject.parse_task_diff', JSON.stringify(diff));
  }
} else if (fixture === 'machine_retain') {
  const payload = {
    ok: true, machine: 'mac-local',
    current: {
      timestamp: 't', reachable: true, remote_error: null,
      agents: { codex: { installed: true } },
      system: { platform: 'darwin', mem_total_mb: 16384 },
      raw_collector_output: 'secret'
    },
    history: [{ ts: 1, reachable: true }, { ts: 2, reachable: false }]
  };
  assertModel(() => contracts.parseMachine(payload), (m) =>
    m.machine === 'mac-local' &&
    m.current.reachable === true &&
    m.current.remote_error === null &&
    m.current.agents.codex.installed === true &&
    m.current.system.platform === 'darwin' &&
    m.current.system.mem_total_mb === 16384 &&
    !Object.prototype.hasOwnProperty.call(m.current, 'raw_collector_output') &&
    Array.isArray(m.current.local_profiles) && m.current.local_profiles.length === 0 &&
    m.history.length === 2 && m.history[1].reachable === false,
    'machine_retain.sanitized');
} else if (fixture === 'error_preserve') {
  const model = contracts.parseError({
    ok: false, error: 'machine_offline', detail: 'not reachable',
    request_id: 'rid-1', traceback: 'SECRET', sql: 'SELECT 1'
  });
  const keys = Object.keys(model).sort();
  if (keys.join(',') === 'detail,error,ok,request_id') ok('error_preserve.keys');
  else fail('error_preserve.keys', 'keys=' + keys.join(','));
  if (model.ok === false && model.error === 'machine_offline' &&
      model.detail === 'not reachable' && model.request_id === 'rid-1') {
    ok('error_preserve.values');
  } else {
    fail('error_preserve.values', JSON.stringify(model));
  }
} else if (fixture === 'misc') {
  assertModel(() => contracts.parseStatus({ updated_at: 'u', machines: [{ machine: 'mac', online: true }] }),
    (m) => m.updated_at === 'u' && m.machines.length === 1 && m.machines[0].online === true &&
      m.features.append_user_turn === false && m.features.apply_local_profile === false,
    'status_parse');
  assertModel(() => contracts.parseStatus({ updated_at: 'u', machines: [],
    features: { append_user_turn: true, apply_local_profile: false, extra: true } }),
    (m) => m.features.append_user_turn === true && m.features.apply_local_profile === false &&
      !Object.prototype.hasOwnProperty.call(m.features, 'extra'),
    'status_features_allowlist');
  assertModel(() => contracts.parseEvents({ events: [{ event: 'state_changed', machine: 'mac', ts: 1, changes: ['online'] }] }),
    (m) => m.events.length === 1 && m.events[0].event === 'state_changed' && m.events[0].machine === 'mac',
    'events_parse');
  assertModel(() => contracts.parseSseEvent('machine_update', { machine: 'mac', online: true, ts: 1, changes: [], agents: {}, system: {} }),
    (m) => m.machine === 'mac' && m.online === true,
    'sse_machine_update');
  assertModel(() => contracts.parseSseEvent('task_update', { task_id: 't', state: 'running', event: 'task_leased', machine: 'mac', ts: 1 }),
    (m) => m.task_id === 't' && m.state === 'running',
    'sse_task_update');
  assertModel(() => contracts.parseSseEvent('task_log', { task_id: 't', line: 'hello', ts: 1 }),
    (m) => m.line === 'hello' && m.task_id === 't',
    'sse_task_log');
  assertModel(() => contracts.parseSseEvent('fleet_event', { event: 'alert', machine: 'mac', changes: [], ts: 1 }),
    (m) => m.event === 'alert' && m.machine === 'mac',
    'sse_fleet_event');
  assertModel(() => contracts.parseTaskList({ ok: true, tasks: [{ task_id: 't-1', machine: 'mac', agent_type: 'codex', project: 'p', state: 'queued', requested_by: 'op', created_at: 'c', expires_at: 'e', instruction: 'x' }] }),
    (m) => m.ok === true && m.tasks.length === 1 && m.tasks[0].task_id === 't-1',
    'task_list_parse');
} else if (fixture === 'store_lifecycle') {
  const storeMod = await import(process.env.FLEET_STORE_URL);
  const S = storeMod.FleetStore;
  const store = new S();

  // 订阅 / 通知
  let notified = 0;
  const unsub = store.subscribe(() => { notified += 1; });
  store.setConnection('open');
  if (notified === 1 && store.getState().connection === 'open') ok('store.setConnection_notifies');
  else fail('store.setConnection_notifies', 'notified=' + notified);
  unsub();
  store.setConnection('disconnected');
  if (notified === 1) ok('store.unsubscribe_stops_notifications');
  else fail('store.unsubscribe_stops_notifications', 'notified=' + notified);

  // 游标 Math.max + 陈旧事件拒绝
  const s2 = new S();
  const okEvent = s2.applySseEvent({ type: 'fleet_event', data: { event: 'a', machine: 'mac', changes: [], ts: 10 } });
  if (okEvent === true && s2.getLastEventTs() === 10) ok('store.cursor_math_max');
  else fail('store.cursor_math_max', 'okEvent=' + okEvent + ' last=' + s2.getLastEventTs());
  const staleRejected = s2.applySseEvent({ type: 'fleet_event', data: { event: 'b', machine: 'mac', changes: [], ts: 5 } });
  if (staleRejected === false && s2.getLastEventTs() === 10 && s2.getState().events.length === 1) ok('store.rejects_stale_ts');
  else fail('store.rejects_stale_ts', 'rej=' + staleRejected + ' last=' + s2.getLastEventTs() + ' ev=' + s2.getState().events.length);

  // 未知事件类型只推进游标、不应用
  const unknownTs = s2.applySseEvent({ type: 'bogus_type', data: { event: 'x', ts: 20 } });
  if (unknownTs === false && s2.getLastEventTs() === 20 && s2.getState().events.length === 1) ok('store.unknown_type_fence_only');
  else fail('store.unknown_type_fence_only', 'rc=' + unknownTs + ' last=' + s2.getLastEventTs());

  // task_log 有界到 500 行，只接受文本
  const s3 = new S();
  for (let i = 0; i < 510; i += 1) {
    s3.applySseEvent({ type: 'task_log', data: { task_id: 't1', line: 'line-' + i, ts: 100 + i } });
  }
  const lines = s3.getState().taskLogs['t1'] || [];
  if (lines.length === 500 && lines[0] === 'line-10' && lines[499] === 'line-509') ok('store.task_log_bounded_500');
  else fail('store.task_log_bounded_500', 'len=' + lines.length + ' first=' + lines[0]);
  s3.applySseEvent({ type: 'task_log', data: { task_id: 't1', line: 12345, ts: 700 } });
  if ((s3.getState().taskLogs['t1'] || []).length === 500) ok('store.task_log_text_only_rejects_number');
  else fail('store.task_log_text_only_rejects_number', 'len=' + (s3.getState().taskLogs['t1'] || []).length);

  // machine_update 更新机器摘要
  const s4 = new S();
  s4.setStatus({ updated_at: 'u', machines: [{ machine: 'mac', online: false, desc: 'desc' }] });
  s4.applySseEvent({ type: 'machine_update', data: { machine: 'mac', online: true, ts: 30, changes: ['online'], agents: { codex: {} }, system: {} } });
  const machine = s4.getState().status.machines.find((m) => m.machine === 'mac');
  if (machine && machine.online === true && machine.desc === 'desc') ok('store.machine_update_merges_summary');
  else fail('store.machine_update_merges_summary', JSON.stringify(machine));

  // 终态 task_update 触发注入的刷新回调
  const s5 = new S();
  let terminalId = null;
  s5.setRefreshTaskHandler((id) => { terminalId = id; });
  s5.applySseEvent({ type: 'task_update', data: { task_id: 't9', machine: 'mac', state: 'succeeded', event: 'task_succeeded', ts: 50 } });
  if (terminalId === 't9' && s5.getState().tasks['t9'].state === 'succeeded') ok('store.terminal_triggers_refresh_hook');
  else fail('store.terminal_triggers_refresh_hook', 'terminalId=' + terminalId);
  const s6 = new S();
  s6.setRefreshTaskHandler((id) => { terminalId = id; });
  s6.applySseEvent({ type: 'task_update', data: { task_id: 't10', machine: 'mac', state: 'running', event: 'task_leased', ts: 60 } });
  if (terminalId === 't9') ok('store.non_terminal_does_not_refresh');
  else fail('store.non_terminal_does_not_refresh', 'terminalId=' + terminalId);

  // setTask 接受 parseTask 模型并写任务
  const s7 = new S();
  s7.setTask({ ok: true, task: { task_id: 'tx', machine: 'mac', state: 'queued', agent_type: 'codex', project: 'p', requested_by: 'op', created_at: 'c', expires_at: 'e', instruction: 'i' } });
  if (s7.getState().tasks['tx'] && s7.getState().tasks['tx'].state === 'queued') ok('store.setTask_accepts_parseTask_model');
  else fail('store.setTask_accepts_parseTask_model', JSON.stringify(s7.getState().tasks['tx']));
} else if (fixture === 'sse_url') {
  const sseMod = await import(process.env.FLEET_SSE_URL);
  const url1 = sseMod.buildStreamUrl('/api', 123.45);
  if (url1 === '/api/stream?since=123.45') ok('sse_url.encodes_since');
  else fail('sse_url.encodes_since', url1);
  const url3 = sseMod.buildStreamUrl('http://x/api', 1.5);
  if (url3 === 'http://x/api/stream?since=1.5') ok('sse_url.preserves_base');
  else fail('sse_url.preserves_base', url3);
  const url0 = sseMod.buildStreamUrl('/api', 0);
  if (url0 === '/api/stream?since=0') ok('sse_url.zero_since');
  else fail('sse_url.zero_since', url0);
} else if (fixture === 'store_log_bounds') {
  const storeMod = await import(process.env.FLEET_STORE_URL);
  const LB = storeMod.FleetStore;
  const MAX_T = storeMod.MAX_TASKS;
  const MAX_LL = storeMod.MAX_LOG_LINE_LENGTH;
  // 不同 task ID 大量的 task_log：taskLogs 映射必须按 MAX_TASKS 有界、淘汰最旧
  const total = MAX_T + 8;
  const sb = new LB();
  for (let i = 0; i < total; i += 1) {
    sb.applySseEvent({ type: 'task_log', data: { task_id: 'logid-' + i, line: 'x-' + i, ts: 1000 + i } });
  }
  const logKeys = Object.keys(sb.getState().taskLogs);
  if (logKeys.length === MAX_T &&
      sb.getState().taskLogs['logid-0'] === undefined &&
      sb.getState().taskLogs['logid-' + (total - 1)] !== undefined) {
    ok('store_log_bounds.max_count_capped');
  } else {
    fail('store_log_bounds.max_count_capped', 'keys=' + logKeys.length + ' firstKept=' + (sb.getState().taskLogs['logid-0'] !== undefined));
  }
  // 超长单行必须在写入前截断到 MAX_LOG_LINE_LENGTH
  const longLine = 'z'.repeat(MAX_LL + 500);
  const sc = new LB();
  sc.applySseEvent({ type: 'task_log', data: { task_id: 'longid', line: longLine, ts: 9000 } });
  const stored = (sc.getState().taskLogs['longid'] || [])[0] || '';
  if (stored.length === MAX_LL && stored === longLine.slice(0, MAX_LL)) ok('store_log_bounds.line_truncated');
  else fail('store_log_bounds.line_truncated', 'len=' + stored.length);
  // 任务被淘汰时，其日志映射必须一并清理
  const sd = new LB();
  for (let i = 0; i < total; i += 1) {
    // ts 严格递增（2i / 2i+1），保证同任务 update 与 log 事件都不会被当作陈旧拒绝
    sd.applySseEvent({ type: 'task_update', data: { task_id: 'evict-' + i, machine: 'mac', state: 'queued', event: 'task_created', ts: 2000 + 2 * i } });
    sd.applySseEvent({ type: 'task_log', data: { task_id: 'evict-' + i, line: 'l' + i, ts: 2000 + 2 * i + 1 } });
  }
  const evictLogs = Object.keys(sd.getState().taskLogs);
  const keptTasks = Object.keys(sd.getState().tasks);
  if (keptTasks.length === MAX_T && evictLogs.length <= MAX_T &&
      sd.getState().taskLogs['evict-0'] === undefined) {
    ok('store_log_bounds.eviction_drops_logs');
  } else {
    fail('store_log_bounds.eviction_drops_logs', 'tasks=' + keptTasks.length + ' logs=' + evictLogs.length);
  }
} else if (fixture === 'sse_events_fence') {
  const storeMod = await import(process.env.FLEET_STORE_URL);
  const sseMod = await import(process.env.FLEET_SSE_URL);
  const store = new (storeMod.FleetStore)();
  let onEventCalls = 0;
  const refreshIds = [];
  const fakeClient = {
    getTask: (id) => {
      refreshIds.push(id);
      return Promise.resolve({ ok: true, task: { task_id: id, machine: 'mac', state: 'succeeded', agent_type: 'codex', project: 'p', requested_by: 'op', created_at: 'c', expires_at: 'e', instruction: 'i' } });
    }
  };
  const sse = new sseMod.SseClient(store, {
    apiBaseUrl: '/api',
    onEvent: () => { onEventCalls += 1; },
    client: fakeClient,
  });
  // 先把游标推进到 10
  store.applySseEvent({ type: 'fleet_event', data: { event: 'bootstrap', machine: 'mac', changes: [], ts: 10 } });
  // 陈旧终态事件（旧 ts < 10）：不得触发 onEvent、不得触发 getTask 刷新
  sse._handle('task_update', { data: JSON.stringify({ task_id: 't-stale', machine: 'mac', state: 'succeeded', event: 'task_succeeded', ts: 5 }) });
  await new Promise((r) => setTimeout(r, 10));
  let st = store.getState();
  if (onEventCalls === 0 && refreshIds.length === 0 &&
      st.lastEventTs === 10 && st.tasks['t-stale'] === undefined) {
    ok('sse_events_fence.stale_gated');
  } else {
    fail('sse_events_fence.stale_gated', 'on=' + onEventCalls + ' refresh=' + refreshIds.length + ' last=' + st.lastEventTs);
  }
  // 有效非终态事件：只触发 onEvent，不触发终态刷新
  sse._handle('task_update', { data: JSON.stringify({ task_id: 't-run', machine: 'mac', state: 'running', event: 'task_leased', ts: 20 }) });
  await new Promise((r) => setTimeout(r, 10));
  st = store.getState();
  if (onEventCalls === 1 && refreshIds.length === 0 &&
      st.tasks['t-run'] && st.tasks['t-run'].state === 'running') {
    ok('sse_events_fence.valid_non_terminal');
  } else {
    fail('sse_events_fence.valid_non_terminal', 'on=' + onEventCalls + ' refresh=' + refreshIds.length);
  }
  // 有效终态事件：触发 onEvent + 代理客户端 getTask 刷新回填
  sse._handle('task_update', { data: JSON.stringify({ task_id: 't-ok', machine: 'mac', state: 'succeeded', event: 'task_succeeded', ts: 30 }) });
  await new Promise((r) => setTimeout(r, 50));
  st = store.getState();
  if (onEventCalls === 2 && refreshIds.length === 1 &&
      refreshIds[0] === 't-ok' && st.tasks['t-ok'] &&
      st.tasks['t-ok'].state === 'succeeded' && st.lastEventTs === 30) {
    ok('sse_events_fence.valid_terminal_refresh');
  } else {
    fail('sse_events_fence.valid_terminal_refresh', 'on=' + onEventCalls + ' refresh=' + refreshIds.join(',') + ' last=' + st.lastEventTs);
  }
} else if (fixture === 'sse_poll_fence') {
  const storeMod = await import(process.env.FLEET_STORE_URL);
  const sseMod = await import(process.env.FLEET_SSE_URL);
  const store = new (storeMod.FleetStore)();
  const poll = new sseMod.SseClient(store, { apiBaseUrl: '/api' });
  let fetchCalls = 0;
  let aborts = 0;
  globalThis.fetch = function (url, opts) {
    fetchCalls += 1;
    const signal = opts && opts.signal ? opts.signal : null;
    return new Promise(function (resolve, reject) {
      if (!signal) { return; }
      signal.addEventListener('abort', function () {
        aborts += 1;
        const err = new Error('aborted');
        err.name = 'AbortError';
        reject(err);
      });
    });
  };
  poll._pollOnce();
  poll._pollOnce();
  if (fetchCalls === 1 && poll._pollPending === true) ok('sse_poll_fence.inflight_dedup');
  else fail('sse_poll_fence.inflight_dedup', 'calls=' + fetchCalls + ' pend=' + poll._pollPending);
  // stop() 中止 A 后，必须允许同步启动 B；A 的迟到 cleanup 不得释放 B 的栅栏。
  poll.stop();
  globalThis.fetch = function (url, opts) {
    fetchCalls += 1;
    return new Promise(function (resolve) {
      setTimeout(function () {
        resolve({ status: 200, json: function () { return Promise.resolve({ updated_at: 'u', machines: [{ machine: 'mac', online: true }] }); } });
      }, 5);
    });
  };
  poll._pollOnce();
  if (fetchCalls === 2 && poll._pollPending === true &&
      poll._pollController !== null) {
    ok('sse_poll_fence.restart_ownership');
  } else {
    fail('sse_poll_fence.restart_ownership', 'calls=' + fetchCalls + ' pend=' + poll._pollPending);
  }
  await new Promise((r) => setTimeout(r, 30));
  if (aborts === 1 && poll._pollPending === false &&
      poll._pollController === null) {
    ok('sse_poll_fence.stop_aborts_and_resume');
  } else {
    fail('sse_poll_fence.stop_aborts_and_resume', 'aborts=' + aborts + ' calls=' + fetchCalls + ' pend=' + poll._pollPending);
  }
} else if (fixture === 'sse_reconnect') {
  const storeMod = await import(process.env.FLEET_STORE_URL);
  const sseMod = await import(process.env.FLEET_SSE_URL);
  const created = [];

  // 模拟 EventSource：只记录 URL，暴露 close 与 onopen/onerror（由 sse.js 赋值）。
  function FakeEventSource(url) {
    this.url = url;
    this.readyState = 0;
    this.onopen = null;
    this.onerror = null;
    created.push(this);
  }
  FakeEventSource.OPEN = 0;
  FakeEventSource.prototype.CONNECTING = 0;
  FakeEventSource.prototype.OPEN = 0;
  FakeEventSource.prototype.CLOSED = 2;
  FakeEventSource.prototype.addEventListener = function () {};
  FakeEventSource.prototype.close = function () { this.readyState = 2; };
  globalThis.EventSource = FakeEventSource;

  // 场景 A：传输错误后，延迟重连必须用“最新 store 游标”创建新连接的新 URL，
  // 而不是沿用原始 since=0 地址（EventSource 自带重连会复用初始 URL）。
  const storeA = new (storeMod.FleetStore)();
  const sseA = new sseMod.SseClient(storeA, { apiBaseUrl: '/api', reconnectDelayMs: 10 });
  sseA.start();
  if (created.length === 1 && created[0].url === '/api/stream?since=0') {
    ok('sse_reconnect.initial_url_since0');
  } else {
    fail('sse_reconnect.initial_url_since0', 'urls=' + created.map((c) => c.url).join(','));
  }
  // 推进游标到 42，然后触发传输错误（走真实 onerror 路径：关闭失败源 + 调度重连）。
  storeA.applySseEvent({ type: 'fleet_event', data: { event: 'bootstrap', machine: 'mac', changes: [], ts: 42 } });
  created[0].onerror();
  if (created[0].readyState === 2 && sseA._source === null && sseA._reconnectTimer !== null) {
    ok('sse_reconnect.closes_failed_source_and_schedules');
  } else {
    fail('sse_reconnect.closes_failed_source_and_schedules', 'ready=' + created[0].readyState + ' src=' + Boolean(sseA._source) + ' t=' + Boolean(sseA._reconnectTimer));
  }
  await new Promise((r) => setTimeout(r, 40));
  if (created.length === 2 && created[1].url === '/api/stream?since=42') {
    ok('sse_reconnect.reconnect_uses_latest_cursor');
  } else {
    fail('sse_reconnect.reconnect_uses_latest_cursor', 'created=' + created.length + ' url=' + (created[1] && created[1].url));
  }
  sseA.stop();

  // 场景 B：stop() 必须取消未决的延迟重连，绝不创建下一个连接。
  created.length = 0;
  const storeB = new (storeMod.FleetStore)();
  const sseB = new sseMod.SseClient(storeB, { apiBaseUrl: '/api', reconnectDelayMs: 10 });
  sseB.start();
  sseB._closeAndScheduleReconnect();
  if (sseB._reconnectTimer !== null) {
    ok('sse_reconnect.pending_before_stop');
  } else {
    fail('sse_reconnect.pending_before_stop', 't=' + Boolean(sseB._reconnectTimer));
  }
  sseB.stop();
  await new Promise((r) => setTimeout(r, 40));
  // 起始已创建 1 个源；stop() 取消未决重连后不新增（仍是 1，绝非 2）。
  if (created.length === 1 && sseB._reconnectTimer === null && sseB._source === null) {
    ok('sse_reconnect.stop_cancels_pending');
  } else {
    fail('sse_reconnect.stop_cancels_pending', 'created=' + created.length + ' t=' + Boolean(sseB._reconnectTimer) + ' src=' + Boolean(sseB._source));
  }
} else if (fixture === 'agent_types') {
  // allowlist：只保留非空字符串，trim、去重
  const clean = contracts.parseAgentTypes({
    agent_types: ['codex', 'pi', ' claude_code ', 'codex', 42, '', null, 'pi']
  });
  const expect = ['codex', 'pi', 'claude_code'];
  if (JSON.stringify(clean.agent_types) === JSON.stringify(expect)) {
    ok('agent_types.allowlist_dedup');
  } else {
    fail('agent_types.allowlist_dedup', JSON.stringify(clean.agent_types));
  }
  // 缺失 / 非数组 agent_types → 空清单（不抛错，交由调用方回退兜底）
  const missing = contracts.parseAgentTypes({});
  const wrong = contracts.parseAgentTypes({ agent_types: 'codex' });
  if (missing.agent_types.length === 0 && wrong.agent_types.length === 0) {
    ok('agent_types.missing_or_non_array_empty');
  } else {
    fail('agent_types.missing_or_non_array_empty',
      JSON.stringify(missing) + ' / ' + JSON.stringify(wrong));
  }
  // 有界：至多 64 项、单项截断到 64 字符
  const many = [];
  for (let i = 0; i < 100; i += 1) many.push('type-' + i);
  const bounded = contracts.parseAgentTypes({ agent_types: many });
  if (bounded.agent_types.length === 64 &&
      bounded.agent_types[0] === 'type-0' &&
      bounded.agent_types[63] === 'type-63') {
    ok('agent_types.count_bounded_64');
  } else {
    fail('agent_types.count_bounded_64', 'len=' + bounded.agent_types.length);
  }
  const long = contracts.parseAgentTypes({ agent_types: ['x'.repeat(200)] });
  if (long.agent_types.length === 1 && long.agent_types[0].length === 64) {
    ok('agent_types.item_truncated_64');
  } else {
    fail('agent_types.item_truncated_64', JSON.stringify(long.agent_types));
  }
  // 非对象响应 → ContractError
  assertThrows(() => contracts.parseAgentTypes('nope'), 'agent_types.rejects_non_object');
} else if (fixture === 'sse_reconnect_bound') {
  // Task 66 二轮防线：重连总次数有界（MAX_RECONNECTS，仅在成功 onopen 复位）、
  // stop()/显式 start() 清除孤儿计时器、陈旧源回调为无副作用 no-op。
  const storeMod = await import(process.env.FLEET_STORE_URL);
  const sseMod = await import(process.env.FLEET_SSE_URL);
  const created = [];

  function FakeEventSourceB(url) {
    this.url = url;
    this.readyState = 0;
    this.onopen = null;
    this.onerror = null;
    created.push(this);
  }
  FakeEventSourceB.OPEN = 0;
  FakeEventSourceB.prototype.CONNECTING = 0;
  FakeEventSourceB.prototype.OPEN = 0;
  FakeEventSourceB.prototype.CLOSED = 2;
  FakeEventSourceB.prototype.addEventListener = function () {};
  FakeEventSourceB.prototype.close = function () { this.readyState = 2; };
  globalThis.EventSource = FakeEventSourceB;
  const MAX_R = sseMod.MAX_RECONNECTS;

  // 场景 A：达到总重连上限后不再调度（计时器保持 null），且计数不自动复位。
  const s1 = new sseMod.SseClient(new (storeMod.FleetStore)(), { apiBaseUrl: '/api', reconnectDelayMs: 10 });
  s1._reconnectCount = MAX_R;
  s1._scheduleReconnect();
  if (s1._reconnectTimer === null && s1._reconnectCount === MAX_R) {
    ok('sse_reconnect_bound.cap_stops_scheduling');
  } else {
    fail('sse_reconnect_bound.cap_stops_scheduling', 't=' + Boolean(s1._reconnectTimer) + ' count=' + s1._reconnectCount);
  }
  // 上限之下调度一次：计数递增、计时器被设置。
  const s2 = new sseMod.SseClient(new (storeMod.FleetStore)(), { apiBaseUrl: '/api', reconnectDelayMs: 10 });
  s2._reconnectCount = MAX_R - 1;
  s2._scheduleReconnect();
  if (s2._reconnectTimer !== null && s2._reconnectCount === MAX_R) {
    ok('sse_reconnect_bound.schedules_below_cap');
  } else {
    fail('sse_reconnect_bound.schedules_below_cap', 't=' + Boolean(s2._reconnectTimer) + ' count=' + s2._reconnectCount);
  }
  s2._cancelReconnect();

  // 场景 B：陈旧源回调（onopen/onerror）在源头被护栏判定为 no-op；
  // 真正拥有的源被取代后，触发旧源回调不得新增连接或触碰状态。
  const storeC = new (storeMod.FleetStore)();
  const s3 = new sseMod.SseClient(storeC, { apiBaseUrl: '/api', reconnectDelayMs: 10 });
  s3.start();
  const stale = created[0];
  stale.onerror(); // 失败：关闭 + 调度重连（此刻它仍是当前持有者）
  await new Promise((r) => setTimeout(r, 20)); // 重连 → 新源（自 so 取代 stale 源）
  if (created.length === 2 && s3._source !== null && s3._source !== stale) {
    ok('sse_reconnect_bound.replaced_by_new_source');
  } else {
    fail('sse_reconnect_bound.replaced_by_new_source', 'created=' + created.length + ' srcStale=' + (s3._source === stale));
  }
  const beforeCnt = created.length;
  const beforeTimer = s3._reconnectTimer;
  stale.onopen();  // 陈旧源回调：必须是 no-op
  stale.onerror(); // 陈旧源回调：必须是 no-op（不得再次调度）
  await new Promise((r) => setTimeout(r, 20));
  if (created.length === beforeCnt && s3._reconnectTimer === beforeTimer && s3._source !== null) {
    ok('sse_reconnect_bound.stale_callbacks_noop');
  } else {
    fail('sse_reconnect_bound.stale_callbacks_noop', 'created=' + created.length + ' was=' + beforeCnt + ' t=' + Boolean(s3._reconnectTimer) + ' src=' + Boolean(s3._source));
  }
  s3.stop();

  // 场景 C：显式 start() 取代未决重连定时器（清除无害孤儿计时器）。
  const storeD = new (storeMod.FleetStore)();
  const s4 = new sseMod.SseClient(storeD, { apiBaseUrl: '/api', reconnectDelayMs: 10 });
  s4.start();
  s4._closeAndScheduleReconnect();
  if (s4._reconnectTimer !== null && s4._reconnectCount === 1) {
    const pendingCount = created.length;
    s4.start(); // 显式 start() 取代：_source 已 null，创建新源并清孤儿计时器
    if (s4._reconnectTimer === null && s4._source !== null && created.length === pendingCount + 1) {
      ok('sse_reconnect_bound.explicit_start_cancels_orphan');
    } else {
      fail('sse_reconnect_bound.explicit_start_cancels_orphan', 't=' + Boolean(s4._reconnectTimer) + ' src=' + Boolean(s4._source) + ' created=' + created.length + ' was=' + pendingCount);
    }
  } else {
    fail('sse_reconnect_bound.explicit_start_cancels_orphan', 'no pending before start t=' + Boolean(s4._reconnectTimer) + ' c=' + s4._reconnectCount);
  }
  s4.stop();
}

if (failures > 0) {
  console.error(failures + ' fixture assertion(s) failed');
  process.exit(1);
}
console.log('fixture ' + fixture + ': PASS');
"""


class StaticFrontendTests(unittest.TestCase):
    """Task 12 Step 1 static shell contracts (exact brief snippets)."""

    def test_frontend_entry_has_no_flask_template_expression(self):
        source = INDEX_HTML.read_text()
        self.assertNotIn('{{', source)
        self.assertNotIn('url_for', source)

    def test_runtime_config_contains_no_credential_names(self):
        source = CONFIG_JS.read_text()
        for secret_name in ('X-Agent-Fleet-Token', 'X-Runner-Credential', 'private_key'):
            self.assertNotIn(secret_name, source)

    def test_runtime_config_exposes_api_base_url_only(self):
        source = CONFIG_JS.read_text()
        self.assertIn('window.FleetConfig', source)
        self.assertIn('apiBaseUrl', source)
        # Default apiBaseUrl must be the same-origin /api.
        self.assertIn('"/api"', source)
        # Frozen config prevents accidental mutation of the shared constant.
        self.assertIn('Object.freeze', source)

    def test_entry_has_page_mount_points(self):
        source = INDEX_HTML.read_text()
        for mount_id in ('mount-fleet', 'mount-machine', 'mount-task'):
            self.assertIn(mount_id, source, f'missing mount point {mount_id}')
        # Navigation state must be non-sensitive: data-page attribute on body.
        self.assertIn('data-page', source)

    def test_entry_loads_module_and_config_scripts(self):
        source = INDEX_HTML.read_text()
        self.assertIn('type="module"', source)
        self.assertIn('config.js', source)
        self.assertIn('routes.js', source)
        self.assertIn('styles/app.css', source)

    def test_entry_contains_no_embedded_observation_or_task_data(self):
        source = INDEX_HTML.read_text()
        # The static shell must not embed any business snapshot values.
        for token in ('hb-online', 'hb-alerts', 'hb-total', 'hb-tasks',
                      'data-machine="', 'data-task-id="', 'state-node'):
            self.assertNotIn(token, source, f'embedded business data: {token}')
        # No server-rendered data placeholders either.
        self.assertNotIn('${', source)

    def test_shell_declares_static_server_friendly_doctype_and_assets(self):
        source = INDEX_HTML.read_text()
        self.assertTrue(source.lstrip().startswith('<!DOCTYPE html>'))
        self.assertIn('<meta charset="utf-8">', source)
        self.assertIn('<meta name="viewport"', source)


class RouteHelperContractTests(unittest.TestCase):
    """Task 12 route helpers: apiPath / pagePath encode segments safely."""

    def test_routes_exports_both_helpers(self):
        source = ROUTES_JS.read_text()
        for helper in ('apiPath', 'pagePath'):
            self.assertIn(f'export function {helper}(', source,
                          f'helper {helper} missing or not exported')

    def test_route_helpers_encode_each_path_segment(self):
        source = ROUTES_JS.read_text()
        # Per-segment percent-encoding must be applied before any join.
        self.assertIn('encodeURIComponent', source)
        self.assertIn('.map(', source)
        # Encoded segments are joined with a literal '/'.
        self.assertIn('join("/")', source)

    def test_route_helpers_never_accept_a_raw_slash_as_a_segment(self):
        source = ROUTES_JS.read_text()
        self.assertIn('throw new Error', source)
        self.assertIn('indexOf("/")', source)
        # Slash rejection must happen before encoding, in a shared guard.
        self.assertIn("if (s.indexOf(\"/\") !== -1)", source)
        # Both helpers delegate to the same guarded builder.
        self.assertIn('buildPath(', source)

    def test_route_helpers_reject_dot_and_dotdot_segments(self):
        # Task 13 hardening: '.' / '..' must never become path traversal.
        source = ROUTES_JS.read_text()
        for guard in ('s === "."', 's === ".."'):
            self.assertIn(guard, source,
                          f'route helper missing {guard} guard')


class ApiClientSourceTests(unittest.TestCase):
    """Task 13 Step 1 source contracts: client owns fetch and API paths."""

    def test_views_are_not_allowed_to_call_fetch_directly(self):
        views_dir = FRONTEND_DIR / 'views'
        if not views_dir.is_dir():
            self.skipTest('frontend/views not yet present (Task 15)')
        for path in views_dir.glob('*.js'):
            self.assertNotIn('fetch(', path.read_text())

    def test_client_owns_all_api_paths(self):
        source = CLIENT_JS.read_text()
        for path in ('/status', '/machines/', '/tasks', '/events'):
            self.assertIn(path, source)

    def test_client_exports_all_required_async_functions(self):
        source = CLIENT_JS.read_text()
        for fn in ('getStatus', 'getMachine', 'getEvents', 'listTasks',
                   'getTask', 'createTask', 'cancelTask', 'retryTask',
                   'pauseTask', 'continueTask', 'confirmTask', 'rejectTask',
                   'getTaskDiff', 'listTaskFiles', 'getTaskFile'):
            self.assertIn(f'export async function {fn}(', source,
                          f'client missing {fn}')

    def test_api_error_has_stable_kind_status_code_detail_request_id(self):
        source = CLIENT_JS.read_text()
        for field in ('this.kind', 'this.status', 'this.code', 'this.detail',
                      'this.requestId'):
            self.assertIn(field, source, f'ApiError missing {field}')

    def test_client_does_not_add_ingest_or_runner_headers(self):
        source = CLIENT_JS.read_text()
        for header in ('X-Agent-Fleet-Token', 'X-Runner-Credential',
                       'Cf-Access-Authenticated-User-Email'):
            self.assertNotIn(header, source)

    def test_client_uses_no_store_cache_and_bounded_timeout(self):
        source = CLIENT_JS.read_text()
        self.assertIn('cache: "no-store"', source)
        self.assertIn('AbortController', source)
        self.assertIn('DEFAULT_TIMEOUT_MS', source)
        self.assertIn('setTimeout', source)

    def test_client_token_is_local_idempotency_not_a_credential(self):
        source = CLIENT_JS.read_text()
        self.assertIn('makeClientToken', source)
        self.assertIn('crypto.randomUUID', source)
        self.assertIn('client_token', source)
        # 幂等值绝不作为认证 header 发送
        self.assertNotIn('Authorization: Bearer', source)

    def test_contracts_exports_all_required_parsers(self):
        source = CONTRACTS_JS.read_text()
        for fn in ('parseStatus', 'parseMachine', 'parseEvents', 'parseTask',
                   'parseSseEvent', 'parseError', 'parseTaskFile',
                   'parseTaskFileList', 'parseTaskDiff'):
            self.assertIn(f'export function {fn}(', source,
                          f'contracts missing {fn}')

    def test_contracts_contains_public_allowlists_and_rejections(self):
        source = CONTRACTS_JS.read_text()
        # 公开字段 allowlist 与内部字段拒绝守卫都在 contracts 源码内
        for token in ('TASK_FIELDS', 'RESULT_FIELDS', 'REJECTED_TASK_FIELDS',
                      'GATE_FIELDS', 'TEST_SUMMARY_FIELDS', 'TASK_DIFF_FIELDS',
                      'attempt_id', 'nonce', 'STATUS_FIELDS', 'STATUS_FEATURE_FIELDS',
                      'LOCAL_PROFILE_FIELDS', 'append_user_turn',
                      'apply_local_profile', 'paused', 'has_diff_patch'):
            self.assertIn(token, source, f'contracts missing {token}')


class ApiClientContractFixtureTests(unittest.TestCase):
    """Task 13 Step 2: run pure contract parsers against synthetic payloads.

    Node is an *optional* local verification tool only; these tests are skipped
    when Node is unavailable and never required for the static release/runtime.
    All payloads below are synthetic and non-secret.
    """

    NODE = shutil.which('node')

    @unittest.skipUnless(shutil.which('node'),
                         'node unavailable; skipping JS fixture')
    def test_task_parser_rejects_internal_and_secret_fields(self):
        self._run_fixture('task_reject')

    @unittest.skipUnless(shutil.which('node'),
                         'node unavailable; skipping JS fixture')
    def test_machine_parser_retains_sanitized_fields(self):
        self._run_fixture('machine_retain')

    @unittest.skipUnless(shutil.which('node'),
                         'node unavailable; skipping JS fixture')
    def test_error_parser_preserves_only_public_error_fields(self):
        self._run_fixture('error_preserve')

    @unittest.skipUnless(shutil.which('node'),
                         'node unavailable; skipping JS fixture')
    def test_status_events_sse_parsers_validate_synthetic_payloads(self):
        self._run_fixture('misc')

    @unittest.skipUnless(shutil.which('node'),
                         'node unavailable; skipping JS fixture')
    def test_agent_types_parser_bounded_allowlist(self):
        self._run_fixture('agent_types')

    def _run_fixture(self, fixture_name):
        env = dict(os.environ)
        env['FLEET_CONTRACTS_URL'] = CONTRACTS_JS.resolve().as_uri()
        env['FLEET_FIXTURE'] = fixture_name
        proc = subprocess.run(
            [self.NODE, '--input-type=module', '-e', NODE_FIXTURE_RUNNER],
            capture_output=True, text=True, env=env, timeout=30,
        )
        if proc.returncode != 0:
            self.fail(
                f'fixture {fixture_name} failed:\nSTDOUT:\n{proc.stdout}\n'
                f'STDERR:\n{proc.stderr}')


class SseBoundaryTests(unittest.TestCase):
    """Task 14 SSE/state boundary source contracts (brief snippets + hardening)."""

    def test_only_sse_module_creates_eventsource(self):
        files = list(FRONTEND_DIR.rglob('*.js'))
        owners = [path for path in files
                  if 'new EventSource' in path.read_text()]
        self.assertEqual([p.relative_to(REPO_ROOT).as_posix() for p in owners],
                         ['frontend/realtime/sse.js'])

    def test_store_deduplicates_or_rejects_stale_event_timestamps(self):
        source = STORE_JS.read_text()
        self.assertIn('lastEventTs', source)
        self.assertIn('Math.max', source)

    def test_sse_exports_client_lifecycle_interface(self):
        source = SSE_JS.read_text()
        for token in ('export function SseClient(', 'SseClient.prototype.start',
                      'SseClient.prototype.stop', 'isOpen',
                      'EventSource.OPEN', 'buildStreamUrl'):
            self.assertIn(token, source, f'sse.js missing {token}')

    def test_sse_builds_url_with_since_from_last_event_ts(self):
        source = SSE_JS.read_text()
        self.assertIn('"/stream?since="', source)
        self.assertIn('encodeURIComponent', source)
        # URL 片段必须来自“从 store 读取的”游标，而非硬编码常量
        self.assertIn('getLastEventTs', source)

    def test_sse_start_is_idempotent_and_stop_clears_timers(self):
        source = SSE_JS.read_text()
        self.assertIn('if (this._source)', source)
        self.assertIn('.close()', source)
        self.assertIn('clearInterval', source)

    def test_sse_onerror_polls_bounded_and_onopen_clears(self):
        source = SSE_JS.read_text()
        self.assertIn('disconnected', source)
        self.assertIn('setInterval', source)
        self.assertIn('MAX_POLLS', source)
        self.assertIn("onopen", source)
        self.assertIn('_clearPoll', source)

    def test_sse_has_no_business_reload(self):
        source = SSE_JS.read_text()
        self.assertNotIn('location.reload', source)

    def test_sse_uses_contracts_parser_and_no_dom(self):
        source = SSE_JS.read_text()
        self.assertIn('parseSseEvent', source)
        for banned in ('document.', 'querySelector'):
            self.assertNotIn(banned, source)

    def test_sse_terminal_refresh_calls_injected_client_get_task(self):
        source = SSE_JS.read_text()
        for token in ('getTask', 'setTask', 'succeeded', 'cancelled',
                      'expired', 'TERMINAL_TASK_STATES'):
            self.assertIn(token, source, f'sse.js missing {token}')

    def test_store_is_dom_and_client_agnostic(self):
        source = STORE_JS.read_text()
        for banned in ('window.', 'document.', 'fetch(', 'new EventSource',
                       'location.', 'import ', 'resolveApiBaseUrl'):
            self.assertNotIn(banned, source,
                             f'store.js must not reference {banned}')

    def test_store_exports_required_fleet_store_interface(self):
        source = STORE_JS.read_text()
        for method in ('getState', 'subscribe', 'setStatus', 'setMachine',
                       'setTask', 'applySseEvent', 'setConnection',
                       'getLastEventTs'):
            self.assertIn(f'FleetStore.prototype.{method}', source,
                          f'store.js missing {method}')

    def test_store_applies_sse_through_four_event_types(self):
        source = STORE_JS.read_text()
        for token in ('machine_update', 'task_update', 'task_log',
                      'fleet_event'):
            self.assertIn(token, source, f'store.js missing {token}')

    def test_store_bounds_logs_to_500_text_lines(self):
        source = STORE_JS.read_text()
        self.assertIn('MAX_LOG_LINES', source)
        self.assertIn('500', source)
        self.assertIn("typeof data.line !== 'string'", source)

    def test_sse_ignores_events_not_applied_by_store(self):
        # Fix 1：applySseEvent 的布尔结果必须被捕获并作为门控——返回 false
        # （陈旧 / 未知）时不转发 onEvent、不触发终态 getTask 刷新。
        source = SSE_JS.read_text()
        self.assertIn('var applied', source)
        self.assertIn('applySseEvent({ type: name, data: parsed })', source)
        self.assertIn('if (!applied)', source)

    def test_sse_poll_has_inflight_and_abort_fencing(self):
        # Fix 3：降级轮询同一时间至多一个在途请求；超时用有界的
        # AbortController；stop() 中止在途请求。
        source = SSE_JS.read_text()
        for token in ('_pollPending', 'if (this._pollPending)',
                      'POLL_TIMEOUT_MS', 'new AbortController()',
                      'signal: controller.signal', 'controller.abort()',
                      'clearTimeout', '_pollController'):
            self.assertIn(token, source, f'sse.js missing {token}')

    def test_sse_stop_aborts_any_pending_poll_request(self):
        source = SSE_JS.read_text()
        self.assertIn('this._pollController.abort()', source)
        # stop() 必须经由 _clearPoll 清空计时器并复位在途栅栏
        self.assertIn('.close()', source)
        self.assertIn('clearInterval', source)
        self.assertIn('_pollPending = false', source)

    def test_store_bounds_task_logs_by_distinct_task_id_count(self):
        # Fix 2：taskLogs 按不同 task_id 计数有界（复用 MAX_TASKS），
        # 并淘汰最旧条目；任务被淘汰时同步清理其日志映射。
        source = STORE_JS.read_text()
        self.assertIn('_evictTaskLogOverflow', source)
        self.assertIn('Object.keys(this._state.taskLogs)', source)
        self.assertIn('delete this._state.taskLogs[evicted]', source)

    def test_store_truncates_overlong_task_log_lines(self):
        # Fix 4：单条日志行写入前按 MAX_LOG_LINE_LENGTH 截断。
        source = STORE_JS.read_text()
        self.assertIn('MAX_LOG_LINE_LENGTH', source)
        self.assertIn('slice(0, MAX_LOG_LINE_LENGTH)', source)


class SseBoundaryFixtureTests(unittest.TestCase):
    """Task 14 store/URL fixtures run through node when available.

    store.js is a pure, DOM/client-agnostic ES module, so the executable
    fixture run its lifecycle (subscribe, cursor fencing, bounded logs,
    machine merge, terminal refresh hook) through ``node``.  Node remains an
    optional local verification tool only; fixtures skip when it is absent.
    All payloads are synthetic and non-secret.
    """

    NODE = shutil.which('node')

    @unittest.skipUnless(shutil.which('node'),
                         'node unavailable; skipping JS fixture')
    def test_store_lifecycle_fixture(self):
        self._run_fixture('store_lifecycle', 'store')

    @unittest.skipUnless(shutil.which('node'),
                         'node unavailable; skipping JS fixture')
    def test_sse_url_fixture(self):
        self._run_fixture('sse_url', 'sse')

    @unittest.skipUnless(shutil.which('node'),
                         'node unavailable; skipping JS fixture')
    def test_store_log_bounds_fixture(self):
        # Fix 2 + Fix 4 执行覆盖：跨多 task_id 有界、淘汰最旧、超长行截断
        self._run_fixture('store_log_bounds', 'store')

    @unittest.skipUnless(shutil.which('node'),
                         'node unavailable; skipping JS fixture')
    def test_sse_events_fence_fixture(self):
        # Fix 1 执行覆盖：陈旧事件不触发 onEvent / 终态刷新，有效事件保留行为
        self._run_fixture('sse_events_fence', 'sse')

    @unittest.skipUnless(shutil.which('node'),
                         'node unavailable; skipping JS fixture')
    def test_sse_poll_fence_fixture(self):
        # Fix 3 执行覆盖：在途去重 + stop() 中止 + 复位后恢复
        self._run_fixture('sse_poll_fence', 'sse')

    @unittest.skipUnless(shutil.which('node'),
                         'node unavailable; skipping JS fixture')
    def test_sse_reconnect_fixture(self):
        # Task 66 执行覆盖：传输错误后重连用最新 store 游标重建 URL；
        # stop() 取消未决延迟重连（不创建新连接）。
        self._run_fixture('sse_reconnect', 'sse')

    @unittest.skipUnless(shutil.which('node'),
                         'node unavailable; skipping JS fixture')
    def test_sse_reconnect_bound_fixture(self):
        # Task 66 二轮防线：重连总次数有界（上限后停止调度）、仅成功 onopen
        # 复位计数、stop()/显式 start() 清除孤儿计时器、陈旧源回调 no-op。
        self._run_fixture('sse_reconnect_bound', 'sse')

    def _run_fixture(self, fixture_name, kind):
        # kind 用于区分 fixture 用途；store/sse/两者皆有时都注入全部 URL，
        # 以便 fixture 按需 import 而无需额外 plumbing。
        env = dict(os.environ)
        env['FLEET_CONTRACTS_URL'] = CONTRACTS_JS.resolve().as_uri()
        env['FLEET_STORE_URL'] = STORE_JS.resolve().as_uri()
        env['FLEET_SSE_URL'] = SSE_JS.resolve().as_uri()
        env['FLEET_FIXTURE'] = fixture_name
        proc = subprocess.run(
            [self.NODE, '--input-type=module', '-e', NODE_FIXTURE_RUNNER],
            capture_output=True, text=True, env=env, timeout=30,
        )
        if proc.returncode != 0:
            self.fail(
                f'fixture {fixture_name} failed:\nSTDOUT:\n{proc.stdout}\n'
                f'STDERR:\n{proc.stderr}')


class ViewBoundaryTests(unittest.TestCase):
    """Task 15 Step 1 view boundary source contracts.

    fleet.js / machine.js must be DOM-safe (no innerHTML /
    insertAdjacentHTML, textContent for dynamic text), use injected client
    methods and store subscriptions only, encode navigation with pagePath, and
    never reach for fetch / EventSource / Flask bootstrap / inline handlers.
    """

    def test_views_use_dom_safe_writes_for_dynamic_text(self):
        for path in (FRONTEND_DIR / 'views' / 'fleet.js',
                     FRONTEND_DIR / 'views' / 'machine.js'):
            source = path.read_text()
            self.assertNotIn('insertAdjacentHTML', source, path)
            self.assertNotIn('innerHTML', source, path)
            self.assertIn('textContent', source, path)

    def test_views_export_mount_interfaces(self):
        fleet = (FRONTEND_DIR / 'views' / 'fleet.js').read_text()
        machine = (FRONTEND_DIR / 'views' / 'machine.js').read_text()
        self.assertIn('export function mountFleet(root, store, client)',
                      fleet)
        self.assertIn(
            'export function mountMachine(root, machineName, store, client)',
            machine)

    def test_views_use_injected_client_methods_only(self):
        fleet = (FRONTEND_DIR / 'views' / 'fleet.js').read_text()
        machine = (FRONTEND_DIR / 'views' / 'machine.js').read_text()
        # 不允许直接调用 HTTP 底层：fetch / EventSource / XMLHttpRequest
        for source in (fleet, machine):
            self.assertNotIn('fetch(', source)
            self.assertNotIn('EventSource', source)
            self.assertNotIn('XMLHttpRequest', source)
        # fleet 依赖 client.getStatus / getEvents
        for token in ('clientMethods.getStatus', 'clientMethods.getEvents'):
            self.assertIn(token, fleet)
        # machine 依赖 getMachine / listTasks / createTask
        for token in ('clientMethods.getMachine', 'clientMethods.listTasks',
                      'clientMethods.createTask'):
            self.assertIn(token, machine)

    def test_views_subscribe_to_store(self):
        fleet = (FRONTEND_DIR / 'views' / 'fleet.js').read_text()
        machine = (FRONTEND_DIR / 'views' / 'machine.js').read_text()
        self.assertIn('store.subscribe', fleet)
        self.assertIn('store.subscribe', machine)

    def test_views_encode_page_navigation_with_page_path(self):
        fleet = (FRONTEND_DIR / 'views' / 'fleet.js').read_text()
        machine = (FRONTEND_DIR / 'views' / 'machine.js').read_text()
        # 机器导航必须用编码的 pagePath("machine", name)
        self.assertIn("pagePath('machine', name)", fleet)
        self.assertIn("import { pagePath } from '../routes.js'", fleet)
        # 任务导航用 pagePath("task", taskId)
        self.assertIn("pagePath('task', task.task_id)", machine)
        self.assertIn("pagePath('session', task.session_id)", machine)
        self.assertIn("import { pagePath } from '../routes.js'", machine)
        for source in (fleet, machine):
            self.assertIn('setAttribute', source)

    def test_views_have_no_flask_bootstrap_or_path_handcoding(self):
        fleet = (FRONTEND_DIR / 'views' / 'fleet.js').read_text()
        machine = (FRONTEND_DIR / 'views' / 'machine.js').read_text()
        for source in (fleet, machine):
            self.assertNotIn('url_for', source)
            self.assertNotIn('window.FleetConfig', source)
            self.assertNotIn("'/api", source)

    def test_machine_form_uses_text_content_errors_and_validates_for_ux(self):
        machine = (FRONTEND_DIR / 'views' / 'machine.js').read_text()
        self.assertIn('errorBox.textContent', machine)
        self.assertIn('请输入项目名', machine)
        self.assertIn('请输入指令', machine)
        self.assertIn('createTask', machine)

    def test_machine_view_optional_session_deep_link_on_create(self):
        machine = (FRONTEND_DIR / 'views' / 'machine.js').read_text()
        self.assertIn("pagePath('session', task.session_id)", machine)
        self.assertIn("typeof task.session_id === 'string'", machine)


class Task15ReviewFixTests(unittest.TestCase):
    """Task 15 review fixes source contracts.

    Review findings fixed here:
      1. machine.js must reflect live store/SSE updates after initial load
         (render from store state, not a stale viewState only).  Initial data is
         seeded into the store (setMachine / setTask) and the machine view reads
         reachability/system/agents (machine_update) and the filtered task list
         (task_update / createTask) from the store, retaining the initial
         fallback only where the store has no corresponding model.
      2. index.html must own the terminal getTask refresh through exactly one
         path (SseClient injected client), not the old
         store.setRefreshTaskHandler wiring.
      3. Both mount functions call render() once immediately after subscribing,
         before async requests, so the loading state paints.
      4. Both views fence teardown (disposed flag) against late async
         success/failure callbacks re-touching a cleared root.
    """

    MACHINE_JS = FRONTEND_DIR / 'views' / 'machine.js'
    FLEET_JS = FRONTEND_DIR / 'views' / 'fleet.js'

    def test_machine_render_sources_live_store_tasks_not_only_stale_viewstate(self):
        source = self.MACHINE_JS.read_text()
        # render 必须读取 store 的任务模型并按当前机器过滤（live 增量来源）
        self.assertIn('mergedTaskList(', source)
        self.assertIn('store.getState()', source)
        self.assertIn("machine === machineName", source)
        # 首屏兜底仅在水合任务已进入 store 之后保留
        self.assertIn('store.setTask(', source)
        self.assertIn('store.setMachine(detail)', source)
        # 兜底条目必须按键去重，避免覆盖 store 实时模型
        self.assertIn('byId[fb.task_id] === undefined', source)
        self.assertIn('byId[task.task_id] = task', source)
        # 有界展示由 renderTaskList 保留（MAX_VISIBLE_TASKS）
        self.assertIn('MAX_VISIBLE_TASKS', source)

    def test_machine_reads_live_reachability_system_agents(self):
        source = self.MACHINE_JS.read_text()
        # machine_update 走 store：渲染层经 machineSnapshot 读取实时增量
        self.assertIn('machineSnapshot(store, machineName)', source)
        self.assertIn('summary.online', source)
        self.assertIn('summary.agents', source)
        self.assertIn('summary.system', source)

    def test_machine_does_not_render_from_stale_viewstate_snapshots(self):
        source = self.MACHINE_JS.read_text()
        # 旧实现把 system/agents 缓存进 viewState 一次性渲染；修复后从 store 读取
        self.assertNotIn('viewState.system', source)
        self.assertNotIn('viewState.agents', source)

    def test_index_html_has_single_terminal_refresh_owner(self):
        source = INDEX_HTML.read_text()
        # 旧的双路径（store.setRefreshTaskHandler + SseClient({client})）已移除，
        # 终态刷新只经注入 client 的 SseClient 触发（getTask 回填在 sse.js 内部）。
        self.assertIn('new SseClient(store, { client: client })', source)
        self.assertNotIn('store.setRefreshTaskHandler', source)
        self.assertNotIn('getRefreshTaskHandler', source)
        # 刷新行为保留：注入 client 正是给 SseClient 的终态 getTask 回填使用
        self.assertIn('{ client: client }', source)

    def test_both_views_render_before_async_requests(self):
        machine = self.MACHINE_JS.read_text()
        fleet = self.FLEET_JS.read_text()
        # 立即渲染（loading）必须发生在 mount 的异步请求链之前
        for source in (machine, fleet):
            marker = source.find('// 订阅后立即渲染一次')
            self.assertNotEqual(marker, -1, 'missing immediate-render marker')
            after = source[marker:]
            init = after.find('render();')
            promise = after.find('Promise.resolve()')
            self.assertNotEqual(init, -1, 'missing immediate render()')
            self.assertNotEqual(promise, -1, 'missing async request chain')
            self.assertLess(init, promise,
                            'render() 必须在异步请求前调用，loading 才先绘制')
        # machine: 订阅回调也是 render（SSE 增量重绘）
        self.assertIn('store.subscribe(function () {', machine)

    def test_views_fence_teardown_against_late_async_callbacks(self):
        machine = self.MACHINE_JS.read_text()
        fleet = self.FLEET_JS.read_text()
        for source in (machine, fleet):
            # disposed 标志在卸载时置位，render 首行即护栏
            self.assertIn('var disposed', source)
            self.assertIn('if (disposed)', source)
            self.assertIn('disposed = true', source)
            # 异步成功/失败回调在触碰状态前检查 disposed
            self.assertIn('if (disposed) {', source)
            self.assertIn('return;', source)
            self.assertNotIn('location.reload', source)
            self.assertNotIn('fetch(', source)


class Task66MachineClassAllowlistTests(unittest.TestCase):
    """Task 66: machine 任务列表的状态样式类必须来自显式白名单映射。

    修复前 renderTaskList 写道 ``'task-state st-' + stateText``——把原始状态
    字符串直接拼进 class 属性。本组契约断言：
      1. 七个已知任务状态都在显式映射中（queued/leased/running/succeeded/
         failed/cancelled/expired）；
      2. 存在安全回退类（未知状态不会得到空洞/注入的 class 值）；
      3. 源码不再出现动态 ``'st-' + state`` 拼接；
      4. 动态状态文本仍以 textContent 呈现（白名单只管类名，不碰文本）。
    """

    MACHINE_JS = FRONTEND_DIR / 'views' / 'machine.js'

    def test_task_state_classes_are_explicit_allowlist_mapping(self):
        source = self.MACHINE_JS.read_text()
        self.assertIn('TASK_STATE_CLASSES', source)
        for state, klass in (
            ('queued', 'st-queued'), ('leased', 'st-leased'),
            ('running', 'st-running'), ('succeeded', 'st-succeeded'),
            ('failed', 'st-failed'), ('cancelled', 'st-cancelled'),
            ('expired', 'st-expired'),
        ):
            self.assertIn(f'{state}: \'{klass}\'', source,
                          f'缺失状态映射 {state} -> {klass}')

    def test_task_state_class_has_safe_fallback(self):
        source = self.MACHINE_JS.read_text()
        self.assertIn('TASK_STATE_FALLBACK_CLASS', source)
        self.assertIn("'st-unknown'", source)

    def test_render_uses_allowlist_helper_not_raw_concatenation(self):
        source = self.MACHINE_JS.read_text()
        # 渲染路径必须经由白名单助手，把动态状态文本与 class 解耦
        self.assertIn('taskStateClass(stateText)', source)
        # 原始拼接（无论变量名 stateText / state / s）一律禁止
        self.assertNotIn("st-' + stateText", source)
        self.assertNotIn("st-' + state", source)
        self.assertNotIn("st-' + ", source)

    def test_dynamic_state_text_remains_text_content_safe(self):
        source = self.MACHINE_JS.read_text()
        # class 由白名单给出；动态文本仍由 textContent 写入（h() 内部）
        self.assertIn('task-state ', source)
        self.assertIn('textContent', source)


class Task66SseReconnectSourceTests(unittest.TestCase):
    """Task 66: SSE 传输错误不得依赖 EventSource 对原始 since=0 地址的无限
    自动重连；必须关闭失败源并调度至多一个有界延迟重连，重连时读取 store
    最新 getLastEventTs() 重建 URL。本类为源码契约，执行路径由
    ``sse_reconnect`` Node fixture 覆盖。
    """

    def test_onerror_closes_failed_source_instead_of_relaying_on_autoreconnect(self):
        source = SSE_JS.read_text()
        self.assertIn('_closeAndScheduleReconnect', source)
        self.assertIn('this._source.close()', source)
        # 失败源必须在 onerror 内被关闭并置空，才能禁用 EventSource 自带的
        # 对初始 URL 的无限自动重连
        self.assertIn('source.close()', source)
        self.assertIn('this._source = null', source)

    def test_reconnect_is_bounded_and_uses_latest_store_cursor(self):
        source = SSE_JS.read_text()
        self.assertIn('RECONNECT_DELAY_MS', source)
        self.assertIn('_scheduleReconnect', source)
        self.assertIn('_doReconnect', source)
        # 重连本体必须经过 start()，start() 读 store.getLastEventTs() 重建 URL
        self.assertIn('_doReconnect = function', source)
        self.assertIn('this.start()', source)

    def test_reconnect_total_is_bounded_by_max_reconnects(self):
        source = SSE_JS.read_text()
        # 重连总次数有上限（与 MAX_POLLS 同量级的有界降级习惯）
        self.assertIn('export var MAX_RECONNECTS', source)
        self.assertIn('MAX_RECONNECTS = MAX_POLLS', source)
        # 调度前检查上限，达到后不再调度
        self.assertIn('if (this._reconnectCount >= MAX_RECONNECTS)', source)
        self.assertIn('this._reconnectCount += 1', source)

    def test_reconnect_counter_resets_only_on_successful_open(self):
        source = SSE_JS.read_text()
        # 计数仅由 onopen（成功建立连接）复位；onerror 只递增、绝不复位
        self.assertIn('self._reconnectCount = 0;', source)
        self.assertIn('self._cancelReconnect();', source)
        # stop() 卸载时也清空计数
        self.assertIn('this._reconnectCount = 0;', source)

    def test_reconnect_timer_is_singleton_and_cancelled_on_open_and_stop(self):
        source = SSE_JS.read_text()
        self.assertIn('if (this._reconnectTimer)', source)
        self.assertIn('_cancelReconnect', source)
        # onopen 触发时取消未决重连；stop() 同样取消
        self.assertIn('self._cancelReconnect(); // 连接恢复', source)

    def test_explicit_start_cancels_pending_reconnect_timer(self):
        source = SSE_JS.read_text()
        # 显式 start() 取代未决的孤儿重连计时器（防其后台再次触发重建）
        self.assertIn('this._cancelReconnect();', source)
        self.assertIn('// 显式 start() 取代任何未决的延迟重连', source)

    def test_stale_source_callbacks_are_noops(self):
        source = SSE_JS.read_text()
        # onopen / onerror 都以 source 持有者比对作为前置护栏
        self.assertIn('if (self._source !== source)', source)

    def test_stop_cancels_pending_reconnect(self):
        source = SSE_JS.read_text()
        self.assertIn('_cancelReconnect()', source)
        self.assertIn('clearTimeout', source)


class AgentTypesDynamicDropdownTests(unittest.TestCase):
    """任务创建表单 agent 下拉的动态化 source 契约（pi pack 第 5 层）。

    下拉清单不再硬编码：首屏渲染内置兜底（FALLBACK_AGENT_TYPES，值必须
    ⊆ agent_profiles.EXECUTABLE_AGENT_TYPES 注册表），随后经注入的
    ``clientMethods.listAgentTypes``（GET /api/agent-types）异步覆盖；
    API 失败时静默保留兜底。填充助手去重且绝不写 innerHTML。

    请求放大契约：listAgentTypes 由 mountMachine 发起一次并共享
    （agentTypesPromise），createTaskForm 随 render 重建时只消费结果——
    render 被 SSE 高频触发，绝不在重建路径内重发请求。
    """

    MACHINE_JS = FRONTEND_DIR / 'views' / 'machine.js'
    CLIENT_JS = FRONTEND_DIR / 'api' / 'client.js'
    INDEX_HTML = FRONTEND_DIR / 'index.html'

    def test_fallback_agent_types_subset_of_executable_registry(self):
        # F5 一致性契约：FALLBACK_AGENT_TYPES 的每个值都必须是
        # agent_profiles.EXECUTABLE_AGENT_TYPES 的成员。兜底清单若混入
        # executable=True 但无 default_command 的 family（或注册表已改名），
        # API 失败时下拉会渲染出永远 exit 127 的选项——此测试使其必然失败。
        from agent_profiles import EXECUTABLE_AGENT_TYPES
        source = self.MACHINE_JS.read_text()
        match = re.search(
            r'var FALLBACK_AGENT_TYPES\s*=\s*\[([^\]]*)\]', source)
        self.assertIsNotNone(match,
                             "FALLBACK_AGENT_TYPES 数组声明必须存在且为字面量")
        fallback = re.findall(r"'([^']+)'", match.group(1))
        self.assertTrue(fallback, "兜底清单不允许为空")
        unknown = set(fallback) - set(EXECUTABLE_AGENT_TYPES)
        self.assertEqual(unknown, set(),
                         f"兜底清单混入注册表之外的 family: {sorted(unknown)}")

    def test_machine_dropdown_uses_fallback_then_api_overwrite(self):
        source = self.MACHINE_JS.read_text()
        # 内置兜底清单存在且含全部四个注册表 family（值 ⊆ EXECUTABLE_AGENT_TYPES）
        self.assertIn('FALLBACK_AGENT_TYPES', source)
        for family in ('codex', 'claude_code', 'hermes', 'pi'):
            self.assertIn(f"'{family}'", source)
        # 填充必须经由助手（去重 + 非 innerHTML），异步覆盖以 isActive 护栏
        self.assertIn('fillAgentSelect(select, FALLBACK_AGENT_TYPES)', source)
        self.assertIn('fillAgentSelect(select, types)', source)
        # 覆盖只在视图仍活跃时发生（卸载后不触碰 DOM）
        self.assertIn('isActive() && Array.isArray(types)', source)

    def test_agent_types_request_shared_per_mount_not_per_render(self):
        source = self.MACHINE_JS.read_text()
        # 请求只在 mount 生命周期发起一次（agentTypesPromise 共享），
        # createTaskForm 签名消费 promise 而非 clientMethods.listAgentTypes
        self.assertIn('var agentTypesPromise', source)
        self.assertIn('agentTypesPromise)', source)
        # 只截取 createTaskForm 函数体（至下一个顶层分区注释），排除其后
        # mountMachine 内合法的一次性请求调用点
        form_body = source.split('function createTaskForm', 1)[1].split(
            '/* --- store 读取辅助', 1)[0]
        # 表单重建路径内不出现请求调用（request 放大回归守卫）
        self.assertNotIn('listAgentTypes()', form_body)

    def test_client_owns_agent_types_path_and_parser(self):
        source = self.CLIENT_JS.read_text()
        # 路径唯一来源在 client 的 API map（字面量，便于 source 契约校验）
        self.assertIn("agentTypes: '/agent-types'", source)
        self.assertIn('export async function listAgentTypes(', source)
        # 解析必须走 contracts 契约层（allowlist，不透传原始响应）
        self.assertIn('parseAgentTypes(await request(API.agentTypes))', source)

    def test_contracts_export_agent_types_parser(self):
        source = CONTRACTS_JS.read_text()
        self.assertIn('export function parseAgentTypes(', source)

    def test_shell_wires_list_agent_types_into_client(self):
        source = self.INDEX_HTML.read_text()
        self.assertIn('listAgentTypes,', source)
        self.assertIn('listAgentTypes: listAgentTypes,', source)


class Task16ShellContractTests(unittest.TestCase):
    """Task 16 Step 2 shell contracts: task view is wired into the static shell.

    index.html must import/mount the task view, resolve ``/task/<id>`` or
    ``?task=<id>``, keep fleet/machine routes, and preserve the single SSE
    terminal-refresh owner (``SseClient(store, { client: client })``).  It must
    never render task/observation business objects itself, and must not wire a
    second terminal-refresh hook (``setRefreshTaskHandler``).
    """

    def test_index_imports_and_mounts_task_view(self):
        source = INDEX_HTML.read_text()
        self.assertIn("import { mountTask } from './views/task.js'", source)
        self.assertIn('mountTask(target, taskId, store, client)', source)

    def test_index_resolves_task_id_from_path_or_query(self):
        source = INDEX_HTML.read_text()
        self.assertIn('function resolveTask', source)
        self.assertIn("if (segments[i] === 'task' && segments[i + 1])", source)
        self.assertIn("params.get('task')", source)
        # 路径段必须解码（URL 编码的任务 id）
        self.assertIn('decodeURIComponent', source)

    def test_index_preserves_fleet_and_machine_routes(self):
        source = INDEX_HTML.read_text()
        self.assertIn('mountFleet(target, store, client)', source)
        self.assertIn('mountMachine(target, machine, store, client)', source)
        self.assertIn("params.get('machine')", source)

    def test_index_preserves_single_terminal_refresh_owner(self):
        source = INDEX_HTML.read_text()
        # 终态刷新仍然唯一由注入 client 的 SseClient 负责
        self.assertIn('new SseClient(store, { client: client })', source)
        self.assertNotIn('store.setRefreshTaskHandler', source)
        self.assertNotIn('getRefreshTaskHandler', source)

    def test_index_keeps_task_route_resolution_without_embedded_business_data(self):
        source = INDEX_HTML.read_text()
        self.assertIn("target.textContent = '未指定任务'", source)
        # Task 视图自身的业务渲染仍留在 views/task.js，不内嵌到 index.html
        self.assertNotIn('data-task-id="', source)
        self.assertNotIn('state-node', source)


class TaskViewBoundaryTests(unittest.TestCase):
    """Task 16 Step 1 view boundary source contracts.

    task.js must:
      - export ``mountTask(root, taskId, store, client) -> teardown``;
      - refresh via injected client methods (getTask / cancelTask / retryTask /
        pauseTask / continueTask / confirmTask / rejectTask / getTaskDiff),
        never via ``fetch`` / ``EventSource`` / ``location.reload``;
      - subscribe to the FleetStore for task state and bounded task_logs;
      - render into DOM-safe APIs (createElement / textContent / setAttribute)
        without any HTML-string construction;
      - bind cancel/retry through delegated listeners, guard in-flight actions
        and fence teardown against late callbacks;
      - keep bounded diff/log output and reject raw internal fields on display.
    """

    TASK_JS = FRONTEND_DIR / 'views' / 'task.js'

    def test_task_view_exports_mount_interface(self):
        source = self.TASK_JS.read_text()
        self.assertIn('export function mountTask(root, taskId, store, client)',
                      source)

    def test_task_view_refreshes_via_client_not_reload_or_fetch(self):
        source = self.TASK_JS.read_text()
        self.assertIn('getTask', source)
        self.assertNotIn('location.reload', source)
        self.assertNotIn('fetch(', source)
        self.assertNotIn('EventSource', source)
        self.assertNotIn('XMLHttpRequest', source)

    def test_task_view_uses_injected_client_methods(self):
        source = self.TASK_JS.read_text()
        for token in ('clientMethods.getTask', 'clientMethods.cancelTask',
                      'clientMethods.retryTask', 'clientMethods.pauseTask',
                      'clientMethods.continueTask', 'clientMethods.confirmTask',
                      'clientMethods.rejectTask', 'clientMethods.getTaskDiff'):
            self.assertIn(token, source, f'task.js missing {token}')

    def test_task_view_subscribes_to_store_for_logs_and_state(self):
        source = self.TASK_JS.read_text()
        self.assertIn('store.subscribe', source)
        self.assertIn('store.getState()', source)
        self.assertIn('taskLogs', source)
        self.assertIn('store.setTask(res)', source)

    def test_task_view_uses_dom_safe_writes(self):
        source = self.TASK_JS.read_text()
        for banned in ('innerHTML', 'insertAdjacentHTML', 'outerHTML',
                       'document.write'):
            self.assertNotIn(banned, source, f'task.js used {banned}')
        for required in ('createElement', 'textContent', 'setAttribute'):
            self.assertIn(required, source, f'task.js missing {required}')

    def test_task_view_has_visible_loading_error_empty_states(self):
        source = self.TASK_JS.read_text()
        self.assertIn('加载中', source)
        self.assertIn('加载失败', source)
        self.assertIn('任务不存在或已过期', source)
        self.assertIn('任务尚未完成', source)
        self.assertIn('暂无日志', source)

    def test_task_view_renders_instruction_stateflow_and_result(self):
        source = self.TASK_JS.read_text()
        for token in ('指令', '状态', '结果', 'diff 摘要', '实时日志',
                      '测试摘要', '脱敏 patch', 'stateflow', 'state-node',
                      'diffstat'):
            self.assertIn(token, source, f'task.js missing {token}')

    def test_task_view_binds_actions_via_delegation_and_guards_inflight(self):
        source = self.TASK_JS.read_text()
        self.assertIn('addEventListener', source)
        self.assertIn("data-action", source)
        self.assertIn('if (disposed || self.busy)', source)
        self.assertIn('self.busy = true', source)
        self.assertIn('self.busy = false', source)

    def test_task_view_fences_teardown_against_late_callbacks(self):
        source = self.TASK_JS.read_text()
        self.assertIn('var disposed', source)
        self.assertIn('if (disposed) {', source)
        self.assertIn('disposed = true', source)
        self.assertIn('return function teardown', source)

    def test_task_view_keeps_bounded_diff_and_log_output(self):
        source = self.TASK_JS.read_text()
        self.assertIn('MAX_DIFF_RENDER', source)
        self.assertIn('MAX_LOG_RENDER', source)
        self.assertIn('slice(0, MAX_DIFF_RENDER)', source)
        self.assertIn('slice(-MAX_LOG_RENDER)', source)

    def test_task_view_never_renders_raw_internal_fields(self):
        source = self.TASK_JS.read_text()
        for banned in ('attempt_id', 'nonce', 'client_token'):
            self.assertNotIn(banned, source, f'task.js referenced {banned}')

    def test_task_view_has_no_flask_bootstrap_or_path_handcoding(self):
        source = self.TASK_JS.read_text()
        self.assertNotIn('url_for', source)
        self.assertNotIn('window.FleetConfig', source)
        self.assertNotIn("'/api", source)
        self.assertNotIn('${', source)

    def test_task_view_optional_session_deep_link(self):
        source = self.TASK_JS.read_text()
        self.assertIn("import { pagePath } from '../routes.js'", source)
        self.assertIn("pagePath('session', task.session_id)", source)
        self.assertIn("typeof task.session_id === 'string'", source)

    def test_task_view_exposes_pause_continue_gate_and_diff_panel(self):
        source = self.TASK_JS.read_text()
        for token in ("data-action", "'pause'", "'continue'", "'confirm'",
                      "'reject'", 'PAUSABLE_STATES', "state === 'paused'",
                      'has_diff_patch', 'test_summary', 'getTaskDiff',
                      'MAX_PATCH_RENDER'):
            self.assertIn(token, source, f'task.js missing {token}')


if __name__ == '__main__':
    unittest.main()
