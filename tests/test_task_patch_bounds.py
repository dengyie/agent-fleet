"""The task patch budget and redaction hold across runner, store and API."""
from pathlib import Path
from typing import Any
import json

import pytest

from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.infrastructure.task_repository import SqliteTaskRepository
from tools.result_files import MAX_DIFF_PATCH, redact_patch


@pytest.mark.parametrize("text", ["x" * (MAX_DIFF_PATCH + 1), "文🌲" * 20000], ids=["ascii", "multibyte"])
def test_redacted_patch_marker_is_inside_utf8_budget(text: str) -> None:
    result, truncated = redact_patch(text)
    assert truncated
    assert len(result.encode("utf-8")) <= MAX_DIFF_PATCH
    assert result.endswith("…[truncated]")
    assert redact_patch(result)[0] == result


def setup_task(tmp_path: Path) -> tuple[Any, SqliteTaskRepository, dict[str, Any], dict[str, Any]]:
    config = FleetConfig.from_root(tmp_path, dev_operator="operator@example.test",
                                   runner_credentials={"machine-a": "fixture"})
    app = create_app(config)
    repo = SqliteTaskRepository(config.task_db)
    task, _ = repo.create_task(machine="machine-a", agent_type="codex", project="project",
                               instruction="test", requested_by="operator@example.test")
    lease = repo.lease_task(machine="machine-a", runner_id="runner-a")
    return app.test_client(), repo, task, lease


def submit(client: Any, lease: dict[str, Any], patch: Any) -> Any:
    body = json.dumps({
        "nonce": lease["nonce"], "exit_code": 0, "duration_s": 1,
        "diff_patch": patch,
    }, ensure_ascii=patch == "\ud800")
    return client.post(f"/api/commands/{lease['attempt_id']}/result", data=body.encode("utf-8"),
                       content_type="application/json", headers={"X-Runner-Credential": "machine-a:fixture"})


def test_hub_redacts_runner_patch_before_persistence(tmp_path: Path) -> None:
    client, repo, task, lease = setup_task(tmp_path)
    secret = "ghp_abcdefghijklmnopqrstuvwxyz123456"
    patch = f"diff --git a/config.txt b/config.txt\n+token={secret}\n"
    assert submit(client, lease, patch).status_code == 200
    stored = repo.get_task(task["task_id"])["result"]["diff_patch"]
    assert secret not in stored
    public = client.get(f"/api/tasks/{task['task_id']}/diff")
    assert public.status_code == 200
    assert secret not in public.get_data(as_text=True)


def test_patch_storage_and_read_report_utf8_truncation(tmp_path: Path) -> None:
    client, repo, task, lease = setup_task(tmp_path)
    assert submit(client, lease, "+" + "🌲" * 30000).status_code == 200
    stored = repo.get_task(task["task_id"])["result"]["diff_patch"]
    assert len(stored.encode("utf-8")) <= MAX_DIFF_PATCH
    response = client.get(f"/api/tasks/{task['task_id']}/diff").get_json()
    assert len(response["diff_patch"].encode("utf-8")) <= MAX_DIFF_PATCH
    assert response["truncated"] is True
    assert response["diff_patch"] == stored


def test_patch_read_bounds_historical_multibyte_rows(tmp_path: Path) -> None:
    client, repo, task, lease = setup_task(tmp_path)
    assert submit(client, lease, "+valid").status_code == 200
    conn = repo._connect()
    try:
        with conn:
            conn.execute("UPDATE results SET diff_patch=? WHERE attempt_id=?", ("🌲" * 30000, lease["attempt_id"]))
    finally:
        conn.close()
    response = client.get(f"/api/tasks/{task['task_id']}/diff").get_json()
    assert len(response["diff_patch"].encode("utf-8")) <= MAX_DIFF_PATCH
    assert response["truncated"] is True


@pytest.mark.parametrize("patch", [["not text"], "\ud800"])
def test_malformed_patch_is_client_error_without_storing_result(tmp_path: Path, patch: Any) -> None:
    client, repo, task, lease = setup_task(tmp_path)
    response = submit(client, lease, patch)
    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_diff_patch"
    assert repo.get_task(task["task_id"])["state"] == "leased"
