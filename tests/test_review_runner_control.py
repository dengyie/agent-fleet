"""Review regressions: cross-instance cancel, hub cancel routing, pruning, dead code
(findings 6, 7, 10, 11)."""
import ast
import errno
import json
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import pytest
from hub.application import control_router as router_mod
from hub.application.control_router import ControlRouter
from hub.application.supervisor_service import AuditStore
from tools.supervisor import handles as handles_mod
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


def _recovered_windows_session(tmp_path):
    sid = "sess_windows_orphan"
    supervisor = sup_mod.Supervisor(
        manifest_dir=tmp_path / "supervisor", machine_id="m1",
        ops=sup_mod.WindowsGroupOps())
    manifest = sup_mod.ManagedSessionManifest(
        session_id=sid, machine_id="m1", process_group_id="grp_test",
        platform="windows")
    supervisor._persist(manifest)
    supervisor.recover()
    owner = supervisor._owner_path(sid)
    owner.parent.mkdir(parents=True, exist_ok=True)
    owner.write_text('{"pid": 4242, "pgid": 4242, "start": "win:old"}')
    return supervisor, sid


@pytest.mark.parametrize(
    ("tree_pids", "child_alive", "expected"),
    [([4242, 5000], True, "liveness_unknown"),
     ([4242], False, "no_live_process"),
     (None, False, "liveness_unknown")],
)
def test_windows_cross_instance_cancel_checks_orphan_liveness(
        tmp_path, tree_pids, child_alive, expected):
    supervisor, sid = _recovered_windows_session(tmp_path)
    with mock.patch.object(handles_mod, "pid_alive", return_value=False), \
            mock.patch.object(handles_mod.os, "name", "nt"), \
            mock.patch.object(sup_mod.WindowsGroupOps, "_tree_pids",
                              return_value=tree_pids), \
            mock.patch.object(sup_mod.WindowsGroupOps, "_pid_alive",
                              return_value=child_alive):
        outcome = supervisor.cancel_attempt(sid)

    assert outcome == expected
    if expected == "no_live_process":
        assert supervisor.get(sid).state in sup_mod.TERMINAL_STATES
    else:
        assert supervisor.get(sid).state not in sup_mod.TERMINAL_STATES


def test_concurrent_supervisors_persist_one_manifest_without_temp_collision(
        tmp_path, monkeypatch):
    manifest_dir = tmp_path / "shared-supervisor"
    first = sup_mod.Supervisor(manifest_dir=manifest_dir, machine_id="m1")
    second = sup_mod.Supervisor(manifest_dir=manifest_dir, machine_id="m1")
    first_manifest = sup_mod.ManagedSessionManifest(
        session_id="sess_concurrent", machine_id="m1",
        process_group_id="grp_first", reason="paused")
    second_manifest = sup_mod.ManagedSessionManifest(
        session_id="sess_concurrent", machine_id="m1",
        process_group_id="grp_second", reason="terminated")
    target = manifest_dir / "sess_concurrent.json"
    barrier = threading.Barrier(2)
    replace_thread = threading.local()
    replace_sources = []
    replace_completed = []
    original_replace = os.replace

    def concurrent_replace(source, destination):
        if Path(destination) == target:
            if not getattr(replace_thread, "started", False):
                replace_thread.started = True
                replace_sources.append(Path(source))
                barrier.wait(timeout=5)
        result = original_replace(source, destination)
        if Path(destination) == target:
            replace_completed.append(Path(source))
        return result

    monkeypatch.setattr(sup_mod.os, "replace", concurrent_replace)
    errors = []

    def persist(supervisor, manifest):
        try:
            supervisor._persist(manifest)
        except Exception as exc:
            errors.append((repr(exc), repr(exc.__context__)))

    threads = [
        threading.Thread(target=persist, args=(first, first_manifest)),
        threading.Thread(target=persist, args=(second, second_manifest)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    persisted = sup_mod.ManagedSessionManifest.from_json(
        (manifest_dir / "sess_concurrent.json").read_text(encoding="utf-8"))
    assert (persisted.process_group_id, persisted.reason) in {
        ("grp_first", "paused"), ("grp_second", "terminated")}
    assert len(set(replace_sources)) == 2
    assert len(replace_completed) == 2
    assert list(manifest_dir.glob("*.json")) == [manifest_dir / "sess_concurrent.json"]
    assert list(manifest_dir.glob("*.json.tmp")) == []


def test_manifest_persist_failure_removes_unique_temporary_file(
        tmp_path, monkeypatch):
    manifest_dir = tmp_path / "supervisor"
    supervisor = sup_mod.Supervisor(manifest_dir=manifest_dir, machine_id="m1")
    manifest = sup_mod.ManagedSessionManifest(
        session_id="sess_failed_write", machine_id="m1",
        process_group_id="grp_failed", reason="paused")
    supervisor._persist(manifest)
    target = manifest_dir / "sess_failed_write.json"
    previous = target.read_bytes()
    manifest.reason = "terminated"

    def fail_replace(source, destination):
        raise OSError("private path must not escape")

    monkeypatch.setattr(sup_mod.os, "replace", fail_replace)
    with pytest.raises(sup_mod.GroupError, match="^manifest_write_failed$"):
        supervisor._persist(manifest)

    assert target.read_bytes() == previous
    assert list(manifest_dir.glob("*.json.tmp")) == []


def test_concurrent_supervisors_persist_owner_without_temp_collision(
        tmp_path, monkeypatch):
    manifest_dir = tmp_path / "shared-supervisor"
    first = sup_mod.Supervisor(manifest_dir=manifest_dir, machine_id="m1")
    second = sup_mod.Supervisor(manifest_dir=manifest_dir, machine_id="m1")
    first_identity = {"pid": 101, "pgid": 101, "start": "test:first"}
    second_identity = {"pid": 202, "pgid": 202, "start": "test:second"}
    first._ops.owner_identity = lambda handle: handle
    second._ops.owner_identity = lambda handle: handle
    target = manifest_dir / "owners" / "sess_owner_race.json"
    barrier = threading.Barrier(2)
    replace_thread = threading.local()
    replace_sources = []
    replace_completed = []
    original_replace = os.replace
    replace_errors = []

    def concurrent_replace(source, destination):
        if Path(destination) == target:
            if not getattr(replace_thread, "started", False):
                replace_thread.started = True
                replace_sources.append(Path(source))
                barrier.wait(timeout=5)
        try:
            result = original_replace(source, destination)
            if Path(destination) == target:
                replace_completed.append(Path(source))
            return result
        except OSError as exc:
            replace_errors.append((exc.errno, getattr(exc, "winerror", None)))
            raise

    monkeypatch.setattr(sup_mod.os, "replace", concurrent_replace)
    threads = [
        threading.Thread(
            target=first._write_owner,
            args=("sess_owner_race", first_identity)),
        threading.Thread(
            target=second._write_owner,
            args=("sess_owner_race", second_identity)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert len(replace_completed) == 2, replace_errors
    assert len(set(replace_sources)) == 2
    assert json.loads(target.read_text(encoding="utf-8")) in [
        first_identity, second_identity]
    assert list(target.parent.glob("*.json.tmp")) == []


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_supervisor_file_replace_recovers_windows_contention(
        tmp_path, monkeypatch, winerror):
    target = tmp_path / "manifest.json"
    target.write_text("previous", encoding="utf-8")
    original_replace = os.replace
    attempts = []
    pauses = []

    def contended_replace(source, destination):
        attempts.append(Path(source))
        if len(attempts) < 3:
            error = PermissionError(errno.EACCES, "fixture file contention")
            error.winerror = winerror
            raise error
        return original_replace(source, destination)

    monkeypatch.setattr(handles_mod.os, "replace", contended_replace)
    monkeypatch.setattr(time, "sleep", pauses.append)
    handles_mod._atomic_replace_text(target, "用户会话")

    assert target.read_text(encoding="utf-8") == "用户会话"
    assert len(attempts) == 3
    assert len(set(attempts)) == 1
    assert len(pauses) == 2
    assert 0 < sum(pauses) <= 0.25
    assert list(tmp_path.glob("*.json.tmp")) == []


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_supervisor_file_replace_bounds_windows_contention(
        tmp_path, monkeypatch, winerror):
    supervisor = sup_mod.Supervisor(manifest_dir=tmp_path, machine_id="m1")
    manifest = sup_mod.ManagedSessionManifest(
        session_id="sess_contended", machine_id="m1",
        process_group_id="grp_contended", reason="paused")
    supervisor._persist(manifest)
    target = tmp_path / "sess_contended.json"
    previous = target.read_bytes()
    attempts = []
    pauses = []

    def denied_replace(source, destination):
        attempts.append(Path(source))
        error = PermissionError(errno.EACCES, "private fixture error")
        error.winerror = winerror
        raise error

    monkeypatch.setattr(handles_mod.os, "replace", denied_replace)
    monkeypatch.setattr(time, "sleep", pauses.append)
    manifest.reason = "terminated"
    with pytest.raises(sup_mod.GroupError, match="^manifest_write_failed$"):
        supervisor._persist(manifest)

    assert 2 <= len(attempts) <= 8
    assert len(pauses) == len(attempts) - 1
    assert 0 < sum(pauses) <= 0.25
    assert target.read_bytes() == previous
    assert list(tmp_path.glob("*.json.tmp")) == []


@pytest.mark.parametrize(
    ("winerror", "error_number"),
    [(None, errno.EACCES), (87, errno.EINVAL), (112, errno.ENOSPC)],
)
def test_supervisor_file_replace_rejects_other_storage_errors(
        tmp_path, monkeypatch, winerror, error_number):
    target = tmp_path / "manifest.json"
    target.write_bytes(b"previous")
    attempts = []
    pauses = []

    def fail_replace(source, destination):
        attempts.append(Path(source))
        error = OSError(error_number, "fixture storage error")
        if winerror is not None:
            error.winerror = winerror
        raise error

    monkeypatch.setattr(handles_mod.os, "replace", fail_replace)
    monkeypatch.setattr(time, "sleep", pauses.append)
    with pytest.raises(OSError):
        handles_mod._atomic_replace_text(target, "updated")

    assert len(attempts) == 1
    assert pauses == []
    assert target.read_bytes() == b"previous"
    assert list(tmp_path.glob("*.json.tmp")) == []


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
