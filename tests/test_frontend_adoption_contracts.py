"""Phase 2 frontend adoption contracts: machine page wires adopt + five actions.

The machine detail page still shows sanitized instance metadata, but Phase 2
wires ``onAdopt`` for attachable rows and drives source control from adoption
records loaded through ``client.js``. Views never own HTTP paths.
"""

import os
import shutil
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = REPO_ROOT / 'frontend'
MACHINE_JS = FRONTEND_DIR / 'views' / 'machine.js'
CLIENT_JS = FRONTEND_DIR / 'api' / 'client.js'
CONTRACTS_JS = FRONTEND_DIR / 'api' / 'contracts.js'

INSTANCE_FIELDS = ('pid', 'pgid', 'exe_path', 'cmdline', 'agent_family',
                   'native_file_path', 'started_at', 'attachable')


class AdoptionPhase2ViewTests(unittest.TestCase):
    """machine.js Phase 2: metadata + wired adopt, no view-owned HTTP."""

    def test_machine_view_exports_readonly_instances_renderer(self):
        source = MACHINE_JS.read_text()
        self.assertIn('export function renderInstances(instances, onAdopt)',
                      source)

    def test_render_instances_is_wired_into_the_detail_view(self):
        source = MACHINE_JS.read_text()
        self.assertIn('renderInstances(live.instances, onAdopt)', source)
        self.assertIn('live.instances', source)

    def test_machine_snapshot_reads_instances_metadata(self):
        source = MACHINE_JS.read_text()
        self.assertIn('instances', source)
        self.assertIn('detail.current.instances', source)

    def test_render_instances_uses_dom_safe_writes_only(self):
        source = MACHINE_JS.read_text()
        for banned in ('innerHTML', 'insertAdjacentHTML', 'outerHTML',
                       'document.write'):
            self.assertNotIn(banned, source, f'machine.js used {banned}')
        for required in ('createElement', 'textContent', 'setAttribute'):
            self.assertIn(required, source, f'machine.js missing {required}')

    def test_render_instances_shows_pid_as_metadata_text_only(self):
        source = MACHINE_JS.read_text()
        begin = source.index('export function renderInstances')
        end = source.index('/* --- 任务列表', begin)
        block = source[begin:end]
        self.assertIn("fmtValue(inst.pid)", block)
        self.assertNotIn('href', block)
        self.assertIn('onAdopt(inst)', block)
        self.assertIn('addEventListener', block)

    def test_phase2_views_do_not_own_http(self):
        source = MACHINE_JS.read_text()
        for banned in ("'/api/adoptions'", "'/api/adopt'", '/api/sessions',
                       'transcript', 'native_transcript', 'process_handle',
                       'conversation', 'fetch(', 'innerHTML'):
            self.assertNotIn(banned, source, f'machine.js 出现 {banned}')

    def test_phase2_wires_onAdopt_and_onAction(self):
        source = MACHINE_JS.read_text()
        self.assertIn('onAdopt(inst)', source)
        self.assertIn('adoptInstance', source)
        self.assertIn('controlSession', source)
        self.assertIn('listAdoptions', source)
        self.assertIn('listSessions', source)

    def test_instance_list_is_bounded_metadata_display(self):
        source = MACHINE_JS.read_text()
        self.assertIn('MAX_VISIBLE_INSTANCES', source)
        self.assertIn('slice(0, MAX_VISIBLE_INSTANCES)', source)

    def test_client_owns_adoption_and_control_paths(self):
        source = CLIENT_JS.read_text()
        self.assertIn("adoptions: '/adoptions'", source)
        self.assertIn("'/adoption/'", source)
        self.assertIn("'/source/control'", source)
        begin = source.index('export async function controlSession')
        nxt = source.find('export async function', begin + 10)
        block = source[begin:nxt if nxt != -1 else begin + 900]
        self.assertIn('action', block)
        self.assertIn('reason_code', block)
        self.assertIn('payload', block)
        self.assertNotIn("'pid'", block)
        self.assertNotIn('signal', block)
        self.assertNotIn('shell', block)
        self.assertNotIn('inject_stdin', block)
        adopt = source[source.index('export async function adoptInstance'):
                       source.index('export async function adoptInstance') + 500]
        self.assertIn('machine_id', adopt)
        self.assertIn('pid', adopt)
        self.assertIn('started_at', adopt)


_CONTROL_START = '/* --- 源控制'
_CONTROL_END = '/* --- 源控制区结束'


def _source_control_block():
    source = MACHINE_JS.read_text()
    begin = source.index(_CONTROL_START)
    end = source.index(_CONTROL_END, begin)
    return source[begin:end] + '\n'


class SourceControlViewTests(unittest.TestCase):
    """machine.js 的受控源控制区（Task 10）：固定令牌 / DOM-safe / 无文本接口。

    The source-control section renders from a fixed five-token allowlist only,
    communicates status through a bounded whitelist map, and wires every click
    to ``onAction(action, session_id)`` — a fixed token, never a text/pid/
    signal shell.  ``client.js`` owns all HTTP; this view builds no path.
    """

    def test_source_control_has_fixed_five_action_tokens(self):
        source = MACHINE_JS.read_text()
        self.assertIn('SOURCE_ACTIONS_LABELS', source)
        begin = source.index('var SOURCE_ACTIONS_LABELS = {')
        end = source.index('};', begin)
        labels = source[begin:end]
        for token in ('pause_session', 'resume_session', 'terminate_session',
                      'quarantine_session', 'cancel_attempt'):
            self.assertIn(token, labels, f'action token {token} 缺失')

    def test_source_control_renderer_exported_and_wired(self):
        source = MACHINE_JS.read_text()
        self.assertIn(
            'export function renderSourceControl(source, actions, onAction)',
            source, 'renderSourceControl 未导出')
        self.assertIn('renderSourceControl(', source, '未挂载 renderSourceControl')

    def test_source_control_empty_state_composes_without_adoption(self):
        source = MACHINE_JS.read_text()
        # 视图没有纳管注册表时 mountMachine 以空 source + no-op 渲染空态，
        # 使区块完整组合而不产生任何副作用。
        self.assertIn('renderSourceControl(null, null, noop)', source)

    def test_source_control_block_dom_safe_writes_only(self):
        block = _source_control_block()
        for banned in ('innerHTML', 'insertAdjacentHTML', 'outerHTML',
                       'document.write', '<input', '<textarea'):
            self.assertNotIn(banned, block, f'源控制区使用了 {banned}')
        for required in ('createElement', 'textContent', 'setAttribute'):
            self.assertIn(required, block, f'源控制区缺少 {required}')

    def test_source_control_click_hands_fixed_token_only(self):
        block = _source_control_block()
        # 点击回调只传递固定动作令牌 + 有界 session_id，绝不读取表单文本/
        # pid/signal，也不拼接任何路径。
        self.assertIn('onAction(tk, source.session_id)', block)
        for leaked in ('.value', '.pid', '.signal', '.cmdline', '.exe_path',
                       'exec_shell', '/api/'):
            self.assertNotIn(leaked, block, f'源控制区引入了 {leaked}')

    def test_source_control_status_whitelist_map(self):
        source = MACHINE_JS.read_text()
        self.assertIn('SOURCE_STATUS_CLASSES', source)
        begin = source.index('var SOURCE_STATUS_CLASSES = {')
        end = source.index('};', begin)
        statuses = source[begin:end]
        for status in ('pending', 'adopted', 'revoked', 'terminal'):
            self.assertIn(status, statuses, f'状态白名单缺少 {status}')


class InstanceDtoContractTests(unittest.TestCase):
    """frontend/api/contracts.js 把 instances 保留在 machine current 上。"""

    def test_machine_current_allows_sanitized_instances(self):
        source = CONTRACTS_JS.read_text()
        self.assertIn("'instances'", source)
        for field in INSTANCE_FIELDS:
            self.assertIn("'%s'" % field, source, f'contracts missing {field}')

    def test_parse_machine_uses_instance_allowlist(self):
        source = CONTRACTS_JS.read_text()
        for token in ('INSTANCE_FIELDS', 'parseInstance', 'out.current.instances'):
            self.assertIn(token, source, f'contracts.js missing {token}')

    def test_contracts_applies_pure_dom_safe_writes(self):
        source = CONTRACTS_JS.read_text()
        # contracts 是纯 parser，绝不引用浏览器/DOM 或注入类 API
        for banned in ('innerHTML', 'insertAdjacentHTML', 'document.',
                       'setAttribute', 'textContent'):
            self.assertNotIn(banned, source, f'contracts.js 出现 {banned}')


INSTANCE_NODE_FIXTURE = r"""
const contracts = await import(process.env.FLEET_CONTRACTS_URL);
let failures = 0;
function fail(label, msg) { failures += 1; console.error('FAIL ' + label + ': ' + msg); }
function ok(label) { console.log('ok ' + label); }

const payload = {
  ok: true,
  machine: 'm1',
  current: {
    timestamp: 't',
    reachable: true,
    remote_error: null,
    agents: {},
    system: {},
    instances: [
      {
        pid: 7, pgid: 6, exe_path: '/bin/codex', cmdline: 'codex',
        agent_family: 'codex', native_file_path: null,
        started_at: '2026-08-30T00:00:00Z', attachable: true,
        conversation: ['private turn'], process_token: 'nope',
      }
    ]
  },
  history: []
};
const m = contracts.parseMachine(payload);
const inst = m.current.instances[0];
const keys = Object.keys(inst).sort();
const want = ['agent_family', 'attachable', 'cmdline', 'exe_path',
              'native_file_path', 'pgid', 'pid', 'started_at'];
if (JSON.stringify(keys) === JSON.stringify(want)) ok('instance.allowlist_keys');
else fail('instance.allowlist_keys', 'keys=' + keys.join(','));
if (inst.pid === 7 && inst.agent_family === 'codex' &&
    inst.attachable === true && inst.native_file_path === null) {
  ok('instance.bounded_values');
} else {
  fail('instance.bounded_values', JSON.stringify(inst));
}
if (inst.conversation === undefined && inst.drop === undefined) {
  ok('instance.drops_unknown');
} else fail('instance.drops_unknown', JSON.stringify(inst));

// 匿名 metadata-only 行 -> 归一为不可纳管的空身份
const anonymous = contracts.parseMachine({
  ok: true, machine: 'm1',
  current: {
    timestamp: 't', reachable: true, agents: {}, system: {}, instances: [
      { agent_family: 'codex', attachable: true, pid: null, pgid: null,
        secret: 'drop-me' }
    ]
  }, history: []
});
const anon = anonymous.current.instances[0];
if (anon.pid === null && anon.pgid === null &&
    anon.agent_family === 'codex' && anon.attachable === false &&
    anon.secret === undefined) {
  ok('instance.allows_anonymous_metadata');
} else fail('instance.allows_anonymous_metadata', JSON.stringify(anon));

// 部分身份仍必须拒绝，避免把不完整候选暴露给纳管流程
try {
  contracts.parseMachine({
    ok: true, machine: 'm1',
    current: {
      timestamp: 't', reachable: true, agents: {}, system: {}, instances: [
        { pgid: 1, exe_path: '/bin/bad', cmdline: '', agent_family: 'codex' }
      ]
    }, history: []
  });
  fail('instance.rejects_partial_identity', 'did not throw');
} catch (e) {
  if (e instanceof contracts.ContractError) ok('instance.rejects_partial_identity');
  else fail('instance.rejects_partial_identity', 'wrong error ' + String(e));
}

// 无 instances -> 默认空列表
const empty = contracts.parseMachine({
  ok: true, machine: 'm1',
  current: { timestamp: 't', reachable: true, agents: {}, system: {} },
  history: []
});
if (Array.isArray(empty.current.instances) && empty.current.instances.length === 0) {
  ok('instance.defaults_empty');
} else fail('instance.defaults_empty', JSON.stringify(empty.current.instances));

if (failures > 0) { console.error(failures + ' assertion(s) failed'); process.exit(1); }
console.log('fixture instance_parser: PASS');
""".replace('und\n', '')


class InstanceParserNodeFixtureTests(unittest.TestCase):
    """Run the contracts instance allowlist through node when available.

    Node is an optional local verification tool only; skipped when absent.
    """

    NODE = shutil.which('node')

    @unittest.skipUnless(shutil.which('node'),
                         'node unavailable; skipping JS fixture')
    def test_parse_machine_allowslists_instance_rows(self):
        env = dict(os.environ)
        env['FLEET_CONTRACTS_URL'] = CONTRACTS_JS.resolve().as_uri()
        env['FLEET_FIXTURE'] = 'instance_parser'
        proc = subprocess.run(
            [self.NODE, '--input-type=module', '-e', INSTANCE_NODE_FIXTURE],
            capture_output=True, text=True, env=env, timeout=30,
        )
        if proc.returncode != 0:
            self.fail(
                f'instance_parser fixture failed:\nSTDOUT:\n{proc.stdout}\n'
                f'STDERR:\n{proc.stderr}')


if __name__ == '__main__':
    unittest.main()