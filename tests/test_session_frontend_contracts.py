"""Source-contract tests for the standalone session timeline (Task 11).

Guards the additive frontend session surface so it can never become a raw
transcript/control-data leak:

- contract parsers in ``frontend/api/contracts.js`` must allowlist session
  metadata / events / policy-signals / control receipts and *reject* internal
  fields (``nonce`` / ``client_token`` / ``token`` / ``signature``);
- ``frontend/api/client.js`` must offer the session detail stream getters as
  the only HTTP path into the session data plane (views never call ``fetch``);
- ``frontend/views/session.js`` is a DOM-safe timeline view (no ``fetch`` /
  ``EventSource`` / ``innerHTML`` / dynamic class concatenation) that
  explicitly renders each event kind and the control queue states
  (queued / executing / succeeded / already_finished / failed / expired);
- ``frontend/index.html`` mounts ``/session/<id>`` per the existing static
  `pageFromPath` / `data-route-view` pattern and owns no business data;
- SSE handling stays allow-list only: ``session_update`` / ``session_summary``
  event frames carry just session_id / cursor / sequence / kind / quality and
  timestamp — never payload text, never transcript, never credentials.

Like ``tests/test_frontend_contracts.py`` these are source-contract assertions
plus optional Node fixture execution when ``node`` is available (an optional
local verification tool, never a runtime dependency).
"""

import shutil
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = REPO_ROOT / 'frontend'
SESSION_JS = FRONTEND_DIR / 'views' / 'session.js'
CONTRACTS_JS = FRONTEND_DIR / 'api' / 'contracts.js'
CLIENT_JS = FRONTEND_DIR / 'api' / 'client.js'
SSE_JS = FRONTEND_DIR / 'realtime' / 'sse.js'
STORE_JS = FRONTEND_DIR / 'state' / 'store.js'
INDEX_HTML = FRONTEND_DIR / 'index.html'


# Node-only fixture runner: the session contract parsers and the store summary
# path are pure ES modules, so a single node invocation asserts them against
# synthetic, non-secret payloads.  Skipped when Node is unavailable.
_NODE_SESSION_FIXTURE_RUNNER = r"""
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

if (fixture === 'session_parsers') {
  // 1) managed session DTO
  assertModel(() => contracts.parseSession({ session: {
    machine_id: 'mac-1', session_id: 's-1', managed: true,
    capture_quality: 'exact', control_capability: 'available',
    attempt_id: 'att-1', process_group_id: 'grp-1',
    agent_family: 'codex', status: 'running' } }),
    (m) => m.session.managed === true &&
      m.session.capture_quality === 'exact' &&
      m.session.control_capability === 'available' &&
      m.session.attempt_id === 'att-1' &&
      !Object.prototype.hasOwnProperty.call(m.session, 'nonce'),
    'session_parsers.managed');
  // 2) unmanaged forced to best_effort and control unavailable
  assertModel(() => contracts.parseSession({ session: {
    machine_id: 'mac-2', session_id: 's-2', managed: false,
    capture_quality: 'structured', control_capability: 'available' } }),
    (m) => m.session.managed === false &&
      m.session.capture_quality === 'best_effort' &&
      m.session.control_capability === 'unavailable',
    'session_parsers.unmanaged_fallback');
  // 3) session DTO must reject nonce/client_token/token/signature
  assertThrows(() => contracts.parseSession({ session: {
    session_id: 's-3', nonce: 'n-1' } }), 'session_parsers.reject_nonce');
  assertThrows(() => contracts.parseSession({ session: {
    session_id: 's-3', client_token: 'tok' } }), 'session_parsers.reject_client_token');
  assertThrows(() => contracts.parseSession({ session: {
    session_id: 's-3', token: 'sk-abc' } }), 'session_parsers.reject_token');
  assertThrows(() => contracts.parseSession({ session: {
    session_id: 's-3', signature: 'sig' } }), 'session_parsers.reject_signature');
  // 4) list surface allow-lists to the same row shape
  assertModel(() => contracts.parseSessionList({ sessions: [
    { machine_id: 'm', session_id: 's-4', managed: false,
      capture_quality: 'best_effort', control_capability: 'unavailable' } ] }),
    (m) => Array.isArray(m.sessions) && m.sessions.length === 1 &&
      m.sessions[0].session_id === 's-4' && m.sessions[0].managed === false,
    'session_parsers.list');
  // 5) events surface keeps payload text but drops unknown and secret keys
  assertModel(() => contracts.parseSessionEvents({ events: [
    { schema_version: 1, event_id: 'e1', stream_id: 'st', machine_id: 'm',
      session_id: 's', sequence: 1, kind: 'user_message',
      capture_quality: 'structured', emitted_at: '2026-08-26T00:00:00Z',
      payload: { text: 'hello', is_complete: true, raw_log: 'x' } },
    { schema_version: 1, event_id: 'e2', stream_id: 'st', machine_id: 'm',
      session_id: 's', sequence: 2, kind: 'capture_gap',
      capture_quality: 'structured', emitted_at: '2026-08-26T00:00:01Z',
      payload: { start_sequence: 2, end_sequence: 4, reason: 'degraded',
                 quality: 'best_effort' } } ] }),
    (m) => m.events.length === 2 && m.events[0].kind === 'user_message' &&
      m.events[0].payload.text === 'hello' &&
      !Object.prototype.hasOwnProperty.call(m.events[0].payload, 'raw_log') &&
      m.events[1].payload.quality === 'best_effort' &&
      typeof m.events[1].payload.start_sequence === 'number',
    'session_parsers.events_bounded');
  // 6) events surface rejects a top-level nonce / token
  assertThrows(() => contracts.parseSessionEvents({ events: [
    { event_id: 'e3', session_id: 's', sequence: 1, kind: 'user_message',
      nonce: 'n-2' } ] }), 'session_parsers.events_reject_nonce');
  // 7) policy signals keep only the bounded index
  assertModel(() => contracts.parseSessionSignals({ signals: [
    { session_id: 's', emitted_at: 't', severity: 'warn', reason: 'r',
      raw_collector: 'SECRET' } ] }),
    (m) => m.signals.length === 1 && m.signals[0].severity === 'warn' &&
      !Object.prototype.hasOwnProperty.call(m.signals[0], 'raw_collector'),
    'session_parsers.signals_bounded');
  // 8) control receipts allow command/action/status and reject nonce/token
  assertThrows(() => contracts.parseControlReceipts({ receipts: [
    { command_id: 'c1', status: 'executing', nonce: 'n-3' } ] }),
    'session_parsers.receipts_reject_nonce');
  assertModel(() => contracts.parseControlReceipts({ receipts: [
    { command_id: 'c1', action: 'cancel_attempt', status: 'queued',
      issued_at: 't', expires_at: 't2', reason: 'r' } ] }),
    (m) => m.receipts.length === 1 && m.receipts[0].status === 'queued' &&
      m.receipts[0].action === 'cancel_attempt',
    'session_parsers.receipts_bounded');
  assertModel(() => contracts.parseAdoption({ ok: true, adoption_id: 'a1',
    session_id: 's1', machine_id: 'm1', status: 'pending', request_id: 'r1' }),
    (m) => m.ok === true && m.session_id === 's1' && m.status === 'pending',
    'session_parsers.adoption_bounded');
  assertThrows(() => contracts.parseAdoption({ session_id: 's1', nonce: 'n-4' }),
    'session_parsers.adoption_reject_nonce');
  assertModel(() => contracts.parseControlEnqueue({ ok: true, command_id: 'c2',
    session_id: 's1', action: 'pause_session', status: 'pending' }),
    (m) => m.action === 'pause_session' && m.status === 'pending' &&
      m.command_id === 'c2',
    'session_parsers.control_enqueue_bounded');
  assertThrows(() => contracts.parseControlEnqueue({
    command_id: 'c2', action: 'pause_session', status: 'pending', pid: 7 }),
    'session_parsers.control_enqueue_reject_pid');
} else if (fixture === 'session_sse_summary') {
  // SSE summary: only session_id / cursor / sequence / kind / quality / status / ts
  const sum = contracts.parseSseEvent('session_update', {
    session_id: 's', cursor: 7, sequence: 3, kind: 'tool_call',
    capture_quality: 'structured', status: 'running', ts: 1111,
    token: 'SECRET', text: 'raw',
  });
  if (sum.session_id === 's' && sum.cursor === 7 && sum.sequence === 3 &&
      sum.kind === 'tool_call' && sum.status === 'running' &&
      !Object.prototype.hasOwnProperty.call(sum, 'token') &&
      !Object.prototype.hasOwnProperty.call(sum, 'text')) {
    ok('session_sse.allowlist');
  } else {
    fail('session_sse.allowlist', JSON.stringify(sum));
  }
  const sum2 = contracts.parseSseEvent('session_summary', {
    session_id: 's2', cursor: 1, ts: 2 });
  if (sum2.session_id === 's2' && sum2.cursor === 1) ok('session_sse.summary_alias');
  else fail('session_sse.summary_alias', JSON.stringify(sum2));
} else if (fixture === 'session_store_summary') {
  const storeMod = await import(process.env.FLEET_STORE_URL);
  const S = storeMod.FleetStore;
  const MAX_SUM = storeMod.MAX_SESSION_SUMMARIES;
  const store = new S();
  // summary applied → getState shows the session summary map
  const applied = store.applySseEvent({ type: 'session_update', data: {
    session_id: 's-1', sequence: 5, cursor: 9, ts: 100 } });
  const state = store.getState();
  if (applied === true && state.sessionSummaries &&
      state.sessionSummaries['s-1'] &&
      state.sessionSummaries['s-1'].sequence === 5 &&
      state.lastEventTs === 100) {
    ok('session_store.applied_and_summarized');
  } else {
    fail('session_store.applied_and_summarized',
      'applied=' + applied + ' sum=' + JSON.stringify(state.sessionSummaries));
  }
  // stale summary (ts < lastEventTs) → rejected, never applied
  const store2 = new S();
  store2.applySseEvent({ type: 'session_update', data: {
    session_id: 's-1', sequence: 1, ts: 50 } });
  store2.applySseEvent({ type: 'session_update', data: {
    session_id: 's-1', sequence: 2, ts: 25 } });
  const st2 = store2.getState();
  if (st2.lastEventTs === 50 &&
      st2.sessionSummaries['s-1'] &&
      st2.sessionSummaries['s-1'].sequence === 1) {
    ok('sum_store.rejects_stale_ts');
  } else {
    fail('store.rejects_stale_ts',
      'last=' + st2.lastEventTs + ' sum=' + JSON.stringify(st2.sessionSummaries));
  }
  // cap: sessionSummaries map is bounded by MAX_SESSION_SUMMARIES, oldest evicted
  const s3 = new S();
  for (let i = 0; i < MAX_SUM + 10; i += 1) {
    s3.applySseEvent({ type: 'session_summary', data: {
      session_id: 'sum-' + i, cursor: i, ts: 1000 + i } });
  }
  const keys = Object.keys(s3.getState().sessionSummaries);
  const st3 = s3.getState();
  if (keys.length <= MAX_SUM &&
      st3.sessionSummaries['sum-' + (MAX_SUM + 9)] !== undefined &&
      st3.sessionSummaries['sum-0'] === undefined) {
    ok('store.bounded_summary_cap');
  } else {
    fail('store.bounded_summary_cap', 'keys=' + keys.length);
  }
} else if (fixture === 'unknown_session_type') {
  const storeMod = await import(process.env.FLEET_STORE_URL);
  const store2 = new (storeMod.FleetStore)();
  store2.applySseEvent({ type: 'session_update', data: {
    session_id: 's-1', sequence: 1, ts: 50 } });
  // an unrelated unknown event type still falls through the unknown branch
  const unknownOk = store2.applySseEvent({ type: 'bogus_type', data: { ts: 99 } });
  if (unknownOk === false && store2.getLastEventTs() === 99) {
    ok('unknown_session.unknown_type_fence_only');
  } else {
    fail('unknown_session.unknown_type_fence_only', 'rc=' + unknownOk);
  }
}

if (failures > 0) {
  console.error(failures + ' fixture assertion(s) failed');
  process.exit(1);
}
console.log('fixture ' + fixture + ': PASS');
"""


class SessionFrontendModuleTests(unittest.TestCase):
    """Task 11 Step 1 source contracts: module existence and export surface."""

    def test_session_view_module_exists(self):
        self.assertTrue(SESSION_JS.is_file(), 'frontend/views/session.js missing')

    def test_session_view_exports_mount_session(self):
        source = SESSION_JS.read_text()
        self.assertIn('export function mountSession(root, sessionId, store, client)',
                      source)
        # The teardown contract mirrors task/machine views.
        self.assertIn('return function teardown', source)
        self.assertIn('disposed', source)

    def test_session_view_has_explicit_states(self):
        source = SESSION_JS.read_text()
        for token in ('加载中', '加载失败', '暂无事件', '暂无策略信号',
                      '会话不存在或已过期', '无控制记录'):
            self.assertIn(token, source, f'缺 visible state text {token}')

    def test_session_view_dom_safe_writes(self):
        source = SESSION_JS.read_text()
        for banned in ('innerHTML', 'insertAdjacentHTML', 'outerHTML',
                       'document.write'):
            self.assertNotIn(banned, source, f'session.js used {banned}')
        for required in ('createElement', 'textContent', 'setAttribute'):
            self.assertIn(required, source, f'session.js missing {required}')

    def test_session_view_no_direct_http_or_stream(self):
        source = SESSION_JS.read_text()
        for banned in ('fetch(', 'EventSource', 'XMLHttpRequest',
                       'location.reload'):
            self.assertNotIn(banned, source, f'session.js used {banned}')

    def test_session_view_uses_injected_client_methods_only(self):
        source = SESSION_JS.read_text()
        for token in ('clientMethods.getSession', 'clientMethods.getSessionEvents',
                      'clientMethods.getSessionSignals'):
            self.assertIn(token, source, f'session.js missing {token}')
        self.assertIn('store.subscribe', source)
        self.assertNotIn('url_for', source)
        self.assertNotIn('window.FleetConfig', source)
        self.assertNotIn("'/api", source)

    def test_session_views_navigation_encoded_with_page_path(self):
        source = SESSION_JS.read_text()
        self.assertIn("import { pagePath } from '../routes.js'", source)
        self.assertIn('setAttribute(\'href\',', source)

    def test_session_view_never_renders_raw_internal_fields(self):
        source = SESSION_JS.read_text()
        # 视图不得把 raw token / nonce / secret 值当作可见文本渲染；惟一的
        # 可见 attempt_id 是受管会话的公开 opaque 标识，已由契约校验。
        for banned in ('nonce', 'client_token'):
            self.assertNotIn(banned, source, f'session.js wrote internal field {banned}')
        self.assertNotIn('BEGIN PRIVATE KEY', source)
        self.assertNotIn('SELECT ', source)

    def test_session_view_gap_and_control_state_render_allowlisted(self):
        source = SESSION_JS.read_text()
        # capture_gap 必须被专属渲染（缺口即降级），而不是默默吞掉。
        self.assertIn('gap', source)
        # 控制队列状态必须显式渲染文本（queued/成功等），不能直接拼状态名。
        for state in ('queued', 'executing', 'succeeded', 'already_finished',
                      'failed', 'expired', 'rejected'):
            self.assertIn(f"'{state}'", source,
                          f'session.js 缺控制状态 {state}')
        # 显式白名单类映射；绝不把状态名直接拼进 class。
        self.assertIn('CONTROL_STATE_CLASS', source)
        self.assertIn('CONTROL_STATE_FALLBACK_CLASS', source)
        self.assertNotIn("'ct-' + ", source)
        self.assertNotIn("ct-' + ", source)

    def test_session_view_optional_task_deep_link(self):
        source = SESSION_JS.read_text()
        self.assertIn('firstBoundTaskId', source)
        self.assertIn("pagePath('task', boundTaskId)", source)
        self.assertIn("payload.task_id", source)

    def test_session_view_bounds_rendering(self):
        source = SESSION_JS.read_text()
        for token in ('MAX_EVENT_RENDER', 'MAX_SIGNAL_RENDER',
                      'MAX_TEXT_RENDER'):
            self.assertIn(token, source, f'session.js missing {token}')
        self.assertIn('slice(', source)

    def test_session_contracts_exports_session_parsers(self):
        source = CONTRACTS_JS.read_text()
        for fn in ('parseSession', 'parseSessionList', 'parseSessionEvents',
                   'parseSessionSignals', 'parseControlReceipts',
                   'parseAdoption', 'parseAdoptionList', 'parseControlEnqueue'):
            self.assertIn(f'export function {fn}(', source,
                          f'contracts missing {fn}')

    def test_session_contracts_contains_session_allowlists(self):
        source = CONTRACTS_JS.read_text()
        for token in ('SESSION_FIELDS', 'SESSION_EVENT_FIELDS',
                      'SESSION_SIGNAL_FIELDS', 'CONTROL_RECEIPT_FIELDS',
                      'SSE_SESSION_SUMMARY_FIELDS', 'EVENT_PAYLOAD_FIELDS'):
            self.assertIn(token, source, f'contracts missing {token}')
        # 内部字段拒绝守卫必须存在于 session 解析路径。
        self.assertIn('nonce', source)
        self.assertIn('client_token', source)

    def test_session_contracts_sse_summary_is_allowlisted(self):
        source = CONTRACTS_JS.read_text()
        for token in ("name === 'session_update'",
                      "name === 'session_summary'"):
            self.assertIn(token, source, f'contracts missing {token}')
        self.assertIn('cursor', source)
        self.assertNotIn('snapshot', SESSION_JS.read_text())

    def test_session_client_surface_has_session_getters(self):
        source = CLIENT_JS.read_text()
        for fn in ('getSession', 'getSessionEvents', 'getSessionSignals',
                   'listSessions', 'listAdoptions', 'adoptInstance',
                   'revokeAdoption', 'retryAdoption', 'controlSession'):
            self.assertIn(f'export async function {fn}(', source)
        self.assertIn("'/sessions'", source)
        self.assertIn("'/adoption/'", source)
        self.assertIn("'/source/control'", source)

    def test_session_view_wires_five_actions_without_stdin(self):
        source = SESSION_JS.read_text()
        self.assertIn('renderSourceControl', source)
        self.assertIn('controlSession', source)
        self.assertIn('pause_session', source)
        self.assertIn("createElement('textarea')", source)
        self.assertIn('append_user_turn', source)
        self.assertIn('apply_local_profile', source)
        self.assertIn('controlFeatures', source)
        self.assertIn('data-role', source)
        self.assertIn('follow-up', source)
        self.assertNotIn('inject_stdin', source)
        self.assertNotIn('composer', source)
        self.assertNotIn('EventSource', source)
        self.assertNotIn('innerHTML', source)
        self.assertIn('followUpText', source)
        self.assertIn('box.value = viewState.followUpText', source)

    def test_sse_accepts_session_update_summary_but_not_other_events(self):
        source = SSE_JS.read_text()
        # 新事件类型到 parse + store 的唯一路径；未改现有事件名。
        self.assertIn('session_update', source)
        self.assertIn('session_summary', source)
        self.assertNotIn('session_start', source)
        # 三旧事件类型名称依然在位
        self.assertIn("'machine_update'", source)
        self.assertIn("'task_log'", source)
        self.assertIn("'fleet_event'", source)

    def test_sse_summary_only_allowlisted_fields_flow_to_view(self):
        source = SSE_JS.read_text()
        # 概要只包含公开摘要字段，绝不透传原始 transcript/token。
        for token in ('cursor', 'sequence'):
            self.assertIn(token, source, f'sse.js missing {token}')

    def test_index_html_mounts_session_route(self):
        source = INDEX_HTML.read_text()
        self.assertIn('mount-session', source)
        self.assertIn('data-route-view="session"', source)
        self.assertIn("import { mountSession } from './views/session.js'", source)
        self.assertIn("function resolveSession", source)
        self.assertIn("if (segments[i] === 'session'", source)
        self.assertIn("mountSession(target", source)
        # 不内嵌会话业务数据
        for token in ('data-session="', 'session_id', 'capture_quality'):
            self.assertNotIn(token, source)

    def test_session_view_has_no_embedded_snapshot_or_business_data(self):
        source = SESSION_JS.read_text()
        # 视图是纯 JS 模块：不内嵌任何来自后端/上传的原始文本或快照常量。
        self.assertNotIn('snapshot', source)
        self.assertNotIn('${', source)
        self.assertNotIn('BEGIN PRIVATE KEY', source)


class SessionFrontendNodeFixtureTests(unittest.TestCase):
    """Node fixtures (pure parsers + store) — skipped when node is absent."""

    NODE = shutil.which('node')

    def _run_fixture(self, fixture_name):
        env = dict(__import__('os').environ)
        env['FLEET_CONTRACTS_URL'] = CONTRACTS_JS.resolve().as_uri()
        env['FLEET_STORE_URL'] = STORE_JS.resolve().as_uri()
        env['FLEET_SSE_URL'] = SSE_JS.resolve().as_uri()
        env['FLEET_FIXTURE'] = fixture_name
        proc = subprocess.run(
            [self.NODE, '--input-type=module', '-e', _NODE_SESSION_FIXTURE_RUNNER],
            capture_output=True, text=True, env=env, timeout=30,
        )
        if proc.returncode != 0:
            self.fail(
                f'fixture {fixture_name} failed:\nSTDOUT:\n{proc.stdout}\n'
                f'STDERR:\n{proc.stderr}')

    @unittest.skipUnless(shutil.which('node'),
                         'node unavailable; skipping JS fixture')
    def test_session_parsers_fixture(self):
        self._run_fixture('session_parsers')

    @unittest.skipUnless(shutil.which('node'),
                         'node unavailable; skipping JS fixture')
    def test_session_sse_summary_fixture(self):
        self._run_fixture('session_sse_summary')

    @unittest.skipUnless(shutil.which('node'),
                         'node unavailable; skipping JS fixture')
    def test_session_store_summary_fixture(self):
        self._run_fixture('session_store_summary')

    @unittest.skipUnless(shutil.which('node'),
                         'node unavailable; skipping JS fixture')
    def test_unknown_session_type_fixture(self):
        self._run_fixture('unknown_session_type')


if __name__ == '__main__':
    unittest.main()