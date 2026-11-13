"""Normal Hub Muse wiring retains the original controlled bridge and journal."""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from test_muse_browser_bridge import Transport, message
from test_workflow_repository import OWNER, WORKSPACE, prepare_database, registered

from hub import web
from tools.browser_bridge.muse import MuseBridge, SubprocessMuseTransport


def manifest(tmp_path):
    journal = tmp_path / "muse.db"
    identity = MuseBridge(journal).initialize()
    definition = json.loads(registered().snapshot)
    definition["template_id"] = "muse-task"
    definition["steps"][0]["kind"] = "external_agent"
    definition["steps"][0]["contract"]["output_specs"] = []
    path = tmp_path / "muse.json"
    path.write_text(json.dumps({"schema_version": 1, "muse": [{"owner_id": OWNER, "workspace_id": WORKSPACE,
        "target_key": "muse-account", "account_ref": "account-a", "journal_path": str(journal),
        "journal_identity": identity, "definition": definition}]}), encoding="utf-8")
    return path, journal


def start(tmp_path, path, monkeypatch, *, enabled=True, dispatch=False):
    monkeypatch.setattr(web, "FLEET_HOME", tmp_path)
    return web.make_app(require_token=False, dev_operator=OWNER, serve_frontend=False,
        platform_enabled=True, platform_db=tmp_path / "platform.db", platform_workflows_enabled=True,
        platform_external_agents_enabled=enabled, platform_workflow_dispatch_enabled=dispatch,
        platform_muse_registration_file=path)


def test_normal_startup_attaches_muse_without_browser_io(tmp_path, monkeypatch):
    prepare_database(tmp_path / "platform.db")
    path, _ = manifest(tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError("startup touched browser")
    monkeypatch.setattr(SubprocessMuseTransport, "request", forbidden)
    app = start(tmp_path, path, monkeypatch)
    templates = app.test_client().get("/api/platform/v1/workflow-templates").get_json()["templates"]
    assert [item["template_id"] for item in templates] == ["muse-task"]
    coordinator = app.extensions["fleet"]["services"]["workflow_coordinator"]
    assert (OWNER, WORKSPACE, "muse-account") in coordinator.dispatchers


def test_journal_cli_preview_then_explicit_initialization(tmp_path, capsys):
    from hub.muse_setup import main
    journal = tmp_path / "private" / "journal.db"
    assert main(["--journal", str(journal)]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "mode": "preview", "state": "uninitialized"}
    assert not journal.parent.exists()
    assert main(["--journal", str(journal), "--initialize"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert len(first["journal_identity"]) == 64
    assert main(["--journal", str(journal), "--initialize"]) == 0
    assert json.loads(capsys.readouterr().out) == first


def test_normal_muse_workflow_one_send_and_gate_off_restart_recovery(tmp_path, monkeypatch):
    repository = prepare_database(tmp_path / "platform.db")
    path, journal = manifest(tmp_path)
    transport = Transport()
    monkeypatch.setattr(SubprocessMuseTransport, "request", lambda self, operation, **kwargs: transport.request(operation, **kwargs))
    app = start(tmp_path, path, monkeypatch, dispatch=True)
    response = app.test_client().post("/api/platform/v1/workflows", headers={"Origin": "http://localhost"}, json={
        "template_id": "muse-task", "revision": 1, "workspace_id": WORKSPACE, "input_refs": [],
        "client_token": "normal-muse", "expected_policy_revision": 1})
    assert response.status_code == 201, response.get_json()
    workflow = response.get_json()["workflow"]["workflow_id"]
    coordinator = app.extensions["fleet"]["services"]["workflow_coordinator"]
    for offset in (2, 4, 6):
        tick = asyncio.run(coordinator.tick(time.time() + offset))
        assert not tick.failures, tick
    assert len(transport.sends) == 1
    saved = repository.get_dispatch(OWNER, workflow, "develop", 1)
    assert saved.binding is not None and saved.binding.kind == "external"
    bridge = MuseBridge(journal)
    receipt = bridge.lookup(saved.token, saved.resolved.snapshot.decode(), scope=WORKSPACE)
    transport.messages.append(message("done", "assistant", "done\n" + receipt["completion_marker"]))
    restarted = start(tmp_path, path, monkeypatch, enabled=False, dispatch=True)
    coordinator = restarted.extensions["fleet"]["services"]["workflow_coordinator"]
    tick = asyncio.run(coordinator.tick(time.time() + 10))
    assert not tick.failures, tick
    current = repository.get_dispatch(OWNER, workflow, "develop", 1)
    assert current.binding == saved.binding and current.token == saved.token
    assert current.step_state == "succeeded" and len(transport.sends) == 1
    result = repository.get(OWNER, workflow)
    assert result.acceptance_state == "pending" and result.validation_state == "not_requested"


@pytest.mark.parametrize("source", ["environment", "file", "explicit_off", "environment_off"])
def test_muse_registration_path_precedence(tmp_path, monkeypatch, source):
    prepare_database(tmp_path / "platform.db")
    path, _ = manifest(tmp_path)
    monkeypatch.setattr(web, "FLEET_HOME", tmp_path)
    monkeypatch.delenv("AGENT_FLEET_PLATFORM_MUSE_REGISTRATION_FILE", raising=False)
    (tmp_path / "fleet-gates.conf").write_text(f"AGENT_FLEET_PLATFORM_MUSE_REGISTRATION_FILE={path}\n", encoding="utf-8")
    options = {}
    if source in ("environment", "explicit_off"):
        monkeypatch.setenv("AGENT_FLEET_PLATFORM_MUSE_REGISTRATION_FILE", str(path))
    if source == "explicit_off": options["platform_muse_registration_file"] = ""
    if source == "environment_off": monkeypatch.setenv("AGENT_FLEET_PLATFORM_MUSE_REGISTRATION_FILE", "")
    app = web.make_app(require_token=False, dev_operator=OWNER, serve_frontend=False, platform_enabled=True,
        platform_db=tmp_path / "platform.db", platform_workflows_enabled=True, **options)
    templates = app.test_client().get("/api/platform/v1/workflow-templates").get_json()["templates"]
    assert len(templates) == (0 if source.endswith("off") else 1)


def test_muse_and_coding_registration_coexist_without_replacing_routes(tmp_path, monkeypatch):
    from test_workflow_startup import registration, seed

    from hub.infrastructure.platform_db import PlatformRepository
    db = tmp_path / "platform.db"
    seed(PlatformRepository(db), tmp_path)
    muse, _ = manifest(tmp_path)
    coding = tmp_path / "coding.json"
    coding.write_text(json.dumps(registration(OWNER, WORKSPACE)), encoding="utf-8")
    monkeypatch.setattr(web, "FLEET_HOME", tmp_path)
    app = web.make_app(require_token=False, dev_operator=OWNER, serve_frontend=False, platform_enabled=True,
        platform_db=db, platform_workflows_enabled=True, platform_workflow_registration_file=coding,
        platform_muse_registration_file=muse)
    templates = app.test_client().get("/api/platform/v1/workflow-templates").get_json()["templates"]
    assert {item["template_id"] for item in templates} == {"coding-delivery", "muse-task"}
    coordinator = app.extensions["fleet"]["services"]["workflow_coordinator"]
    assert len(coordinator.dispatchers) == 4 and len(coordinator.registrations) == 4


@pytest.mark.parametrize("change", ["version", "fields", "row_fields", "empty", "many", "duplicate", "scope", "journal_missing",
    "journal_changed", "journal_relative", "journal_identity", "outputs", "validators", "capabilities", "kind", "duplicate_json", "oversized"])
def test_muse_configuration_rejects_unavailable_or_unsupported_entries(tmp_path, monkeypatch, change):
    from hub.composition.muse_registration import MAX_REGISTRATION_BYTES
    prepare_database(tmp_path / "platform.db")
    path, journal = manifest(tmp_path)
    value = json.loads(path.read_text()); row = value["muse"][0]
    if change == "version": value["schema_version"] = True
    elif change == "fields": value["unknown"] = 1
    elif change == "row_fields": row["transport"] = "arbitrary"
    elif change == "empty": value["muse"] = []
    elif change == "many": value["muse"] *= 33
    elif change == "duplicate": value["muse"] *= 2
    elif change == "scope": row["workspace_id"] = "missing"
    elif change == "journal_missing": row["journal_path"] = str(tmp_path / "missing.db")
    elif change == "journal_changed": row["journal_identity"] = "0" * 64
    elif change == "journal_relative": row["journal_path"] = "relative.db"
    elif change == "journal_identity": row["journal_identity"] = False
    elif change == "outputs": row["definition"]["steps"][0]["contract"]["output_specs"] = json.loads(registered().snapshot)["steps"][0]["contract"]["output_specs"]
    elif change == "validators": row["definition"]["steps"][0]["contract"]["validator_refs"] = [{"id": "validator", "revision": 1}]
    elif change == "capabilities": row["definition"]["steps"][0]["contract"]["capability_requirements"] = [{"id": "remote", "revisions": [1]}]
    elif change == "kind": row["definition"]["steps"][0]["kind"] = "native_run"
    path.write_text(json.dumps(value), encoding="utf-8")
    if change == "duplicate_json": path.write_text('{"schema_version":1,"schema_version":1,"muse":[]}', encoding="utf-8")
    if change == "oversized": path.write_bytes(b" " * (MAX_REGISTRATION_BYTES + 1))
    before = journal.read_bytes()
    with pytest.raises((ValueError, RuntimeError)):
        start(tmp_path, path, monkeypatch)
    assert journal.read_bytes() == before
    assert not (tmp_path / "missing.db").exists()


@pytest.mark.parametrize("mode", ["relative", "directory", "corrupt_preview", "corrupt_initialize"])
def test_journal_cli_invalid_files_return_stable_errors(tmp_path, capsys, mode):
    from hub.muse_setup import main
    journal = tmp_path / "journal.db"
    if mode == "directory": journal = tmp_path
    elif mode.startswith("corrupt"): journal.write_bytes(b"not sqlite")
    args = ["--journal", "relative.db" if mode == "relative" else str(journal)]
    if mode == "corrupt_initialize": args.append("--initialize")
    assert main(args) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False and str(tmp_path) not in json.dumps(report)


def test_journal_cli_module_initializes_without_transport_io(tmp_path):
    import subprocess
    import sys
    journal = tmp_path / "journal.db"
    result = subprocess.run([sys.executable, "-m", "hub.muse_setup", "--journal", str(journal), "--initialize"],
        check=False, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["journal_identity"] == MuseBridge(journal).journal_identity()


def test_external_gate_off_blocks_new_sends_but_keeps_adapter_attached(tmp_path, monkeypatch):
    repository = prepare_database(tmp_path / "platform.db")
    path, _ = manifest(tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError("disabled adapter touched browser")
    monkeypatch.setattr(SubprocessMuseTransport, "request", forbidden)
    app = start(tmp_path, path, monkeypatch, enabled=False, dispatch=True)
    response = app.test_client().post("/api/platform/v1/workflows", headers={"Origin": "http://localhost"}, json={
        "template_id": "muse-task", "revision": 1, "workspace_id": WORKSPACE, "input_refs": [],
        "client_token": "disabled-muse", "expected_policy_revision": 1})
    assert response.status_code == 201, response.get_json()
    coordinator = app.extensions["fleet"]["services"]["workflow_coordinator"]
    assert (OWNER, WORKSPACE, "muse-account") in coordinator.dispatchers
    for offset in (2, 4, 6):
        tick = asyncio.run(coordinator.tick(time.time() + offset))
        assert not tick.failures, tick
    with repository.transaction(write=False) as connection:
        assert connection.execute("SELECT count(*) FROM workflow_dispatches").fetchone()[0] == 0


def test_muse_configuration_rejects_ambiguous_python_adapter_injection(tmp_path):
    from hub.bootstrap import create_app
    from hub.composition.muse_registration import load_muse_registration
    from hub.config import FleetConfig
    from hub.infrastructure.platform_db import PlatformRepository
    prepare_database(tmp_path / "platform.db")
    path, _ = manifest(tmp_path)
    loaded = load_muse_registration(path, PlatformRepository(tmp_path / "platform.db"))
    with pytest.raises(ValueError, match="muse_registration_conflicts_with_injected_adapters"):
        create_app(FleetConfig.from_root(tmp_path, platform_enabled=True, platform_db=tmp_path / "platform.db", platform_workflows_enabled=True,
            platform_muse_registration_file=path, serve_frontend=False),
            repositories={"workflow_external_agents": loaded["workflow_external_agents"]})


def test_muse_journal_identity_change_during_attachment_is_rejected(tmp_path, monkeypatch):
    prepare_database(tmp_path / "platform.db")
    path, _ = manifest(tmp_path)
    expected = json.loads(path.read_text())["muse"][0]["journal_identity"]
    reads = []
    def changed(self):
        reads.append(True)
        return expected if len(reads) == 1 else "0" * 64
    monkeypatch.setattr(MuseBridge, "journal_identity", changed)
    with pytest.raises(ValueError, match="muse_registration_journal_changed"):
        start(tmp_path, path, monkeypatch)
