"""Finite upload passes preserve concurrent output and stop between requests."""
import threading

import pytest

from tests.test_session_uploader import _evt, make_spool
from tests.test_review_node_runner_capture import _cfg, _task, _Hub
from tools.session.runner_capture import RunnerCapture, replay_pending
from tools.session.uploader import SessionUploader


@pytest.mark.parametrize('batch_events', [1, 3])
def test_flush_pass_does_not_chase_concurrently_appended_events(tmp_path, batch_events):
    spool = make_spool(tmp_path)
    sent = []
    producing = True
    for sequence in range(1, 5):
        spool.append(_evt(sequence))

    def post(payload):
        batch = payload['events']
        sent.append([row['sequence'] for row in batch])
        # A live producer keeps filling the spool during every HTTP request.
        # Bound the broken implementation's loop so failure never hangs CI.
        if producing and len(sent) <= 4:
            spool.append(_evt(spool.status()['next_sequence']))
        return {'accepted_through': batch[-1]['sequence']}

    try:
        uploader = SessionUploader(post, spool=spool, batch_events=batch_events)
        assert uploader.flush_once() == 4
        assert sent == ([[1], [2], [3], [4]] if batch_events == 1 else [[1, 2, 3], [4]])
        expected = list(range(5, 5 + len(sent)))
        assert [row['sequence'] for row in spool.read_after(0, 100, 262144)] == expected
        producing = False
        assert uploader.flush_once() == len(expected)
        assert [sequence for batch in sent for sequence in batch] == list(range(1, 5)) + expected
    finally:
        spool.close()


@pytest.mark.parametrize('stop_before_request', [False, True])
def test_stop_keeps_unsubmitted_events_and_commits_inflight_ack(tmp_path, stop_before_request):
    spool = make_spool(tmp_path)
    stop = threading.Event()
    sent = []
    for sequence in range(1, 4):
        spool.append(_evt(sequence))
    if stop_before_request:
        stop.set()

    def post(payload):
        sent.append([row['sequence'] for row in payload['events']])
        stop.set()
        return {'accepted_through': payload['events'][-1]['sequence']}

    try:
        uploader = SessionUploader(post, spool=spool, batch_events=1)
        assert uploader.flush_once(should_stop=stop.is_set) == (0 if stop_before_request else 1)
        assert sent == ([] if stop_before_request else [[1]])
        assert spool.status()['ack_sequence'] == (0 if stop_before_request else 1)
    finally:
        spool.close()
    # Durable ACK and retained events survive reconstruction, not just memory.
    spool = make_spool(tmp_path)
    try:
        expected = [1, 2, 3] if stop_before_request else [2, 3]
        assert [row['sequence'] for row in spool.read_after(0, 100, 262144)] == expected
        recovered = []

        def recover(payload):
            recovered.extend(row['sequence'] for row in payload['events'])
            return {'accepted_through': payload['events'][-1]['sequence']}

        assert SessionUploader(recover, spool=spool).flush_once() == len(expected)
        assert recovered == expected
    finally:
        spool.close()


def test_capture_background_stop_finishes_only_the_inflight_batch(tmp_path):
    sent = []
    capture = None

    def post(cfg, path, events):
        sent.append([row['sequence'] for row in events])
        capture.stopping.set()
        return 200, {'accepted_through': events[-1]['sequence']}

    capture = RunnerCapture(_cfg(tmp_path), _task(), post)
    try:
        capture.bridge.uploader._batch_events = 1
        before = capture.bridge.spool.status()['last_sequence']
        assert before >= 2
        capture.start_upload()
        capture.worker.join(5)
        assert not capture.worker.is_alive()
        assert sent == [[1]]
        assert capture.bridge.spool.status()['ack_sequence'] == 1
        assert capture.bridge.spool.status()['last_sequence'] == before
        # finish() must still upload close events after background stop.
        capture.finish(0, resumable=True)
        assert capture.fully_acked()
    finally:
        capture.stop_upload()
        capture.bridge.close()


def test_history_replay_stop_releases_owner_and_preserves_remaining_events(tmp_path):
    cfg = _cfg(tmp_path)
    capture = RunnerCapture(cfg, _task(), _Hub(status=503))
    session_id = capture.session_id
    capture.finish(0, resumable=True)
    stop = threading.Event()
    sent = []

    def post(cfg, path, events):
        sent.append([row['sequence'] for row in events])
        stop.set()
        # Only part of the current request was accepted. The rest must wait.
        return 200, {'accepted_through': events[0]['sequence']}

    replay_pending(cfg, post, should_stop=stop.is_set)
    assert len(sent) == 1
    fresh = RunnerCapture(cfg, _task(), _Hub(), replay=True)
    try:
        assert fresh.session_id == session_id
        assert fresh.bridge.spool.status()['ack_sequence'] == 1
        assert fresh.bridge.spool.read_after(0, 100, 262144)
    finally:
        fresh.bridge.close()


def test_stop_after_spool_read_does_not_start_another_request(tmp_path, monkeypatch):
    spool = make_spool(tmp_path)
    stop = threading.Event()
    sent = []
    spool.append(_evt(1))
    read = spool.read_after

    def stop_after_read(*args):
        rows = read(*args)
        stop.set()
        return rows

    monkeypatch.setattr(spool, 'read_after', stop_after_read)
    try:
        uploader = SessionUploader(lambda payload: sent.append(payload), spool=spool)
        assert uploader.flush_once(should_stop=stop.is_set) == 0
        assert sent == []
        assert spool.status()['ack_sequence'] == 0
        assert len(read(0, 100, 262144)) == 1
    finally:
        spool.close()


def test_http_response_loss_and_stop_recover_original_events_after_restart(tmp_path):
    from tests.test_review_node_sync import _client

    cfg, client = _cfg(tmp_path), _client(tmp_path)
    stop = threading.Event()
    attempts = []
    lost = True

    def post(cfg, path, events):
        nonlocal lost
        attempts.append([row['event_id'] for row in events])
        response = client.post(path, json=events,
            headers={'X-Runner-Credential': 'pxed:secret'})
        assert response.status_code == 200
        if lost:
            lost = False
            stop.set()
            raise ConnectionError('accepted response was lost')
        return response.status_code, response.get_json()

    capture = RunnerCapture(cfg, _task(), post, process_group_id='group-task')
    session_id = capture.session_id
    capture.line('{"type":"item.completed","item":{"type":"agent_message","text":"retained reply"}}', strict=True)
    expected = [row['event_id'] for row in capture.bridge.spool.read_after(0, 100, 262144)]
    try:
        assert capture.flush(should_stop=stop.is_set) == 0
        assert capture.bridge.spool.status()['ack_sequence'] == 0
    finally:
        capture.bridge.close()
    fresh = RunnerCapture(cfg, _task(), post, replay=True)
    try:
        assert fresh.flush(should_stop=stop.is_set) == 0
        assert len(attempts) == 1
        stop.clear()
        assert fresh.flush(should_stop=stop.is_set) == len(expected)
        assert attempts == [expected, expected]
        assert fresh.fully_acked()
        events = client.get('/api/sessions/' + session_id + '/events?tail=1').get_json()['events']
        assert [row['event_id'] for row in events] == expected
        assert len({row['cursor'] for row in events}) == len(expected)
        assert [row['payload']['text'] for row in events if row['kind'] == 'assistant_message'] == ['retained reply']
    finally:
        fresh.bridge.close()
