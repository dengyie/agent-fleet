"""Runner source capture, immutable private cache and bounded retry lifecycle."""
from __future__ import annotations

import hashlib
import json
import stat
import threading
import time
import sys
import io
import tarfile
import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from tools import runner_source
from tools.runner_config import RunnerConfig
from test_runner import load_agent_runner
from test_workflow_source import repo_with_commit, git


class SourceTransport:
    def __init__(self, statuses: list[int]) -> None:
        self.statuses = iter(statuses)
        self.requests: list[tuple[bytes, dict[str, str], dict[str, Any]]] = []
        self.json_requests: list[tuple[str, dict[str, Any]]] = []
        self.events: list[str] = []
        self.heartbeat_status = 200

    def post_bytes(self, url: str, body: bytes, headers: dict[str, str], **options: Any) -> tuple[int, dict[str, Any]]:
        self.requests.append((body, headers, options))
        self.events.append("source")
        status = next(self.statuses)
        return status, ({"ok": True, "artifact_id": "source-attempt-" + url.split("/")[-2],
                         "sha256": hashlib.sha256(body).hexdigest(), "size": len(body)} if status == 200 else {"error": "fixture_unavailable"})

    def post_json(self, url: str, body: dict[str, Any], headers: dict[str, str], **options: Any) -> tuple[int, dict[str, Any]]:
        self.json_requests.append((url, body))
        self.events.append(url.rsplit("/", 1)[-1])
        return (self.heartbeat_status if url.endswith("/heartbeat") else 200), {"ok": True}


def fixture(tmp_path: Path, statuses: list[int]) -> tuple[Any, RunnerConfig, dict[str, Any], Path, str, SourceTransport]:
    runner = load_agent_runner()
    repo, base, _, _ = repo_with_commit(tmp_path)
    transport = SourceTransport(statuses)
    cfg = RunnerConfig(hub="https://hub.invalid", machine="mac-local", credential="test-secret", runner_id="runner",
                       projects={"agent-fleet": repo}, agents={"codex": {"timeout_s": 30}}, cache_dir=tmp_path / "cache", transport=transport)
    task = {"task_id": "task-a", "attempt_id": "attempt-a", "nonce": "nonce-a", "project": "agent-fleet",
            "agent_type": "codex", "source_repository_id": "repo-a", "source_capture": True}
    return runner, cfg, task, repo, base, transport


def test_runner_captures_real_source_into_private_durable_cache(tmp_path: Path) -> None:
    runner, cfg, task, repo, base, transport = fixture(tmp_path, [200])
    (repo / "src.txt").write_text("CLI output that was not committed\n", encoding="utf-8")
    original_head, original_index = git(repo, "rev-parse", "HEAD"), git(repo, "ls-files", "--stage")
    result = runner._source_archive(cfg, task, repo, base)
    assert result["sha256"] == hashlib.sha256(transport.requests[0][0]).hexdigest()
    archives = tuple(cfg.cache_dir.rglob("source.tar"))
    assert len(archives) == 1 and archives[0].read_bytes() == transport.requests[0][0]
    assert stat.S_IMODE(archives[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(archives[0].parent.stat().st_mode) == 0o700
    assert git(repo, "rev-parse", "HEAD") == original_head and git(repo, "ls-files", "--stage") == original_index
    assert b"test-secret" not in (archives[0].parent.parent / "attempt.json").read_bytes()


def test_transient_source_upload_reuses_the_same_packet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, cfg, task, repo, base, transport = fixture(tmp_path, [503, 200])
    monkeypatch.setattr(runner_source, "SOURCE_RETRY_DELAYS", (0, 0), raising=False)
    runner._source_archive(cfg, task, repo, base)
    assert len(transport.requests) == 2
    assert transport.requests[0][0:2] == transport.requests[1][0:2]


def test_failed_upload_can_recover_original_attempt_without_recapturing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, cfg, task, repo, base, _ = fixture(tmp_path, [503, 503, 503])
    monkeypatch.setattr(runner_source, "SOURCE_RETRY_DELAYS", (0, 0), raising=False)
    with pytest.raises(RuntimeError):
        runner._source_archive(cfg, task, repo, base)
    before = tuple(cfg.cache_dir.rglob("source.tar"))
    assert len(before) == 1
    frozen = before[0].read_bytes()
    monkeypatch.setattr(runner.worktree, "freeze_source_commit", lambda *args, **kwargs: pytest.fail("must recover the fixed archive"))
    cfg.transport = SourceTransport([200])
    runner._source_archive(cfg, task, repo, base)
    assert cfg.transport.requests[0][0] == frozen


def test_loss_of_lease_stops_source_retries_and_keeps_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, cfg, task, repo, base, transport = fixture(tmp_path, [503])
    lost = threading.Event()
    original = transport.post_bytes
    def lose_lease(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        lost.set()
        return result
    monkeypatch.setattr(transport, "post_bytes", lose_lease)
    with pytest.raises(RuntimeError):
        runner._source_archive(cfg, task, repo, base, lease_lost=lost, deadline=time.monotonic() + 10)
    assert len(transport.requests) == 1
    assert len(tuple(cfg.cache_dir.rglob("source.tar"))) == 1


def test_another_attempt_or_nonce_cannot_reuse_cached_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, cfg, task, repo, base, _ = fixture(tmp_path, [200, 200, 200])
    runner._source_archive(cfg, task, repo, base)
    original = runner.worktree.freeze_source_commit
    calls = []
    def capture(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs["task_id"])
        return original(*args, **kwargs)
    monkeypatch.setattr(runner.worktree, "freeze_source_commit", capture)
    runner._source_archive(cfg, {**task, "attempt_id": "attempt-b"}, repo, base)
    runner._source_archive(cfg, {**task, "nonce": "nonce-b"}, repo, base)
    assert len(calls) == 2 and len(tuple(cfg.cache_dir.rglob("source.tar"))) == 3


def test_expired_source_lifecycle_never_uploads(tmp_path: Path) -> None:
    runner, cfg, task, repo, base, transport = fixture(tmp_path, [])
    with pytest.raises(RuntimeError):
        runner._source_archive(cfg, task, repo, base, deadline=time.monotonic() - 1)
    assert transport.requests == []


@pytest.mark.parametrize("managed", [False, True])
def test_real_cli_result_waits_for_source_and_recovers_without_cli_reexecution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                            managed: bool) -> None:
    runner, cfg, task, repo, _, transport = fixture(tmp_path, [503, 503, 503])
    monkeypatch.setattr(runner_source, "SOURCE_RETRY_DELAYS", (0, 0))
    script = "from pathlib import Path; Path('generated.txt').write_text('fixed CLI output'); print('done')"
    cfg.agents["codex"]["command"] = [sys.executable, "-c", script]
    task["instruction"] = "produce a fixture file"
    if managed:
        supervisor = runner._bf_supervisor().Supervisor(manifest_dir=tmp_path / "manifests", machine_id=cfg.machine)
        execute = lambda: runner.run_task_managed(cfg, task, supervisor)
    else:
        execute = lambda: runner.run_task(cfg, task)
    outcome = execute()
    assert outcome["source_pending"] is True
    assert not any(url.endswith("/result") for url, _ in transport.json_requests)
    with tarfile.open(fileobj=io.BytesIO(transport.requests[0][0])) as archive:
        assert archive.extractfile("generated.txt").read() == b"fixed CLI output"
    pending = cfg.cache_dir / "pending" / "attempt-a.json"
    assert json.loads(pending.read_bytes())["exit_code"] == 0
    monkeypatch.setattr(runner.worktree, "create_worktree", lambda *args: pytest.fail("cannot rerun the CLI"))
    cfg.transport = recovered = SourceTransport([200])
    outcome = execute()
    assert outcome == {"exit_code": 0, "source_pending": False}
    assert recovered.requests[0][0] == transport.requests[0][0]
    assert recovered.events.index("source") < recovered.events.index("result")
    assert not pending.exists()
    assert not (repo.parent / f"{repo.name}.fleet-worktrees" / task["task_id"]).exists()


def test_pending_replay_checks_original_lease_before_upload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, cfg, task, repo, base, _ = fixture(tmp_path, [200])
    runner._source_archive(cfg, task, repo, base)
    from tools.runner_source import SourceCache, atomic_private_json, attempt_identity

    identity = attempt_identity(cfg, task)
    cache = SourceCache(cfg.cache_dir, identity)
    body = {"attempt_id": task["attempt_id"], "nonce": task["nonce"], "exit_code": 0}
    cache.save_result(body)
    pending = cfg.cache_dir / "pending" / "attempt-a.json"
    atomic_private_json(pending, {**body, "source_upload": identity})
    cfg.transport = denied = SourceTransport([])
    denied.heartbeat_status = 409
    runner.flush_pending(cfg)
    assert denied.requests == [] and pending.exists()
    assert not any(url.endswith("/result") for url, _ in denied.json_requests)
    cfg.transport = accepted = SourceTransport([200])
    runner.flush_pending(cfg)
    assert not pending.exists() and accepted.events.index("source") < accepted.events.index("result")


def test_lost_result_response_replays_only_original_result_after_source_confirmation(tmp_path: Path,
                                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    runner, cfg, task, repo, base, transport = fixture(tmp_path, [200])
    runner._source_archive(cfg, task, repo, base)
    original = transport.post_json
    def lost_response(url: str, *args: Any, **kwargs: Any) -> Any:
        status, value = original(url, *args, **kwargs)
        return (0, {"error": "fixture_lost_response"}) if url.endswith("/result") else (status, value)
    monkeypatch.setattr(transport, "post_json", lost_response)
    body = {"attempt_id": task["attempt_id"], "nonce": task["nonce"], "exit_code": 0}
    cfg.transport.statuses = iter([200])
    assert runner._publish_source_result(cfg, task, repo, base, body, lease_lost=threading.Event(),
                                         deadline=time.monotonic() + 10) == 0
    cfg.transport = recovered = SourceTransport([])
    recovered.heartbeat_status = 409
    runner.flush_pending(cfg)
    assert recovered.requests == []
    assert [value for url, value in recovered.json_requests if url.endswith("/result")] == [body]
    assert not (cfg.cache_dir / "pending" / "attempt-a.json").exists()


@pytest.mark.parametrize("failure", ["expired", "rejected", "corrupt"])
def test_permanent_pending_source_failure_is_retained_for_reconciliation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                        failure: str) -> None:
    runner, cfg, task, repo, base, _ = fixture(tmp_path, [200])
    runner._source_archive(cfg, task, repo, base)
    cache = runner_source.SourceCache(cfg.cache_dir, runner_source.attempt_identity(cfg, task))
    body = {"attempt_id": task["attempt_id"], "nonce": task["nonce"], "exit_code": 0}
    cache.save_result(body)
    pending = cfg.cache_dir / "pending" / "attempt-a.json"
    runner_source.atomic_private_json(pending, {**body, "source_upload": cache.identity})
    original = pending.read_bytes()
    if failure == "expired":
        monkeypatch.setattr(runner_source.SourceCache, "remaining", lambda *args: -1)
    elif failure == "corrupt":
        cache.archive().write_bytes(b"tampered")
    cfg.transport = transport = SourceTransport([409])
    runner.flush_pending(cfg)
    assert not pending.exists()
    retained = tuple((cfg.cache_dir / "reconciliation").glob("*/attempt-a.json"))
    assert len(retained) == 1 and retained[0].read_bytes() == original
    reason = json.loads((retained[0].parent / "reason.json").read_bytes())
    assert reason["error_code"] == {"expired": "source_deadline_exceeded", "rejected": "source_upload_rejected_409",
                                   "corrupt": "source_cache_digest_mismatch"}[failure]
    calls = (len(transport.requests), len(transport.json_requests))
    runner.flush_pending(cfg)
    assert calls == (len(transport.requests), len(transport.json_requests))
    assert not any(url.endswith("/result") for url, _ in transport.json_requests)


def test_crashed_cli_attempt_is_never_automatically_executed_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, cfg, task, _, _, transport = fixture(tmp_path, [])
    from tools.runner_source import SourceCache, attempt_identity

    SourceCache(cfg.cache_dir, attempt_identity(cfg, task)).begin(time.time() + 30)
    monkeypatch.setattr(runner.worktree, "create_worktree", lambda *args: pytest.fail("attempt has already started"))
    assert runner.run_task(cfg, task)["error"] == "source_capture_unconfirmed"
    assert transport.requests == [] and transport.json_requests == []


@pytest.mark.parametrize("change", ["content", "symlink", "permissions", "receipt"])
def test_cache_or_receipt_corruption_cannot_publish_success(tmp_path: Path, change: str) -> None:
    runner, cfg, task, repo, base, _ = fixture(tmp_path, [200])
    runner._source_archive(cfg, task, repo, base)
    archive = next(cfg.cache_dir.rglob("source.tar"))
    cfg.transport = transport = SourceTransport([200])
    if change == "content":
        archive.write_bytes(b"tampered")
    elif change == "symlink":
        archive.rename(archive.with_suffix(".original"))
        archive.symlink_to(archive.with_suffix(".original"))
    elif change == "permissions":
        archive.chmod(0o644)
    else:
        original = transport.post_bytes
        def false_receipt(*args: Any, **kwargs: Any) -> Any:
            status, value = original(*args, **kwargs)
            return status, {**value, "sha256": "0" * 64}
        transport.post_bytes = false_receipt
    with pytest.raises(RuntimeError):
        runner._source_archive(cfg, task, repo, base)
    assert len(transport.requests) == (1 if change == "receipt" else 0)


@pytest.mark.parametrize("stop", ["lease", "deadline"])
def test_source_git_stops_and_reaps_a_real_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: str) -> None:
    from tools.worktree import WorktreeError, _source_git

    original = asyncio.create_subprocess_exec
    children = []
    lost = threading.Event()
    async def scenario() -> None:
        spawned = asyncio.Event()
        async def launch(*args: Any, **kwargs: Any) -> Any:
            process = await original(sys.executable, "-c", "import time; time.sleep(30)", **kwargs)
            children.append(process)
            spawned.set()
            return process
        monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
        operation = asyncio.create_task(_source_git(["git", "status"], tmp_path, {}, None,
            time.monotonic() + (.2 if stop == "deadline" else 5), lost.is_set))
        await spawned.wait()
        if stop == "lease":
            lost.set()
        with pytest.raises(WorktreeError, match="source_capture_stopped"):
            await operation
        assert children[0].returncode is not None
    asyncio.run(scenario())


def test_capture_cannot_follow_a_parent_swapped_during_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, cfg, task, repo, base, transport = fixture(tmp_path, [200])
    folder = repo / "directory"
    folder.mkdir()
    (folder / "data.txt").write_text("approved source", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data.txt").write_text("private host data", encoding="utf-8")
    original_open = os.open
    swapped = False
    def race(path: Any, flags: int, *args: Any, **kwargs: Any) -> Any:
        nonlocal swapped
        if str(path).endswith("data.txt") and not swapped:
            swapped = True
            folder.rename(repo / "original-directory")
            folder.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", race)
    runner._source_archive(cfg, task, repo, base)
    assert swapped
    with tarfile.open(fileobj=io.BytesIO(transport.requests[0][0])) as archive:
        assert archive.extractfile("directory/data.txt").read() == b"approved source"


def test_capture_preserves_cli_directory_deletions(tmp_path: Path) -> None:
    runner, cfg, task, repo, _, transport = fixture(tmp_path, [200])
    folder = repo / "deleted-directory"
    folder.mkdir()
    (folder / "old.txt").write_text("tracked before the CLI", encoding="utf-8")
    git(repo, "add", "deleted-directory")
    git(repo, "commit", "-qm", "directory before task")
    base = git(repo, "rev-parse", "HEAD")
    folder.rename(tmp_path / "removed-directory")
    runner._source_archive(cfg, task, repo, base)
    with tarfile.open(fileobj=io.BytesIO(transport.requests[0][0])) as archive:
        assert "deleted-directory/old.txt" not in archive.getnames()


@pytest.mark.parametrize("managed", [False, True])
def test_live_source_heartbeat_uses_the_original_transport_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                  managed: bool) -> None:
    runner, cfg, task, _, _, transport = fixture(tmp_path, [200])
    cfg.heartbeat_interval_s = .01
    cfg.agents["codex"]["command"] = [sys.executable, "-c", "import time; time.sleep(.2)"]
    task["instruction"] = "wait for a heartbeat"
    options_seen = []
    original = transport.post_json

    def record(url: str, *args: Any, **options: Any) -> Any:
        if url.endswith("/heartbeat"):
            options_seen.append(options)
        return original(url, *args, **options)

    monkeypatch.setattr(transport, "post_json", record)
    if managed:
        supervisor = runner._bf_supervisor().Supervisor(manifest_dir=tmp_path / "manifests", machine_id=cfg.machine)
        outcome = runner.run_task_managed(cfg, task, supervisor)
    else:
        outcome = runner.run_task(cfg, task)
    assert outcome == {"exit_code": 0, "source_pending": False}
    assert options_seen
    assert all("deadline" in options and callable(options.get("should_cancel")) for options in options_seen)
    assert len({options["deadline"] for options in options_seen}) == 1


@pytest.mark.parametrize("managed", [False, True])
def test_local_lease_expiry_cancels_source_capture_after_cli_completion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                      managed: bool) -> None:
    runner, cfg, task, _, _, transport = fixture(tmp_path, [])
    cfg.heartbeat_interval_s = .01
    cfg.agents["codex"]["command"] = [sys.executable, "-c", "print('completed')"]
    task["instruction"] = "complete before source capture"
    expired = [False]
    stopped = []
    monkeypatch.setattr(runner._LeaseClock, "expired", lambda self: expired[0])
    transport.heartbeat_status = 503

    def capture(*args: Any, lease_lost: threading.Event, **kwargs: Any) -> Any:
        expired[0] = True
        stopped.append(lease_lost.wait(.5))
        raise runner_source.SourceUploadError("source_lease_lost")

    monkeypatch.setattr(runner, "_source_archive", capture)
    if managed:
        supervisor = runner._bf_supervisor().Supervisor(manifest_dir=tmp_path / "manifests", machine_id=cfg.machine)
        outcome = runner.run_task_managed(cfg, task, supervisor)
    else:
        outcome = runner.run_task(cfg, task)
    assert outcome["source_pending"] is True
    assert stopped == [True]
    assert not any(url.endswith("/result") for url, _ in transport.json_requests)
