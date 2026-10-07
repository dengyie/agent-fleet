"""Runner and Hub share valid, finite and byte-bounded test evidence."""
import json
from pathlib import Path
from typing import Any, Callable

import pytest

from hub.domain.task import public_result
from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.infrastructure.task_repository import SqliteTaskRepository, _normalize_test_summary
from tools.result_files import normalize_test_summary


@pytest.mark.parametrize("normalize", [normalize_test_summary, _normalize_test_summary], ids=["runner", "hub"])
@pytest.mark.parametrize("value", [
    {"passed": float("inf")}, {"passed": 0.5}, {"passed": True}, {"passed": -1},
    {"duration_s": float("nan")}, {"duration_s": -1}, {"duration_s": 10**1000},
    {"failed_names": [{"not": "text"}]}, {"failed_names": ["\ud800"]},
], ids=["count-inf", "count-fraction", "count-bool", "count-negative", "time-nan", "time-negative", "time-overflow", "name-object", "name-surrogate"])
def test_invalid_summary_values_are_rejected(normalize: Callable, value: Any) -> None:
    with pytest.raises(ValueError, match="invalid_test_summary"):
        normalize(value)


def test_summary_persistence_never_slices_serialized_json(tmp_path: Path) -> None:
    repo = SqliteTaskRepository(tmp_path / "tasks.db")
    repo.init()
    task, _ = repo.create_task(machine="machine-a", agent_type="codex", project="project",
                               instruction="test", requested_by="operator@example.test", now=100)
    lease = repo.lease_task(machine="machine-a", runner_id="runner-a", now=100)
    summary = {"framework": "pytest", "failed": 20, "failed_names": ["\x00" * 200] * 20}
    repo.complete_task(attempt_id=lease["attempt_id"], nonce=lease["nonce"], exit_code=1,
                       log_summary="failed", diff_stat="", duration_s=1, now=101, test_summary=summary)
    conn = repo._connect()
    try:
        encoded = conn.execute("SELECT test_summary FROM results WHERE attempt_id=?", (lease["attempt_id"],)).fetchone()[0]
    finally:
        conn.close()
    assert len(encoded.encode("utf-8")) <= 20480
    persisted = json.loads(encoded)
    assert persisted["failed"] == 20
    assert 0 < len(persisted["failed_names"]) <= 20
    assert repo.get_task(task["task_id"])["result"]["test_summary"] == persisted
    assert normalize_test_summary(summary) == persisted


def test_unicode_summary_persistence_stays_within_utf8_byte_budget(tmp_path: Path) -> None:
    repo = SqliteTaskRepository(tmp_path / "tasks.db")
    repo.init()
    task, _ = repo.create_task(machine="machine-a", agent_type="codex", project="project",
                               instruction="test", requested_by="operator@example.test", now=100)
    lease = repo.lease_task(machine="machine-a", runner_id="runner-a", now=100)
    summary = {"framework": "pytest", "failed_names": ["中" * 200] * 20}
    repo.complete_task(attempt_id=lease["attempt_id"], nonce=lease["nonce"], exit_code=1,
                       log_summary="failed", diff_stat="", duration_s=1, now=101, test_summary=summary)
    conn = repo._connect()
    try:
        encoded = conn.execute("SELECT test_summary FROM results WHERE attempt_id=?", (lease["attempt_id"],)).fetchone()[0]
    finally:
        conn.close()
    assert len(encoded.encode("utf-8")) <= 20480


def test_public_summary_projection_revalidates_unsafe_legacy_values() -> None:
    with pytest.raises(ValueError, match="invalid_test_summary"):
        public_result({"test_summary": {"passed": float("inf")}})


def test_summary_preserves_valid_fields_and_drops_unknown_keys() -> None:
    summary = {"framework": "pytest", "passed": 10, "failed": 1, "skipped": 2,
               "errors": 0, "duration_s": 0.5, "failed_names": ["test_中文"], "unknown": "discard"}
    expected = {key: value for key, value in summary.items() if key != "unknown"}
    assert normalize_test_summary(summary) == _normalize_test_summary(summary) == expected
    assert public_result({"test_summary": summary}) == {"test_summary": expected}


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_summary_json_rejects_nonstandard_constants_even_in_unknown_fields(constant: str) -> None:
    with pytest.raises(ValueError, match="invalid_test_summary") as error:
        normalize_test_summary('{"ignored":' + constant + '}')
    assert isinstance(error.value.__cause__, ValueError)


@pytest.mark.parametrize("summary", [{"passed": 0.5}, {"duration_s": float("nan")}, {"failed_names": [{}]}],
                         ids=["fractional-count", "nonfinite-time", "nontext-name"])
def test_invalid_runner_summary_returns_400_without_committing_result(tmp_path: Path, summary: dict[str, Any]) -> None:
    config = FleetConfig.from_root(tmp_path, dev_operator="operator@example.test",
                                   runner_credentials={"machine-a": "fixture"})
    client = create_app(config).test_client()
    repo = SqliteTaskRepository(config.task_db)
    task, _ = repo.create_task(machine="machine-a", agent_type="codex", project="project",
                               instruction="test", requested_by="operator@example.test")
    lease = repo.lease_task(machine="machine-a", runner_id="runner-a")
    response = client.post(f"/api/commands/{lease['attempt_id']}/result", json={
        "nonce": lease["nonce"], "exit_code": 0, "duration_s": 1, "test_summary": summary,
    }, headers={"X-Runner-Credential": "machine-a:fixture"})
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_test_summary"
    assert repo.get_task(task["task_id"])["state"] == "leased"


def test_runner_http_rejects_nonstandard_constant_in_unknown_summary_field(tmp_path: Path) -> None:
    config = FleetConfig.from_root(tmp_path, dev_operator="operator@example.test",
                                   runner_credentials={"machine-a": "fixture"})
    client = create_app(config).test_client()
    repo = SqliteTaskRepository(config.task_db)
    task, _ = repo.create_task(machine="machine-a", agent_type="codex", project="project",
                               instruction="test", requested_by="operator@example.test")
    lease = repo.lease_task(machine="machine-a", runner_id="runner-a")
    body = (
        '{"nonce":"' + lease["nonce"] + '","exit_code":0,"duration_s":1,'
        '"test_summary":{"unknown":NaN}}'
    )
    response = client.post(f"/api/commands/{lease['attempt_id']}/result", data=body,
                           content_type="application/json",
                           headers={"X-Runner-Credential": "machine-a:fixture"})
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_test_summary"
    assert repo.get_task(task["task_id"])["state"] == "leased"
