import pytest

from tools.browser_bridge.muse import MuseBridge, MuseBridgeError


def message(mid, role, text, busy=False):
    return {'id': mid, 'role': role, 'text': text, 'busy': busy}


class Transport:
    def __init__(self):
        self.messages = [message('old', 'assistant', 'old reply')]
        self.sends = []
        self.error = None
        self.draft = False

    def request(self, operation, **kwargs):
        if operation == 'send':
            self.sends.append(kwargs)
            if self.error:
                raise MuseBridgeError(self.error)
            self.messages.append(message('user-1', 'user', kwargs['prompt']))
            return {'sent': True}
        return {'target_id': 'tab-1', 'url': 'https://muse.ai/',
                'ready': True, 'draft': self.draft, 'messages': self.messages}


@pytest.fixture
def setup(tmp_path):
    transport = Transport()
    return MuseBridge(tmp_path / 'bridge.db', transport=transport), transport, tmp_path


def test_submit_is_durable_idempotent_and_prompt_bound(setup):
    bridge, transport, path = setup
    first = bridge.submit('task-1', 'Find facts', scope='workspace')
    restarted = MuseBridge(path / 'bridge.db', transport=transport)
    assert restarted.submit('task-1', 'Find facts', scope='workspace') == first
    assert len(transport.sends) == 1
    with pytest.raises(MuseBridgeError, match='task_conflict'):
        restarted.submit('task-1', 'different', scope='workspace')
    assert len(transport.sends) == 1


def test_cannot_overwrite_draft_or_send_parallel_task(setup):
    bridge, transport, _ = setup
    transport.draft = True
    with pytest.raises(MuseBridgeError, match='composer_has_draft'):
        bridge.submit('task-1', 'Hello', scope='workspace')
    assert transport.sends == []
    transport.draft = False
    bridge.submit('task-1', 'Hello', scope='workspace')
    with pytest.raises(MuseBridgeError, match='agent_busy'):
        bridge.submit('task-2', 'Other', scope='workspace')
    assert len(transport.sends) == 1


def test_existing_streaming_reply_blocks_new_submission(setup):
    bridge, transport, _ = setup
    transport.messages[-1]['busy'] = True
    assert bridge.status()['busy'] is True
    with pytest.raises(MuseBridgeError, match='agent_busy'):
        bridge.submit('task-1', 'Hello', scope='workspace')
    assert not transport.sends


def test_reply_requires_matching_task_marker_and_finished_message(setup):
    bridge, transport, _ = setup
    task = bridge.submit('task-1', 'Hello', scope='workspace')
    marker = task['completion_marker']
    transport.messages.append(message('progress', 'assistant', 'I am working'))
    assert bridge.read('task-1', scope='workspace')['state'] == 'running'
    transport.messages.append(message('done', 'assistant', 'Answer\n' + marker, busy=True))
    assert bridge.read('task-1', scope='workspace')['state'] == 'running'
    transport.messages[-1]['busy'] = False
    result = bridge.read('task-1', scope='workspace')
    assert result['state'] == 'completed'
    assert result['reply'] == 'Answer'
    assert result['completion_source'] == 'agent_reported'


def test_textarea_newline_normalization_preserves_task_anchor(setup):
    bridge, transport, _ = setup
    task = bridge.submit('task-1', 'line 1\r\nline 2\rline 3', scope='workspace')
    user = transport.messages[-1]
    user['text'] = user['text'].replace('\r\n', '\n').replace('\r', '\n')
    transport.messages.append(message('done', 'assistant', 'Answer\n' + task['completion_marker']))
    assert bridge.read('task-1', scope='workspace')['state'] == 'completed'


def test_maximum_unicode_prompt_fits_helper_input_limit(monkeypatch):
    import subprocess
    from types import SimpleNamespace
    from tools.browser_bridge.muse import SubprocessMuseTransport

    def run(*args, **kwargs):
        assert len(kwargs['input'].encode('utf-8')) <= 64 * 1024
        return SimpleNamespace(returncode=0, stdout='{"ok":true,"result":{"sent":true}}')

    monkeypatch.setattr(subprocess, 'run', run)
    assert SubprocessMuseTransport().request('send', prompt='😀' * 8192)['sent']


def test_unknown_send_is_not_replayed_and_can_reconcile(setup):
    bridge, transport, path = setup
    transport.error = 'bridge_unavailable'
    task = bridge.submit('task-1', 'Hello', scope='workspace')
    assert task['state'] == 'unknown'
    restarted = MuseBridge(path / 'bridge.db', transport=transport)
    assert restarted.submit('task-1', 'Hello', scope='workspace')['state'] == 'unknown'
    assert len(transport.sends) == 1
    transport.error = None
    transport.messages.extend([message('user-1', 'user', transport.sends[0]['prompt']),
        message('done', 'assistant', 'Done\n' + task['completion_marker'])])
    assert restarted.read('task-1', scope='workspace')['state'] == 'completed'


def test_old_or_unrelated_replies_never_complete_task(setup):
    bridge, transport, _ = setup
    task = bridge.submit('task-1', 'Hello', scope='workspace')
    transport.messages.append(message('unrelated', 'assistant', '[FLEET_DONE:other]'))
    assert bridge.read('task-1', scope='workspace')['state'] == 'running'
    transport.messages = [message('no-anchor', 'assistant', task['completion_marker'])]
    assert bridge.read('task-1', scope='workspace')['state'] == 'unknown'


def test_cross_workspace_reads_and_submits_rejected(setup):
    bridge, transport, _ = setup
    bridge.submit('task-1', 'Hello', scope='workspace')
    with pytest.raises(MuseBridgeError, match='task_not_found'):
        bridge.read('task-1', scope='other')
    with pytest.raises(MuseBridgeError, match='task_conflict'):
        bridge.submit('task-1', 'Hello', scope='other')
    assert len(transport.sends) == 1


@pytest.mark.parametrize('prompt', ['', '   ', 1, 'x' * 8193, chr(0)], ids=['empty', 'blank', 'number', 'oversized', 'nul'])
def test_invalid_prompt_never_dispatches(setup, prompt):
    bridge, transport, _ = setup
    with pytest.raises(MuseBridgeError, match='prompt_invalid'):
        bridge.submit('task-1', prompt, scope='workspace')
    assert not transport.sends


def test_human_interleaving_does_not_misattribute_reply(setup):
    bridge, transport, _ = setup
    task = bridge.submit('task-1', 'Hello', scope='workspace')
    transport.messages.extend([message('human', 'user', 'Unrelated manual task'),
        message('reply', 'assistant', 'answer\n' + task['completion_marker'])])
    assert bridge.read('task-1', scope='workspace')['state'] == 'unknown'


def test_waiting_input_is_not_completion(setup):
    bridge, transport, _ = setup
    task = bridge.submit('task-1', 'Hello', scope='workspace')
    transport.messages.append(message('question', 'assistant', 'Need approval\n' + task['input_marker']))
    result = bridge.read('task-1', scope='workspace')
    assert result['state'] == 'waiting_input'
    assert result['reply'] == 'Need approval'
    assert len(transport.sends) == 1


def test_approval_followed_by_reply_can_finish_same_task(setup):
    bridge, transport, _ = setup
    task = bridge.submit('task-1', 'Hello', scope='workspace')
    transport.messages.append(message('question', 'assistant', 'Need approval\n' + task['input_marker']))
    assert bridge.read('task-1', scope='workspace')['state'] == 'waiting_input'
    # Clicking Muse's own approval control need not add a user chat message.
    transport.messages.append(message('done', 'assistant', 'Approved and done\n' + task['completion_marker']))
    result = bridge.read('task-1', scope='workspace')
    assert result['state'] == 'completed'
    assert result['reply'] == 'Approved and done'
    assert len(transport.sends) == 1


def test_real_chromium_fixed_helper(tmp_path):
    import os
    import subprocess
    from pathlib import Path
    module = os.environ.get('FLEET_PLAYWRIGHT_MODULE')
    if not module:
        pytest.skip('FLEET_PLAYWRIGHT_MODULE required for Chromium fixture')
    result = subprocess.run(['node', str(Path(__file__).parent / 'fixtures/browser/muse_bridge.cjs')],
                            capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stderr[-3000:]
    assert '"ok":true' in result.stdout


def test_crashed_dispatch_recovers_as_unknown_without_send(setup):
    bridge, transport, path = setup
    bridge.submit('task-1', 'Hello', scope='workspace')
    with bridge._store() as conn:
        conn.execute("UPDATE tasks SET state='dispatching'")
        conn.commit()
    restarted = MuseBridge(path / 'bridge.db', transport=transport)
    assert restarted.submit('task-1', 'Hello', scope='workspace')['state'] == 'unknown'
    assert len(transport.sends) == 1


def test_discovery_does_not_export_transcript(setup):
    bridge, transport, _ = setup
    transport.messages[0]['text'] = 'PRIVATE_HISTORY'
    assert 'PRIVATE_HISTORY' not in str(bridge.status())
    assert not transport.sends


def test_concurrent_submission_has_single_writer(setup):
    import threading
    bridge, transport, path = setup
    entered, release = threading.Event(), threading.Event()
    original = transport.request
    def request(op, **kwargs):
        if op == 'send':
            entered.set()
            assert release.wait(5)
        return original(op, **kwargs)
    transport.request = request
    errors = []
    def submit():
        try:
            bridge.submit('task-1', 'Hello', scope='workspace')
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=submit)
    thread.start()
    try:
        assert entered.wait(5)
        with pytest.raises(MuseBridgeError, match='bridge_busy'):
            MuseBridge(path / 'bridge.db', transport=transport).submit('task-2', 'Other', scope='workspace')
    finally:
        release.set()
        thread.join(5)
    assert not errors
    assert len(transport.sends) == 1
