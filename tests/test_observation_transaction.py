"""每机观测事务回归：read→判定→save 全程持有线程锁 + 跨进程文件锁。

覆盖：
- ``machine_transaction`` 同线程可重入，嵌套 save 不会对同一锁文件新开 fd 自死锁；
- 事务对其他线程、其他进程互斥，异常退出后锁被释放；
- 另一个进程（如 ``hub/scan.py``）的 reconcile 读到旧快照后，期间到达的
  ingest 不能被其 reachable=False 覆盖（不再出现假离线 + 假 state_changed）。
"""

import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from hub import state as store
from hub.application.observe_service import HostConfig, ObserveService
from hub.infrastructure.state_repository import JsonlObservationRepository

REPO_ROOT = Path(__file__).resolve().parents[1]
BLOCK_WINDOW_S = 0.6
PROCESS_TIMEOUT_S = 60

_SAVE_PROGRAM = (
    "import sys; from pathlib import Path; "
    "from hub.infrastructure.state_repository import JsonlObservationRepository; "
    "repo = JsonlObservationRepository(Path(sys.argv[1])); "
    "print('ready', flush=True); "
    "repo.save_snapshot('hk', {'machine': 'hk', 'writer': 'child'}); "
    "print('saved', flush=True)"
)

_INGEST_PROGRAM = (
    "import sys\n"
    "from pathlib import Path\n"
    "from hub.application.observe_service import HostConfig, ObserveService\n"
    "from hub.infrastructure.state_repository import JsonlObservationRepository\n"
    "class Publisher:\n"
    "    def emit(self, event_type, **kw):\n"
    "        print('event', event_type, ','.join(kw.get('changes') or []), flush=True)\n"
    "service = ObserveService(JsonlObservationRepository(Path(sys.argv[1])), "
    "Publisher(), HostConfig([]))\n"
    "print('ready', flush=True)\n"
    "result = service.ingest({'machine': 'hk', 'agents': {}, 'system': {}})\n"
    "print('ingested', ','.join(result['changes']), flush=True)\n"
)


def _spawn(program: str, root: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", program, str(root)],
        cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _await_ready(test: unittest.TestCase, worker: subprocess.Popen) -> None:
    line = worker.stdout.readline()
    if line.strip() != "ready":
        _, stderr = worker.communicate(timeout=PROCESS_TIMEOUT_S)
        test.fail(f"worker did not start: {line!r} {stderr}")


def _finish(worker: subprocess.Popen) -> tuple[str, str]:
    try:
        return worker.communicate(timeout=PROCESS_TIMEOUT_S)
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.communicate()


class MachineTransactionReentrancyTests(unittest.TestCase):
    def test_nested_transaction_and_saves_do_not_self_deadlock(self):
        repo = JsonlObservationRepository(Path(tempfile.mkdtemp()))
        outcome = {}

        def body():
            with repo.machine_transaction("hk"):
                with repo.machine_transaction("hk"):
                    repo.save_snapshot("hk", {"machine": "hk", "seq": 1})
                outcome["inserted"] = repo.save_snapshot_once(
                    "hk", {"machine": "hk", "seq": 2}, "r-1")
                outcome["current"] = repo.read_current("hk")
            outcome["done"] = True

        worker = threading.Thread(target=body, daemon=True)
        worker.start()
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive(), "nested machine_transaction deadlocked")
        self.assertTrue(outcome.get("done"))
        self.assertTrue(outcome["inserted"])
        self.assertEqual(outcome["current"]["seq"], 2)
        self.assertEqual([row["seq"] for row in repo.read_history("hk", 10)], [1, 2])

    def test_depth_resets_so_later_transactions_relock(self):
        root = Path(tempfile.mkdtemp())
        repo = JsonlObservationRepository(root)
        with self.assertRaises(RuntimeError):
            with repo.machine_transaction("hk"):
                with repo.machine_transaction("hk"):
                    raise RuntimeError("boom")
        self.assertEqual(repo._txn_local.depths, {})
        # 异常退出后文件锁已释放：另一进程可以立即写入。
        worker = _spawn(_SAVE_PROGRAM, root)
        stdout, stderr = _finish(worker)
        self.assertEqual(worker.returncode, 0, stderr)
        self.assertIn("saved", stdout)
        self.assertEqual(repo.read_current("hk")["writer"], "child")

    def test_transaction_excludes_other_threads(self):
        repo = JsonlObservationRepository(Path(tempfile.mkdtemp()))
        saved = threading.Event()

        def writer():
            repo.save_snapshot("hk", {"machine": "hk", "writer": "thread"})
            saved.set()

        with repo.machine_transaction("hk"):
            repo.save_snapshot("hk", {"machine": "hk", "writer": "owner"})
            other = threading.Thread(target=writer, daemon=True)
            other.start()
            self.assertFalse(saved.wait(BLOCK_WINDOW_S))
            self.assertEqual(repo.read_current("hk")["writer"], "owner")
        other.join(timeout=10)
        self.assertTrue(saved.is_set())
        self.assertEqual(repo.read_current("hk")["writer"], "thread")

    def test_legacy_state_facade_exposes_reentrant_transaction(self):
        old_dir = store.STATE_DIR
        store.STATE_DIR = Path(tempfile.mkdtemp())
        try:
            with store.machine_transaction("hk"):
                with store.machine_transaction("hk"):
                    store.save_snapshot("hk", {"machine": "hk", "seq": 7})
            self.assertEqual(store.read_current("hk")["seq"], 7)
        finally:
            store.STATE_DIR = old_dir


class MachineTransactionCrossProcessTests(unittest.TestCase):
    def test_transaction_blocks_writer_in_another_process(self):
        root = Path(tempfile.mkdtemp())
        repo = JsonlObservationRepository(root)
        repo.save_snapshot("hk", {"machine": "hk", "writer": "parent"})
        with repo.machine_transaction("hk"):
            worker = _spawn(_SAVE_PROGRAM, root)
            try:
                _await_ready(self, worker)
                time.sleep(BLOCK_WINDOW_S)
                self.assertIsNone(worker.poll(), "child wrote while transaction held")
                self.assertEqual(repo.read_current("hk")["writer"], "parent")
            except BaseException:
                worker.kill()
                worker.communicate()
                raise
        stdout, stderr = _finish(worker)
        self.assertEqual(worker.returncode, 0, stderr)
        self.assertIn("saved", stdout)
        self.assertEqual(repo.read_current("hk")["writer"], "child")

    def test_reconcile_in_one_process_cannot_overwrite_ingest_from_another(self):
        root = Path(tempfile.mkdtemp())
        test = self
        workers = []

        class InterleavingRepository(JsonlObservationRepository):
            """reconcile 读到旧快照后、写回前，让另一个进程尝试 ingest。"""

            interleave = False

            def read_current(self, machine):
                current = super().read_current(machine)
                if self.interleave:
                    self.interleave = False
                    worker = _spawn(_INGEST_PROGRAM, root)
                    workers.append(worker)
                    _await_ready(test, worker)
                    time.sleep(BLOCK_WINDOW_S)
                    test.assertIsNone(worker.poll(),
                                      "ingest slipped into reconcile's critical section")
                return current

        repo = InterleavingRepository(root)
        repo.save_snapshot("hk", {"machine": "hk", "source": "ingest",
                                  "reachable": True, "agents": {}, "system": {}})
        events = []

        class Publisher:
            def emit(self, event_type, **kw):
                events.append((event_type, list(kw.get("changes") or [])))

        service = ObserveService(repo, Publisher(), HostConfig([]),
                                 clock=lambda: time.time() + 10_000)
        repo.interleave = True
        try:
            results = service.reconcile("hk")
            self.assertEqual(len(workers), 1)
            stdout, stderr = _finish(workers[0])
        finally:
            for worker in workers:
                if worker.poll() is None:
                    worker.kill()
                    worker.communicate()
        self.assertEqual(workers[0].returncode, 0, stderr)
        self.assertEqual([r["machine"] for r in results], ["hk"])
        self.assertEqual(events, [("state_changed", ["reachable", "remote_error"])])
        # 子进程的 ingest 在 reconcile 写完后才进入，看到的是离线快照并把它恢复。
        self.assertIn("ingested reachable,remote_error", stdout)
        current = JsonlObservationRepository(root).read_current("hk")
        self.assertTrue(current["reachable"])
        self.assertIsNone(current["remote_error"])
        history = JsonlObservationRepository(root).read_history("hk", 10)
        self.assertEqual([row["reachable"] for row in history], [True, False, True])


if __name__ == "__main__":
    unittest.main()
