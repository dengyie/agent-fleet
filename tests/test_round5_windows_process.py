"""Round-5 N8: Windows supervisor trees are owned by a Job Object.

``taskkill /PID root /T`` cannot reach descendants once the root exited, and
the old ``group_reap`` only waited.  A spawned root now starts suspended in a
``WindowsJob`` created WITHOUT kill-on-close (the tree must survive supervisor
death for manifest re-attach); force-kill/reap terminate the job while its
handle is held, and otherwise kill the enumerated descendants one by one.
Everything here runs on any OS through fakes (no real Win32 calls).
"""
from __future__ import annotations

import subprocess
from unittest import mock

import pytest

from fleet_core.process import windows_job as wj
from tools.supervisor import group_ops
from tools.supervisor.group_ops import GroupError, WindowsGroupOps
from tools.supervisor.handles import _Bound

_SUSPENDED = 0x00000004


class FakeJob:
    def __init__(self, active=0, start_error=None):
        self.active = active
        self.start_error = start_error
        self.started = []
        self.terminated = 0
        self.closed = 0
        self.released = 0

    def start(self, handle):
        if self.start_error is not None:
            raise self.start_error
        self.started.append(handle)

    def active_processes(self):
        return self.active

    def terminate(self):
        self.terminated += 1
        self.active = 0
        return True

    def release(self):
        self.released += 1

    def close(self):
        self.closed += 1
        self.active = 0


class FakeProc:
    def __init__(self, rc=None):
        self.rc = rc
        self.pid = 4242
        self._handle = 777
        self.stdout = None
        self.killed = 0

    def poll(self):
        return self.rc

    def wait(self, timeout=None):
        if self.rc is None:
            raise subprocess.TimeoutExpired("x", timeout)
        return self.rc

    def kill(self):
        self.killed += 1
        self.rc = 1


def _create(monkeypatch, job, proc):
    seen = {}

    def popen(argv, **kwargs):
        seen.update(kwargs)
        return proc

    monkeypatch.setattr(group_ops, "popen_local", popen)
    monkeypatch.setattr(WindowsGroupOps, "_new_job", staticmethod(lambda: job))
    handle = WindowsGroupOps().create(["agent"], "/tmp", {})
    return handle, seen


def test_create_spawns_suspended_inside_the_job(monkeypatch):
    job, proc = FakeJob(), FakeProc()
    handle, seen = _create(monkeypatch, job, proc)
    assert seen["creationflags"] & _SUSPENDED
    assert job.started == [777]
    assert handle._job is job


def test_create_assignment_failure_kills_the_unowned_child(monkeypatch):
    job, proc = FakeJob(start_error=OSError("denied")), FakeProc()
    with pytest.raises(GroupError) as error:
        _create(monkeypatch, job, proc)
    assert error.value.code == "spawn_failed"
    assert proc.killed == 1
    assert job.closed == 1


def test_create_without_job_support_keeps_legacy_spawn(monkeypatch):
    handle, seen = _create(monkeypatch, None, FakeProc())
    assert not seen["creationflags"] & _SUSPENDED
    assert handle._job is None


def test_group_kill_after_root_exit_terminates_the_job_not_taskkill(monkeypatch):
    ops = WindowsGroupOps()
    job = FakeJob(active=2)
    handle = _Bound(proc=FakeProc(rc=0), _job=job)
    taskkill = mock.Mock(return_value=False)  # root gone: /T would fail
    monkeypatch.setattr(WindowsGroupOps, "_taskkill", staticmethod(taskkill))
    assert ops.group_kill(handle, group_ops._SIGKILL) is True
    assert job.terminated == 1
    taskkill.assert_not_called()
    assert ops.group_alive(handle) is False


class _Tree:
    """Fake process table: ``_tree_pids``/``_pid_alive``/``_kill_pid``."""

    def __init__(self, monkeypatch, pids, dead=()):
        self.pids = list(pids)
        self.dead = set(dead)
        self.killed = []
        self.taskkill = mock.Mock(return_value=False)
        monkeypatch.setattr(WindowsGroupOps, "_tree_pids",
                            staticmethod(lambda root: list(self.pids)))
        monkeypatch.setattr(WindowsGroupOps, "_pid_alive",
                            staticmethod(lambda pid: pid not in self.dead))
        monkeypatch.setattr(WindowsGroupOps, "_kill_pid", staticmethod(self.kill))
        monkeypatch.setattr(WindowsGroupOps, "_taskkill", staticmethod(self.taskkill))

    def kill(self, pid):
        self.killed.append(pid)
        self.dead.add(pid)
        return True


def test_group_kill_without_job_kills_orphans_of_an_exited_root(monkeypatch):
    ops = WindowsGroupOps()
    tree = _Tree(monkeypatch, [4242, 5000, 5001], dead={4242})
    handle = _Bound(proc=FakeProc(rc=0))
    assert ops.group_kill(handle, group_ops._SIGKILL) is True
    # The exited root's (possibly reused) pid is never signalled.
    assert tree.killed == [5000, 5001]
    tree.taskkill.assert_not_called()
    assert ops.group_alive(handle) is False


def test_group_kill_without_job_kills_live_root_and_descendants(monkeypatch):
    ops = WindowsGroupOps()
    tree = _Tree(monkeypatch, [4242, 5000])
    handle = _Bound(proc=FakeProc(rc=None))
    assert ops.group_kill(handle, group_ops._SIGKILL) is True
    assert tree.killed == [4242, 5000]


def test_group_kill_attached_tree_uses_per_pid_fallback(monkeypatch):
    # After a supervisor restart there is no job handle: re-attached tree.
    ops = WindowsGroupOps()
    tree = _Tree(monkeypatch, [4242, 6000])
    handle = group_ops._AttachedHandle(pid=4242, pgid=4242,
                                       started_at="s", exe_path="x")
    assert ops.group_kill(handle, group_ops._SIGKILL) is True
    assert tree.killed == [4242, 6000]
    tree.dead.clear()
    tree.killed.clear()
    tree.dead.add(4242)  # root already exited: orphans still cleaned up
    assert ops.group_kill(handle, group_ops._SIGKILL) is True
    assert tree.killed == [6000]


def test_group_kill_without_enumeration_falls_back_to_taskkill(monkeypatch):
    ops = WindowsGroupOps()
    monkeypatch.setattr(WindowsGroupOps, "_tree_pids", staticmethod(lambda pid: None))
    taskkill = mock.Mock(return_value=True)
    monkeypatch.setattr(WindowsGroupOps, "_taskkill", staticmethod(taskkill))
    assert ops.group_kill(_Bound(proc=FakeProc(rc=None)), group_ops._SIGKILL) is True
    taskkill.assert_called_once_with(4242, force=True)


def test_group_reap_without_job_kills_leftover_descendants(monkeypatch):
    ops = WindowsGroupOps()
    tree = _Tree(monkeypatch, [4242, 5000], dead={4242})
    handle = _Bound(proc=FakeProc(rc=0))
    assert ops.group_reap(handle, timeout=0.2) is True
    assert tree.killed == [5000]


def test_supervisor_job_is_not_kill_on_close(monkeypatch):
    created = []

    class RecordingJob:
        def __init__(self, kill_on_close=True):
            created.append(kill_on_close)

    monkeypatch.setattr(wj, "WindowsJob", RecordingJob)
    assert isinstance(WindowsGroupOps._new_job(), RecordingJob)
    assert created == [False]


class _Fn:
    def __init__(self, impl):
        self.impl = impl
        self.argtypes = self.restype = None

    def __call__(self, *args):
        return self.impl(*args)


class _CreateKernel:
    def __init__(self):
        self.limits = []
        self.CreateJobObjectW = _Fn(lambda attrs, name: 55)
        self.SetInformationJobObject = _Fn(self._set)
        for name in ("AssignProcessToJobObject", "TerminateJobObject",
                     "QueryInformationJobObject", "CloseHandle"):
            setattr(self, name, _Fn(lambda *args: True))

    def _set(self, handle, cls, ref, size):
        self.limits.append((cls, ref._obj.basic.flags))
        return True


@pytest.mark.parametrize("kill_on_close, expected", [
    (True, [(9, 0x2000)]),
    (False, []),
])
def test_windows_job_kill_on_close_flag(monkeypatch, kill_on_close, expected):
    kernel = _CreateKernel()
    monkeypatch.setattr(wj.ctypes, "WinDLL", lambda *a, **k: kernel, raising=False)
    job = wj.WindowsJob(kill_on_close=kill_on_close)
    assert job.kill_on_close is kill_on_close
    assert kernel.limits == expected


def test_windows_job_defaults_to_kill_on_close(monkeypatch):
    kernel = _CreateKernel()
    monkeypatch.setattr(wj.ctypes, "WinDLL", lambda *a, **k: kernel, raising=False)
    assert wj.WindowsJob().kill_on_close is True
    assert kernel.limits == [(9, 0x2000)]


def test_group_alive_counts_orphans_through_the_job(monkeypatch):
    ops = WindowsGroupOps()
    # Tree enumeration from the dead root no longer finds the orphan.
    monkeypatch.setattr(WindowsGroupOps, "_tree_pids", staticmethod(lambda pid: []))
    handle = _Bound(proc=FakeProc(rc=0), _job=FakeJob(active=1))
    assert ops.group_alive(handle) is True
    handle._job.active = 0
    assert ops.group_alive(handle) is False


def test_group_reap_kills_leftover_descendants_and_releases_job():
    ops = WindowsGroupOps()
    job = FakeJob(active=1)
    handle = _Bound(proc=FakeProc(rc=0), _job=job)
    assert ops.group_reap(handle, timeout=0.2) is True
    assert job.terminated == 1
    assert job.released == 1
    assert handle._job is None


def test_group_reap_does_not_kill_when_root_still_running():
    ops = WindowsGroupOps()
    job = FakeJob(active=1)
    handle = _Bound(proc=FakeProc(rc=None), _job=job)
    assert ops.group_reap(handle, timeout=0.01) is False
    assert job.terminated == 0


def test_close_never_kills_a_live_tree_but_releases_an_empty_job():
    ops = WindowsGroupOps()
    live = FakeJob(active=1)
    handle = _Bound(proc=FakeProc(rc=None), _job=live)
    ops.close(handle)
    assert live.terminated == 0 and live.closed == 0 and live.released == 0
    assert handle._job is live
    live.active = 0
    ops.close(handle)
    assert live.released == 1 and handle._job is None


class _Kernel:
    def __init__(self, active):
        self.active = list(active)
        self.terminated = []
        self.closed = []

    def TerminateJobObject(self, handle, code):  # noqa: N802 - Win32 name
        self.terminated.append(handle)
        return True

    def QueryInformationJobObject(self, handle, cls, ref, size, ret):  # noqa: N802
        ref._obj.active = self.active.pop(0) if len(self.active) > 1 else self.active[0]
        return True

    def CloseHandle(self, handle):  # noqa: N802
        self.closed.append(handle)
        return True


def _job(kernel):
    job = object.__new__(wj.WindowsJob)
    job._kernel = kernel
    job._handle = 99
    return job


def test_windows_job_query_terminate_release():
    kernel = _Kernel([3])
    job = _job(kernel)
    assert job.active_processes() == 3
    assert job.terminate() is True
    assert kernel.terminated == [99]
    job.release()
    assert kernel.closed == [99]
    assert job.active_processes() is None
    assert job.terminate() is False


def test_windows_job_close_terminates_and_waits_for_exit():
    kernel = _Kernel([2, 0])
    job = _job(kernel)
    job.close()
    assert kernel.terminated == [99] and kernel.closed == [99]
