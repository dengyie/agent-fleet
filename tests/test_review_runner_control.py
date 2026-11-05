"""Review regressions: cross-instance cancel, hub cancel routing, pruning, dead code
(findings 6, 7, 10, 11)."""
import ast
import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from hub.application import control_router as router_mod
from hub.application.control_router import ControlRouter
from hub.application.supervisor_service import AuditStore
from tools.supervisor import control_client as cc
from tools.supervisor import supervisor as sup_mod

ROOT = Path(__file__).resolve().parents[1]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _wait_gone(pid: int, wait_s: float = 5.0) -> bool:
    end = time.monotonic() + wait_s
    while time.monotonic() < end:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


@unittest.skipIf(os.name == "nt", "POSIX process groups")
class CrossInstanceCancelTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp())
        self.manifests = self.temp / "sup"
        self.runner_sup = sup_mod.Supervisor(manifest_dir=self.manifests, machine_id="m1")
        self.manifest = self.runner_sup.launch(
            agent="codex", command=[sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=str(self.temp), env_allowlist={}, attempt_id="att1")
        self.pid = self.runner_sup._entries[self.manifest.session_id].handle.proc.pid

    def tearDown(self):
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self.runner_sup._entries[self.manifest.session_id].handle.proc.wait(timeout=5)
        except Exception:
            pass

    def test_second_instance_cancels_runner_session(self):
        probe_sup = sup_mod.Supervisor(manifest_dir=self.manifests, machine_id="m1")
        sid = self.manifest.session_id
        self.assertEqual(probe_sup.get(sid).attempt_id, "att1")
        outcome = probe_sup.cancel_attempt(sid)
        self.assertIn(outcome, ("terminated", "terminated_forced"))
        # 子进程属于 runner 实例：由它回收僵尸后进程即消失
        self.runner_sup._entries[sid].handle.proc.wait(timeout=5)
        self.assertTrue(_wait_gone(self.pid))

    def test_unverifiable_owner_reports_liveness_unknown(self):
        sid = self.manifest.session_id
        owner = self.manifests / "owners" / f"{sid}.json"
        self.assertTrue(owner.exists())
        owner.unlink()
        probe_sup = sup_mod.Supervisor(manifest_dir=self.manifests, machine_id="m1")
        probe_sup.get(sid)
        self.assertEqual(probe_sup.cancel_attempt(sid), "liveness_unknown")
        self.assertNotIn(probe_sup.get(sid).state, sup_mod.TERMINAL_STATES)
        self.assertTrue(_alive(self.pid))

    def test_prune_removes_old_terminal_manifests_only(self):
        done = self.runner_sup.launch(
            agent="codex", command=[sys.executable, "-c", "pass"],
            cwd=str(self.temp), env_allowlist={})
        self.runner_sup.wait_session(done.session_id, timeout_s=10)
        old = time.time() - 30 * 86400
        for path in self.manifests.glob("*.json"):
            os.utime(path, (old, old))
        removed = self.runner_sup.prune_manifests()
        self.assertGreaterEqual(removed, 1)
        names = {p.name for p in self.manifests.glob("*.json")}
        self.assertNotIn(f"{done.session_id}.json", names)
        self.assertIn(f"{self.manifest.session_id}.json", names)  # 运行中的不删


class _Supervisor:
    def __init__(self):
        self.n = 0

    def enqueue(self, machine, session_id, attempt, action, reason):
        self.n += 1
        return {"command_id": f"cmd{self.n}"}

    def command_status(self, command_id):
        return ""


class _Tasks:
    def get_task(self, task_id):
        return {"task_id": task_id, "machine": "m1"}


class _Sessions:
    def __init__(self, fail=False):
        self.fail = fail
        self.finished = []

    def list_sessions(self, machine_id, limit):
        if self.fail:
            raise RuntimeError("db down")
        return [{"session_id": f"s-{a}", "managed": True, "attempt_id": a}
                for a in ("a1", "a2", "a3", "a4", "a5")]

    def close_session(self, *a):
        return {}

    def finish_managed_session(self, session_id, finished_at):
        self.finished.append(session_id)
        return True


class ControlRouterTests(unittest.TestCase):
    def _router(self, sessions):
        self.audit = AuditStore()
        return ControlRouter(supervisor=_Supervisor(), task_repository=_Tasks(),
                             session_repo=sessions, audit_store=self.audit)

    def _events(self):
        return [(r.get("event"), r.get("code")) for r in self.audit.read(limit=200)]

    def test_lookup_failure_is_control_failed(self):
        router = self._router(_Sessions(fail=True))
        self.assertIsNone(router.enqueue_cancel_attempt("t1", "a1", "op"))
        self.assertIn(("control_failed", "session_lookup_failed"), self._events())

    def test_liveness_unknown_never_closes_session(self):
        sessions = _Sessions()
        router = self._router(sessions)
        for status in ("failed", "succeeded"):
            cid = router.enqueue_cancel_attempt("t1", "a1", "op")
            out = router.on_supervisor_receipt(cid, status=status, reason="liveness_unknown")
            self.assertEqual(out["outcome"], "failed")
        self.assertEqual(sessions.finished, [])
        self.assertIn(("control_failed", "liveness_unknown"), self._events())

    def test_terminated_receipt_still_closes_session(self):
        sessions = _Sessions()
        router = self._router(sessions)
        cid = router.enqueue_cancel_attempt("t1", "a1", "op")
        self.assertEqual(router.on_supervisor_receipt(cid, status="succeeded",
                                                      reason="terminated")["outcome"],
                         "terminated")
        self.assertEqual(sessions.finished, ["s-a1"])

    def test_tracking_maps_are_bounded(self):
        router = self._router(_Sessions())
        with mock.patch.object(router_mod, "_TRACK_MAX", 3):
            for attempt in ("a1", "a2", "a3", "a4", "a5"):
                cid = router.enqueue_cancel_attempt("t1", attempt, "op")
                router.on_supervisor_receipt(cid, status="failed", reason="x")
        self.assertLessEqual(router.pending_count(), 3)
        self.assertLessEqual(len(router._finalized), 3)

    def test_tracking_entries_expire(self):
        router = self._router(_Sessions())
        now = [1000.0]
        router._clock = lambda: now[0]
        first = router.enqueue_cancel_attempt("t1", "a1", "op")
        now[0] += router_mod._TRACK_TTL_S + 1
        router.enqueue_cancel_attempt("t1", "a2", "op")
        self.assertIsNone(router.tracked(first))


class NonceStorePruneTests(unittest.TestCase):
    def test_prune_removes_only_expired_markers_and_receipts(self):
        store = cc.NonceStore(path=Path(tempfile.mkdtemp()))
        self.assertTrue(store.mark("old-nonce"))
        self.assertTrue(store.mark("new-nonce"))
        self.assertTrue(store.save_receipt("old-cmd", {"status": "succeeded"}))
        old = time.time() - 10 * 86400
        os.utime(store._marker("old-nonce"), (old, old))
        os.utime(store._receipt_marker("old-cmd"), (old, old))
        removed = store.prune(marker_max_age_s=86400, receipt_max_age_s=7 * 86400)
        self.assertEqual(removed, 2)
        self.assertFalse(store.used("old-nonce"))
        self.assertTrue(store.used("new-nonce"))
        self.assertIsNone(store.receipt("old-cmd"))

    def test_poll_prunes_rate_limited(self):
        store = cc.NonceStore(path=Path(tempfile.mkdtemp()))
        store.mark("stale")
        old = time.time() - 30 * 86400
        os.utime(store._marker("stale"), (old, old))
        client = cc.ControlClient(
            hub_url="https://hub.test", machine_id="m1", credential="c",
            hub_public_key=b"k" * 32, public_supervisor=object(),
            transport=lambda path, body, headers: (200, {"ok": True, "commands": []}),
            nonce_store=store)
        client.poll_once()
        self.assertFalse(store.used("stale"))
        with mock.patch.object(store, "prune") as prune:
            client.poll_once()
        prune.assert_not_called()


class DeadCodeTests(unittest.TestCase):
    def _imports(self, rel):
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        names = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
        return names

    def test_unused_symbols_removed(self):
        self.assertFalse(hasattr(cc.ControlClient, "_reject"))
        self.assertNotIn("urllib.error", self._imports("tools/supervisor/control_client.py"))
        cli = self._imports("tools/supervisor/control_client_cli.py")
        self.assertFalse(cli & {"json", "urllib.error", "urllib.request"})
        self.assertFalse(hasattr(sup_mod.GroupOps, "group_id"))


if __name__ == "__main__":
    unittest.main()
