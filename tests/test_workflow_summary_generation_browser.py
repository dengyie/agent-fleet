"""Actual account UI drives the original summary Run and version authority."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.platform.providers.base import ModelResponse
from test_account_http import ORIGIN, PASSWORD
from test_workflow_browser import _browser_acceptance
from test_workflow_repository import OWNER, WORKSPACE, prepare_database


def test_model_summary_generation_and_publication_in_real_desktop_and_mobile_browser(tmp_path):
    if not os.environ.get("FLEET_PLAYWRIGHT_MODULE"):
        pytest.skip("Set FLEET_PLAYWRIGHT_MODULE for actual browser acceptance")
    repository = prepare_database(tmp_path / "platform.db")
    root = Path(__file__).resolve().parents[1]
    app = create_app(FleetConfig.from_root(tmp_path, frontend_dir=root / "frontend", platform_enabled=True,
        platform_db=repository.db_path, platform_workflows_enabled=True, platform_worker_enabled=True,
        accounts={"enabled": True, "origin": ORIGIN, "registration": "open", "sender": lambda *_args: None}),
        repositories={"workflow_clock": lambda: 101})
    app.testing = True
    app.config["ACCOUNT_COOKIE_SECURE"] = False
    app.extensions["accounts"].bootstrap_admin(OWNER, PASSWORD)
    client = app.test_client()
    assert client.post("/api/accounts/login", json={"email": OWNER, "password": PASSWORD},
        headers={"Origin": ORIGIN}, base_url=ORIGIN).status_code == 200
    owner = client.get("/api/accounts/me", base_url=ORIGIN).get_json()["user"]["id"]
    platform = app.extensions["fleet"]["platform_repository"]
    platform.upsert_workspace(owner, {"workspace_id": WORKSPACE, "root_path": str(tmp_path)})
    platform.upsert_workspace(owner, {"workspace_id": "workspace-b", "root_path": str(tmp_path)})
    platform.upsert_model(owner, {"profile_id": "model-a", "provider": "deterministic", "model": "摘要模型"})
    platform.create_conversation(owner, "source-chat", title="来源对话", workspace_id=WORKSPACE)
    platform.append_turn(owner, "source-chat", "source-message-a", "source-run-a",
        text='{"adapter_ref":{"id":"native","revision":1},"contract":{"objective":"完整用户输入 🌱"}}',
        client_token="source-token", config_snapshot={"workspace_id": WORKSPACE}, now=99)
    platform.set_run_state(owner, "source-run-a", "succeeded", now=100, result_text="完整成功结果 🌱\n")
    for index in range(21):
        platform.append_turn(owner, "source-chat", f"newer-message-{index}", f"newer-run-{index}",
            text=f"较新的输入 {index}", client_token=f"newer-token-{index}",
            config_snapshot={"workspace_id": WORKSPACE}, now=100 + index)
        platform.set_run_state(owner, f"newer-run-{index}", "succeeded", now=101 + index, result_text="较新的结果")
    calls = []
    class Provider:
        def complete(self, messages, tools, *, request_observer=None):
            calls.append((messages, tools))
            return ModelResponse(kind="final", text="模型生成的完整摘要\n保留决策与未完成工作。\n")
    worker = app.extensions["fleet"]["services"]["platform_worker"]
    worker.provider_factory = lambda _profile: Provider()
    worker.clock = lambda: 101
    app.extensions["fleet"]["services"]["run_events"].clock = lambda: 101
    service = app.extensions["fleet"]["services"]["workflow_summary_generations"]
    original = service.create
    def advance_after_acceptance(actor, values):
        item = original(actor, values)
        outcome = worker.run_once(actor)
        assert outcome is None or outcome["state"] == "succeeded"
        return item
    service.create = advance_after_acceptance
    screenshots = Path(os.environ.get("FLEET_SCREENSHOTS", str(tmp_path / "screenshots")))
    screenshots.mkdir(parents=True, exist_ok=True)
    for mode in ("desktop", "mobile"):
        _browser_acceptance(app, "workflow_summary_generation_browser.cjs", mode, str(screenshots / f"summary-generation-{mode}.png"))
    assert len(calls) == 4 and all(tools == [] for _, tools in calls)
    assert all("完整成功结果" in messages[0]["content"] for messages, _ in calls)
