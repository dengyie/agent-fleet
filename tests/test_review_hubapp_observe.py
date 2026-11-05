"""评审修复 #7/#15：reconcile 与 ingest 的每机串行化；提交后事件发布不抛出。"""
import logging
import threading
import time
import unittest

from hub.application.event_emit import safe_emit
from hub.application.observe_service import HostConfig, ObserveService


class _Repo:
    def __init__(self):
        self.rows = {}
        self.before_save = None
        self._txn_lock = threading.RLock()

    def machine_transaction(self, machine):
        return self._txn_lock

    def machines(self):
        return list(self.rows)

    def read_current(self, machine):
        row = self.rows.get(machine)
        return dict(row) if row else None

    def save_snapshot(self, machine, snapshot):
        if self.before_save is not None:
            self.before_save(snapshot)
        row = dict(snapshot)
        row["_ts"] = time.time()
        self.rows[machine] = row

    def save_snapshot_once(self, machine, snapshot, report_id):
        self.save_snapshot(machine, snapshot)
        return True


class _Publisher:
    def __init__(self, fail=False):
        self.fail = fail
        self.events = []

    def emit(self, event_type, **kw):
        if self.fail:
            raise OSError("event store down")
        self.events.append(event_type)
        return {"event": event_type}


def _report(machine="mac-a"):
    return {"machine": machine, "agents": {}, "system": {}}


class ReconcileIngestRaceTests(unittest.TestCase):
    def test_reconcile_cannot_overwrite_concurrent_ingest(self):
        repo = _Repo()
        service = ObserveService(repo, _Publisher(), HostConfig([]))
        repo.rows["mac-a"] = {"machine": "mac-a", "source": "ingest",
                              "reachable": True, "agents": {}, "_ts": 0.0}
        ingest_started = threading.Event()
        barrier = threading.Barrier(2)

        def stall_reconcile(snapshot):
            # 只拦截 reconcile 的离线写：让 ingest 在它"读旧快照后、写之前"抢入
            if snapshot.get("reachable") is False:
                barrier.wait(timeout=5)
                ingest_started.wait(timeout=5)
                time.sleep(0.2)

        repo.before_save = stall_reconcile

        def do_ingest():
            barrier.wait(timeout=5)
            ingest_started.set()
            service.ingest(_report())

        t_rec = threading.Thread(target=service.reconcile)
        t_ing = threading.Thread(target=do_ingest)
        t_rec.start(); t_ing.start()
        t_rec.join(5); t_ing.join(5)
        self.assertTrue(repo.rows["mac-a"]["reachable"],
                        "reconcile 用旧快照覆盖了更新的 ingest")

    def test_reconcile_skips_when_fresh_ingest_already_landed(self):
        repo = _Repo()
        service = ObserveService(repo, _Publisher(), HostConfig([]))
        service.ingest(_report())
        self.assertEqual(service.reconcile(), [])
        self.assertTrue(repo.rows["mac-a"]["reachable"])


class SafeEmitTests(unittest.TestCase):
    def test_ingest_and_reconcile_survive_publisher_failure(self):
        repo = _Repo()
        service = ObserveService(repo, _Publisher(fail=True), HostConfig([]))
        with self.assertLogs("hub.application.event_emit", logging.WARNING):
            self.assertTrue(service.ingest(_report())["ok"])
        repo.rows["mac-a"]["_ts"] = 0.0
        with self.assertLogs("hub.application.event_emit", logging.WARNING):
            self.assertEqual(len(service.reconcile()), 1)
        self.assertFalse(repo.rows["mac-a"]["reachable"])

    def test_safe_emit_never_raises_and_returns_event(self):
        self.assertIsNone(safe_emit(None, "x"))
        self.assertIsNone(safe_emit(_Publisher(fail=True), "x"))
        self.assertEqual(safe_emit(_Publisher(), "x", machine="m"), {"event": "x"})

    def test_task_service_emit_uses_safe_emit(self):
        from hub.application.task_service import TaskService
        holder = TaskService.__new__(TaskService)
        holder.publisher = _Publisher(fail=True)
        holder._emit("task_queued", "mac-a", "t1", "queued")  # 不抛出


if __name__ == "__main__":
    unittest.main()
