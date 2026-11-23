"""Private ACP framing/mailbox limits; no vendor executable or model."""
import io
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

from tools.providers.acp_mailbox import AcpMailbox, _atomic
from tools.providers.acp_transport import AcpTransport, AcpTransportError


def test_mailbox_new_incarnation_ignores_old_pending(tmp_path):
    root = AcpMailbox.begin(tmp_path / 'mailbox', session_id='runner_fixture')
    assert AcpMailbox.publish(root, 'old', [{'id': 'option_0', 'label': 'Allow'}])
    owner = AcpMailbox._read(root / 'owner.json')
    _atomic(root / 'owner.json', {**owner, 'state': 'closed'})
    AcpMailbox.begin(root, session_id='runner_fixture')
    assert AcpMailbox.publish(root, 'new', [{'id': 'option_0', 'label': 'Allow'}])
    assert AcpMailbox.answer(root, 'old', 'option_0', timeout=.01) == 'interaction_expired'


def test_mailbox_rejects_second_live_owner(tmp_path):
    root = AcpMailbox.begin(tmp_path / 'mailbox', session_id='runner_fixture')
    with pytest.raises(ValueError, match='mailbox_in_use'):
        AcpMailbox.begin(root, session_id='runner_fixture')


def test_mailbox_reads_bounded_bytes_without_following_links(tmp_path, monkeypatch):
    oversized = tmp_path / 'oversized.json'
    oversized.write_bytes(b' ' * 20000)
    monkeypatch.setattr(type(oversized), 'read_bytes', lambda _self: pytest.fail('unbounded read'))
    assert AcpMailbox._read(oversized) is None
    target = tmp_path / 'target.json'
    target.write_text('{"secret":"private"}', encoding='utf-8')
    link = tmp_path / 'pending.json'
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip('symlink privilege unavailable')
    assert AcpMailbox._read(link) is None


@pytest.mark.parametrize('pid', [None, [], {}, 'bad', True, -1])
def test_mailbox_corrupt_owner_is_unavailable(tmp_path, pid):
    _atomic(tmp_path / 'owner.json', {'pid': pid, 'state': 'active', 'start_identity': 'fake'})
    assert AcpMailbox.live_owner(tmp_path) is None


def test_mailbox_ack_is_bound_to_dispatch_and_incarnation(tmp_path):
    root = AcpMailbox.begin(tmp_path / 'mailbox', session_id='runner_fixture')
    assert AcpMailbox.publish(root, 'current', [{'id': 'option_0', 'label': 'Allow'}])
    assert not AcpMailbox.ack(root, 'current'), 'undispatched question cannot be delivered'
    result = []
    writer = threading.Thread(target=lambda: result.append(AcpMailbox.answer(root, 'current', 'option_0')))
    writer.start()
    assert AcpMailbox.wait(root, 'current', timeout=1) == 'option_0'
    assert AcpMailbox.ack(root, 'current')
    writer.join(3)
    assert result == ['answered']
    assert AcpMailbox.wait(root, 'current', timeout=.01) is None


def test_mailbox_parallel_publish_has_one_current_question(tmp_path):
    root = AcpMailbox.begin(tmp_path / 'mailbox', session_id='runner_fixture')
    barrier = threading.Barrier(3)
    results = []
    def publish(request):
        barrier.wait()
        results.append(AcpMailbox.publish(root, request, [{'id': 'option_0', 'label': 'Allow'}]))
    threads = [threading.Thread(target=publish, args=(str(index),)) for index in range(2)]
    for thread in threads: thread.start()
    barrier.wait()
    for thread in threads: thread.join(2)
    assert sorted(results) == [False, True]


def test_mailbox_expired_undispatched_question_can_be_replaced(tmp_path):
    root = AcpMailbox.begin(tmp_path / 'mailbox', session_id='runner_fixture')
    assert AcpMailbox.publish(root, 'old', [{'id': 'option_0', 'label': 'Allow'}])
    pending = AcpMailbox._read(root / 'pending.json')
    _atomic(root / 'pending.json', {**pending, 'expires': time.time() - 1})
    assert AcpMailbox.publish(root, 'new', [{'id': 'option_0', 'label': 'Allow'}])
    assert AcpMailbox.answer(root, 'old', 'option_0', timeout=.01) == 'interaction_expired'


@pytest.mark.parametrize('raw', [
    b'{"jsonrpc":"2.0","id":1,"result":{},"result":{}}\n',
    b'{"jsonrpc":"2.0","id":1,"result":{"n":NaN}}\n',
    b'{"jsonrpc":"2.0","id":42,"result":{}}\n',
    b'{"jsonrpc":"2.0","id":1,"result":{},"error":{"code":1}}\n',
    b'{"jsonrpc":"2.0","result":{}}\n',
])
def test_transport_rejects_ambiguous_or_unknown_response(raw):
    process = SimpleNamespace(stdout=io.BytesIO(raw), stderr=io.BytesIO(), stdin=io.BytesIO())
    transport = AcpTransport(process)
    deadline = time.monotonic() + 1
    while not transport.error and time.monotonic() < deadline:
        time.sleep(.005)
    assert transport.error in {'invalid_frame', 'invalid_response', 'unexpected_response'}
    transport.close()


def test_worker_owner_close_failure_still_reaps_vendor(tmp_path, monkeypatch, capsys):
    from tools.providers import agy_worker
    from tools.providers.acp_transport import AcpTransportError
    class Process:
        def __init__(self):
            self.stdin = io.BytesIO()
            self.waited = False
        def wait(self, **_kwargs): self.waited = True
    process = Process()
    class Runtime:
        def __init__(self, *_args, **_kwargs): pass
        def run(self, *_args, **_kwargs): return 0
    monkeypatch.setattr(agy_worker, 'popen_local', lambda *_args, **_kwargs: process)
    monkeypatch.setattr(agy_worker, 'AgyRuntime', Runtime)
    def fail(*_args): raise OSError('SECRET disk failure path')
    monkeypatch.setattr(agy_worker, '_atomic', fail)
    result = agy_worker.main(['--vendor-command', '["fixture"]', '--instruction', 'test',
        '--control-dir', str(tmp_path / 'runner_fixture')])
    assert process.stdin.closed and process.waited
    assert result != 0
    assert 'SECRET' not in capsys.readouterr().out
