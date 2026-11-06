"""Round-5 regressions for the operator adoption surface (A1-A5).

- A1: a revoke whose detach enqueue fails must leave the seat revocable so
  the operator's retry still issues exactly one detach.
- A2: ``retry`` serializes with ``revoke`` on the admission lock — a revoke
  can never land between the pending check and the adopt enqueue.
- A3: deeply nested JSON is a bounded 400 ``invalid_json``, never a 500.
- A4: ``GET /api/adoptions`` validates ``machine_id`` and maps a store
  failure to a bounded 503.
- A5: source control lives under the Access-protected ``/api/adoptions*``
  prefix; the singular legacy path stays an alias for one release.
"""
from __future__ import annotations

import threading
import time

from hub.application.supervisor_service import SupervisorServiceError
from hub.infrastructure.adoption_repository import AdoptionRepositoryError
from test_adoption_http import (   # sibling import (tests/ is a non-package)
    MACHINE,
    build_app,
    ingest_token_headers,
    operator_headers,
    valid_candidate,
)


def _svc(app):
    return app.extensions["fleet"]["services"]["adoptions"]


def _create(client) -> str:
    resp = client.post("/api/adoptions", json=valid_candidate(),
                       headers=operator_headers())
    assert resp.status_code == 200
    return resp.get_json()["session_id"]


def _record_actions(svc, actions):
    orig = svc.supervisor.enqueue

    def spy(*args, **kwargs):
        actions.append(args[3])
        return orig(*args, **kwargs)
    svc.supervisor.enqueue = spy
    return orig


# -- A1 ----------------------------------------------------------------------


def test_revoke_enqueue_failure_keeps_seat_revocable_and_retry_detaches(tmp_path):
    app = build_app(tmp_path)
    client = app.test_client()
    sid = _create(client)
    svc = _svc(app)
    svc.adoption_repo.update_status(sid, "adopted")
    orig = svc.supervisor.enqueue

    def boom(*_a, **_k):
        raise SupervisorServiceError("supervisor_signing_unavailable", "", 503)
    svc.supervisor.enqueue = boom
    first = client.delete(f"/api/adoptions/{sid}", headers=operator_headers())
    assert first.status_code == 503
    # The failed enqueue did not flip the store: the seat is still adopted.
    assert svc.adoption_repo.get(sid).status == "adopted"

    actions: list = []
    svc.supervisor.enqueue = orig
    _record_actions(svc, actions)
    second = client.delete(f"/api/adoptions/{sid}", headers=operator_headers())
    assert second.status_code == 202
    assert svc.adoption_repo.get(sid).status == "revoked"
    assert actions == ["detach"]

    # Already revoked: idempotent, never a second detach.
    third = client.delete(f"/api/adoptions/{sid}", headers=operator_headers())
    assert third.status_code == 202
    assert actions == ["detach"]


# -- A2 ----------------------------------------------------------------------


def test_retry_and_concurrent_revoke_never_adopt_after_detach(tmp_path):
    app = build_app(tmp_path)
    client = app.test_client()
    sid = _create(client)
    svc = _svc(app)
    repo = svc.adoption_repo
    real_get = repo.get
    in_retry_read = threading.Event()
    revoke_done = threading.Event()
    gate = {"armed": True}

    def slow_get(session_id):
        record = real_get(session_id)
        if gate["armed"] and threading.current_thread().name == "retry":
            gate["armed"] = False
            in_retry_read.set()
            # Give the concurrent revoke every chance to interleave.
            revoke_done.wait(0.5)
        return record
    repo.get = slow_get
    actions: list = []
    _record_actions(svc, actions)

    def do_revoke():
        in_retry_read.wait(5)
        svc.revoke(sid, "op2")
        revoke_done.set()

    errors: list = []

    def do_retry():
        try:
            svc.retry(sid, "op1")
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    t_retry = threading.Thread(target=do_retry, name="retry")
    t_revoke = threading.Thread(target=do_revoke, name="revoke")
    t_retry.start()
    t_revoke.start()
    t_retry.join(10)
    t_revoke.join(10)
    repo.get = real_get
    assert not errors
    # The revoke waited for the retry's admission: adopt first, then detach.
    assert actions == ["adopt", "detach"]
    assert real_get(sid).status == "revoked"


def test_retry_after_revoke_is_rejected_without_enqueue(tmp_path):
    app = build_app(tmp_path)
    client = app.test_client()
    sid = _create(client)
    svc = _svc(app)
    svc.revoke(sid, "op")
    actions: list = []
    _record_actions(svc, actions)
    resp = client.post(f"/api/adoptions/{sid}/retry", headers=operator_headers())
    assert resp.status_code == 409
    assert resp.get_json()["error"] == "adoption_revoked"
    assert actions == []


# -- A3 ----------------------------------------------------------------------


def test_deeply_nested_json_is_bounded_invalid_json(tmp_path):
    client = build_app(tmp_path).test_client()
    body = "[" * 100000 + "]" * 100000
    resp = client.post("/api/adoptions", data=body,
                       content_type="application/json",
                       headers=operator_headers())
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "invalid_json"
    nested_obj = '{"a":' * 20000 + "1" + "}" * 20000
    resp = client.post("/api/adoptions/adopt_x/source/control", data=nested_obj,
                       content_type="application/json",
                       headers=operator_headers())
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "invalid_json"


# -- A4 ----------------------------------------------------------------------


def test_list_rejects_malformed_machine_id(tmp_path):
    client = build_app(tmp_path).test_client()
    for bad in ("A" * 5000, "../etc", "-lead", "a b"):
        resp = client.get("/api/adoptions", query_string={"machine_id": bad},
                          headers=operator_headers())
        assert resp.status_code == 400, bad
        body = resp.get_json()
        assert body["ok"] is False
        assert body["error"] == "invalid_request"
        assert bad not in resp.get_data(as_text=True)


def test_list_store_failure_is_bounded_503(tmp_path):
    app = build_app(tmp_path)
    client = app.test_client()

    def broken(_machine):
        raise AdoptionRepositoryError("adoption_store")
    _svc(app).adoption_repo.list = broken
    resp = client.get(f"/api/adoptions?machine_id={MACHINE}",
                      headers=operator_headers())
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["ok"] is False
    assert body["error"] == "adoption_store"


# -- A5 ----------------------------------------------------------------------


def test_source_control_canonical_route_only(tmp_path):
    app = build_app(tmp_path)
    client = app.test_client()
    sid = _create(client)
    _svc(app).adoption_repo.update_status(sid, "adopted")
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    assert "/api/adoptions/<session_id>/source/control" in rules
    # The one-release singular alias sat outside the Access-protected
    # ``/api/adoptions*`` prefix; it is gone and must not come back.
    assert "/api/adoption/<session_id>/source/control" not in rules
    body = {"action": "pause_session", "reason_code": "operator_requested"}
    resp = client.post(f"/api/adoptions/{sid}/source/control",
                       json=body, headers=operator_headers())
    assert resp.status_code == 202
    payload = resp.get_json()
    assert payload["ok"] is True
    assert payload["action"] == "pause_session"
    assert payload["status"] == "pending"
    unauth = client.post(f"/api/adoptions/{sid}/source/control",
                         json=body, headers=ingest_token_headers())
    assert unauth.status_code == 401
    legacy = client.post(f"/api/adoption/{sid}/source/control",
                         json=body, headers=operator_headers())
    assert legacy.status_code in (404, 405)


def test_admission_lock_is_held_during_retry(tmp_path):
    """White-box guard for A2: retry admission takes the service lock."""
    app = build_app(tmp_path)
    client = app.test_client()
    sid = _create(client)
    svc = _svc(app)
    observed: list = []
    orig = svc.supervisor.enqueue

    def probe(*args, **kwargs):
        # The RLock is reentrant here; prove another thread is blocked.
        holder = {}

        def other():
            holder["got"] = svc._admission_lock.acquire(timeout=0.05)
            if holder["got"]:
                svc._admission_lock.release()
        t = threading.Thread(target=other)
        t.start()
        t.join()
        observed.append(holder["got"])
        return orig(*args, **kwargs)
    svc.supervisor.enqueue = probe
    started = time.monotonic()
    svc.retry(sid, "op")
    assert time.monotonic() - started < 5
    assert observed == [False]


def test_retry_refuses_when_revoke_lands_between_read_and_enqueue(tmp_path):
    """A revoke that slips in after retry's read (same thread, reentrant lock)
    must not be followed by a signed ``adopt``."""
    app = build_app(tmp_path)
    client = app.test_client()
    sid = _create(client)
    svc = _svc(app)
    repo = svc.adoption_repo
    real_get = repo.get
    state = {"armed": True}

    def get_then_revoke(session_id):
        record = real_get(session_id)
        if state["armed"]:
            state["armed"] = False
            svc.revoke(session_id, "op2")
        return record
    repo.get = get_then_revoke
    actions: list = []
    _record_actions(svc, actions)
    resp = client.post(f"/api/adoptions/{sid}/retry", headers=operator_headers())
    repo.get = real_get
    assert resp.status_code == 409
    assert resp.get_json()["error"] == "adoption_revoked"
    assert actions == ["detach"]
    assert real_get(sid).status == "revoked"
