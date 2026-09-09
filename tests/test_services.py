"""Pure domain and application-service contract tests."""

import threading
import time
import unittest
from unittest import mock

from hub.application.observe_service import ObserveError, ObserveService
from hub.application.task_service import ApplicationError, TaskService
from hub.application.runner_service import RunnerService
from hub.domain.machine import public_machine_detail, public_machine_summary
from hub.domain.task import (
    DomainError,
    can_cancel,
    can_continue,
    can_pause,
    can_retry,
    is_terminal_state,
    public_result,
    public_task,
    validate_task_input,
)


class TaskDomainTests(unittest.TestCase):
    def test_public_task_omits_internal_fields(self):
        task = {
            "task_id": "t-1",
            "attempt_id": "a",
            "client_token": "secret",
            "machine": "hk",
            "agent_type": "codex",
            "project": "agent-fleet",
            "state": "queued",
            "requested_by": "op@example.com",
            "created_at": "2026-08-21T00:00:00Z",
            "expires_at": "2026-08-22T00:00:00Z",
            "instruction": "run",
            "result": {"task_id": "t-1", "attempt_id": "a", "exit_code": 0},
        }
        public = public_task(task, with_result=True)
        self.assertNotIn("attempt_id", public)
        self.assertNotIn("client_token", public)
        self.assertNotIn("attempt_id", public["result"])
        self.assertNotIn("task_id", public["result"])
        self.assertEqual(public["instruction"], "run")

    def test_public_task_has_legacy_wire_fields(self):
        task = {"task_id": "t-1", "machine": "hk", "agent_type": "codex",
                "project": "agent-fleet", "state": "queued",
                "requested_by": "op", "created_at": "created",
                "expires_at": "expires", "instruction": "run"}
        self.assertEqual(
            set(public_task(task)),
            {"task_id", "machine", "agent_type", "project", "state",
             "requested_by", "created_at", "expires_at", "instruction"},
        )

    def test_public_task_includes_optional_session_id_when_valid(self):
        task = {"task_id": "t-1", "machine": "hk", "agent_type": "codex",
                "project": "agent-fleet", "state": "queued",
                "requested_by": "op", "created_at": "created",
                "expires_at": "expires", "instruction": "run",
                "session_id": "sess_bound_1"}
        public = public_task(task)
        self.assertEqual(public["session_id"], "sess_bound_1")
        self.assertNotIn("attempt_id", public)

    def test_public_task_omits_invalid_session_id(self):
        task = {"task_id": "t-1", "machine": "hk", "agent_type": "codex",
                "project": "agent-fleet", "state": "queued",
                "requested_by": "op", "created_at": "created",
                "expires_at": "expires", "instruction": "run",
                "session_id": "../secret.json"}
        self.assertNotIn("session_id", public_task(task))

    def test_public_result_allowlists_and_bounds_fields(self):
        result = public_result({
            "task_id": "hidden",
            "attempt_id": "secret",
            "exit_code": 1,
            "log_summary": "x" * 20000,
            "diff_stat": "y" * 8000,
            "duration_s": 1.5,
            "finished_at": "done",
            "unexpected": "hidden",
        })
        self.assertEqual(
            set(result),
            {"exit_code", "log_summary", "diff_stat", "duration_s", "finished_at"},
        )  # optional diff_patch/test_summary omitted when absent
        self.assertEqual(len(result["log_summary"]), 10240)
        self.assertEqual(len(result["diff_stat"]), 5120)

    def test_state_predicates_match_existing_api(self):
        self.assertTrue(can_cancel("running"))
        self.assertFalse(can_cancel("succeeded"))
        self.assertTrue(can_retry("failed"))
        self.assertFalse(can_retry("queued"))
        self.assertTrue(is_terminal_state("expired"))
        self.assertFalse(is_terminal_state("leased"))
        self.assertTrue(can_pause("queued"))
        self.assertTrue(can_cancel("paused"))
        self.assertTrue(can_continue("paused"))
        self.assertFalse(can_continue("queued"))

    def test_validate_task_input_returns_normalized_public_inputs(self):
        result = validate_task_input({
            "machine": "hk",
            "agent_type": "codex",
            "project": "agent-fleet",
            "instruction": "run tests",
            "client_token": "token-1",
            "ignored": "not copied",
        })
        self.assertEqual(result, {
            "machine": "hk",
            "agent_type": "codex",
            "project": "agent-fleet",
            "instruction": "run tests",
            "client_token": "token-1",
        })

    def test_validate_task_input_has_stable_domain_errors(self):
        with self.assertRaises(DomainError) as caught:
            validate_task_input({"machine": "../escape"})
        self.assertEqual(caught.exception.code, "invalid_machine")
        self.assertLessEqual(len(caught.exception.detail), 200)


class MachineDomainTests(unittest.TestCase):
    def test_public_machine_summary_sanitizes_snapshot(self):
        row = public_machine_summary(
            {
                "machine": "hk",
                "timestamp": "now",
                "reachable": True,
                "agents": {
                    "hermes": {
                        "installed": True,
                        "session_count": 2,
                        "sessions": [{"session_id": "secret"}],
                    }
                },
                "system": {"platform": "darwin", "load": "1.0", "command": "secret"},
                "_ts": 123,
            },
            {"name": "hk", "desc": "Hong Kong"},
        )
        self.assertEqual(row["machine"], "hk")
        self.assertEqual(row["desc"], "Hong Kong")
        self.assertTrue(row["online"])
        self.assertNotIn("sessions", row["agents"]["hermes"])
        self.assertNotIn("command", row["system"])
        self.assertNotIn("_ts", row)

    def test_public_machine_summary_handles_missing_and_unreachable(self):
        self.assertEqual(
            public_machine_summary(None, {"name": "hk", "desc": "Hong Kong"}),
            {"machine": "hk", "desc": "Hong Kong", "online": False,
             "error": "no data"},
        )
        self.assertEqual(
            public_machine_summary(
                {"machine": "hk", "reachable": False},
                {"name": "hk", "desc": "Hong Kong"},
            ),
            {"machine": "hk", "desc": "Hong Kong", "online": False,
             "error": "unreachable"},
        )

    def test_public_machine_detail_only_contains_public_current_and_history(self):
        detail = public_machine_detail(
            {
                "machine": "hk",
                "timestamp": "now",
                "reachable": True,
                "remote_error": None,
                "agents": {"codex": {"installed": True, "prompt": "secret"}},
                "system": {"platform": "darwin", "raw": "secret"},
                "_ts": 100,
            },
            [{"_ts": 100, "reachable": True, "prompt": "secret"}],
        )
        self.assertEqual(detail["machine"], "hk")
        self.assertEqual(detail["current"]["timestamp"], "now")
        self.assertNotIn("_ts", detail["current"])
        self.assertNotIn("prompt", detail["current"]["agents"]["codex"])
        self.assertEqual(detail["history"], [{"ts": 100, "reachable": True}])


class FakeObservationRepository:
    """Minimal in-memory observation repository for service tests.

    ``current`` (constructor seed) maps machine names to their latest snapshot;
    after ``save_snapshot`` the instance attribute ``current`` holds the most
    recently saved snapshot so tests can assert on the persisted payload.
    """

    def __init__(self, current=None) -> None:
        self._store = {}
        self.current = None
        for machine, snapshot in (current or {}).items():
            self._store[machine] = dict(snapshot)

    def save_snapshot(self, machine, snapshot):
        self._store[machine] = dict(snapshot)
        self.current = dict(snapshot)

    def read_current(self, machine):
        snapshot = self._store.get(machine)
        return dict(snapshot) if snapshot else None

    def read_history(self, machine, limit=50):
        snapshot = self._store.get(machine)
        return [dict(snapshot)] if snapshot else []

    def machines(self):
        return set(self._store)


class FakePublisher:
    """Records emitted events and exposes recent reads like EventPublisher."""

    def __init__(self) -> None:
        self.events = []

    def emit(self, event_type, machine=None, changes=None, snapshot=None, **extra):
        event = {
            "event": event_type,
            "machine": machine,
            "changes": list(changes or []),
            "ts": 1.0,
            "extra": extra,
        }
        self.events.append(event)
        return event

    def read_recent(self, limit=50):
        return list(self.events[-limit:])


class FakeHostConfig:
    """Minimal host config for service tests.

    Hosts may be seeded as ``hosts`` rows (like the real hosts config) or as
    an explicit ``projects`` mapping (like the app project whitelist). The
    ``online`` kwarg mirrors the policy default accepted by task creation.
    """

    def __init__(self, hosts=None, *, online=False, projects=None) -> None:
        self._hosts = [dict(h) for h in (hosts or [])]
        self._online = bool(online)
        self._projects = dict(projects or {})

    def names(self):
        return [h["name"] for h in self._hosts]

    def hosts(self):
        return [dict(h) for h in self._hosts]

    def stale_after_s(self, name):
        return 300

    def project_allowed(self, machine, project):
        allowed = self._projects.get(machine)
        if allowed is None:
            for h in self._hosts:
                if h.get("name") == machine:
                    allowed = h.get("projects") or []
                    break
        return project in (allowed or [])


class ObserveServiceTests(unittest.TestCase):
    def test_ingest_sanitizes_persists_and_emits_change(self):
        repo = FakeObservationRepository()
        publisher = FakePublisher()
        service = ObserveService(repo, publisher, FakeHostConfig())
        result = service.ingest({"machine": "hk", "agents": {"hermes": {
            "installed": True, "sessions": [{"session_id": "hidden"}]}}})
        self.assertTrue(result["ok"])
        self.assertNotIn("sessions", repo.current["agents"]["hermes"])
        self.assertEqual(publisher.events[0]["event"], "state_changed")

    def test_status_uses_repository_and_public_fields_only(self):
        repo = FakeObservationRepository(current={"hk": {
            "machine": "hk", "source": "ingest", "reachable": True,
            "agents": {}, "system": {}}})
        result = ObserveService(repo, FakePublisher(), FakeHostConfig()).status()
        self.assertIn("machines", result)
        self.assertNotIn("_ts", result["machines"][0])

    def test_ingest_rejects_invalid_machine_name(self):
        service = ObserveService(FakeObservationRepository(), FakePublisher(),
                                 FakeHostConfig())
        with self.assertRaises(ObserveError) as caught:
            service.ingest({"machine": "../escape", "agents": {}})
        self.assertEqual(caught.exception.code, "invalid_machine")

    def test_machine_detail_raises_not_found_and_bounds_history(self):
        repo = FakeObservationRepository(current={"hk": {
            "machine": "hk", "reachable": True, "agents": {"codex": {"installed": True}},
            "system": {}, "_ts": 100}})
        service = ObserveService(repo, FakePublisher(), FakeHostConfig())
        detail = service.machine_detail("hk")
        self.assertTrue(detail["ok"])
        self.assertNotIn("prompt", detail["current"]["agents"]["codex"])
        self.assertEqual(detail["history"], [{"ts": 100, "reachable": True}])
        with self.assertRaises(ObserveError) as caught:
            service.machine_detail("ghost")
        self.assertEqual(caught.exception.code, "not_found")

    def test_events_returns_public_summaries_without_snapshot_bodies(self):
        publisher = FakePublisher()
        publisher.events.append({
            "event": "state_changed", "machine": "hk", "ts": 1.0,
            "changes": ["agents"], "extra": {"snapshot": "secret"},
        })
        service = ObserveService(FakeObservationRepository(), publisher, FakeHostConfig())
        items = service.events(10)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["event"], "state_changed")
        self.assertNotIn("snapshot", items[0])

    def test_reconcile_marks_stale_ingest_machine_offline(self):
        repo = FakeObservationRepository(current={"hk": {
            "machine": "hk", "source": "ingest", "reachable": True,
            "agents": {}, "system": {}, "_ts": time.time() - 5000}})
        publisher = FakePublisher()
        service = ObserveService(repo, publisher, FakeHostConfig(),
                                 clock=lambda: time.time())
        results = service.reconcile(machine="hk")
        self.assertEqual([r["machine"] for r in results], ["hk"])
        self.assertFalse(repo.read_current("hk")["reachable"])
        self.assertIn("reachable", results[0]["changed"])
        self.assertEqual(publisher.events[-1]["event"], "state_changed")


class FakeTaskRepository:
    """Minimal in-memory task repository for service tests.

    ``created`` records each task row create requests so tests can assert the
    repository write happens only after policy checks pass. ``lease`` /
    ``result`` seed the runner-facing responses.
    """

    def __init__(self, lease=None, result=None) -> None:
        self.created = []
        self.lease = lease
        self.result = result
        self._row = None
        self._changed = None
        self.heartbeat_called = None
        self.completed = None

    def create_task(self, *, machine, agent_type, project, instruction,
                    requested_by, client_token=None, **extra):
        row = {
            "task_id": "t-new", "machine": machine, "agent_type": agent_type,
            "project": project, "instruction": instruction, "state": "queued",
            "requested_by": requested_by,
            "created_at": "2026-08-22T00:00:00Z", "expires_at": "2026-08-23T00:00:00Z",
            "client_token": client_token,
        }
        self.created.append(dict(row))
        return dict(row), True

    def get_task(self, task_id):
        if not self._row:
            return None
        return dict(self._row)

    def list_tasks(self, machine=None, state=None, limit=50):
        return [dict(self._row)] if self._row else []

    def lease_task(self, *, machine, runner_id, lease_ttl_s=300, now=None):
        if self.lease is None:
            return None
        return dict(self.lease)

    def heartbeat(self, *, attempt_id, nonce, extend_s=300, now=None):
        self.heartbeat_calls = (attempt_id, nonce)
        return {"task_id": attempt_id, "lease_expires_at": "2026-08-22T00:05:00Z"}

    def complete_task(self, *, attempt_id, nonce, exit_code, log_summary,
                      diff_stat, duration_s, now=None, files=None,
                      diff_patch=None, test_summary=None):
        self.completed = (attempt_id, nonce, exit_code)
        self.completed_files = files
        self.completed_patch = diff_patch
        self.completed_tests = test_summary
        if self.result is None:
            return None
        return dict(self.result)

    def pause_task(self, task_id, actor, now=None):
        if not self._row:
            return None, False
        if self._row.get("state") not in ("queued", "leased", "running"):
            return dict(self._row), False
        self._row = dict(self._row, state="paused")
        return dict(self._row), True

    def continue_task(self, task_id, actor, now=None):
        if not self._row:
            return None, False
        if self._row.get("state") != "paused":
            return dict(self._row), False
        self._row = dict(self._row, state="queued", attempt_id="a-new")
        return dict(self._row), True

    def confirm_task(self, task_id, actor, now=None):
        if not self._row:
            return None, False
        gate = self._row.get("gate") or {}
        if gate.get("state") != "pending":
            return dict(self._row), False
        self._row = dict(self._row, gate=dict(gate, state="confirmed"))
        return dict(self._row), True

    def reject_task(self, task_id, actor, now=None):
        if not self._row:
            return None, False
        gate = self._row.get("gate") or {}
        if gate.get("state") != "pending":
            return dict(self._row), False
        self._row = dict(self._row, state="cancelled",
                         gate=dict(gate, state="rejected"))
        return dict(self._row), True

    def list_result_files(self, task_id):
        return []

    def get_result_file(self, task_id, path):
        return None

    def audit_action(self, actor, action, task_id, detail=None, now=None):
        return None

    def expire_leases(self, now=None):
        return []

    def expire_tasks(self, now=None):
        return []


class TaskServiceTests(unittest.TestCase):
    def test_create_rejects_offline_machine_before_repository_write(self):
        repo = FakeTaskRepository()
        service = TaskService(repo, FakeObservationRepository(), FakePublisher(),
                              FakeHostConfig(online=False))
        with self.assertRaises(ApplicationError) as ctx:
            service.create({"machine": "hk", "agent_type": "codex",
                            "project": "agent-fleet", "instruction": "run"}, "op")
        self.assertEqual(ctx.exception.code, "machine_offline")
        self.assertEqual(repo.created, [])

    def test_create_requires_allowed_project(self):
        # 机器在线但项目未在 whitelist → project_not_allowed，不写库
        repo = FakeTaskRepository()
        service = TaskService(
            repo,
            FakeObservationRepository(current={"hk": {"machine": "hk",
                                                      "reachable": True}}),
            FakePublisher(),
            FakeHostConfig(projects={"hk": ["other"]}))
        with self.assertRaises(ApplicationError) as ctx:
            service.create({"machine": "hk", "agent_type": "codex",
                            "project": "agent-fleet", "instruction": "run"}, "op")
        self.assertEqual(ctx.exception.code, "project_not_allowed")
        self.assertEqual(repo.created, [])

    def test_create_persists_and_emits_queued_when_allowed(self):
        repo = FakeTaskRepository()
        publisher = FakePublisher()
        service = TaskService(
            repo,
            FakeObservationRepository(current={"hk": {"machine": "hk",
                                                       "reachable": True}}),
            publisher,
            FakeHostConfig(projects={"hk": ["agent-fleet"]}))
        result = service.create({"machine": "hk", "agent_type": "codex",
                                 "project": "agent-fleet", "instruction": "run"}, "op")
        self.assertTrue(result["created"])
        self.assertEqual(result["task"]["state"], "queued")
        self.assertEqual(publisher.events[-1]["event"], "task_queued")
        self.assertEqual(repo.created[0]["requested_by"], "op")

    def test_create_reports_idempotent_no_event_on_dup(self):
        repo = FakeTaskRepository()
        publisher = FakePublisher()
        calls = {"n": 0}

        def create_task(**kw):
            calls["n"] += 1
            row = {"task_id": "t-dup", "machine": kw["machine"], "state": "queued"}
            return row, calls["n"] == 1

        repo.create_task = create_task
        service = TaskService(
            repo,
            FakeObservationRepository(current={"hk": {"machine": "hk",
                                                       "reachable": True}}),
            publisher,
            FakeHostConfig(projects={"hk": ["agent-fleet"]}))
        data = {"machine": "hk", "agent_type": "codex", "project": "agent-fleet",
                "instruction": "run", "client_token": "dup"}
        first = service.create(data, "op")
        self.assertTrue(first["created"])
        second = service.create(dict(data), "op")
        self.assertFalse(second["created"])
        # idempotent 命中不重复 emit task_queued
        queued = [e for e in publisher.events if e["event"] == "task_queued"]
        self.assertEqual(len(queued), 1)

    def test_get_missing_raises_not_found(self):
        service = TaskService(FakeTaskRepository(), FakeObservationRepository(),
                              FakePublisher(), FakeHostConfig())
        with self.assertRaises(ApplicationError) as ctx:
            service.get("t-missing")
        self.assertEqual(ctx.exception.code, "not_found")
        self.assertEqual(ctx.exception.status, 404)

    def test_cancel_and_retry_publish_events(self):
        repo = FakeTaskRepository()
        repo._row = {"task_id": "t-1", "machine": "hk", "agent_type": "codex",
                     "project": "p", "state": "queued", "requested_by": "op",
                     "created_at": "c", "expires_at": "e", "instruction": "run"}
        repo.cancel_task = lambda task_id, actor: (dict(repo._row), True)
        repo.retry_task = lambda task_id, actor: (dict(repo._row), True)
        publisher = FakePublisher()
        service = TaskService(repo, FakeObservationRepository(), publisher,
                              FakeHostConfig())
        cancelled = service.cancel("t-1", "op")
        self.assertTrue(cancelled["changed"])
        self.assertEqual(publisher.events[-1]["event"], "task_cancelled")
        retried = service.retry("t-1", "op")
        self.assertTrue(retried["changed"])
        self.assertEqual(publisher.events[-1]["event"], "task_queued")

    def test_public_dto_omits_session_id_without_session_repo(self):
        repo = FakeTaskRepository()
        repo._row = {"task_id": "t-1", "machine": "hk", "agent_type": "codex",
                     "project": "p", "state": "running", "requested_by": "op",
                     "created_at": "c", "expires_at": "e", "instruction": "run",
                     "attempt_id": "att-1"}
        service = TaskService(repo, FakeObservationRepository(), FakePublisher(),
                              FakeHostConfig())
        public = service.get("t-1")["task"]
        self.assertNotIn("session_id", public)
        self.assertNotIn("attempt_id", public)

    def test_public_dto_projects_session_id_from_existing_attempt_binding(self):
        repo = FakeTaskRepository()
        repo._row = {"task_id": "t-1", "machine": "hk", "agent_type": "codex",
                     "project": "p", "state": "running", "requested_by": "op",
                     "created_at": "c", "expires_at": "e", "instruction": "run",
                     "attempt_id": "att-bound"}

        class FakeSessionRepo:
            def list_sessions(self, machine_id=None, limit=1000):
                return [{
                    "session_id": "sess_bound_1",
                    "machine_id": machine_id,
                    "attempt_id": "att-bound",
                    "managed": True,
                }]

        service = TaskService(repo, FakeObservationRepository(), FakePublisher(),
                              FakeHostConfig(), session_repo=FakeSessionRepo())
        public = service.get("t-1")["task"]
        self.assertEqual(public["session_id"], "sess_bound_1")
        self.assertNotIn("attempt_id", public)

    def test_public_dto_omits_session_id_when_attempt_does_not_match(self):
        repo = FakeTaskRepository()
        repo._row = {"task_id": "t-1", "machine": "hk", "agent_type": "codex",
                     "project": "p", "state": "running", "requested_by": "op",
                     "created_at": "c", "expires_at": "e", "instruction": "run",
                     "attempt_id": "att-other"}

        class FakeSessionRepo:
            def list_sessions(self, machine_id=None, limit=1000):
                return [{
                    "session_id": "sess_bound_1",
                    "machine_id": machine_id,
                    "attempt_id": "att-bound",
                    "managed": True,
                }]

        service = TaskService(repo, FakeObservationRepository(), FakePublisher(),
                              FakeHostConfig(), session_repo=FakeSessionRepo())
        self.assertNotIn("session_id", service.get("t-1")["task"])


class RunnerServiceTests(unittest.TestCase):
    def test_poll_returns_lease_and_emits_leased(self):
        lease = {"task_id": "t-1", "machine": "hk", "attempt_id": "a-1",
                 "nonce": "n-1", "lease_ttl_s": 300, "lease_expires_at": "e"}
        publisher = FakePublisher()
        service = RunnerService(FakeTaskRepository(lease=lease), publisher)
        result = service.poll("hk", "runner-1")
        self.assertEqual(result["task"]["task_id"], "t-1")
        self.assertEqual(publisher.events[-1]["event"], "task_leased")

    def test_heartbeat_uses_adapter_provided_machine_only(self):
        repo = FakeTaskRepository()
        repo.result = None
        publisher = FakePublisher()
        service = RunnerService(repo, publisher)
        out = service.heartbeat("hk", "a-1", "n-1", ["line1"])
        self.assertEqual(out["ok"], True)
        self.assertEqual(repo.heartbeat_calls, ("a-1", "n-1"))
        self.assertEqual(publisher.events[-1]["event"], "task_log")
        self.assertEqual(publisher.events[-1]["machine"], "hk")
        # 事件里带着 adapter 传入的机器，而不是 runner JSON 中的 machine
        self.assertNotEqual(publisher.events[-1]["machine"], "evil")

    def test_runner_result_publishes_task_finished_after_repository_result(self):
        repo = FakeTaskRepository(result={"task_id": "t-1", "state": "succeeded",
                                          "stored": True})
        publisher = FakePublisher()
        service = RunnerService(repo, publisher)
        result = service.result("hk", "a-1", "n-1", 0, "ok", "1 file", 1.2)
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(repo.completed, ("a-1", "n-1", 0))
        # 事件在仓库返回之后发布
        self.assertEqual(publisher.events[-1]["event"], "task_finished")
        self.assertEqual(publisher.events[-1]["machine"], "hk")

    def test_heartbeat_raises_lease_expired_when_repo_returns_none(self):
        repo = FakeTaskRepository(lease=None)
        repo.heartbeat = lambda **kw: None
        service = RunnerService(repo, FakePublisher())
        with self.assertRaises(ApplicationError) as ctx:
            service.heartbeat("hk", "a-1", "n-1", [])
        self.assertEqual(ctx.exception.code, "lease_expired")
        self.assertEqual(ctx.exception.status, 409)

    def test_result_sanitizes_files_before_repository_write(self):
        repo = FakeTaskRepository(result={"task_id": "t-1", "state": "succeeded",
                                          "stored": True})
        service = RunnerService(repo, FakePublisher())
        service.result("hk", "a-1", "n-1", 0, "ok", "1 file", 1.2, files=[
            {"path": "../etc/passwd", "content": "root:x:0:0\n"},
            {"path": ".env", "content": "SECRET=1\n"},
            {"path": "ok.py", "content": "x = 1\n"},
        ])
        paths = [item["path"] for item in repo.completed_files]
        self.assertEqual(paths, ["ok.py"])
        self.assertEqual(repo.completed_files[0]["content"], "x = 1\n")

    def test_result_raises_lease_mismatch_when_repo_returns_none(self):
        repo = FakeTaskRepository(result=None)
        service = RunnerService(repo, FakePublisher())
        with self.assertRaises(ApplicationError) as ctx:
            service.result("hk", "a-1", "n-1", 1, "x", "", 0.1)
        self.assertEqual(ctx.exception.code, "lease_mismatch")
        self.assertEqual(ctx.exception.status, 409)


class FakeObserveService:
    """Records reconcile calls so tests can assert forwarding to the service."""

    def __init__(self) -> None:
        self.reconcile_calls = []

    def reconcile(self, machine=None, *, now=None):
        self.reconcile_calls.append((machine, now))
        return []


class ReconciliationServiceTests(unittest.TestCase):
    def _service(self, observe=None, repo=None, publisher=None, clock=None):
        from hub.application.reconciliation_service import ReconciliationService
        return ReconciliationService(
            observe or FakeObserveService(),
            repo or FakeTaskRepository(),
            publisher or FakePublisher(),
            clock=clock or (lambda: 100.0),
        )

    def test_observation_reconcile_only_calls_push_state_service(self):
        from hub.application.reconciliation_service import ReconciliationService
        service = ReconciliationService(
            FakeObserveService(), FakeTaskRepository(), FakePublisher())
        with mock.patch("subprocess.run", side_effect=AssertionError("remote execution")):
            result = service.reconcile_observation()
        self.assertEqual(result, [])

    def test_reconcile_observation_forwards_machine_and_uses_injected_clock(self):
        observe = FakeObserveService()
        service = self._service(observe=observe, clock=lambda: 123.0)
        service.reconcile_observation(machine="hk")
        self.assertEqual(observe.reconcile_calls[-1], ("hk", 123.0))

    def test_expire_task_leases_emits_one_update_per_requeued_task(self):
        repo = FakeTaskRepository()
        repo.expire_leases = lambda now=None: ["t-1", "t-2"]
        publisher = FakePublisher()
        service = self._service(repo=repo, publisher=publisher)
        self.assertEqual(service.expire_task_leases(), ["t-1", "t-2"])
        updates = [e for e in publisher.events if e["event"] == "task_update"]
        self.assertEqual([e["extra"]["task_id"] for e in updates], ["t-1", "t-2"])
        self.assertTrue(all(e["extra"]["state"] == "queued" for e in updates))

    def test_expire_task_leases_emits_nothing_when_none_requeued(self):
        publisher = FakePublisher()
        service = self._service(publisher=publisher)
        self.assertEqual(service.expire_task_leases(), [])
        self.assertEqual(publisher.events, [])

    def test_expire_tasks_forwards_to_repository(self):
        repo = FakeTaskRepository()
        repo.expire_tasks = lambda now=None: ["t-expired"]
        service = self._service(repo=repo)
        self.assertEqual(service.expire_tasks(), ["t-expired"])

    def test_reconcile_leases_runs_requeue_then_ttl_sweep_with_one_clock(self):
        repo = FakeTaskRepository()
        calls = []
        repo.expire_leases = lambda now=None: calls.append(now) or ["t-1"]
        repo.expire_tasks = lambda now=None: calls.append(now) or []
        service = self._service(repo=repo, clock=lambda: 7.0)
        self.assertEqual(service.reconcile_leases(), ["t-1"])
        self.assertEqual(calls, [7.0, 7.0])


class ReconciliationDaemonTests(unittest.TestCase):
    def test_start_reconciliation_invokes_callback_until_stopped(self):
        from hub.application.reconciliation_service import start_reconciliation
        calls = []

        def cb():
            calls.append(1)

        stop = start_reconciliation(cb, interval_s=1)
        deadline = time.monotonic() + 3.0
        while len(calls) < 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        stop.set()
        self.assertGreaterEqual(len(calls), 1)

    def test_start_reconciliation_respects_injected_stop_event(self):
        from hub.application.reconciliation_service import start_reconciliation
        calls = []

        def cb():
            calls.append(1)

        stop = threading.Event()
        stop.set()
        handle = start_reconciliation(cb, stop_event=stop, interval_s=1)
        self.assertIs(handle, stop)
        time.sleep(0.05)
        self.assertEqual(calls, [])
        self.assertTrue(stop.is_set())

    def test_start_lease_reconciler_invokes_callback_until_stopped(self):
        from hub.application.reconciliation_service import start_lease_reconciler
        calls = []

        def cb():
            calls.append("lease")

        stop = start_lease_reconciler(cb, interval_s=1)
        deadline = time.monotonic() + 3.0
        while len(calls) < 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        stop.set()
        self.assertGreaterEqual(len(calls), 1)



if __name__ == "__main__":
    unittest.main()
