from pathlib import Path

import pytest

from hub.infrastructure.platform_db import PlatformRepository, PlatformRepositoryError
from hub.application.legacy_task_bridge import LegacyTaskBridge, legacy_client_token
from hub.application.task_service import ApplicationError, TaskHostPolicy, TaskService
from hub.infrastructure.task_repository import SqliteTaskRepository


OWNER = "owner@example.test"
REQUEST = {
    "machine": "mac-local",
    "agent_type": "codex",
    "project": "agent-fleet",
    "instruction": "运行测试并整理结果",
    "confirm": False,
}


def _repository(tmp_path: Path) -> PlatformRepository:
    repository = PlatformRepository(tmp_path / "platform.db")
    repository.init()
    conversation = repository.create_conversation(
        OWNER, "conv-legacy", title="", workspace_id=None,
    )
    repository.append_turn(
        OWNER,
        conversation["conversation_id"],
        "msg-legacy",
        "run-legacy",
        text="桥接旧任务",
        client_token="legacy-turn",
        config_snapshot={},
        now=1.0,
    )
    return repository


def test_enqueue_legacy_task_is_idempotent_and_moves_run_to_waiting_task(tmp_path):
    repository = _repository(tmp_path)

    first = repository.enqueue_legacy_task(OWNER, "run-legacy", REQUEST, now=2.0)
    second = repository.enqueue_legacy_task(OWNER, "run-legacy", dict(REQUEST), now=3.0)

    assert first["run_id"] == second["run_id"] == "run-legacy"
    assert first["state"] == second["state"] == "pending"
    assert repository.get_run(OWNER, "run-legacy")["state"] == "waiting_task"
    events = repository.list_run_events(OWNER, "run-legacy")["events"]
    assert [event["kind"] for event in events] == ["legacy_task_queued"]
    assert events[0]["payload"] == {"bridge_state": "pending", "state": "waiting_task"}


def test_enqueue_rejects_changed_request_and_non_queued_run(tmp_path):
    repository = _repository(tmp_path)
    repository.enqueue_legacy_task(OWNER, "run-legacy", REQUEST, now=2.0)

    changed = dict(REQUEST, instruction="另一项任务")
    with pytest.raises(PlatformRepositoryError, match="legacy_task_conflict"):
        repository.enqueue_legacy_task(OWNER, "run-legacy", changed, now=3.0)

    with pytest.raises(PlatformRepositoryError, match="run_not_found"):
        repository.enqueue_legacy_task("other@example.test", "run-legacy", REQUEST, now=3.0)


def test_claim_legacy_task_has_a_fenced_retry_lease(tmp_path):
    repository = _repository(tmp_path)
    repository.enqueue_legacy_task(OWNER, "run-legacy", REQUEST, now=2.0)

    first = repository.claim_legacy_task(OWNER, worker_id="bridge-a", now=3.0, lease_s=5.0)
    assert first["lease_owner"] == "bridge-a"
    assert first["bridge_attempt"] == 1
    assert repository.claim_legacy_task(OWNER, worker_id="bridge-b", now=4.0, lease_s=5.0) is None

    reclaimed = repository.claim_legacy_task(OWNER, worker_id="bridge-b", now=9.0, lease_s=5.0)
    assert reclaimed["lease_owner"] == "bridge-b"
    assert reclaimed["bridge_attempt"] == 2


def test_projection_updates_attempt_and_finishes_run_once(tmp_path):
    repository = _repository(tmp_path)
    repository.enqueue_legacy_task(OWNER, "run-legacy", REQUEST, now=2.0)
    claim = repository.claim_legacy_task(OWNER, worker_id="bridge-a", now=3.0, lease_s=5.0)

    projection = {
        "task_id": "t-legacy",
        "attempt_id": "attempt-2",
        "session_id": None,
        "task_state": "succeeded",
        "bridge_state": "succeeded",
        "summary": "测试通过",
        "diff": {"stat": "1 file changed", "has_patch": True, "patch_bytes": 42},
        "tests": {"framework": "pytest", "passed": 3, "failed": 0},
        "files": [{"path": "report.md", "bytes": 12}],
    }
    updated, changed = repository.apply_legacy_task_projection(
        OWNER, "run-legacy", projection, now=4.0,
        worker_id="bridge-a", lease_attempt=claim["bridge_attempt"],
    )
    again, changed_again = repository.apply_legacy_task_projection(
        OWNER, "run-legacy", projection, now=5.0,
    )

    assert changed is True
    assert changed_again is False
    assert updated["attempt_id"] == again["attempt_id"] == "attempt-2"
    assert updated["state"] == "succeeded"
    assert repository.get_run(OWNER, "run-legacy")["state"] == "succeeded"
    events = repository.list_run_events(OWNER, "run-legacy")["events"]
    assert [event["kind"] for event in events] == ["legacy_task_queued", "legacy_task_update"]
    assert events[-1]["payload"]["task_id"] == "t-legacy"


def test_projection_does_not_overwrite_a_cancelled_run(tmp_path):
    repository = _repository(tmp_path)
    repository.enqueue_legacy_task(OWNER, "run-legacy", REQUEST, now=2.0)
    repository.cancel_run(OWNER, "run-legacy", now=3.0)
    claim = repository.claim_legacy_task(OWNER, worker_id="bridge-a", now=4.0, lease_s=5.0)

    projection = {
        "task_id": "t-legacy",
        "attempt_id": "attempt-1",
        "task_state": "succeeded",
        "bridge_state": "succeeded",
        "summary": "迟到结果",
        "diff": {},
        "tests": {},
        "files": [],
    }
    updated, changed = repository.apply_legacy_task_projection(
        OWNER, "run-legacy", projection, now=5.0,
        worker_id="bridge-a", lease_attempt=claim["bridge_attempt"],
    )

    assert changed is True
    assert updated["state"] == "succeeded"
    assert repository.get_run(OWNER, "run-legacy")["state"] == "cancelling"


class _ObservationRepository:
    def read_current(self, machine):
        return {"machine": machine, "reachable": True}


class _Publisher:
    def __init__(self):
        self.events = []

    def emit(self, event, **payload):
        self.events.append((event, payload))


def _bridge(tmp_path, repository=None):
    repository = repository or _repository(tmp_path)
    task_repository = SqliteTaskRepository(tmp_path / "legacy.db")
    task_repository.init()
    task_service = TaskService(
        task_repository,
        _ObservationRepository(),
        _Publisher(),
        TaskHostPolicy(project_whitelist={"mac-local": ["agent-fleet"]}),
    )
    return repository, task_repository, LegacyTaskBridge(
        repository, task_service, task_repository, worker_id="bridge-test",
    )


def test_bridge_retries_with_one_stable_legacy_task_and_projects_result(tmp_path):
    repository, task_repository, bridge = _bridge(tmp_path)

    queued = bridge.enqueue(OWNER, "run-legacy", REQUEST)
    first = bridge.process_once(OWNER, run_id="run-legacy")
    second = bridge.process_once(OWNER, run_id="run-legacy")

    assert queued["state"] == "pending"
    assert first["state"] == second["state"] == "linked"
    assert first["task_id"] == second["task_id"]
    assert first["session_id"] is None
    assert len(task_repository.list_tasks()) == 1
    assert legacy_client_token(OWNER, "run-legacy").startswith("legacy-run-")

    lease = task_repository.lease_task(machine="mac-local", runner_id="runner-1", now=10.0)
    task_repository.complete_task(
        attempt_id=lease["attempt_id"], nonce=lease["nonce"], exit_code=0,
        log_summary="summary " * 800, diff_stat="1 file changed",
        diff_patch="+result\n", duration_s=1.2, now=11.0,
        test_summary={"framework": "pytest", "passed": 3, "failed": 0},
        files=[{"path": "report.md", "content": "ok", "bytes": 2}],
    )
    finished = bridge.process_once(OWNER, run_id="run-legacy")

    assert finished["state"] == "succeeded"
    assert finished["task_state"] == "succeeded"
    assert len(finished["projection"]["summary"]) == 2000
    assert finished["projection"]["diff"]["has_patch"] is True
    assert finished["projection"]["tests"]["passed"] == 3
    assert finished["projection"]["files"] == [{
        "path": "report.md", "bytes": 2, "truncated": False, "redacted": False,
    }]
    assert repository.get_run(OWNER, "run-legacy")["state"] == "succeeded"


def test_bridge_refreshes_attempt_after_pause_and_continue_without_session_fabrication(tmp_path):
    repository, task_repository, bridge = _bridge(tmp_path)
    bridge.enqueue(OWNER, "run-legacy", REQUEST)
    linked = bridge.process_once(OWNER, run_id="run-legacy")
    task_id = linked["task_id"]

    paused = task_service = bridge.tasks.pause(task_id, OWNER)
    assert paused["task"]["state"] == "paused"
    paused_projection = bridge.process_once(OWNER, run_id="run-legacy")
    first_attempt = paused_projection["attempt_id"]

    continued = task_service = bridge.tasks.continue_task(task_id, OWNER)
    assert continued["task"]["state"] == "queued"
    continued_projection = bridge.process_once(OWNER, run_id="run-legacy")

    assert continued_projection["state"] == "linked"
    assert continued_projection["attempt_id"] != first_attempt
    assert continued_projection["session_id"] is None
    assert repository.get_run(OWNER, "run-legacy")["state"] == "waiting_task"


def test_bridge_transient_create_failure_releases_outbox_for_retry(tmp_path):
    repository = _repository(tmp_path)
    _, task_repository, real_bridge = _bridge(tmp_path, repository)

    class FlakyTasks:
        def __init__(self, delegate):
            self.delegate = delegate
            self.failed = False

        def create(self, request, actor):
            if not self.failed:
                self.failed = True
                raise ApplicationError("tasks_unavailable", "暂时不可用", 503)
            return self.delegate.create(request, actor)

        def get(self, task_id):
            return self.delegate.get(task_id)

        def list_files(self, task_id):
            return self.delegate.list_files(task_id)

    real_bridge.tasks = FlakyTasks(real_bridge.tasks)
    real_bridge.enqueue(OWNER, "run-legacy", REQUEST)
    first = real_bridge.process_once(OWNER, run_id="run-legacy")
    assert first["state"] == "pending"
    assert first["task_id"] is None

    second = real_bridge.process_once(OWNER, run_id="run-legacy")
    assert second["state"] == "linked"
    assert len(task_repository.list_tasks()) == 1


def test_bridge_rejects_oversized_instruction_without_creating_outbox(tmp_path):
    _repository_obj, _task_repository, bridge = _bridge(tmp_path)
    with pytest.raises(ApplicationError) as error:
        bridge.enqueue(OWNER, "run-legacy", dict(REQUEST, instruction="x" * 2001))
    assert error.value.code == "invalid_instruction"
    assert bridge.repository.get_legacy_task_link(OWNER, "run-legacy") is None
