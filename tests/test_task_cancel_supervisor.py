"""Task 10 tests — connect task cancel to managed-attempt termination.

Covers:
- cancel rules for queued/leased/running/terminal tasks (state machine intact);
- a managed running attempt gets exactly ONE ``cancel_attempt`` command and a
  bounded ``control_pending`` audit;
- unmanaged cancels stay byte-identical (no command, no control audit, no
  session involvement);
- a command-queue failure produces bounded ``control_failed`` audit and never
  mutates the already-committed cancelled state;
- receipt finalization maps to the bounded outcomes ``terminated`` /
  ``already_finished`` / ``failed`` / ``expired`` / ``rejected`` and replays
  are idempotent (the ORIGINAL outcome is returned);
- no token/secret/key/path/nonce value ever appears in an audit row or error.

Signing keys are test-only placeholders; their VALUES never appear in output.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hub import events
from hub import state as store
from hub import task_store
from hub.application import control_router as cr
from hub.application.supervisor_service import SupervisorService
from hub.application.task_service import TaskService
from hub.domain import control as ctrl
from hub.infrastructure.session_repository import SessionRepository
from hub.infrastructure.task_repository import SqliteTaskRepository


def _keypair():
    return ctrl.generate_ed25519_keypair()


class _Publisher:
    def emit(self, event, **kwargs):
        pass


class _Hosts:
    def project_allowed(self, machine, project):
        return True


class _Obs:
    def read_current(self, machine):
        return {"machine": machine, "reachable": True}


def _session_repo(path):
    repo = SessionRepository(path)
    repo.init()
    return repo


def _start_managed_session(session_repo, machine, attempt_id, session_id=None):
    session_id = session_id or f"sess_{machine}_test"
    session_repo.upsert_session({
        "session_id": session_id,
        "machine_id": machine,
        "managed": True,
        "process_group_id": "pg_1",
        "attempt_id": attempt_id,
    })
    return session_id


class ManagedCancelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fleet-cancel-"))
        self.old_state = store.STATE_DIR
        self.old_events = events.EVENT_LOG
        self.old_db = task_store.DB_PATH
        store.STATE_DIR = self.tmp
        events.EVENT_LOG = self.tmp / "events.jsonl"
        task_store.DB_PATH = self.tmp / "fleet.db"
        task_store.init_db()

        self.task_repo = SqliteTaskRepository(self.tmp / "fleet.db")
        self.session_repo = _session_repo(self.tmp / "sessions" / "meta.db")
        self.priv, _pub = _keypair()
        self.supervisor = SupervisorService(signing_key=self.priv)
        self.router = cr.ControlRouter(
            supervisor=self.supervisor,
            task_repository=self.task_repo,
            session_repo=self.session_repo,
        )
        self.service = TaskService(
            task_repo=self.task_repo,
            observation_repo=_Obs(),
            event_publisher=_Publisher(),
            host_policy=_Hosts(),
        )
        self.service.cancel_router = self.router

    def tearDown(self):
        store.STATE_DIR = self.old_state
        events.EVENT_LOG = self.old_events
        task_store.DB_PATH = self.old_db

    # ---------------------------------------------------------------- #
    # helpers
    # ---------------------------------------------------------------- #

    def _create(self, machine="mac-1"):
        return self.task_repo.create_task(
            machine=machine, agent_type="codex", project="p", instruction="do",
            requested_by="op")[0]

    def _lease(self, task_id, machine="mac-1"):
        return self.task_repo.lease_task(machine=machine, runner_id="r1")

    def _managed_session(self, machine="mac-1"):
        """Create + lease a task and bind a managed session to its attempt."""
        task = self._create(machine)
        lease = self._lease(task["task_id"], machine)
        session_id = _start_managed_session(
            self.session_repo, machine, lease["attempt_id"])
        return task, lease, session_id

    def _managed_lease(self, machine="mac-1"):
        #: alias to avoid confusion in the tests
        return self._managed_session(machine)

    def _lease_only(self, machine="mac-1"):
        task = self._create(machine)
        lease = self._lease(task["task_id"], machine)
        return task, lease

    def _last_tracked(self):
        """Return the most-recently-tracked command id (fresh per call)."""
        keys = list(self.router._tracked)
        return keys[-1] if keys else ""

    def _lease_command(self):
        """Set up a managed attempt, mark it running, and cancel → cmd_id."""
        task, lease, _session_id = self._managed_lease()
        self.task_repo.heartbeat(
            attempt_id=lease["attempt_id"], nonce=lease["nonce"])
        self.service.cancel(task["task_id"], "op")
        cmd_id = self._last_tracked()
        return task, lease, cmd_id

    # ------------------------------------------------------------- #
    # queued / leased / running / terminal cancel rules
    # ------------------------------------------------------------- #

    def test_queued_cancel_no_command(self):
        task = self._create()
        result = self.service.cancel(task["task_id"], "op")
        self.assertTrue(result["changed"])
        self.assertEqual(result["task"]["state"], "cancelled")
        self.assertEqual(self.router.pending_count(), 0)
        self.assertEqual(self.router.audit_recent(50), [])

    def test_leased_cancel_routes_managed(self):
        task, lease, _session_id = self._managed_lease()
        result = self.service.cancel(task["task_id"], "op")
        self.assertTrue(result["changed"])
        self.assertEqual(self.router.pending_count(), 1)
        audit_events = [a["event"] for a in self.router.audit_recent(50)]
        self.assertIn("control_pending", audit_events)
        row = next(iter(self.router._tracked.values()))
        self.assertEqual(row["action"], "cancel_attempt")
        self.assertEqual(row["attempt_id"], lease["attempt_id"])

    def test_running_cancel_managed(self):
        task, lease, _session_id = self._managed_lease()
        # heartbeat turns lease → running
        self.task_repo.heartbeat(
            attempt_id=lease["attempt_id"], nonce=lease["nonce"], extend_s=300)
        result = self.service.cancel(task["task_id"], "op")
        self.assertTrue(result["changed"])
        self.assertEqual(result["task"]["state"], "cancelled")
        self.assertEqual(self.router.pending_count(), 1)
        audit_events = [a["event"] for a in self.router.audit_recent()]
        self.assertIn("control_pending", audit_events)
        row = next(iter(self.router._tracked.values()))
        self.assertEqual(row["attempt_id"], lease["attempt_id"])

    def test_terminal_task_cancel_is_noop_no_command(self):
        task = self._create()
        conn = task_store._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE tasks SET state='succeeded' WHERE task_id=?",
                         (task["task_id"],))
            conn.execute("COMMIT")
        finally:
            conn.close()
        result = self.service.cancel(task["task_id"], "op")
        self.assertFalse(result["changed"])
        self.assertEqual(result["task"]["state"], "succeeded")
        self.assertEqual(self.router.pending_count(), 0)

    # ------------------------------------------------------------- #
    # unmanaged cancel compatibility (byte-identical)
    # ------------------------------------------------------------- #

    def test_unmanaged_cancel_no_command_no_audit(self):
        task, lease = self._lease_only()
        result = self.service.cancel(task["task_id"], "op")
        self.assertTrue(result["changed"])
        self.assertEqual(result["task"]["state"], "cancelled")
        self.assertEqual(self.router.pending_count(), 0)
        self.assertEqual(self.router.audit_recent(), [])

    def test_unmanaged_other_machine_cancel_unaffected(self):
        task = self._create("mac-2")
        self.service.cancel(task["task_id"], "op")
        self.assertEqual(self.router.pending_count(), 0)

    def test_cancel_response_shape_unchanged(self):
        task, lease, _s = self._managed_lease()
        result = self.service.cancel(task["task_id"], "op")
        self.assertEqual(set(result), {"ok", "changed", "task"})
        self.assertNotIn("command_id", result)
        self.assertNotIn("attempt_id", result["task"])

    # ------------------------------------------------------------- #
    # command-queue failure → bounded control_failed, state intact
    # ------------------------------------------------------------- #

    def test_enqueue_failure_records_control_failed_state_stays_cancelled(self):
        self.router._supervisor = SupervisorService(signing_key=None)
        task, lease, _s = self._managed_lease()
        result = self.service.cancel(task["task_id"], "op")
        self.assertTrue(result["changed"])
        self.assertEqual(result["task"]["state"], "cancelled")  # NOT mutated
        self.assertEqual(self.router.pending_count(), 0)
        audit_events = [a["event"] for a in self.router.audit_recent()]
        self.assertIn("control_failed", audit_events)
        text = json.dumps(self.router.audit_recent())
        self.assertNotIn("Traceback", text)
        self.assertNotIn("private_key", text.lower())
        self.assertNotIn("signature", text)

    # ------------------------------------------------------------- #
    # receipt-driven finalization + idempotent replay
    # ------------------------------------------------------------- #

    def test_receipt_outcomes_mapping(self):
        for status, expected in [
            ("succeeded", "terminated"),
            ("already_finished", "already_finished"),
            ("failed", "failed"),
            ("expired", "expired"),
            ("rejected", "rejected"),
        ]:
            task, lease, cmd_id = self._lease_command()
            self.assertIsNotNone(cmd_id, status)
            outcome = self.router.on_supervisor_receipt(cmd_id, status=status)
            self.assertEqual(outcome, {"command_id": cmd_id,
                                       "outcome": expected, "first": True}, status)
            self.assertEqual(self.router.outcome_of(cmd_id), expected)
            # replay returns the ORIGINAL outcome deterministically
            replay = self.router.on_supervisor_receipt(cmd_id, status="failed")
            self.assertEqual(replay, {"command_id": cmd_id,
                                      "outcome": expected, "first": False})

    def test_non_terminal_status_does_not_finalize(self):
        task, lease, cmd_id = self._manual_command()
        self.assertIsNone(self.router.on_supervisor_receipt(cmd_id, status="accepted"))
        self.assertIsNone(self.router.on_supervisor_receipt(cmd_id, status="executing"))
        self.assertIsNone(self.router.on_supervisor_receipt(cmd_id, status="queued"))
        self.assertIsNone(self.router.outcome_of(cmd_id))

    def test_unknown_command_not_tracked(self):
        self.assertIsNone(self.router.on_supervisor_receipt("cmd-ghost",
                                                            status="succeeded"))

    def test_late_success_cannot_overwrite(self):
        task, lease, cmd_id = self._manual_command()
        self.router.on_supervisor_receipt(cmd_id, status="already_finished")
        late = self.router.on_supervisor_receipt(cmd_id, status="succeeded")
        self.assertEqual(self.router.outcome_of(cmd_id), "already_finished")
        self.assertEqual(late["outcome"], "already_finished")
        self.assertFalse(late["first"])

    def test_manual_command_helper(self):
        # the helper returns the command_id (managed running attempt)
        task, lease, cmd_id = self._manual_command()
        self.assertEqual(self.router.pending_count(), 1)
        self.assertEqual(cmd_id, next(iter(self.router._tracked)))

    def _manual_command(self):
        task, lease, _session_id = self._managed_lease()
        self.task_repo.heartbeat(
            attempt_id=lease["attempt_id"], nonce=lease["nonce"])
        self.service.cancel(task["task_id"], "op")
        cmd_id = self._last_tracked()
        return task, lease, cmd_id

    # ------------------------------------------------------------- #
    # audit bounded / secret-free
    # ------------------------------------------------------------- #

    def test_audit_rows_secret_free(self):
        task, lease, cmd_id = self._manual_command()
        self.router.on_supervisor_receipt(cmd_id, status="succeeded")
        text = json.dumps(self.router.audit_recent(50))
        self.assertNotIn("signature", text)
        self.assertNotIn("secret", text.lower())
        self.assertNotIn("private", text.lower())
        self.assertNotIn("token", text.lower())
        for row in self.router.audit_recent(50):
            self.assertIn("event", row)
            for value in row.values():
                if isinstance(value, str):
                    self.assertLessEqual(len(value), 320)

    def test_cancellation_surfaces_fixed_action_only(self):
        task = self._create()
        # a queued cancel routes through the fixed-action set only when managed;
        # here it is unmanaged so no arbitrary action can ever be issued
        self.service.cancel(task["task_id"], "op")
        for cmd in self.router._tracked.values():
            self.assertIn(cmd["action"], ctrl.CONTROL_ACTIONS)


if __name__ == "__main__":
    unittest.main()