from threading import Event, enumerate as threads

import pytest

from hub.application.run_lease_heartbeat import LeaseHeartbeat, RunLeaseLost


def test_heartbeat_failure_is_fenced_and_thread_joins():
    renewed, failed = Event(), Event()
    calls = []
    def renew():
        calls.append(True)
        if len(calls) == 1:
            renewed.set()
        else:
            failed.set()
            raise RuntimeError('store unavailable')
    heartbeat = LeaseHeartbeat(renew, interval=.01, name='failure-test')
    heartbeat.start()
    try:
        assert renewed.wait(1) and failed.wait(1)
        with pytest.raises(RunLeaseLost):
            heartbeat.pulse()
        assert len(calls) == 2, 'lost ownership never renews again'
    finally:
        heartbeat.close()
    assert not any(t.name == 'run-lease-failure-test' for t in threads())


def test_heartbeat_close_before_start_is_safe():
    heartbeat = LeaseHeartbeat(lambda: None, interval=1, name='not-started')
    heartbeat.close()
