"""Private ACP framing/mailbox limits; no vendor executable or model."""
import errno
import io
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

from tools.providers import acp_mailbox as acp_mailbox_mod
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


@pytest.mark.parametrize('winerror', [5, 32, 33])
def test_mailbox_atomic_replace_recovers_windows_contention(
        tmp_path, monkeypatch, winerror):
    target = tmp_path / 'pending.json'
    target.write_text('{"state":"previous"}', encoding='utf-8')
    original_replace = os.replace
    attempts = []
    pauses = []

    def contended_replace(source, destination):
        attempts.append(os.fspath(source))
        if len(attempts) < 3:
            error = PermissionError(errno.EACCES, 'fixture file contention')
            error.winerror = winerror
            raise error
        return original_replace(source, destination)

    monkeypatch.setattr(acp_mailbox_mod.os, 'replace', contended_replace)
    monkeypatch.setattr(acp_mailbox_mod.time, 'sleep', pauses.append)
    _atomic(target, {'state': 'delivered'})

    assert json.loads(target.read_text(encoding='utf-8')) == {'state': 'delivered'}
    assert len(attempts) == 3
    assert len(set(attempts)) == 1
    assert len(pauses) == 2
    assert 0 < sum(pauses) <= 0.25
    assert list(tmp_path.glob('.pending.json.*.tmp')) == []


@pytest.mark.parametrize('winerror', [5, 32, 33])
def test_mailbox_atomic_replace_bounds_windows_contention(
        tmp_path, monkeypatch, winerror):
    target = tmp_path / 'pending.json'
    target.write_text('{"state":"previous"}', encoding='utf-8')
    previous = target.read_bytes()
    attempts = []
    pauses = []

    def denied_replace(source, destination):
        attempts.append(os.fspath(source))
        error = PermissionError(errno.EACCES, 'fixture file contention')
        error.winerror = winerror
        raise error

    monkeypatch.setattr(acp_mailbox_mod.os, 'replace', denied_replace)
    monkeypatch.setattr(acp_mailbox_mod.time, 'sleep', pauses.append)
    with pytest.raises(PermissionError):
        _atomic(target, {'state': 'delivered'})

    assert 2 <= len(attempts) <= 6
    assert len(set(attempts)) == 1
    assert len(pauses) == len(attempts) - 1
    assert 0 < sum(pauses) <= 0.25
    assert target.read_bytes() == previous
    assert list(tmp_path.glob('.pending.json.*.tmp')) == []


def test_mailbox_atomic_replace_does_not_retry_other_storage_errors(
        tmp_path, monkeypatch):
    target = tmp_path / 'pending.json'
    target.write_bytes(b'{"state":"previous"}')
    previous = target.read_bytes()
    attempts = []
    pauses = []

    def fail_replace(source, destination):
        attempts.append(os.fspath(source))
        raise OSError(errno.ENOSPC, 'fixture storage error')

    monkeypatch.setattr(acp_mailbox_mod.os, 'replace', fail_replace)
    monkeypatch.setattr(acp_mailbox_mod.time, 'sleep', pauses.append)
    with pytest.raises(OSError):
        _atomic(target, {'state': 'delivered'})

    assert len(attempts) == 1
    assert pauses == []
    assert target.read_bytes() == previous
    assert list(tmp_path.glob('.pending.json.*.tmp')) == []


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


class _FragmentedOutput(io.BytesIO):
    """Independent byte source; count the kernel-facing read operations."""
    def __init__(self, raw, fragment):
        super().__init__(raw)
        self.fragment = fragment
        self.line_reads = 0
        self.chunk_reads = 0

    def read(self, size=-1):
        self.chunk_reads += 1
        assert 0 < size <= 65536
        return super().read(min(size, self.fragment))

    read1 = read

    def readline(self, size=-1):
        self.line_reads += 1
        return super().readline(size)


def _wire_notification(text):
    return (json.dumps({'jsonrpc': '2.0', 'method': 'fixture/update',
        'params': {'text': text}}, ensure_ascii=False) + '\n').encode('utf-8')


@pytest.mark.parametrize('fragment', [1, 3, 65536])
def test_transport_frames_fragmented_unicode_and_coalesced_records(fragment):
    raw = _wire_notification('🌲中文') + _wire_notification('same') * 100
    output = _FragmentedOutput(raw, fragment)
    rows = []
    process = SimpleNamespace(stdout=output, stderr=io.BytesIO(), stdin=io.BytesIO())
    transport = AcpTransport(process, on_notification=rows.append)
    transport._reader.join(2)
    transport.close()
    assert not transport._reader.is_alive()
    assert [row['params']['text'] for row in rows] == ['🌲中文'] + ['same'] * 100
    assert output.line_reads == 0, 'unbuffered FileIO.readline performs a read per byte'
    assert output.chunk_reads <= len(raw) // fragment + 2


@pytest.mark.parametrize('suffix,expected', [(b'\n', None), (b'\r\n', None),
    (b'', 'frame_too_large'), (b' \n', 'frame_too_large')])
def test_transport_exact_frame_byte_bound_and_partial_eof(monkeypatch, suffix, expected):
    from tools.providers import acp_transport
    prefix = b'{"jsonrpc":"2.0","method":"fixture","params":{"text":"'
    postfix = b'"}}'
    limit = 128
    monkeypatch.setattr(acp_transport, 'MAX_FRAME', limit)
    size = limit - len(prefix) - len(postfix) - (len(suffix) if suffix in (b'\n', b'\r\n') else 1)
    raw = prefix + b'x' * size + postfix + suffix
    rows = []
    output = _FragmentedOutput(raw, 7)
    transport = AcpTransport(SimpleNamespace(stdout=output, stderr=io.BytesIO(), stdin=io.BytesIO()),
                             on_notification=rows.append)
    transport._reader.join(1)
    transport.close()
    if expected:
        assert rows == []
        assert transport.error == expected
    else:
        assert len(rows) == 1


class _HeldOutput:
    def __init__(self):
        self.release = threading.Event()

    def read(self, _size):
        assert self.release.wait(4), 'fixture failed to close held pipe'
        return b''

    readline = read


@pytest.fixture
def held_transport():
    output = _HeldOutput()
    process = SimpleNamespace(stdout=output, stderr=io.BytesIO(), stdin=io.BytesIO())
    transport = AcpTransport(process)
    yield transport
    transport.close()
    output.release.set()
    transport._reader.join(1)
    transport._writer.join(1)


def test_transport_write_lock_contention_has_one_deadline(held_transport):
    transport = held_transport
    completed = threading.Event()
    codes = []
    transport._write_lock.acquire()

    def send():
        try:
            transport.notify('fixture', {})
        except AcpTransportError as exc:
            codes.append(exc.code)
        finally:
            completed.set()

    sender = threading.Thread(target=send)
    sender.start()
    try:
        assert completed.wait(1.3), 'lock acquisition must be inside the write deadline'
        assert codes == ['write_timeout']
        assert transport.process.stdin.getvalue() == b''
    finally:
        transport._write_lock.release()
        sender.join(2)


def test_transport_pre_cancelled_request_never_writes(held_transport):
    transport = held_transport
    transport.should_abort = lambda: True
    with pytest.raises(AcpTransportError, match='^cancelled$'):
        transport.request('session/prompt', {'private': 'MUST_NOT_DISPATCH'}, timeout=.1)
    assert transport.process.stdin.getvalue() == b''
    assert transport._pending == {}


def test_transport_close_wakes_pending_request(held_transport):
    transport = held_transport
    sent, finished = threading.Event(), threading.Event()
    original = transport._send
    codes = []

    def traced(row):
        original(row)
        sent.set()

    transport._send = traced

    def request():
        try:
            transport.request('fixture', {}, timeout=3)
        except AcpTransportError as exc:
            codes.append(exc.code)
        finally:
            finished.set()

    caller = threading.Thread(target=request)
    caller.start()
    try:
        assert sent.wait(1)
        transport.close()
        assert finished.wait(.3), 'close must wake waiters without waiting for vendor EOF'
        assert codes == ['protocol_eof']
        assert transport._pending == {}
    finally:
        transport._fail('protocol_eof')
        caller.join(1)


def test_transport_cancel_while_waiting_for_writer_never_queues(held_transport):
    transport = held_transport
    abort, checking = threading.Event(), threading.Event()
    def should_abort():
        checking.set()
        return abort.is_set()
    transport.should_abort = should_abort
    codes = []
    transport._write_lock.acquire()
    def request():
        try:
            transport.request('session/prompt', {}, timeout=2)
        except AcpTransportError as exc:
            codes.append(exc.code)
    caller = threading.Thread(target=request)
    caller.start()
    try:
        assert checking.wait(1)
        abort.set()
        caller.join(.3)
        assert not caller.is_alive()
        assert codes == ['cancelled']
        assert transport.process.stdin.getvalue() == b''
        assert transport._outgoing.empty()
        assert transport._pending == {}
    finally:
        transport._write_lock.release()
        caller.join(2)


def test_transport_first_error_wakes_all_callers_and_survives_close(held_transport):
    transport = held_transport
    sent = threading.Barrier(3)
    codes = []
    original = transport._send
    def send(row):
        original(row)
        sent.wait(2)
    transport._send = send
    def request():
        try:
            transport.request('fixture', {}, timeout=3)
        except AcpTransportError as exc:
            codes.append(exc.code)
    callers = [threading.Thread(target=request) for _ in range(2)]
    for caller in callers:
        caller.start()
    try:
        sent.wait(2)
        transport._fail('invalid_frame')
        transport.close()
        transport._fail('write_timeout')
        for caller in callers:
            caller.join(1)
        assert not any(caller.is_alive() for caller in callers)
        assert codes == ['invalid_frame', 'invalid_frame']
        assert transport.error == 'invalid_frame'
        assert transport._pending == {}
    finally:
        transport.close()
        for caller in callers:
            caller.join(1)


def test_transport_real_stalled_stdin_fails_and_reaps_owned_peer():
    import subprocess
    import sys
    from fleet_core.process.local_process import popen
    process = popen([sys.executable, '-c', 'import time; time.sleep(10)'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    transport = AcpTransport(process)
    try:
        started = time.monotonic()
        with pytest.raises(AcpTransportError, match='^write_timeout$'):
            transport.notify('fixture', {'text': 'x' * 900000})
        assert time.monotonic() - started < 2
        assert transport.error == 'write_timeout'
        with pytest.raises(AcpTransportError, match='^write_timeout$'):
            transport.request('session/prompt', {}, timeout=.1)
    finally:
        transport.close()
        process.kill()
        process.wait(timeout=2)
        for thread in (transport._writer, transport._reader, transport._stderr):
            thread.join(2)
            assert not thread.is_alive()
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
