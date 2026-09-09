"""Task 10 tests — cancellation races and attempt fencing.

Covers the interaction of task cancel with the existing lease/heartbeat/result
state machine:

- cancel-wins vs natural-completion-wins race;
- stale heartbeat after cancel is REJECTED (409 in the runner service);
- stale result after cancel is REJECTED (late success cannot overwrite a
  cancelled/terminated attempt);
- a natural completion BEFORE the cancel makes the cancel a no-op (terminal);
- receipt retry determinism (duplicate receipts replay the ORIGINAL receipt);
- lease reconciliation: an expired lease of a cancelled task is never
  requeued (cancelled not leased/running), so the task stays cancelled.

These tests reuse the real task/lease repository (identical existing contracts
are untouched) plus the Task 10 ControlRouter for the managed-cancel path.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hub import events
from hub import state as store
from hub import task_store
from hub.application import control_router as cr
from hub.application.runner_service import ApplicationError, RunnerService
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


def _bind_managed(session_repo, machine, attempt_id, session_id="sess_race"):
    session_repo.upsert_session({
        "session_id": session_id,
        "machine_id": machine,
        "managed": True,
        "process_group_id": "pg_1",
        "attempt_id": attempt_id,
    })


class CancelRaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fleet-race-"))
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
        self.svc = TaskService(
            task_repo=self.task_repo, observation_repo=_Obs(),
            event_publisher=_Publisher(), host_policy=_Hosts())
        self.svc.cancel_router = self.router
        self.runner = RunnerService(self.task_repo, _Publisher())

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

    def _cancellable(self, machine="mac-1", managed=True):
        """Create + lease (+ optionally bind a managed session) a task."""
        task = self._create(machine)
        lease = self._lease(task["task_id"], machine)
        self.assertIsNotNone(lease, "lease should exist for the queued task")
        if managed:
            _bind_managed(self.session_repo, machine, lease["attempt_id"])
        return task, lease

    def _last_tracked(self):
        """Return the most-recently-tracked command id (fresh per call)."""
        keys = list(self.router._tracked)
        return keys[-1] if keys else ""

    def _lease_command(self):
        """Managed running attempt, then cancel → the tracked command id."""
        task, lease = self._cancellable(machine="mac-1", managed=True)
        self.task_repo.heartbeat(
            attempt_id=lease["attempt_id"], nonce=lease["nonce"])
        self.svc.cancel(task["task_id"], "op")
        cmd_id = self._last_tracked()
        self.assertNotEqual(cmd_id, "", "cancel should have enqueued a command")
        return task, lease, cmd_id

    # ---------------------------------------------------------------- #
    # natural-completion race
    # ---------------------------------------------------------------- #

    def test_cancel_wins_then_stale_result_rejected(self):
        task, lease = self._cancellable(machine="mac-1", managed=True)
        # lease → running first
        self.task_repo.heartbeat(attempt_id=lease["attempt_id"],
                                 nonce=lease["nonce"])
        self.svc.cancel(task["task_id"], "op")
        # a LATE runner result must be REJECTED (no overwrite of cancelled)
        # at the repository level...
        late = self.task_repo.complete_task(
            attempt_id=lease["attempt_id"], nonce=lease["nonce"],
            exit_code=0, log_summary="late", diff_stat="", duration_s=1.0)
        self.assertIsNone(late)
        got = self.task_repo.get_task(task["task_id"])
        self.assertEqual(got["state"], "cancelled")
        self.assertIsNone(got["result"])
        # ...and at the HTTP-facing runner surface it is a bounded 409.
        with self.assertRaises(ApplicationError) as cm:
            self.runner.result("mac-1", lease["attempt_id"], lease["nonce"],
                               0, "late", "", 1.0)
        self.assertEqual(cm.exception.code, "lease_mismatch")
        self.assertEqual(cm.exception.status, 409)

    def test_natural_completion_wins_then_cancel_noop(self):
        task, lease = self._cancellable(machine="mac-1", managed=True)
        out = self.task_repo.heartbeat(
            attempt_id=lease["attempt_id"], nonce=lease["nonce"])
        self.assertIsNotNone(out)
        self.task_repo.complete_task(
            attempt_id=lease["attempt_id"], nonce=lease["nonce"],
            exit_code=0, log_summary="done", diff_stat="", duration_s=9.0)
        result = self.svc.cancel(task["task_id"], "op")
        self.assertFalse(result["changed"])
        self.assertEqual(result["task"]["state"], "succeeded")
        # the attempt finished naturally before cancel → no control command
        self.assertEqual(self.router.pending_count(), 0)

    def test_heartbeat_after_cancel_is_409(self):
        task, lease = self._cancellable(machine="mac-1", managed=True)
        self.svc.cancel(task["task_id"], "op")
        with self.assertRaises(ApplicationError) as cm:
            self.runner.heartbeat("mac-1", lease["attempt_id"], lease["nonce"])
        self.assertEqual(cm.exception.code, "lease_expired")
        self.assertEqual(cm.exception.status, 409)
        with self.assertRaises(ApplicationError) as cm:
            self.runner.heartbeat("mac-1", lease["attempt_id"], lease["nonce"],
                                  ["line"])
        self.assertEqual(cm.exception.code, "lease_expired")

    def test_heartbeat_does_not_extend_after_cancel(self):
        task, lease = self._cancellable(machine="mac-1", managed=True)
        self.svc.cancel(task["task_id"], "op")
        got = self.task_repo.heartbeat(
            attempt_id=lease["attempt_id"], nonce=lease["nonce"], extend_s=999)
        self.assertIsNone(got)

    # ---------------------------------------------------------------- #
    # receipt retry determinism
    # ---------------------------------------------------------------- #

    def test_receipt_retry_returns_original_outcome(self):
        task, lease, cmd_id = self._lease_command()
        hook = cr.SupervisorReceiptHook(self.supervisor, self.router)
        # the Agent sends a terminal receipt through the SAME path bootstrap
        # installs — the hook → supervisor → router cascade
        first = hook.receipt(cmd_id, "succeeded", "")
        self.assertEqual(first["status"], "succeeded")
        self.assertTrue(first["first"])
        self.assertEqual(self.router.outcome_of(cmd_id), "terminated")
        for _ in range(3):
            replay = hook.receipt(cmd_id, "failed", "late-junk")
            # the supervisor replays the ORIGINAL terminal receipt...
            self.assertEqual(replay["status"], "succeeded")
            self.assertFalse(replay["first"])
            # ...and the router finalizes to the same bounded outcome
            self.assertEqual(self.router.outcome_of(cmd_id), "terminated")
        self.assertEqual(self.router.outcome_of(cmd_id), "terminated")
        self.assertEqual(self.supervisor.command_status(cmd_id), "succeeded")

    def test_supervisor_receipt_replay_deterministic(self):
        # end-to-end through SupervisorService: duplicate terminal → original
        cmd = self.supervisor.enqueue("mac-1", "sess_s", "att_s",
                                      "cancel_attempt", "operator_requested")
        cid = cmd["command_id"]
        first = self.supervisor.receipt(cid, "already_finished",
                                        "no_live_process")
        self.assertTrue(first["first"])
        for _ in range(3):
            replay = self.supervisor.receipt(cid, "failed", "late junk")
            self.assertFalse(replay["first"])
            self.assertEqual(replay["status"], "already_finished")
        self.assertEqual(self.supervisor.command_status(cid), "already_finished")

    # ---------------------------------------------------------------- #
    # lease reconciliation
    # ---------------------------------------------------------------- #

    def test_lease_reconciliation_skips_cancelled_task(self):
        task, lease = self._cancellable(machine="mac-1", managed=True)
        self.svc.cancel(task["task_id"], "op")
        # Force the lease into the past, then sweep.  A cancelled task must
        # NOT be requeued back into the queue.
        conn = self.task_repo._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE leases SET expires_at='2000-01-01T00:00:00Z'"
                " WHERE attempt_id=?", (lease["attempt_id"],))
            conn.execute("COMMIT")
        finally:
            conn.close()
        requeued = self.task_repo.expire_leases()
        self.assertNotIn(task["task_id"], requeued)
        self.assertEqual(self.task_repo.get_task(task["task_id"])["state"],
                         "cancelled")

    def test_lease_reconciliation_keeps_live_lease(self):
        task, lease = self._cancellable(machine="mac-1", managed=True)
        # a live (not yet expired) lease is untouched by the sweep
        requeued = self.task_repo.expire_leases()
        self.assertEqual(requeued, [])
        self.assertEqual(self.task_repo.get_task(task["task_id"])["state"],
                         "leased")


if __name__ == "__main__":
    unittest.main()