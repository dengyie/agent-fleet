"""Exercise reconnect replay through the actual Flask streaming route."""
import json

import pytest

from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.application.event_publisher import EventPublisher
from hub.infrastructure.event_repository import JsonlEventRepository


@pytest.mark.parametrize('count', [205, 601])
def test_stream_replays_all_pages_before_live_events(tmp_path, count):
    publisher = EventPublisher(JsonlEventRepository(tmp_path / 'events.jsonl'))
    app = create_app(FleetConfig.from_root(tmp_path), publisher=publisher)
    for number in range(count):
        publisher.emit('task_state', task_id=str(number), state='running')
    response = app.test_client().get('/api/stream?since=1', buffered=False)
    try:
        stream = iter(response.response)
        assert next(stream).startswith(b': connected')
        # This event can appear in both a later replay page and the live queue.
        publisher.emit('task_state', task_id='live', state='succeeded')
        received = []
        for expected in range(2, count + 2):
            chunk = next(stream).decode()
            row = json.loads(chunk.split('data: ', 1)[1])
            received.append(row['event_seq'])
            assert row['event_seq'] == expected
        assert received == list(range(2, count + 2))
    finally:
        response.close()


def test_stream_deduplicates_subscription_watermark_overlap(tmp_path, monkeypatch):
    publisher = EventPublisher(JsonlEventRepository(tmp_path / 'events.jsonl'))
    app = create_app(FleetConfig.from_root(tmp_path), publisher=publisher)
    publisher.emit('alert')
    publisher.emit('alert')
    original = publisher.read_recent

    def snapshot(limit):
        publisher.emit('alert', changes=['during-subscription'])
        return original(limit)

    monkeypatch.setattr(publisher, 'read_recent', snapshot)
    response = app.test_client().get('/api/stream?since=1', buffered=False)
    try:
        stream = iter(response.response)
        assert next(stream).startswith(b': connected')
        publisher.emit('alert', changes=['after-watermark'])
        result = [json.loads(next(stream).decode().split('data: ', 1)[1])['event_seq'] for _ in range(3)]
        assert result == [2, 3, 4]
    finally:
        response.close()
