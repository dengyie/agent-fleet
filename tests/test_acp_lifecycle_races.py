"""Actual ACP peer races at the node's permission persistence boundary."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from fleet_core.process.local_process import popen
from tools.providers.acp_mailbox import AcpMailbox
from tools.providers.agy_runtime import AgyRuntime


PEER = Path(__file__).parent / 'fixtures/providers/agy_acp_peer.py'


@pytest.fixture
def acp_peer_runtime(tmp_path):
    """Use independent JSON-RPC stdio, with no vendor executable or model."""
    root = AcpMailbox.begin(tmp_path / 'mailbox', session_id='runner_fixture')
    process = popen([sys.executable, str(PEER), 'permission_early',
                     str(tmp_path / 'wire.jsonl')],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        bufsize=0)
    abort, finished = threading.Event(), threading.Event()
    rows, outcomes = [], []
    runtime = AgyRuntime(process, cwd=tmp_path, emit=rows.append, control_dir=root,
        should_abort=abort.is_set, request_timeout=.5, settle_gap=.05,
        background_timeout=2)
    # A real prompt may have been quiet long before the permission frame. The
    # regression must not depend on initial object construction masking that.
    runtime.last_activity = time.monotonic() - 1

    def run():
        try:
            outcomes.append(runtime.run('literal fixture instruction'))
        finally:
            finished.set()

    runner = threading.Thread(target=run, name='fixture-acp-runner')
    try:
        yield SimpleNamespace(root=root, runtime=runtime, rows=rows,
            outcomes=outcomes, finished=finished, runner=runner)
    finally:
        abort.set()
        runtime.transport.close()
        if runner.ident is not None:
            runner.join(2)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
        for thread in (runtime.transport._reader, runtime.transport._writer,
                       runtime.transport._stderr):
            thread.join(1)


def _pending(root):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        pending = AcpMailbox._read(root / 'pending.json')
        if pending:
            return pending
        time.sleep(.005)
    pytest.fail('independent peer did not publish a permission request')


def test_prompt_result_waits_for_permission_publish_and_native_delivery(
        acp_peer_runtime, monkeypatch):
    peer = acp_peer_runtime
    publishing, release = threading.Event(), threading.Event()
    publish = AcpMailbox.publish

    def delayed_publish(*args, **kwargs):
        publishing.set()
        if not release.wait(2):
            raise OSError('fixture publication did not resume')
        return publish(*args, **kwargs)

    monkeypatch.setattr(AcpMailbox, 'publish', delayed_publish)
    peer.runner.start()
    try:
        assert publishing.wait(1), 'permission frame must reach node persistence'
        assert not peer.finished.wait(.15), (
            'end_turn cannot complete while its received permission is publishing')
    finally:
        release.set()
    pending = _pending(peer.root)
    assert AcpMailbox.answer(peer.root, pending['request_id'], 'option_0') == 'answered'
    assert peer.finished.wait(2)
    assert peer.outcomes == [0], peer.rows
    assert any(row.get('type') == 'interaction_response' for row in peer.rows)


def test_permission_ack_disk_failure_cannot_report_success_or_escape_thread(
        acp_peer_runtime, monkeypatch):
    peer = acp_peer_runtime
    ack_attempted, thread_errors = threading.Event(), []

    def failing_ack(*_args, **_kwargs):
        ack_attempted.set()
        raise OSError('private fixture disk /private/native/owner')

    monkeypatch.setattr(AcpMailbox, 'ack', failing_ack)
    monkeypatch.setattr(threading, 'excepthook', thread_errors.append)
    peer.runner.start()
    pending = _pending(peer.root)
    assert AcpMailbox.answer(peer.root, pending['request_id'], 'option_0',
                             timeout=.15) == 'execution_unknown'
    assert ack_attempted.wait(1)
    assert peer.finished.wait(2)
    assert len(peer.outcomes) == 1 and peer.outcomes[0] != 0, peer.rows
    assert peer.runtime.transport.error in {
        'client_request_failed', 'permission_delivery_unknown'}
    assert thread_errors == [], 'persistence exceptions must remain bounded'
    errors = [row for row in peer.rows if row.get('type') == 'fleet_acp_error']
    assert errors and errors[-1]['code'] == peer.runtime.transport.error
    assert '/private/native' not in json.dumps(peer.rows)
