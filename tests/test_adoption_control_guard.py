"""Task 9 tests — identity guard over every adopted-session control.

The five operator controls (``pause`` / ``resume`` / ``terminate`` /
``quarantine`` / ``cancel_attempt``) run against whatever the private handle
says, SO the guard re-verifies the LIVE identity of an ADOPTED session before
ANY signal:

- ``adoption_revoked``  — the probe already released this adoption (a signed
  ``detach`` or a guard revoke).  The client pre-check maps the removed
  session to this bounded code, and nothing is ever signalled.
- ``pid_reused``        — the live re-read sees a DIFFERENT ``started_at``
  for the same pid (the slot was reused); the guard detaches (revokes) with
  ZERO signals and returns the bounded code.  A ``terminate`` here NEVER
  signals.
- ``exe_changed``       — the live re-read sees a different ``exe_path``;
  same revoke-without-signal outcome.
- ``no_permission``     — the identity reader itself failed to produce a
  table; same revoke-without-signal outcome.
- Launched (non-attached) sessions pass the guard untouched — the invariant
  "launched control runs unchanged".

This suite drives the REAL ``Supervisor`` with a **recording** ``GroupOps`` so
any leaked signal is observable on the private handle (``signals`` /
``group_signals``); the guard-path assertions insist both stay empty.

RED phase: the controls today run against whatever the handle says with NO
identity re-check, so ``pid_reused`` terminate SIGKILLs the fake group and the
non-empty-signal assertions below are what fail first.  The fixed-reason
assertions are what make Step 2 red on the bounded codes too.
"""
from __future__ import annotations

import json
import signal
import tempfile
import unittest
from pathlib import Path

from test_supervisor_control import (
    _FakeOps,
    _Pipe,
    _command_payload,
    _keypair,
    _opaque,
    _sign,
)

from tools.supervisor.supervisor import Supervisor, _AttachedHandle

_MACHINE = "agent-a"


class _RecordingHandle(_AttachedHandle):
    """A private adopted handle that is a REAL ``_AttachedHandle`` subclass.

    The supervisor's guard recognizes adopted entries via
    ``isinstance(handle, _AttachedHandle)``; the control-suite fake handle is a
    plain object, so this suite uses its own recording handle that truly is an
    attached handle and records every signal the ops sends to it.
    """

    def __init__(self, pid, started_at, exe_path):
        super().__init__(pid=int(pid), pgid=int(pid),
                         started_at=started_at, exe_path=exe_path)
        self.signals = []          # every group signal attempted on the handle
        self.group_signals = []    # same, in group-terms (the signal value)
        self.alive = True


class _RecordingOps(_FakeOps):
    """In-memory GroupOps that RECORDS every signal on an adopted handle.

    The dispatch's recording shape: ``group_terminate`` / ``group_kill``
    append to the handle's arrays when it has them, and return True — a leaked
    signal to an adopted handle is visible, never silently ignored.
    ``attach`` returns a real ``_RecordingHandle`` so the identity guard's
    ``isinstance`` recognition works with the real class.
    """

    def attach(self, pid, started_at, exe_path):
        return _RecordingHandle(pid, started_at, exe_path)

    def group_alive(self, handle):
        return True

    def group_terminate(self, handle, sig):
        if hasattr(handle, "signals"):
            handle.signals.append(sig)
            handle.group_signals.append(sig)
        return True

    def group_kill(self, handle, sig):
        if hasattr(handle, "group_signals"):
            handle.group_signals.append(sig)
        return True


class AdoptionControlGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fleet-adopt-guard-"))
        from tools.supervisor.supervisor import Supervisor

        self.sup = Supervisor(
            manifest_dir=str(self.tmp / "sup"), machine_id=_MACHINE,
            ops=_RecordingOps())
        self.priv, self.pub = _keypair()
        self.adopt_sid = _opaque("adopt")
        self.pid = 8123
        self.started = "2026-08-30T19:00:00Z"
        self.exe = "/opt/agent/bin/code"
        # The attach AND every follow-up guard re-read share this default
        # reader (attach binds probe=None so the entry replays the same
        # default on each control re-read; flipping it flips the guard).
        self.sup._default_attach_probe = (
            lambda p: (self.started, self.exe) if int(p) == self.pid else None)

    # ---- harness helpers --------------------------------------------------- #

    def _client(self, transport=None):
        from tools.supervisor.control_client import ControlClient, NonceStore

        return ControlClient(
            hub_url="https://agent.test",
            machine_id=_MACHINE,
            credential="secret-secret",
            hub_public_key=self.pub,
            public_supervisor=self.sup,
            transport=transport or _Pipe(),
            nonce_store=NonceStore(self.tmp / "used"),
        )

    def _signed(self, action, session, *, attempt=None):
        body = _command_payload(
            machine=_MACHINE, session=session, attempt=attempt,
            action=action, nonce=_opaque("n"), command_id=_opaque("cmd"))
        return _sign(body, self.priv)

    def _adopt(self):
        rc = self.sup.attach_to_existing(
            self.pid, self.started, self.exe, None, "codex",
            session_id=self.adopt_sid)
        self.assertEqual(rc, "adopted")
        return self.sup._handle_of(self.adopt_sid)

    # ---- replicated portions of the brief ----------------------------------- #

    def test_revoked_adoption_control_has_no_signal(self):
        handle = self._adopt()
        # emit the revocation THE WAY THE REAL SYSTEM DOES: a signed detach
        # (the operator side supervises the repository row and issues detach;
        # the probe learns revocation via detach — never from a repository).
        detach_pipe = _Pipe([self._signed("detach", self.adopt_sid)])
        self._client(detach_pipe).poll_once()
        self.assertEqual(detach_pipe.uploads[0]["status"], "succeeded")
        self.assertEqual(detach_pipe.uploads[0]["reason"], "detached")
        self.assertNotIn(self.adopt_sid, self.sup.all_status())

        pause_pipe = _Pipe([self._signed("pause_session", self.adopt_sid)])
        self._client(pause_pipe).poll_once()
        receipt = pause_pipe.uploads[0]
        self.assertEqual(receipt["status"], "rejected")
        self.assertEqual(receipt["reason"], "adoption_revoked")
        # the private handle was never signalled and the entry is gone
        self.assertEqual(handle.signals, [])
        self.assertEqual(handle.group_signals, [])
        self.assertNotIn(self.adopt_sid, self.sup._entries)

    def test_pid_reuse_revokes_without_terminate(self):
        handle = self._adopt()
        # same pid, but the live re-read now sees a DIFFERENT started_at —
        # the pid slot was reused by a different invocation.
        self.sup._default_attach_probe = (
            lambda p: ("2026-08-31T00:00:00Z", self.exe)
            if int(p) == self.pid else None)
        pipe = _Pipe([self._signed("terminate_session", self.adopt_sid)])
        self._client(pipe).poll_once()
        receipt = pipe.uploads[0]
        self.assertEqual(receipt["status"], "rejected")
        self.assertEqual(receipt["reason"], "pid_reused")
        # terminate for a revoked/mismatched adoption is NEVER a signal
        self.assertEqual(handle.signals, [])
        self.assertEqual(handle.group_signals, [])
        # the supervisor dropped the entry (profile-level revoked)
        self.assertNotIn(self.adopt_sid, self.sup.all_status())
        self.assertNotIn(self.adopt_sid, self.sup._entries)

    def test_exe_changed_revokes_without_signal(self):
        handle = self._adopt()
        self.sup._default_attach_probe = (
            lambda p: (self.started, "/usr/bin/different")
            if int(p) == self.pid else None)
        pipe = _Pipe([self._signed("pause_session", self.adopt_sid)])
        self._client(pipe).poll_once()
        receipt = pipe.uploads[0]
        self.assertEqual(receipt["status"], "rejected")
        self.assertEqual(receipt["reason"], "exe_changed")
        self.assertEqual(handle.signals, [])
        self.assertEqual(handle.group_signals, [])
        self.assertNotIn(self.adopt_sid, self.sup.all_status())

    def test_no_permission_revokes_without_signal(self):
        handle = self._adopt()

        def broken(_pid):
            raise OSError("identity unreadable")

        self.sup._default_attach_probe = broken
        pipe = _Pipe([self._signed("resume_session", self.adopt_sid)])
        self._client(pipe).poll_once()
        receipt = pipe.uploads[0]
        self.assertEqual(receipt["status"], "rejected")
        self.assertEqual(receipt["reason"], "no_permission")
        self.assertEqual(handle.signals, [])
        self.assertEqual(handle.group_signals, [])
        self.assertNotIn(self.adopt_sid, self.sup.all_status())

    def test_launched_control_stays_compatible(self):
        launched = _opaque("sess")
        self.sup.launch(
            agent="codex", command=["/bin/true"], cwd="/tmp",
            env_allowlist={}, session_id=launched, attempt_id=None)
        pipe = _Pipe([self._signed("pause_session", launched)])
        self._client(pipe).poll_once()
        receipt = pipe.uploads[0]
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["reason"], "paused")
        self.assertEqual(self.sup.get(launched).state, "paused")

    def test_adopted_valid_control_stays_controllable(self):
        handle = self._adopt()
        pipe = _Pipe([self._signed("pause_session", self.adopt_sid)])
        self._client(pipe).poll_once()
        receipt = pipe.uploads[0]
        self.assertEqual(receipt["status"], "succeeded")
        self.assertEqual(receipt["reason"], "paused")
        # the ONE accepted signal for an adopted-valid session: a SIGSTOP to
        # the group, recorded exactly once on the private handle
        self.assertEqual(handle.group_signals, [signal.SIGSTOP])
        self.assertEqual(handle.signals, [signal.SIGSTOP])

    def test_bounded_receipt_never_leaks(self):
        handle = self._adopt()
        self.sup._default_attach_probe = (
            lambda p: ("1999-01-01T00:00:00Z", self.exe)
            if int(p) == self.pid else None)
        pipe = _Pipe([self._signed("terminate_session", self.adopt_sid)])
        self._client(pipe).poll_once()
        receipt = pipe.uploads[0]
        self.assertEqual(
            set(receipt),
            {"command_id", "machine_id", "status", "reason", "received_at"})
        text = json.dumps(receipt)
        for raw in (str(self.pid), self.started, self.exe):
            self.assertNotIn(raw, text)
        self.assertEqual(handle.signals, [])
        self.assertEqual(handle.group_signals, [])


if __name__ == "__main__":
    unittest.main()