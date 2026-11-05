"""Review regression: TTL expiry must announce ``task_update`` per task."""

import logging

from hub.application.reconciliation_service import ReconciliationService


class _Repo:
    def __init__(self, expired):
        self._expired = expired

    def expire_leases(self, *, now):
        return []

    def expire_tasks(self, *, now):
        return list(self._expired)


class _Publisher:
    def __init__(self, fail_for=()):
        self.events = []
        self.fail_for = set(fail_for)

    def emit(self, event, **payload):
        if payload.get("task_id") in self.fail_for:
            raise RuntimeError("bus down")
        self.events.append((event, payload))


def test_expire_tasks_emits_task_update_per_expired_task():
    pub = _Publisher()
    svc = ReconciliationService(None, _Repo(["t1", "t2"]), pub, clock=lambda: 100.0)
    assert svc.expire_tasks() == ["t1", "t2"]
    assert pub.events == [
        ("task_update", {"task_id": "t1", "state": "expired"}),
        ("task_update", {"task_id": "t2", "state": "expired"}),
    ]


def test_expiry_publish_failure_is_logged_and_does_not_stop_others(caplog):
    pub = _Publisher(fail_for={"t1"})
    svc = ReconciliationService(None, _Repo(["t1", "t2"]), pub, clock=lambda: 100.0)
    with caplog.at_level(logging.WARNING):
        assert svc.reconcile_leases() == []
    assert pub.events == [("task_update", {"task_id": "t2", "state": "expired"})]
    assert "reconciliation_event_publish_failed" in caplog.text


def test_retention_runs_from_lease_sweep_at_most_hourly_and_never_raises(caplog):
    calls = []
    clock = [1000.0]

    def retention(*, now):
        calls.append(now)
        if len(calls) == 2:
            raise RuntimeError("disk")

    svc = ReconciliationService(None, _Repo([]), _Publisher(),
                                clock=lambda: clock[0], retention=retention)
    svc.reconcile_leases()
    clock[0] += 60
    svc.reconcile_leases()
    assert calls == [1000.0]
    clock[0] += ReconciliationService.RETENTION_INTERVAL_S
    with caplog.at_level(logging.WARNING):
        svc.reconcile_leases()
    assert len(calls) == 2
    assert "store_retention_failed" in caplog.text
