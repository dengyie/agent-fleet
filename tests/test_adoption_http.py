"""HTTP tests for the operator adoption surface (Task 6).

Covers the three operator routes behind ``@require_operator``:

* ``GET /api/adoptions?machine_id=<machine>``         — operator management view
  (persisted adoption records, bounded projection that NEVER exposes pid /
  pgid / exe_path / cmdline / native_file_path / started_at);
* ``POST /api/adoptions`` ``{"machine_id", "pid", "started_at"}`` -> pending row
  (actor is ``g.operator``; the pid stays probe-private);
* ``DELETE /api/adoptions/<session_id>`` -> 202 once the record is adopted
  (``invalid_adoption`` -> 404, ``invalid_status_transition`` -> 409).

Also nails the auth isolation: the ingest token can never create/list/revoke an
adoption, even though it is the observation-domain credential.
"""
from __future__ import annotations

import pytest

from hub import events as events_mod
from hub import state as store
from hub import task_store
from hub.bootstrap import create_app
from hub.config import FleetConfig

MACHINE = "mac-local"
OPERATOR = "op@example.com"
INGEST = "test-only"
DEFAULT_PID = 4242
DEFAULT_STARTED_AT = "2026-07-30T09:15:00Z"


class FakeObservationRepo:
    """Duck-typed observation repository exposing ``read_current`` only.

    The adoption service reads ONLY the latest sanitized snapshot through this
    interface; the real ``JsonlObservationRepository`` satisfies the same shape.
    """

    def __init__(self, current):
        self._current = current

    def read_current(self, machine):
        return self._current


def instance_row(**overrides):
    """A sanitized ``instances[]`` row (exactly INSTANCE_FIELDS keys)."""
    data = {
        "pid": DEFAULT_PID,
        "pgid": DEFAULT_PID,
        "exe_path": "/usr/local/bin/codex",
        "cmdline": "codex session --tour",
        "agent_family": "codex",
        "native_file_path": None,
        "started_at": DEFAULT_STARTED_AT,
        "attachable": True,
    }
    data.update(overrides)
    return data


def snapshot(rows):
    return {"machine": MACHINE, "instances": rows}


def valid_candidate(**overrides):
    data = {"machine_id": MACHINE, "pid": DEFAULT_PID,
            "started_at": DEFAULT_STARTED_AT}
    data.update(overrides)
    return data


def operator_headers():
    return {"Cf-Access-Authenticated-User-Email": OPERATOR}


def ingest_token_headers():
    return {"X-Agent-Fleet-Token": INGEST}


def runner_headers():
    return {"X-Runner-Credential": f"{MACHINE}:runner-secret"}


def supervisor_headers():
    return {"X-Supervisor-Credential": f"{MACHINE}:sup-secret"}


def build_app(tmp_path, *, adoption_enabled=True,
              append_user_turn_enabled=False,
              apply_local_profile_enabled=False):
    old_state = (store.STATE_DIR, events_mod.EVENT_LOG, task_store.DB_PATH)
    store.STATE_DIR = tmp_path / "state"
    events_mod.EVENT_LOG = tmp_path / "state" / "events.jsonl"
    task_store.DB_PATH = tmp_path / "state" / "fleet.db"
    task_store.init_db()
    cfg = FleetConfig.from_root(
        tmp_path,
        ingest_token=INGEST,
        dev_operator=OPERATOR,
        runner_credentials={MACHINE: "runner-secret"},
        project_whitelist={MACHINE: ["agent-fleet"]},
        supervisor_enabled=True,
        supervisor_credentials={MACHINE: "sup-secret"},
        supervisor_signing_raw=b"\x01" * 32,
        session_repositories_enabled=True,
        session_encryption_raw=b"\x02" * 32,
        adoption_repositories_enabled=adoption_enabled,
        append_user_turn_enabled=append_user_turn_enabled,
        apply_local_profile_enabled=apply_local_profile_enabled,
    )
    observation = FakeObservationRepo(
        {**snapshot([instance_row()]), "machine": MACHINE})
    app = create_app(cfg, repositories={"observation": observation})
    app._fleet_test_cleanup = old_state
    return app


@pytest.fixture
def client(tmp_path):
    app = build_app(tmp_path)
    yield app.test_client()
    store.STATE_DIR, events_mod.EVENT_LOG, task_store.DB_PATH = \
        app._fleet_test_cleanup


# ---------------------------------------------------------------------------
# auth isolation — the ingest token can never reach the operator surface
# ---------------------------------------------------------------------------


def test_adoption_requires_operator_not_ingest(client):
    resp = client.post("/api/adoptions", json=valid_candidate(),
                       headers=ingest_token_headers())
    assert resp.status_code == 401
    body = resp.get_json()
    assert body["error"] == "unauthorized"


def test_ingest_token_cannot_list_adoptions(client):
    resp = client.get("/api/adoptions?machine_id=mac-local",
                      headers=ingest_token_headers())
    assert resp.status_code == 401
    assert not resp.get_json()["ok"]


def test_ingest_token_cannot_revoke_adoption(client):
    resp = client.delete("/api/adoptions/adopt_0000",
                         headers=ingest_token_headers())
    assert resp.status_code == 401


def test_runner_credential_cannot_adopt(client):
    resp = client.post("/api/adoptions", json=valid_candidate(),
                       headers=runner_headers())
    assert resp.status_code == 401
    assert resp.get_json()["error"] == "unauthorized"


def test_supervisor_credential_cannot_adopt(client):
    resp = client.post("/api/adoptions", json=valid_candidate(),
                       headers=supervisor_headers())
    assert resp.status_code == 401
    assert resp.get_json()["error"] == "unauthorized"


def test_runner_credential_cannot_list_adoptions(client):
    resp = client.get("/api/adoptions?machine_id=mac-local",
                      headers=runner_headers())
    assert resp.status_code == 401


def test_supervisor_credential_cannot_list_adoptions(client):
    resp = client.get("/api/adoptions?machine_id=mac-local",
                      headers=supervisor_headers())
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/adoptions
# ---------------------------------------------------------------------------


def test_post_then_delete_enqueues_detach(client):
    created = client.post("/api/adoptions", json=valid_candidate(),
                          headers=operator_headers())
    assert created.status_code == 200
    body = created.get_json()
    assert body["status"] == "pending"
    assert body["adoption_id"]
    assert body["machine_id"] == MACHINE
    session_id = body["session_id"]
    assert session_id.startswith("adopt_")
    assert body["request_id"]

    supervisor = client.application.extensions["fleet"]["services"]["supervisor"]
    commands = supervisor.poll(MACHINE)
    assert any(c["action"] == "adopt" and c["target"]["session_id"] == session_id
               for c in commands)

    # Advance the adoption to ``adopted`` (the Task 7 enploy step) so a revoke
    # is legal; a ``pending`` row must refuse the pending->revoked transition.
    adoptions = client.application.extensions["fleet"]["services"]["adoptions"]
    adoptions.adoption_repo.update_status(session_id, "adopted")

    deleted = client.delete(f"/api/adoptions/{session_id}",
                            headers=operator_headers())
    assert deleted.status_code == 202
    assert deleted.get_json()["session_id"] == session_id

    detached = supervisor.poll(MACHINE)
    assert any(c["action"] == "detach" and c["target"]["session_id"] == session_id
               for c in detached)


def test_post_fields_actor_from_operator(client):
    body = client.post("/api/adoptions", json=valid_candidate(),
                       headers=operator_headers()).get_json()
    record = client.application.extensions["fleet"]["services"] \
        ["adoptions"].adoption_repo.get(body["session_id"])
    assert record.actor == OPERATOR
    assert record.machine_id == MACHINE
    assert record.status == "pending"


def test_post_never_leaks_pid_or_paths(client):
    body = client.post("/api/adoptions", json=valid_candidate(),
                       headers=operator_headers()).get_json()
    text = " ".join(str(v) for v in body.values())
    assert str(DEFAULT_PID) not in text
    assert "/usr/local/bin/codex" not in text
    assert "codex session" not in text
    assert DEFAULT_STARTED_AT not in text


def test_post_stale_candidate_is_bounded_400(client):
    resp = client.post("/api/adoptions", json=valid_candidate(pid=404),
                       headers=operator_headers())
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error"] == "stale_candidate"
    assert not body["ok"]


def test_post_missing_machine_id_is_invalid_request(client):
    resp = client.post("/api/adoptions", json={"pid": DEFAULT_PID,
                                               "started_at": DEFAULT_STARTED_AT},
                       headers=operator_headers())
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "invalid_request"


def test_post_requires_operator_identity(client):
    client.application.config["DEV_OPERATOR"] = None
    resp = client.post("/api/adoptions", json=valid_candidate())
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/adoptions?machine_id=<machine>
# ---------------------------------------------------------------------------


def test_get_requires_machine_id(client):
    resp = client.get("/api/adoptions", headers=operator_headers())
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error"] == "invalid_request"
    assert not body["ok"]


def test_get_empty_machine_returns_empty_items(client):
    resp = client.get("/api/adoptions?machine_id=nobody",
                      headers=operator_headers())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["items"] == []


def test_get_lists_persisted_records_with_bounded_projection(client):
    created = client.post("/api/adoptions", json=valid_candidate(),
                          headers=operator_headers()).get_json()
    resp = client.get(f"/api/adoptions?machine_id={MACHINE}",
                      headers=operator_headers())
    assert resp.status_code == 200
    body = resp.get_json()
    items = body["items"]
    assert len(items) == 1
    item = items[0]
    assert item["session_id"] == created["session_id"]
    assert item["adoption_id"] == created["adoption_id"]
    assert item["machine_id"] == MACHINE
    assert item["status"] == "pending"
    assert "request_id" in body
    # raw probe-private fields must never leave the hub
    for raw in ("pid", "pgid", "exe_path", "cmdline", "native_file_path",
                "started_at"):
        assert raw not in item


# ---------------------------------------------------------------------------
# DELETE /api/adoptions/<session_id>
# ---------------------------------------------------------------------------


def test_delete_unknown_session_is_404(client):
    resp = client.delete("/api/adoptions/adopt_missing",
                         headers=operator_headers())
    assert resp.status_code == 404
    body = resp.get_json()
    assert body["error"] == "invalid_adoption"
    assert not body["ok"]


def test_delete_pending_conflict_is_409(client):
    session_id = client.post("/api/adoptions", json=valid_candidate(),
                             headers=operator_headers()).get_json()["session_id"]
    resp = client.delete(f"/api/adoptions/{session_id}",
                         headers=operator_headers())
    assert resp.status_code == 409
    assert resp.get_json()["error"] == "invalid_status_transition"


def test_delete_bounded_error_never_exposes_paths(client):
    # a path-shaped session id is rejected by the shared id boundary; the
    # public error must not echo it back.
    resp = client.delete("/api/adoptions/adopt_invalid..json",
                         headers=operator_headers())
    assert resp.status_code == 404
    body = resp.get_json()
    assert body["error"] == "invalid_adoption"
    text = " ".join(str(v) for v in body.values())
    assert ".." not in text
    assert ".json" not in text


# ---------------------------------------------------------------------------
# disabled surface stays identical to the pre-adoption hub
# ---------------------------------------------------------------------------


def test_retry_pending_reuses_session_and_requires_operator(client):
    created = client.post("/api/adoptions", json=valid_candidate(),
                          headers=operator_headers()).get_json()
    session_id = created["session_id"]
    denied = client.post(f"/api/adoptions/{session_id}/retry",
                         headers=ingest_token_headers())
    assert denied.status_code == 401
    retried = client.post(f"/api/adoptions/{session_id}/retry",
                          headers=operator_headers())
    assert retried.status_code == 200
    body = retried.get_json()
    assert body["ok"] is True
    assert body["session_id"] == session_id
    assert body["status"] == "pending"
    assert "pid" not in body
    assert "/usr/local/bin/codex" not in " ".join(str(v) for v in body.values())


def test_retry_unknown_session_is_404(client):
    resp = client.post("/api/adoptions/adopt_missing/retry",
                       headers=operator_headers())
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "invalid_adoption"


def test_retry_revoked_is_409(client):
    created = client.post("/api/adoptions", json=valid_candidate(),
                          headers=operator_headers()).get_json()
    session_id = created["session_id"]
    adoptions = client.application.extensions["fleet"]["services"]["adoptions"]
    adoptions.adoption_repo.update_status(session_id, "adopted")
    client.delete(f"/api/adoptions/{session_id}", headers=operator_headers())
    resp = client.post(f"/api/adoptions/{session_id}/retry",
                       headers=operator_headers())
    assert resp.status_code == 409
    assert resp.get_json()["error"] == "adoption_revoked"


def test_adoptions_not_registered_when_disabled(tmp_path):
    app = build_app(tmp_path, adoption_enabled=False)
    try:
        rules = {r.rule for r in app.url_map.iter_rules()}
        assert "/api/adoptions" not in rules
        resp = app.test_client().get("/api/adoptions",
                                     headers=operator_headers())
        assert resp.status_code in (404, 405)
        body = resp.get_json()
        assert body["error"] in ("not_found", "method_not_allowed")
    finally:
        store.STATE_DIR, events_mod.EVENT_LOG, task_store.DB_PATH = \
            app._fleet_test_cleanup