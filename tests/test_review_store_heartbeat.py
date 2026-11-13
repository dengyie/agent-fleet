"""Only fencing mismatches are fatal; transient renew errors retry until expiry."""

from threading import Event, enumerate as threads

import pytest

from hub.application.run_lease_heartbeat import LeaseHeartbeat, RunLeaseLost
from hub.platform.resource_lease import ResourceLeaseError


def test_transient_error_is_retried_and_ownership_kept():
    calls = []
    recovered = Event()

    def renew():
        calls.append(True)
        if len(calls) in (2, 3):
            raise RuntimeError('database is locked')
        if len(calls) >= 4:
            recovered.set()

    heartbeat = LeaseHeartbeat(renew, interval=.02, name='transient-test', ttl=5.0)
    heartbeat.start()
    try:
        assert recovered.wait(2)
        heartbeat.pulse()  # 未被栅栏
        assert heartbeat.failure is None
    finally:
        heartbeat.close()
    assert not any(t.name == 'run-lease-transient-test' for t in threads())


def test_transient_errors_become_fatal_once_lease_could_have_expired():
    clock = [100.0]
    fail = [False]

    def renew():
        if fail[0]:
            raise OSError('disk unavailable')

    heartbeat = LeaseHeartbeat(renew, interval=10, name='expiry-test', clock=lambda: clock[0])
    heartbeat.pulse()
    fail[0] = True
    clock[0] += 29  # ttl 默认 3 * interval = 30
    heartbeat.pulse()
    clock[0] += 1
    with pytest.raises(RunLeaseLost):
        heartbeat.pulse()
    fail[0] = False
    with pytest.raises(RunLeaseLost):
        heartbeat.pulse()


@pytest.mark.parametrize('error', [RunLeaseLost('lease_mismatch'), ResourceLeaseError('lease_mismatch')])
def test_fencing_mismatch_is_fatal_immediately(error):
    calls = []

    def renew():
        calls.append(True)
        if len(calls) > 1:
            raise error

    heartbeat = LeaseHeartbeat(renew, interval=100, name='fence-test')
    heartbeat.pulse()
    with pytest.raises(RunLeaseLost):
        heartbeat.pulse()
    with pytest.raises(RunLeaseLost):
        heartbeat.pulse()
    assert len(calls) == 2
