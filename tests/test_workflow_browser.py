"""Real browser controls against the workflow HTTP and SQLite boundary."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from threading import Thread

import pytest
from flask import Flask
from werkzeug.serving import make_server

from hub.application.workflow_templates import RegisteredWorkflowTemplate, WorkflowTemplateCatalog, coding_delivery_definition
from hub.bootstrap import create_app
from hub.config import FleetConfig
from hub.domain.workflow_contract_types import ArtifactReference
from hub.platform.artifacts import ArtifactStore
from hub.platform.workflow_source import extract_source_bundle
from test_workflow_coding_delivery import DeliveryFixture, REVIEWER, review, run_tests
from test_workflow_coordinator import make_setup
from test_workflow_control import control
from test_workflow_repository import OWNER, WORKSPACE

ROOT = Path(__file__).resolve().parents[1]


def _browser_acceptance(app: Flask, script: str, *arguments: str) -> None:
    server = make_server("127.0.0.1", 0, app, threaded=True)
    origin = f"http://127.0.0.1:{server.server_port}"
    app.config["ACCOUNT_ORIGIN"] = origin
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(["node", str(ROOT / "tests/fixtures/frontend" / script), origin, *arguments],
                                capture_output=True, text=True, timeout=90)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive()


@pytest.mark.parametrize("enabled", [False, True])
def test_workflow_panel_in_real_browser(tmp_path: Path, enabled: bool) -> None:
    if not os.environ.get("FLEET_PLAYWRIGHT_MODULE"):
        pytest.skip("Set FLEET_PLAYWRIGHT_MODULE to run Chromium acceptance")
    source = tmp_path / "requirements.txt"
    source.write_text("Fixed approved requirements", encoding="utf-8")
    manifest = ArtifactStore(tmp_path / "var/platform/artifacts").put_file(OWNER, WORKSPACE, source)
    setup = make_setup(tmp_path, count=2, source={key: manifest[key] for key in ("artifact_id", "sha256", "size")})
    with setup.repository.transaction() as connection:
        connection.execute("UPDATE workflow_steps SET reason_code=? WHERE workflow_id='workflow-001'",
                           ('<img src=x onerror="window.workflowXss=1">',))
    config = FleetConfig.from_root(tmp_path, frontend_dir=ROOT / "frontend", dev_operator=OWNER,
        platform_enabled=True, platform_workflows_enabled=enabled, platform_db=setup.repository.db_path)
    catalog = WorkflowTemplateCatalog((RegisteredWorkflowTemplate(OWNER, WORKSPACE, setup.definition),))
    app = create_app(config, repositories={"workflow_templates": catalog, "workflow_clock": setup.clock})
    platform = app.extensions["fleet"]["platform_repository"]
    platform.upsert_model(OWNER, {"profile_id": "fixture", "provider": "deterministic", "model": "Workflow fixture"})
    platform.update_defaults(OWNER, {"model_profile_id": "fixture", "workspace_id": WORKSPACE}, 0)
    _browser_acceptance(app, "workflow_browser.cjs", "on" if enabled else "off")


@pytest.mark.parametrize("scenario", ["empty", "waiting_node", "waiting_external", "unknown", "cancelling", "awaiting_acceptance"])
def test_workflow_states_and_read_recovery_in_real_browser(tmp_path: Path, scenario: str) -> None:
    if not os.environ.get("FLEET_PLAYWRIGHT_MODULE"):
        pytest.skip("Set FLEET_PLAYWRIGHT_MODULE to run Chromium acceptance")
    setup = make_setup(tmp_path, count={"empty": 0, "waiting_node": 2}.get(scenario, 1))
    if scenario == "waiting_node":
        coordinator = setup.coordinator(registrations=(replace(setup.registrations[0], capacity=1),))
        setup.tick(coordinator)
        setup.tick(coordinator)
    elif scenario != "empty":
        for _ in range(3):
            setup.tick()
        if scenario == "cancelling":
            control(setup, "cancel")
        elif scenario in ("unknown", "awaiting_acceptance"):
            setup.platform.set_run_state(OWNER, setup.run_id(), "unknown" if scenario == "unknown" else "succeeded", now=setup.clock())
            setup.tick()
    config = FleetConfig.from_root(tmp_path, frontend_dir=ROOT / "frontend", dev_operator=OWNER,
        platform_enabled=True, platform_workflows_enabled=True, platform_db=setup.repository.db_path)
    catalog = WorkflowTemplateCatalog((RegisteredWorkflowTemplate(OWNER, WORKSPACE, setup.definition),))
    app = create_app(config, repositories={"workflow_templates": catalog, "workflow_clock": setup.clock})
    setup.platform.upsert_model(OWNER, {"profile_id": "fixture", "provider": "deterministic", "model": "Workflow fixture"})
    setup.platform.update_defaults(OWNER, {"model_profile_id": "fixture", "workspace_id": WORKSPACE}, 0)
    _browser_acceptance(app, "workflow_states_browser.cjs", scenario)


@pytest.mark.parametrize("passed", [False, True])
def test_workflow_evidence_download_and_acceptance_in_real_browser(tmp_path: Path, passed: bool) -> None:
    if not os.environ.get("FLEET_PLAYWRIGHT_MODULE"):
        pytest.skip("Set FLEET_PLAYWRIGHT_MODULE to run Chromium acceptance")
    fixture = DeliveryFixture(tmp_path)
    bundle = fixture.exported.revision.source_bundle
    archive = tmp_path / "source.tar"
    archive.write_bytes(fixture.store.read(OWNER, WORKSPACE, bundle.artifact_id))
    manifest = fixture.store.put_file(OWNER, WORKSPACE, archive, name="<img src=x onerror=window.workflowXss=1>.tar")
    fixture.exported = replace(fixture.exported, manifest=manifest, revision=replace(fixture.exported.revision,
        source_bundle=ArtifactReference(manifest["artifact_id"], manifest["sha256"], manifest["size"])))
    target = fixture.start("develop")
    source_receipt = asyncio.run(fixture.service.record_source(target, fixture.exported, executor_id="developer-1"))
    fixture.finish(target)
    reviewer = fixture.start("review")
    report = json.loads(review(fixture.exported.revision, subject=source_receipt.subject_digest, verdict="passed" if passed else "failed"))
    report["findings"] = [{"code": "literal-fixture", "severity": "warning",
        "message": '<img src=x onerror="window.workflowXss=1"> [probe](javascript:window.workflowXss=1) ' + "长中文评审正文" * 64}]
    asyncio.run(fixture.service.record_review(reviewer, json.dumps(report, ensure_ascii=False).encode("utf-8"), executor=REVIEWER))
    fixture.finish(reviewer)
    if passed:
        test_target = fixture.start("test")
        checkout = tmp_path / "test-checkout"
        extract_source_bundle(fixture.store, OWNER, WORKSPACE, fixture.exported.revision.source_bundle.artifact_id,
                             checkout, expected_revision=fixture.exported.revision)
        result = run_tests(checkout, fixture.exported.revision, source_receipt.subject_digest)
        asyncio.run(fixture.service.record_test(test_target, result))
        fixture.finish(test_target)
    config = FleetConfig.from_root(tmp_path, frontend_dir=ROOT / "frontend", dev_operator=OWNER,
        platform_enabled=True, platform_workflows_enabled=True, platform_db=fixture.repository.db_path,
        platform_artifact_root=fixture.store.root)
    app = create_app(config, repositories={"workflow_clock": lambda: 110})
    platform = app.extensions["fleet"]["platform_repository"]
    platform.upsert_model(OWNER, {"profile_id": "fixture", "provider": "deterministic", "model": "Workflow fixture"})
    platform.update_defaults(OWNER, {"model_profile_id": "fixture", "workspace_id": WORKSPACE}, 0)
    _browser_acceptance(app, "workflow_evidence_browser.cjs", "passed" if passed else "failed")


@pytest.mark.parametrize("session_check", ["current", "held-session", "held-unauthorized", "held-feature-unauthorized"])
def test_workflow_cookie_identity_and_pending_operation_isolation(tmp_path: Path, session_check: str) -> None:
    if not os.environ.get("FLEET_PLAYWRIGHT_MODULE"):
        pytest.skip("Set FLEET_PLAYWRIGHT_MODULE to run Chromium acceptance")
    config = FleetConfig.from_root(tmp_path, frontend_dir=ROOT / "frontend", platform_enabled=True,
        platform_workflows_enabled=True, accounts={"enabled": True, "origin": "http://127.0.0.1", "registration": "invite"})
    app = create_app(config)
    accounts = app.extensions["accounts"]
    accounts.bootstrap_admin("first@example.test", "Workflow-password-123!")
    mail = []
    accounts.invite("second@example.test")
    accounts.issue_code("second@example.test", "register", "invite", lambda *args: mail.append(args))
    accounts.complete_code("second@example.test", "register", mail[-1][2], "Workflow-password-123!")
    actor = next(user["id"] for user in accounts.users() if user["role"] == "admin")
    second = next(user for user in accounts.users() if user["email"] == "second@example.test")
    accounts.manage(actor, second["id"], changes={"role": "admin"}, expected_revision=second["revision"])
    accounts.invite("basic@example.test")
    accounts.issue_code("basic@example.test", "register", "invite", lambda *args: mail.append(args))
    accounts.complete_code("basic@example.test", "register", mail[-1][2], "Workflow-password-123!")
    platform = app.extensions["fleet"]["platform_repository"]
    workflows = app.extensions["fleet"]["repositories"]["workflows"]
    definition = coding_delivery_definition()
    registrations = []
    for user in accounts.users():
        owner = user["id"]
        platform.upsert_model(owner, {"profile_id": "fixture", "provider": "deterministic", "model": "Workflow fixture"})
        platform.upsert_workspace(owner, {"workspace_id": "home", "root_path": str(tmp_path / owner)})
        platform.update_defaults(owner, {"model_profile_id": "fixture", "workspace_id": "home"}, 0)
        if user["role"] != "admin":
            continue
        workflow_id = "workflow-user-a" if user["email"] == "first@example.test" else "workflow-user-b"
        workflows.create_or_get(owner, "home", workflow_id, client_token=workflow_id, definition=definition, now=time.time())
        registrations.append(RegisteredWorkflowTemplate(owner, "home", definition))
    app.extensions["fleet"]["services"]["workflows"].catalog = WorkflowTemplateCatalog(tuple(registrations))
    _browser_acceptance(app, "workflow_accounts_browser.cjs", session_check)
