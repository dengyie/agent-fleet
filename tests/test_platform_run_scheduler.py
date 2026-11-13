import threading
from concurrent.futures import ThreadPoolExecutor

from hub.application.run_event_service import RunEventService
from hub.application.run_scheduler_service import RunSchedulerService
from hub.application.run_worker_service import LocalRunWorkerService
from hub.infrastructure.platform_db import PlatformRepository
from hub.platform.providers.base import ModelResponse

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
        def complete(self, messages, tools, *, request_observer=None):
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


def test_scheduler_preserves_worker_result_when_slot_release_fails(caplog):
    """A cleanup failure must not rewrite a completed worker outcome."""
    from types import SimpleNamespace

    class Repository:
        def renew_worker_slot(self, *args, **kwargs):
            return True

        def renew_run_lease(self, *args, **kwargs):
            return True

        def release_worker_slot(self, *args, **kwargs):
            raise OSError("slot store unavailable")

    scheduler = RunSchedulerService(
        Repository(),
        SimpleNamespace(
            lease_s=60,
            worker_id="worker-a",
            configure_workspace_concurrency=lambda capacity: None,
            execute_claim=lambda claim: {
                "run_id": claim["run_id"], "state": "succeeded"
            },
        ),
        scheduler_id="scheduler-a",
    )

    result = scheduler._execute(
        {"owner_id": OWNER_A, "run_id": "run-a", "lease_id": "run-lease", "attempt": 1},
        {},
        {"lease_id": "slot-lease"},
    )

    assert result == {"run_id": "run-a", "state": "succeeded"}
    assert "platform_slot_release_failed" in caplog.text


def test_background_scheduler_refills_free_slot_while_slow_run_continues(tmp_path):
    repo = PlatformRepository(tmp_path / "platform.db")
    repo.init()
    for run_id, workspace_id in (("a-slow", "slow"), ("b-fast", "fast"),
                                 ("c-next", "next")):
        _queued(repo, OWNER_A, run_id, workspace_id, tmp_path / workspace_id)
    slow_entered = threading.Event()
    next_entered = threading.Event()
    release = threading.Event()

    class Provider:
        def complete(self, messages, tools, *, request_observer=None):
            run_id = messages[-1]["message_id"].removeprefix("msg-")
            if run_id == "a-slow":
                slow_entered.set()
                if not release.wait(5):
                    raise TimeoutError("test did not release the slow provider")
            elif run_id == "c-next":
                next_entered.set()
            return ModelResponse(kind="final", text="done")

    scheduler = _scheduler(repo, max_concurrency=2,
                           provider_factory=lambda profile: Provider())
    scheduler.interval_s = 0.1
    shutdown = scheduler.start()
    try:
        assert slow_entered.wait(2)
        assert next_entered.wait(2), "free slot was not refilled during slow execution"
        assert repo.get_run(OWNER_A, "a-slow")["state"] == "running"
        assert repo.get_run(OWNER_A, "b-fast")["state"] == "succeeded"
    finally:
        release.set()
        shutdown()


def test_scheduler_skips_busy_workspace_without_claiming_its_run(tmp_path):
    repo = PlatformRepository(tmp_path / "platform.db")
    repo.init()
    _queued(repo, OWNER_A, "a-busy", "busy", tmp_path / "busy")
    _queued(repo, OWNER_A, "b-free", "free", tmp_path / "free")
    repo.claim_worker_slot(
        OWNER_A, "blocker", worker_id="other-scheduler",
        workspace_key=f"{OWNER_A}:busy", max_concurrency=2,
        max_workspace_concurrency=1, now=2, lease_s=60,
    )

    result = _scheduler(repo, max_concurrency=2).tick_once()

    assert result["run_id"] == "b-free"
    assert result["state"] == "succeeded"
    assert repo.get_run(OWNER_A, "a-busy")["state"] == "queued"
    assert repo.get_run(OWNER_A, "a-busy")["attempt"] == 0


def test_scheduler_skips_owner_with_only_busy_workspaces(tmp_path):
    repo = PlatformRepository(tmp_path / "platform.db")
    repo.init()
    _queued(repo, OWNER_A, "a-busy", "busy", tmp_path / "busy")
    _queued(repo, OWNER_B, "b-free", "free", tmp_path / "free")
    repo.claim_worker_slot(
        OWNER_A, "blocker", worker_id="other-scheduler",
        workspace_key=f"{OWNER_A}:busy", max_concurrency=2,
        max_workspace_concurrency=1, now=2, lease_s=60,
    )

    result = _scheduler(repo, max_concurrency=2).tick_once()

    assert result["run_id"] == "b-free"
    assert result["state"] == "succeeded"
    assert repo.get_run(OWNER_A, "a-busy")["attempt"] == 0


def test_capacity_filter_keeps_interrupted_run_recovery_eligible(tmp_path):
    repo = PlatformRepository(tmp_path / "platform.db")
    repo.init()
    _queued(repo, OWNER_A, "a-interrupted", "busy", tmp_path / "busy")
    claim = repo.claim_run(OWNER_A, worker_id="dead-worker", now=2, lease_s=5)
    repo.append_run_event(
        OWNER_A, "a-interrupted", "run_started", {}, now=2,
        lease_id=claim["lease_id"], worker_id="dead-worker",
    )
    repo.claim_worker_slot(
        OWNER_A, "other-run", worker_id="other-scheduler",
        workspace_key=f"{OWNER_A}:busy", max_concurrency=2,
        max_workspace_concurrency=1, now=10, lease_s=60,
    )
    scheduler = _scheduler(repo, max_concurrency=2)
    scheduler.clock = lambda: 10

    result = scheduler.tick_once()

    assert result["run_id"] == "a-interrupted"
    assert result["state"] == "unknown"
    assert repo.get_run(OWNER_A, "a-interrupted")["attempt"] == 1


def test_capacity_filter_uses_conversation_workspace_when_snapshot_has_none(tmp_path):
    repo = PlatformRepository(tmp_path / "platform.db")
    repo.init()
    _queued(repo, OWNER_A, "a-busy", "busy", tmp_path / "busy")
    _queued(repo, OWNER_A, "b-free", "free", tmp_path / "free")
    with repo._connect() as conn:
        conn.execute("UPDATE runs SET config_snapshot='{}' WHERE run_id='a-busy'")
    repo.claim_worker_slot(
        OWNER_A, "other-run", worker_id="other-scheduler",
        workspace_key=f"{OWNER_A}:busy", max_concurrency=2,
        max_workspace_concurrency=1, now=2, lease_s=60,
    )

    assert _scheduler(repo, max_concurrency=2).tick_once()["run_id"] == "b-free"
    assert repo.get_run(OWNER_A, "a-busy")["attempt"] == 0


def test_background_stop_drains_admitted_work_without_claiming_more(tmp_path):
    repo = PlatformRepository(tmp_path / "platform.db")
    repo.init()
    _queued(repo, OWNER_A, "a-slow", "slow", tmp_path / "slow")
    _queued(repo, OWNER_A, "b-next", "next", tmp_path / "next")
    entered = threading.Event()
    release = threading.Event()
    stopped = threading.Event()
    shutdown_started = threading.Event()

    class Provider:
        def complete(self, messages, tools, *, request_observer=None):
            entered.set()
            assert release.wait(5)
            return ModelResponse(kind="final", text="done")

    scheduler = _scheduler(repo, provider_factory=lambda profile: Provider())
    scheduler.interval_s = 0.1
    shutdown = scheduler.start(stop_event=stopped)

    def close():
        stopped.set()
        shutdown_started.set()
        shutdown()

    thread = threading.Thread(target=close)
    try:
        assert entered.wait(2)
        thread.start()
        assert shutdown_started.wait(2)
        assert repo.get_run(OWNER_A, "b-next")["attempt"] == 0
    finally:
        release.set()
        if thread.ident is not None:
            thread.join(3)
        else:
            shutdown()
    assert not thread.is_alive()
    assert repo.get_run(OWNER_A, "a-slow")["state"] == "succeeded"
    assert repo.get_run(OWNER_A, "b-next")["state"] == "queued"


def test_background_worker_exception_releases_slot_and_dispatches_next(tmp_path):
    repo = PlatformRepository(tmp_path / "platform.db")
    repo.init()
    for run_id, workspace_id in (("a-error", "error"), ("b-next", "next")):
        _queued(repo, OWNER_A, run_id, workspace_id, tmp_path / workspace_id)
    entered = threading.Event()
    scheduler = _scheduler(repo)
    scheduler.interval_s = 0.1
    original = scheduler.worker.execute_claim

    def execute(claim):
        if claim["run_id"] == "a-error":
            raise RuntimeError("controlled worker failure")
        result = original(claim)
        entered.set()
        return result

    scheduler.worker.execute_claim = execute
    shutdown = scheduler.start()
    try:
        assert entered.wait(2)
        assert repo.get_run(OWNER_A, "b-next")["state"] == "succeeded"
    finally:
        shutdown()


def test_configured_same_workspace_parallel_runs_both_reach_provider(tmp_path):
    repo = PlatformRepository(tmp_path / "platform.db")
    repo.init()
    _queued(repo, OWNER_A, "a-shared", "shared", tmp_path / "shared")
    _queued(repo, OWNER_A, "b-shared", "shared", tmp_path / "shared")
    entered = {run: threading.Event() for run in ("a-shared", "b-shared")}
    release = threading.Event()

    class Provider:
        def complete(self, messages, tools, *, request_observer=None):
            run_id = messages[-1]["message_id"].removeprefix("msg-")
            entered[run_id].set()
            assert release.wait(5)
            return ModelResponse(kind="final", text="done")

    scheduler = _scheduler(repo, max_concurrency=2, max_workspace_concurrency=2,
                           provider_factory=lambda profile: Provider())
    scheduler.interval_s = 0.1
    shutdown = scheduler.start()
    try:
        assert entered["a-shared"].wait(2)
        assert entered["b-shared"].wait(2), "admitted Run failed at the exclusive process lease"
        assert repo.get_run(OWNER_A, "a-shared")["state"] == "running"
        assert repo.get_run(OWNER_A, "b-shared")["state"] == "running"
    finally:
        release.set()
        shutdown()
    assert repo.get_run(OWNER_A, "a-shared")["state"] == "succeeded"
    assert repo.get_run(OWNER_A, "b-shared")["state"] == "succeeded"
