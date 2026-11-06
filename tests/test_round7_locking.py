"""Round 7: fleet_core.identity / fleet_core.locking（可诊断锁）。"""
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from fleet_core.identity import ProcessIdentity
from fleet_core.locking import (
    LockBusy,
    OwnedLock,
    inspect_lock,
    owner_path,
    read_owner,
    reclaim_stale,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _hold_in_child(lock_path: Path, ready: Path):
    code = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(REPO_ROOT)!r})
        from fleet_core.locking import OwnedLock
        lock = OwnedLock({str(lock_path)!r}, "child", {{"version": "v1"}}).acquire()
        open({str(ready)!r}, "w").close()
        time.sleep(60)
    """)
    proc = subprocess.Popen([sys.executable, "-c", code])
    deadline = time.monotonic() + 10
    while not ready.exists():
        assert proc.poll() is None, "child exited early"
        assert time.monotonic() < deadline
        time.sleep(0.02)
    return proc


def test_identity_current_is_alive_and_roundtrips():
    ident = ProcessIdentity.current()
    assert ident.pid == os.getpid()
    assert ProcessIdentity.current() is ident
    again = ProcessIdentity.from_dict(ident.to_dict())
    assert again == ident
    assert again.alive() in (True, None)


def test_identity_rejects_bad_dicts():
    assert ProcessIdentity.from_dict(None) is None
    assert ProcessIdentity.from_dict({"pid": True, "startMarker": "x"}) is None
    assert ProcessIdentity.from_dict({"pid": 0, "startMarker": "x"}) is None
    assert ProcessIdentity.from_dict({"pid": 5, "startMarker": "x" * 200}) is None


def test_identity_marker_mismatch_means_not_same_process():
    ident = ProcessIdentity(os.getpid(), "definitely-not-the-marker")
    if ProcessIdentity.current().start_marker:
        assert ident.alive() is False


def test_owned_lock_writes_and_removes_owner(tmp_path):
    path = tmp_path / "x.lock"
    with OwnedLock(path, "unit", {"version": "abc"}) as lock:
        assert lock.held
        owner = read_owner(path)
        assert owner["purpose"] == "unit"
        assert owner["pid"] == os.getpid()
        assert owner["extra"] == {"version": "abc"}
        assert "nonce" in owner
    assert not owner_path(path).exists()
    assert inspect_lock(path).state == "free"


def test_concurrent_acquire_reports_holder(tmp_path):
    path = tmp_path / "busy.lock"
    proc = _hold_in_child(path, tmp_path / "ready")
    try:
        with pytest.raises(LockBusy) as info:
            OwnedLock(path, "me").acquire(timeout=0.2)
        assert info.value.owner["purpose"] == "child"
        assert info.value.owner["pid"] == proc.pid
        state = inspect_lock(path)
        assert state.state == "held"
        assert state.owner_alive in (True, None)
        assert reclaim_stale(path) is False
        assert owner_path(path).exists()
    finally:
        proc.kill()
        proc.wait()


def test_killed_holder_leaves_stale_metadata_only(tmp_path):
    path = tmp_path / "stale.lock"
    proc = _hold_in_child(path, tmp_path / "ready")
    proc.kill()
    proc.wait()
    state = inspect_lock(path)
    assert state.state == "stale"
    assert state.owner["pid"] == proc.pid
    # 内核已释放锁：下一代可直接取得，并覆盖元数据
    with OwnedLock(path, "next") as lock:
        assert lock.held
        assert read_owner(path)["purpose"] == "next"
    assert inspect_lock(path).state == "free"


def test_reclaim_stale_removes_leftover(tmp_path):
    path = tmp_path / "r.lock"
    proc = _hold_in_child(path, tmp_path / "ready")
    proc.kill()
    proc.wait()
    assert reclaim_stale(path) is True
    assert inspect_lock(path).state == "free"
    assert reclaim_stale(path) is False
    assert inspect_lock(tmp_path / "missing.lock").state == "absent"


def test_release_keeps_foreign_owner_record(tmp_path):
    path = tmp_path / "n.lock"
    lock = OwnedLock(path, "a").acquire()
    owner_path(path).write_text('{"nonce":"other"}')
    lock.release()
    assert owner_path(path).exists()
    lock.release()  # idempotent
