"""Real Flask, operator identity, strict JSON and scoped SQLite workflow DTOs."""
from __future__ import annotations

from pathlib import Path
import json
from typing import Any

import pytest

from hub.application.workflow_templates import RegisteredWorkflowTemplate, WorkflowTemplateCatalog
from hub.bootstrap import create_app
from hub.config import FleetConfig
from test_workflow_coordinator import make_setup
from test_workflow_repository import OWNER, WORKSPACE

ORIGIN = {"Origin": "http://localhost"}


def http_setup(tmp_path: Path, **setup_options: Any) -> tuple[Any, Any, Any]:
    setup = make_setup(tmp_path, **setup_options)
    catalog = WorkflowTemplateCatalog((RegisteredWorkflowTemplate(OWNER, WORKSPACE, setup.definition),))
    config = FleetConfig.from_root(tmp_path, platform_enabled=True, platform_db=setup.repository.db_path,
                                   platform_workflows_enabled=True, dev_operator=OWNER, serve_frontend=False)
    app = create_app(config, repositories={"workflow_templates": catalog, "workflow_clock": setup.clock,
                                          "workflow_identity": lambda: "created-workflow"})
    return app, app.test_client(), setup


def body() -> dict[str, Any]:
    return {"template_id": "fixture", "revision": 1, "workspace_id": WORKSPACE,
            "input_refs": [], "client_token": "created", "expected_policy_revision": 1}


def test_create_detail_events_and_idempotence_use_public_dtos(tmp_path: Path) -> None:
    _, client, setup = http_setup(tmp_path)
    assert client.get("/api/platform/v1/workflow-templates").get_json()["templates"][0]["template_id"] == "fixture"
    first = client.post("/api/platform/v1/workflows", json=body(), headers=ORIGIN)
    assert first.status_code == 201, first.get_json()
    repeated = client.post("/api/platform/v1/workflows", json=body(), headers=ORIGIN)
    assert repeated.get_json() == first.get_json()
    detail = client.get("/api/platform/v1/workflows/created-workflow")
    assert detail.status_code == 200
    workflow = detail.get_json()["workflow"]
    assert workflow["created_at"] == "1970-01-01T00:01:40.000Z"
    assert workflow["execution_state"] == "running" and workflow["delivery_state"] == "in_progress"
    serialized = detail.get_data(as_text=True)
    for forbidden in (str(tmp_path), "config_snapshot", "lease_owner", "coordinator_epoch", "instruction", "owner_id", "secret_ref"):
        assert forbidden not in serialized
    events = client.get("/api/platform/v1/workflows/created-workflow/events?after=0&limit=1").get_json()
    assert events["next_cursor"] == 1 and events["events"][0]["sequence"] == 1
    assert client.get("/api/platform/v1/workflows/created-workflow/events?after=1&limit=1").get_json()["events"] == []
    assert setup.executor.created == 0


def test_create_receipt_is_readable_after_input_access_is_revoked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hub.infrastructure.workflow_repository import WorkflowRepositoryError

    app, client, setup = http_setup(tmp_path)
    first = client.post("/api/platform/v1/workflows", json=body(), headers=ORIGIN)
    assert first.status_code == 201
    authorizer = app.extensions["fleet"]["services"]["workflows"].repository.input_authorizer

    def revoked(*args, **kwargs):
        raise WorkflowRepositoryError("reference_forbidden")

    monkeypatch.setattr(authorizer, "authorize_references", revoked)
    repeated = client.post("/api/platform/v1/workflows", json=body(), headers=ORIGIN)
    assert repeated.status_code == 201, repeated.get_json()
    assert repeated.get_json() == first.get_json()
    changed = {**body(), "input_refs": [{"ref_key": "input", "kind": "artifact", "artifact_id": "forbidden", "sha256": "a" * 64, "size": 1}]}
    assert client.post("/api/platform/v1/workflows", json=changed, headers=ORIGIN).status_code == 400
    assert client.post("/api/platform/v1/workflows", json={**body(), "client_token": "new"}, headers=ORIGIN).status_code == 404
    assert len(setup.repository.list(OWNER)) == 2


def test_template_lists_scoped_fixed_inputs_for_creation(tmp_path: Path) -> None:
    from hub.platform.artifacts import ArtifactStore

    path = tmp_path / "requirements.txt"
    path.write_text("Approved workflow input", encoding="utf-8")
    manifest = ArtifactStore(tmp_path / "var/platform/artifacts").put_file(OWNER, WORKSPACE, path)
    reference = {key: manifest[key] for key in ("artifact_id", "sha256", "size")}
    _, client, _ = http_setup(tmp_path, source=reference)
    templates = client.get("/api/platform/v1/workflow-templates").get_json()["templates"]
    assert templates[0]["input_refs"] == [{"ref_key": "source", "kind": "artifact", **reference}]
    payload = {**body(), "input_refs": templates[0]["input_refs"]}
    assert client.post("/api/platform/v1/workflows", json=payload, headers=ORIGIN).status_code == 201
    foreign = client.get("/api/platform/v1/workflow-templates", headers={"Cf-Access-Authenticated-User-Email": "other@example.test"})
    assert foreign.get_json()["templates"] == []


@pytest.mark.parametrize("extra", [{"owner_id": "evil"}, {"lease": "evil"}, {"shell": "evil"}, {"root_path": "/tmp"}])
def test_create_rejects_untrusted_control_fields(tmp_path: Path, extra: dict[str, str]) -> None:
    _, client, _ = http_setup(tmp_path)
    response = client.post("/api/platform/v1/workflows", json={**body(), **extra}, headers=ORIGIN)
    assert response.status_code == 400


@pytest.mark.parametrize("value", [True, False, -1, 0, 1.0, "1", None])
def test_revision_must_be_an_exact_positive_json_integer(tmp_path: Path, value: Any) -> None:
    _, client, _ = http_setup(tmp_path)
    response = client.post("/api/platform/v1/workflows/workflow-a/pause", json={"expected_revision": value, "client_token": "pause"}, headers=ORIGIN)
    assert response.status_code == 400


@pytest.mark.parametrize("raw", [b'{"expected_revision":1,"expected_revision":1,"client_token":"p"}', b'{"expected_revision":NaN,"client_token":"p"}',
                                  b'[]', b'null', b'{', b'{"expected_revision":1e400,"client_token":"p"}'])
def test_raw_json_is_strict_before_mutation(tmp_path: Path, raw: bytes) -> None:
    _, client, setup = http_setup(tmp_path)
    response = client.post("/api/platform/v1/workflows/workflow-a/pause", data=raw, content_type="application/json", headers=ORIGIN)
    assert response.status_code == 400
    assert setup.repository.get(OWNER, "workflow-a").revision == 1


@pytest.mark.parametrize("origin", [None, "https://evil.test", "null", "http://localhost.evil.test", "http://localhost/path"])
def test_writes_require_the_exact_operator_origin(tmp_path: Path, origin: str | None) -> None:
    _, client, _ = http_setup(tmp_path)
    response = client.post("/api/platform/v1/workflows", json=body(), headers={"Origin": origin} if origin is not None else {})
    assert response.status_code == 403


@pytest.mark.parametrize("header", ["X-Runner-Credential", "X-Platform-Node-Credential", "X-Agent-Fleet-Token", "X-Supervisor-Credential", "Authorization"])
def test_machine_credentials_never_become_an_operator_even_with_a_claimed_email(tmp_path: Path, header: str) -> None:
    _, client, _ = http_setup(tmp_path)
    response = client.get("/api/platform/v1/workflows", headers={header: "test", "Cf-Access-Authenticated-User-Email": OWNER})
    assert response.status_code == 401


def test_cross_owner_and_absent_workflow_are_indistinguishable(tmp_path: Path) -> None:
    _, client, _ = http_setup(tmp_path)
    headers = {"Cf-Access-Authenticated-User-Email": "other@example.test"}
    foreign = client.get("/api/platform/v1/workflows/workflow-a", headers=headers)
    missing = client.get("/api/platform/v1/workflows/missing", headers=headers)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.get_json()["error"] == missing.get_json()["error"] == "not_found"
    assert client.get("/api/platform/v1/workflows", headers=headers).get_json()["workflows"] == []


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "limit=1.0", "limit=true", "limit=1&limit=2", "cursor=garbage", "owner_id=other"])
def test_list_query_is_bounded_and_strict(tmp_path: Path, query: str) -> None:
    _, client, _ = http_setup(tmp_path)
    assert client.get("/api/platform/v1/workflows?" + query).status_code == 400


def test_control_cas_receipts_and_pagination_are_durable(tmp_path: Path) -> None:
    _, client, setup = http_setup(tmp_path)
    assert client.post("/api/platform/v1/workflows", json=body(), headers=ORIGIN).status_code == 201
    first = client.get("/api/platform/v1/workflows?limit=1").get_json()
    second = client.get("/api/platform/v1/workflows?limit=1&cursor=" + first["next_cursor"]).get_json()
    assert len({first["workflows"][0]["workflow_id"], second["workflows"][0]["workflow_id"]}) == 2
    operation = {"expected_revision": 1, "client_token": "pause"}
    first = client.post("/api/platform/v1/workflows/workflow-a/pause", json=operation, headers=ORIGIN)
    assert first.status_code == 202
    assert client.post("/api/platform/v1/workflows/workflow-a/pause", json=operation, headers=ORIGIN).get_json() == first.get_json()
    conflict = client.post("/api/platform/v1/workflows/workflow-a/resume", json=operation, headers=ORIGIN)
    assert conflict.status_code == 409
    assert setup.repository.get(OWNER, "workflow-a").pause_requested


def test_acceptance_is_cas_bound_to_current_delivery_digest(tmp_path: Path) -> None:
    _, client, setup = http_setup(tmp_path)
    for _ in range(3):
        setup.tick()
    setup.platform.set_run_state(OWNER, setup.run_id(), "succeeded", now=setup.clock())
    setup.tick()
    detail = client.get("/api/platform/v1/workflows/workflow-a").get_json()["workflow"]
    assert detail["delivery_state"] == "awaiting_acceptance"
    value = {"decision": "accepted", "delivery_digest": detail["delivery_digest"], "expected_revision": detail["revision"], "client_token": "accept"}
    response = client.post("/api/platform/v1/workflows/workflow-a/acceptance", json=value, headers=ORIGIN)
    assert response.status_code == 202, response.get_json()
    assert client.post("/api/platform/v1/workflows/workflow-a/acceptance", json=value, headers=ORIGIN).get_json() == response.get_json()
    assert client.get("/api/platform/v1/workflows/workflow-a").get_json()["workflow"]["delivery_state"] == "delivered"


def test_reconciliation_uses_revision_and_current_generation_without_client_terminal_state(tmp_path: Path) -> None:
    _, client, setup = http_setup(tmp_path)
    for _ in range(3):
        setup.tick()
    setup.platform.set_run_state(OWNER, setup.run_id(), "unknown", now=setup.clock())
    setup.tick()
    revision = setup.repository.get(OWNER, "workflow-a").revision
    value = {"mode": "inspect", "expected_revision": revision, "client_token": "inspect"}
    response = client.post("/api/platform/v1/workflows/workflow-a/steps/develop/reconcile", json=value, headers=ORIGIN)
    assert response.status_code == 202, response.get_json()
    repeated = client.post("/api/platform/v1/workflows/workflow-a/steps/develop/reconcile", json=value, headers=ORIGIN)
    assert repeated.get_json() == response.get_json()
    with setup.repository.transaction(write=False) as connection:
        assert tuple(connection.execute("SELECT generation,mode,state FROM workflow_reconciliation_requests").fetchone()) == (1, "inspect", "pending")
    assert setup.platform.get_run(OWNER, setup.run_id())["state"] == "unknown"


def test_retry_requires_generation_and_replays_the_original_receipt(tmp_path: Path) -> None:
    from dataclasses import replace

    _, client, setup = http_setup(tmp_path)
    setup.registrations = tuple(replace(item, reserve_tokens=20, reserve_requests=0) for item in setup.registrations)
    for _ in range(3):
        setup.tick()
    setup.platform.set_run_state(OWNER, setup.run_id(), "failed", now=setup.clock())
    setup.tick()
    revision = setup.repository.get(OWNER, "workflow-a").revision
    value = {"expected_revision": revision, "expected_generation": 1, "client_token": "retry"}
    response = client.post("/api/platform/v1/workflows/workflow-a/steps/develop/retry", json=value, headers=ORIGIN)
    assert response.status_code == 202, response.get_json()
    assert response.get_json()["operation"]["generations"] == [{"step_key": "develop", "generation": 2}]
    assert client.post("/api/platform/v1/workflows/workflow-a/steps/develop/retry", json=value, headers=ORIGIN).get_json() == response.get_json()


def test_public_events_exclude_dispatch_tokens_and_attempt_details(tmp_path: Path) -> None:
    _, client, setup = http_setup(tmp_path)
    for _ in range(3):
        setup.tick()
    record = setup.repository.get_dispatch(OWNER, "workflow-a", "develop", 1)
    response = client.get("/api/platform/v1/workflows/workflow-a/events").get_json()
    raw = json.dumps(response)
    assert record.token not in raw
    assert "dispatch_token" not in raw and "binding_id" not in raw and "attempt_id" not in raw


def test_budget_totals_above_javascript_precision_are_decimal_strings(tmp_path: Path) -> None:
    _, client, setup = http_setup(tmp_path)
    for _ in range(3):
        setup.tick()
    with setup.repository.transaction() as connection:
        connection.execute("UPDATE workflow_budget_reservations SET reserved_tokens=?", (9007199254740992,))
    detail = client.get("/api/platform/v1/workflows/workflow-a").get_json()["workflow"]
    assert detail["budget"][0]["reserved_tokens"] == "9007199254740992"


def test_oversized_body_and_foreign_cursor_are_rejected_without_mutation(tmp_path: Path) -> None:
    _, client, setup = http_setup(tmp_path, count=2)
    oversized = client.post("/api/platform/v1/workflows", data=b'{"padding":"' + b"x" * 65536 + b'"}', content_type="application/json", headers=ORIGIN)
    assert oversized.status_code == 413
    page = client.get("/api/platform/v1/workflows?limit=1").get_json()
    foreign = client.get("/api/platform/v1/workflows?cursor=" + page["next_cursor"], headers={"Cf-Access-Authenticated-User-Email": "other@example.test"})
    assert foreign.status_code == 400
    assert setup.repository.get(OWNER, "workflow-000").revision == 1


def test_acceptance_rejects_success_with_missing_required_outputs(tmp_path: Path) -> None:
    _, client, setup = http_setup(tmp_path, outputs=True)
    for _ in range(3):
        setup.tick()
    setup.platform.set_run_state(OWNER, setup.run_id(), "succeeded", now=setup.clock())
    setup.tick()
    detail = client.get("/api/platform/v1/workflows/workflow-a").get_json()["workflow"]
    assert detail["delivery_state"] == "awaiting_validation"
    value = {"decision": "accepted", "delivery_digest": detail["delivery_digest"], "expected_revision": detail["revision"], "client_token": "accept"}
    response = client.post("/api/platform/v1/workflows/workflow-a/acceptance", json=value, headers=ORIGIN)
    assert response.status_code == 409 and response.get_json()["error"] == "delivery_not_ready"


def test_delivery_digest_binds_current_report_hash_and_invalidates_acceptance(tmp_path: Path) -> None:
    import hashlib
    import json
    from hub.domain.workflow_contract import parse_definition

    _, client, setup = http_setup(tmp_path)
    for _ in range(3):
        setup.tick()
    setup.platform.set_run_state(OWNER, setup.run_id(), "succeeded", now=setup.clock())
    setup.tick()
    value = json.loads(setup.definition.snapshot)
    value["steps"][0]["contract"]["validator_refs"] = [{"id": "review", "revision": 1}]
    definition = parse_definition(json.dumps(value).encode())
    with setup.repository.transaction() as connection:
        connection.execute("UPDATE workflow_runs SET definition_snapshot=?,definition_digest=? WHERE workflow_id='workflow-a'", (definition.snapshot, hashlib.sha256(definition.snapshot).hexdigest()))
        connection.execute("UPDATE workflow_steps SET subject_digest=? WHERE workflow_id='workflow-a'", ("a" * 64,))
        connection.execute("INSERT INTO workflow_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)", (OWNER, WORKSPACE, "workflow-a", "develop", 1, "review-report", "review@1", "a" * 64, "independent-review", "passed", "report-artifact", "b" * 64, 100, "c" * 64, setup.clock()))
    detail = client.get("/api/platform/v1/workflows/workflow-a").get_json()["workflow"]
    assert detail["validation_state"] == "passed" and detail["delivery_state"] == "awaiting_acceptance"
    acceptance = {"decision": "accepted", "delivery_digest": detail["delivery_digest"], "expected_revision": detail["revision"], "client_token": "accept"}
    assert client.post("/api/platform/v1/workflows/workflow-a/acceptance", json=acceptance, headers=ORIGIN).status_code == 202
    assert client.get("/api/platform/v1/workflows/workflow-a").get_json()["workflow"]["delivery_state"] == "delivered"
    with setup.repository.transaction() as connection:
        connection.execute("UPDATE workflow_evidence SET report_hash=? WHERE workflow_id='workflow-a'", ("d" * 64,))
    changed = client.get("/api/platform/v1/workflows/workflow-a").get_json()["workflow"]
    assert changed["delivery_digest"] != detail["delivery_digest"]
    assert changed["acceptance_state"] == "pending" and changed["delivery_state"] == "awaiting_acceptance"
    assert changed["steps"][0]["evidence"][0]["report"]["sha256"] == "d" * 64
