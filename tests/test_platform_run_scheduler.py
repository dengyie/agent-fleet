from concurrent.futures import ThreadPoolExecutor
import threading

from hub.application.run_scheduler_service import RunSchedulerService
from hub.application.run_worker_service import LocalRunWorkerService
from hub.application.run_event_service import RunEventService
from hub.infrastructure.platform_db import PlatformRepository
from tools.platform.providers.base import ModelResponse


OWNER_A = "owner-a@example.test"
OWNER_B = "owner-b@example.test"


def _queued(repo, owner, run_id, workspace_id, root):
    repo.upsert_workspace(owner, {
        "workspace_id": workspace_id, "root_path": str(root),
    })
    repo.upsert_model(owner, {
        "profile_id": "local", "provider": "deterministic", "model": "local",
    })
    conversation = repo.create_conversation(
        owner, f"conv-{run_id}", title="", workspace_id=workspace_id)
    return repo.append_turn(
        owner, conversation["conversation_id"], f"msg-{run_id}", run_id,
        text="hello", client_token=f"token-{run_id}",
        config_snapshot={"workspace_id": workspace_id, "model_profile_id": "local"},
        now=1,
    )["run"]


def _scheduler(repo, *, scheduler_id="scheduler-a", max_concurrency=1,
               max_workspace_concurrency=1, provider_factory=None):
    events = RunEventService(repo, clock=lambda: 2.0)
    worker = LocalRunWorkerService(
        repo, events, worker_id=f"worker-{scheduler_id}", lease_s=60,
        clock=lambda: 2.0, provider_factory=provider_factory,
    )
    return RunSchedulerService(
        repo, worker, scheduler_id=scheduler_id,
        max_concurrency=max_concurrency,
        max_workspace_concurrency=max_workspace_concurrency,
        owner_lease_s=60, clock=lambda: 2.0,
    )


def test_durable_owner_lease_is_atomic_fair_and_reclaimable(tmp_path):
    repo = PlatformRepository(tmp_path / "platform.db")
    repo.init()
    _queued(repo, OWNER_A, "run-a", "home", tmp_path / "a")
    _queued(repo, OWNER_B, "run-b", "home", tmp_path / "b")

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda worker_id: repo.claim_worker_owner(
            worker_id=worker_id, now=2, lease_s=5), ("scheduler-a", "scheduler-b")))
    assert {claim["owner_id"] for claim in claims if claim} == {OWNER_A, OWNER_B}
    assert repo.claim_worker_owner(worker_id="scheduler-c", now=3, lease_s=5) is None
    assert repo.claim_worker_owner(worker_id="scheduler-c", now=8, lease_s=5) is not None


def test_scheduler_dispatches_multiple_owners_and_persists_results(tmp_path):
    repo = PlatformRepository(tmp_path / "platform.db")
    repo.init()
    _queued(repo, OWNER_A, "run-a", "home", tmp_path / "a")
    _queued(repo, OWNER_B, "run-b", "home", tmp_path / "b")

    scheduler = _scheduler(repo, max_concurrency=2, max_workspace_concurrency=1)
    results = scheduler.run_once(max_ticks=2)

    assert {result["state"] for result in results} == {"succeeded"}
    assert {repo.get_run(owner, run_id)["state"]
            for owner, run_id in ((OWNER_A, "run-a"), (OWNER_B, "run-b"))} == {"succeeded"}


def test_durable_slots_enforce_global_and_workspace_limits_and_release(tmp_path):
    repo = PlatformRepository(tmp_path / "platform.db")
    repo.init()
    _queued(repo, OWNER_A, "run-a1", "home", tmp_path / "a")
    _queued(repo, OWNER_A, "run-a2", "home", tmp_path / "a")
    _queued(repo, OWNER_B, "run-b1", "other", tmp_path / "b")

    slot = repo.claim_worker_slot(
        OWNER_A, "run-a1", worker_id="scheduler-a", workspace_key="owner-a:home",
        max_concurrency=1, max_workspace_concurrency=1, now=2, lease_s=5)
    assert slot is not None
    assert repo.claim_worker_slot(
        OWNER_B, "run-b1", worker_id="scheduler-b", workspace_key="owner-b:other",
        max_concurrency=1, max_workspace_concurrency=1, now=2, lease_s=5) is None
    assert repo.claim_worker_slot(
        OWNER_A, "run-a2", worker_id="scheduler-b", workspace_key="owner-a:home",
        max_concurrency=2, max_workspace_concurrency=1, now=2, lease_s=5) is None

    repo.release_worker_slot(slot["lease_id"], worker_id="scheduler-a")
    assert repo.claim_worker_slot(
        OWNER_A, "run-a2", worker_id="scheduler-b", workspace_key="owner-a:home",
        max_concurrency=1, max_workspace_concurrency=1, now=3, lease_s=5) is not None


def test_scheduler_requeues_when_slot_is_full_without_counting_attempt(tmp_path):
    repo = PlatformRepository(tmp_path / "platform.db")
    repo.init()
    queued = _queued(repo, OWNER_A, "run-a", "home", tmp_path / "a")
    blocker = repo.claim_worker_slot(
        OWNER_B, "run-b", worker_id="scheduler-other", workspace_key="other:home",
        max_concurrency=1, max_workspace_concurrency=1, now=2, lease_s=5)
    scheduler = _scheduler(repo, max_concurrency=1)

    result = scheduler.tick_once()

    assert result["state"] == "concurrency_deferred"
    current = repo.get_run(OWNER_A, queued["run_id"])
    assert current["state"] == "queued"
    assert current["attempt"] == 0
    assert repo.release_worker_slot(blocker["lease_id"], worker_id="scheduler-other")


def test_scheduler_parallel_slots_count_across_process_instances(tmp_path):
    repo_a = PlatformRepository(tmp_path / "platform.db")
    repo_a.init()
    _queued(repo_a, OWNER_A, "run-a", "home", tmp_path / "a")
    _queued(repo_a, OWNER_B, "run-b", "home", tmp_path / "b")
    entered = threading.Event()
    release = threading.Event()

    class BlockingProvider:
        def complete(self, messages, tools):
            entered.set()
            release.wait(2)
            return ModelResponse(kind="final", text="done")

    scheduler_a = _scheduler(
        repo_a, scheduler_id="scheduler-a", max_concurrency=1,
        provider_factory=lambda profile: BlockingProvider())
    results_a = []
    thread = threading.Thread(target=lambda: results_a.extend(scheduler_a.run_once(max_ticks=1)))
    thread.start()
    assert entered.wait(2)

    repo_b = PlatformRepository(tmp_path / "platform.db")
    repo_b.init()
    scheduler_b = _scheduler(repo_b, scheduler_id="scheduler-b", max_concurrency=1)
    result_b = scheduler_b.tick_once()
    assert result_b["state"] == "concurrency_deferred"

    release.set()
    thread.join(3)
    assert not thread.is_alive()
    assert results_a[0]["state"] == "succeeded"
    assert repo_b.get_run(OWNER_B, "run-b")["attempt"] == 0
