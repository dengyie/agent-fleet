"""Explicit immutable summary authority over actual SQLite and operator HTTP."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import multiprocessing
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from hub.domain.workflow_contract_types import ContextReference
from hub.domain.workflow_identity import canonical_json
from hub.infrastructure.platform_backup import BackupError, PlatformBackupService
from hub.infrastructure.workflow_repository import WorkflowRepository, WorkflowRepositoryError
from test_workflow_repository import OWNER, WORKSPACE, prepare_database

CONTENT = "显式摘要 🌱\n保留完整第二行与末尾空白。  \n"


def store_for(path: Path) -> Any:
    from hub.infrastructure.workflow_summary_repository import WorkflowSummaryRepository

    return WorkflowSummaryRepository(prepare_database(path))


def publish(store: Any, *, summary_id: str = "summary-a", content: str = CONTENT,
            token: str = "publish-a", epoch: int = 0, now: float = 100) -> Any:
    return store.publish(OWNER, WORKSPACE, summary_id, title="人工摘要", content=content,
        client_token=token, expected_epoch=epoch, now=now)


def reference(item: Any) -> ContextReference:
    return ContextReference("summary", item.summary_id, item.revision, item.sha256)


def test_summary_publish_is_immutable_canonical_scoped_and_reconstructable(tmp_path: Path) -> None:
    from hub.infrastructure.workflow_summary_repository import WorkflowSummaryRepository

    store = store_for(tmp_path / "platform.db")
    original = publish(store)
    newer = publish(store, token="publish-b", epoch=1, content="第二版", now=101)
    assert original.revision == 1 and newer.revision == 2
    assert original.content == CONTENT and original.created_by == OWNER and original.created_at_ms == 100000
    assert original.source_kind == "operator"
    assert original.sha256 == hashlib.sha256(original.snapshot).hexdigest()
    assert canonical_json(json.loads(original.snapshot)) == original.snapshot
    assert CONTENT not in repr(original)
    recreated = WorkflowSummaryRepository(WorkflowRepository(store.workflows.db_path))
    recreated.workflows.init()
    assert recreated.get(OWNER, WORKSPACE, "summary-a", revision=1) == original
    assert recreated.read_selected(OWNER, WORKSPACE, (reference(original), reference(newer))) == (original, newer)
    assert recreated.head(OWNER, WORKSPACE, "summary-a")["epoch"] == 2
    with recreated.workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT minimum_reader_version FROM workflow_schema_meta").fetchone()[0] == 11


@pytest.mark.parametrize("media_kind,reader", [("image", 13), ("audio", 14), ("video", 15)])
def test_first_summary_preserves_existing_native_media_reader_and_recovery(tmp_path: Path, media_kind: str, reader: int) -> None:
    import asyncio

    from hub.application.workflow_media_inputs import WorkflowMediaInputs
    from hub.infrastructure.workflow_run_executor import RunStepExecutor
    from hub.infrastructure.workflow_summary_repository import WorkflowSummaryRepository
    from test_workflow_native_audio_inputs import WAV
    from test_workflow_native_audio_inputs import _target as audio_target
    from test_workflow_native_media_inputs import PNG
    from test_workflow_native_media_inputs import _target as image_target
    from test_workflow_native_text_inputs import make_text
    from test_workflow_native_video_inputs import MP4, video_target

    content_type, raw, target_factory = {
        "image": ("image/png", PNG, image_target),
        "audio": ("audio/wav", WAV, audio_target),
        "video": ("video/mp4", MP4, video_target),
    }[media_kind]
    setup = make_text(tmp_path, raw=raw, content_type=content_type)
    setup.target = target_factory(setup)
    executor = RunStepExecutor(setup.workflows, setup.target, clock=setup.clock,
        media_inputs=WorkflowMediaInputs(setup.artifacts))
    binding = setup.dispatch(executor).binding
    store = WorkflowSummaryRepository(setup.workflows)
    first = publish(store)
    second = publish(store, token="second-after-media", epoch=1, content="second", now=101)
    assert (first.revision, second.revision) == (1, 2)
    with setup.workflows.transaction(write=False) as connection:
        assert connection.execute("SELECT minimum_reader_version FROM workflow_schema_meta").fetchone()[0] == reader
    setup.workflows.init()
    assert asyncio.run(executor.lookup(binding.token, setup.resolved.digest, setup.resolved)) == binding
    assert store.get(OWNER, WORKSPACE, "summary-a", revision=1) == first
    summary = PlatformBackupService().create_backup(setup.workflows.db_path, setup.artifacts.root, tmp_path / "backup")
    assert summary["workflow"]["minimum_reader_version"] == reader


def test_failed_first_summary_does_not_change_existing_higher_reader(tmp_path: Path, monkeypatch: Any) -> None:
    store = store_for(tmp_path / "platform.db")
    with store.workflows.transaction() as connection:
        connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=15")
    original = store.workflows.transaction

    @contextmanager
    def rejected(**kwargs: Any) -> Any:
        with original(**kwargs) as connection:
            connection.set_authorizer(lambda action, table, *rest: sqlite3.SQLITE_DENY
                if (action, table) == (sqlite3.SQLITE_INSERT, "workflow_summary_versions") else sqlite3.SQLITE_OK)
            yield connection

    monkeypatch.setattr(store.workflows, "transaction", rejected)
    with pytest.raises(WorkflowRepositoryError):
        publish(store)
    with original(write=False) as connection:
        assert connection.execute("SELECT minimum_reader_version FROM workflow_schema_meta").fetchone()[0] == 15
        assert connection.execute("SELECT name FROM sqlite_master WHERE name GLOB 'workflow_summary_*'").fetchall() == []


def test_summary_token_returns_original_version_after_append_and_disable(tmp_path: Path) -> None:
    store = store_for(tmp_path / "platform.db")
    original = publish(store)
    publish(store, token="publish-b", epoch=1, content="第二版", now=101)
    store.set_enabled(OWNER, WORKSPACE, "summary-a", enabled=False, expected_epoch=2)
    assert publish(store, now=200) == original
    with pytest.raises(WorkflowRepositoryError, match="idempotency_conflict"):
        publish(store, content="changed")
    with pytest.raises(WorkflowRepositoryError, match="idempotency_conflict"):
        publish(store, summary_id="other-summary")
    with pytest.raises(WorkflowRepositoryError, match="revision_conflict"):
        publish(store, epoch=1, token="stale")
    assert store.head(OWNER, WORKSPACE, "summary-a")["epoch"] == 3


@pytest.mark.parametrize("change", ["owner", "workspace", "hash", "revision", "disabled"])
def test_summary_selection_checks_exact_scope_version_hash_and_current_enabled(tmp_path: Path, change: str) -> None:
    store = store_for(tmp_path / "platform.db")
    item = publish(store)
    owner, workspace, ref = OWNER, WORKSPACE, reference(item)
    if change == "owner": owner = "other@example.test"
    if change == "workspace": workspace = "other-workspace"
    if change == "hash": ref = replace(ref, sha256="a" * 64)
    if change == "revision": ref = replace(ref, revision=2)
    if change == "disabled": store.set_enabled(OWNER, WORKSPACE, "summary-a", enabled=False, expected_epoch=1)
    with pytest.raises(ValueError, match="summary_selection_stale"):
        store.read_selected(owner, workspace, (ref,))
    assert store.get(OWNER, WORKSPACE, "summary-a", revision=1) == item


@pytest.mark.parametrize("field,value", [("revision", 0), ("revision", True), ("revision", 1.0),
    ("title", ""), ("title", "x" * 161), ("content", ""), ("content", "x" * 8193),
    ("content", "🌱" * 2050), ("content", "\ud800"), ("created_at_ms", True),
    ("created_by", "other@example.test"), ("source_kind", "model_verified")])
def test_summary_domain_rejects_bad_complete_values(tmp_path: Path, field: str, value: Any) -> None:
    item = publish(store_for(tmp_path / "platform.db"))
    with pytest.raises(ValueError):
        replace(item, **{field: value})


def test_summary_publication_rollback_includes_schema_reader_and_receipt(tmp_path: Path, monkeypatch: Any) -> None:
    store = store_for(tmp_path / "platform.db")
    original = store.workflows.transaction

    @contextmanager
    def rejected(**kwargs: Any) -> Any:
        with original(**kwargs) as connection:
            connection.set_authorizer(lambda action, table, *rest: sqlite3.SQLITE_DENY
                if (action, table) == (sqlite3.SQLITE_INSERT, "workflow_summary_versions") else sqlite3.SQLITE_OK)
            yield connection

    monkeypatch.setattr(store.workflows, "transaction", rejected)
    with pytest.raises(WorkflowRepositoryError): publish(store)
    with original(write=False) as connection:
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name GLOB 'workflow_summary_*'").fetchall()
        assert connection.execute("SELECT minimum_reader_version FROM workflow_schema_meta").fetchone()[0] == 1


def _publish_competitor(path: str, token: str, ready: Any, result: Any) -> None:
    from hub.infrastructure.workflow_summary_repository import WorkflowSummaryRepository

    store = WorkflowSummaryRepository(WorkflowRepository(Path(path)))
    ready.wait(10)
    try:
        item = publish(store, token=token, epoch=1, content=token, now=101)
        result.put(("published", item.revision))
    except WorkflowRepositoryError as exc:
        result.put((exc.code, None))


def test_summary_two_processes_cannot_append_the_same_epoch(tmp_path: Path) -> None:
    store = store_for(tmp_path / "platform.db")
    publish(store)
    context = multiprocessing.get_context("spawn")
    ready, result = context.Event(), context.Queue()
    children = [context.Process(target=_publish_competitor, args=(str(store.workflows.db_path), token, ready, result))
        for token in ("compete-a", "compete-b")]
    for child in children: child.start()
    ready.set()
    observed = [result.get(timeout=20) for _ in children]
    for child in children:
        child.join(10)
        assert child.exitcode == 0
    assert sorted(observed) == [("published", 2), ("revision_conflict", None)]
    assert store.head(OWNER, WORKSPACE, "summary-a")["epoch"] == 2


@pytest.mark.parametrize("encrypted", [False, True])
def test_summary_backup_keeps_all_versions_and_disabled_authority(tmp_path: Path, encrypted: bool) -> None:
    from hub.infrastructure.workflow_summary_repository import WorkflowSummaryRepository

    store = store_for(tmp_path / "platform.db")
    first = publish(store)
    second = publish(store, token="second", epoch=1, content="second", now=101)
    store.set_enabled(OWNER, WORKSPACE, "summary-a", enabled=False, expected_epoch=2)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    service, backup, restored = PlatformBackupService(), tmp_path / "backup", tmp_path / "restored"
    if encrypted:
        service.create_encrypted_backup(store.workflows.db_path, artifacts, backup, b"k" * 32, "test-key")
        service.restore_encrypted_backup(backup, restored, b"k" * 32)
    else:
        service.create_backup(store.workflows.db_path, artifacts, backup)
        service.restore_backup(backup, restored)
    recovered = WorkflowSummaryRepository(WorkflowRepository(restored / "platform.db"))
    recovered.workflows.init()
    assert recovered.get(OWNER, WORKSPACE, "summary-a", revision=1) == first
    assert recovered.get(OWNER, WORKSPACE, "summary-a", revision=2) == second
    assert recovered.head(OWNER, WORKSPACE, "summary-a")["enabled"] is False
    with pytest.raises(WorkflowRepositoryError, match="admission_frozen"):
        publish(recovered, epoch=3, token="restored", now=102)


@pytest.mark.parametrize("mutation", ["body", "hash", "head", "low_reader", "partial", "future"])
def test_summary_corrupt_authority_is_rejected_by_startup_and_backup(tmp_path: Path, mutation: str) -> None:
    store = store_for(tmp_path / "platform.db")
    publish(store)
    with store.workflows.transaction() as connection:
        if mutation == "body": connection.execute("UPDATE workflow_summary_versions SET snapshot=?", (b"{}",))
        if mutation == "hash": connection.execute("UPDATE workflow_summary_versions SET sha256=?", ("a" * 64,))
        if mutation == "head": connection.execute("UPDATE workflow_summary_heads SET latest_revision=2")
        if mutation == "low_reader": connection.execute("UPDATE workflow_schema_meta SET minimum_reader_version=10")
        if mutation == "partial": connection.execute("DROP TABLE workflow_summary_versions")
        if mutation == "future": connection.execute("UPDATE workflow_summary_schema_meta SET schema_version=2")
    with pytest.raises(WorkflowRepositoryError): store.workflows.init()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    with pytest.raises(BackupError): PlatformBackupService().create_backup(store.workflows.db_path, artifacts, tmp_path / "bad")


def test_summary_history_checks_signed_rowids_and_last_page_without_unbounded_rows(tmp_path: Path) -> None:
    from hub.infrastructure.workflow_summary_repository import WorkflowSummaryRepository

    store = store_for(tmp_path / "platform.db")
    for index in range(129):
        publish(store, token=f"version-{index}", epoch=index, content=f"version {index}", now=100+index)
    with store.workflows.transaction() as connection:
        connection.execute("UPDATE workflow_summary_versions SET rowid=-9223372036854775808 WHERE revision=1")
        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        WorkflowSummaryRepository.validate_snapshot(connection)
        pages = [sql for sql in statements if sql.startswith("SELECT rowid AS summary_rowid,")]
        assert len(pages) == 2 and all(sql.endswith("ORDER BY rowid LIMIT 128") for sql in pages)
        connection.execute("UPDATE workflow_summary_versions SET snapshot=? WHERE revision=129", (b"{}",))
    with pytest.raises(WorkflowRepositoryError): store.workflows.init()


def test_summary_selected_reads_use_indexed_versions_in_large_workspace_history(tmp_path: Path) -> None:
    from hub.infrastructure.workflow_summary_repository import validate_summary_input_closure

    store = store_for(tmp_path / "platform.db")
    first = publish(store)
    for index in range(1, 1500):
        publish(store, token=f"version-{index}", epoch=index, content=f"version {index}", now=100+index)
    with store.workflows.transaction(write=False) as connection:
        steps = []
        connection.set_progress_handler(lambda: steps.append(1) or 0, 100)
        assert store.read_selected(OWNER, WORKSPACE, (reference(first),), connection=connection) == (first,)
        selected_steps = len(steps) * 100
        steps.clear()
        from types import SimpleNamespace

        validate_summary_input_closure(connection, [(SimpleNamespace(owner_id=OWNER, workspace_id=WORKSPACE), (("summary", first),))])
        assert len(steps) * 100 < 4000
        assert selected_steps < 4000


def test_summary_version_batches_preserve_multiple_revisions_and_disabled_history(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from hub.infrastructure.workflow_summary_repository import validate_summary_input_closure

    store = store_for(tmp_path / "platform.db")
    versions = [publish(store, token=f"version-{index}", epoch=index, content=f"version {index}", now=100+index)
        for index in range(129)]
    assert store.read_selected(OWNER, WORKSPACE, (reference(versions[-1]), reference(versions[0]))) == (versions[-1], versions[0])
    store.set_enabled(OWNER, WORKSPACE, "summary-a", enabled=False, expected_epoch=129)
    scope = SimpleNamespace(owner_id=OWNER, workspace_id=WORKSPACE)
    records = [(scope, (("summary", item),)) for item in versions]
    with store.workflows.transaction() as connection:
        validate_summary_input_closure(connection, records)
        connection.execute("UPDATE workflow_summary_versions SET snapshot=? WHERE revision=129", (b"{}",))
        with pytest.raises(WorkflowRepositoryError, match="summary_snapshot_invalid"):
            validate_summary_input_closure(connection, records)


def test_summary_http_publish_select_revoke_and_original_retry(tmp_path: Path) -> None:
    from test_workflow_http import ORIGIN, http_setup

    _, client, _ = http_setup(tmp_path)
    url = "/api/platform/v1/workflow-summaries"
    payload = {"workspace_id": WORKSPACE, "summary_id": "summary-a", "title": "人工摘要", "content": CONTENT,
        "client_token": "publish-a", "expected_epoch": 0}
    first = client.post(url, json=payload, headers=ORIGIN)
    assert first.status_code == 201, first.get_json()
    item = first.get_json()["summary"]
    assert item["created_by"] == OWNER and item["content"] == CONTENT and item["revision"] == 1
    selected = client.get(url + f"/version?workspace_id={WORKSPACE}&summary_id=summary-a&revision=1")
    assert selected.status_code == 200 and selected.get_json()["summary"] == item
    listing = client.get(url + f"?workspace_id={WORKSPACE}&limit=1").get_json()
    assert len(listing["summaries"]) == 1 and CONTENT not in json.dumps(listing, ensure_ascii=False)
    disabled = client.post(url + "/enabled", json={"workspace_id": WORKSPACE, "summary_id": "summary-a",
        "expected_epoch": 1, "enabled": False}, headers=ORIGIN)
    assert disabled.status_code == 200 and disabled.get_json()["summary"]["enabled"] is False
    assert client.post(url, json=payload, headers=ORIGIN).get_json() == first.get_json()
    foreign = {"Cf-Access-Authenticated-User-Email": "other@example.test"}
    assert client.get(url + f"/version?workspace_id={WORKSPACE}&summary_id=summary-a&revision=1", headers=foreign).status_code == 404
    assert client.get(url + f"?workspace_id={WORKSPACE}", headers=foreign).get_json()["summaries"] == []
    assert client.post(url, json={**payload, "created_by": "forged"}, headers=ORIGIN).status_code == 400
    assert client.post(url, json=payload).status_code == 403
    assert client.get(url, headers={"Authorization": "Bearer test"}).status_code == 401


def test_summary_gate_off_startup_rejects_corrupt_body_but_valid_history_is_visible(tmp_path: Path) -> None:
    from hub.bootstrap import create_app
    from hub.config import FleetConfig

    store = store_for(tmp_path / "platform.db")
    publish(store)
    config = FleetConfig.from_root(tmp_path, platform_enabled=True, platform_db=store.workflows.db_path,
        dev_operator=OWNER, serve_frontend=False)
    app = create_app(config)
    assert app.test_client().get("/api/platform/v1/workflow-summaries").status_code == 404
    with store.workflows.transaction() as connection:
        connection.execute("UPDATE workflow_summary_versions SET snapshot=?", (b"{}",))
    with pytest.raises(WorkflowRepositoryError): create_app(config)


def test_summary_http_exact_head_finds_record_beyond_first_page(tmp_path: Path) -> None:
    from test_workflow_http import ORIGIN, http_setup

    app, client, _ = http_setup(tmp_path)
    service = app.extensions["fleet"]["services"]["workflow_summaries"]
    for index in range(51):
        service.publish(OWNER, {"workspace_id": WORKSPACE, "summary_id": f"summary-{index:03d}",
            "title": "分页摘要", "content": CONTENT, "client_token": f"publish-{index}", "expected_epoch": 0})
    url = "/api/platform/v1/workflow-summaries"
    listing = client.get(url + f"?workspace_id={WORKSPACE}&limit=50").get_json()
    assert len(listing["summaries"]) == 50 and listing["next_cursor"] == "summary-049"
    query = f"/head?workspace_id={WORKSPACE}&summary_id=summary-050"
    response = client.get(url + query)
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["summary"] == {"workspace_id": WORKSPACE, "summary_id": "summary-050",
        "latest_revision": 1, "epoch": 1, "enabled": True}
    assert client.post(url + "/enabled", json={"workspace_id": WORKSPACE, "summary_id": "summary-050",
        "expected_epoch": 1, "enabled": False}, headers=ORIGIN).status_code == 200
    assert client.get(url + query).get_json()["summary"]["enabled"] is False
    assert client.get(url + query + "&revision=1").status_code == 400
    foreign = {"Cf-Access-Authenticated-User-Email": "other@example.test"}
    assert client.get(url + query, headers=foreign).status_code == 404
