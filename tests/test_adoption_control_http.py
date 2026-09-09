"""HTTP tests for the operator source-control surface (Task 10).

One new operator route, ``POST /api/adoption/<session_id>/source/control``
(note the SINGULAR ``adoption`` — exactly the plan's path), accepts ONLY the
five existing source-control action names against an ``adopted`` record and
returns 202 + command id + bounded ``pending``.  The operator can now
PAUSE/RESUME/TERMINATE/QUARANTINE/CANCEL an adopted source through the
existing signed supervisor channel; the probe-side identity guard (Task 9)
remains the final authority before any signal.

Err surface (unified ``body["error"]``, never a second ``code`` field):

* ``invalid_adoption``            -> 404 (unknown / malformed session id);
* ``adoption_revoked``            -> 409 (the seat is gone);
* ``invalid_status_transition``   -> 409 (a still-``pending`` seat);
* ``unsupported_action``          -> 400 (not one of the five fixed tokens);
* ``invalid_request``             -> 400 (missing/blank action or reason).

The route ignores unknown body keys (attacker-sent ``pid``/``signal``/
``shell``/``command`` are silently dropped — never echoed, never signed).
"""
from __future__ import annotations

import pytest

from hub import events as events_mod
from hub import state as store
from hub import task_store

from test_adoption_http import (   # sibling import (tests/ is a non-package)
    DEFAULT_PID,
    DEFAULT_STARTED_AT,
    MACHINE,
    build_app,
    ingest_token_headers,
    operator_headers,
    runner_headers,
    supervisor_headers,
    valid_candidate,
)

PATH = "/api/adoption"   # singular "adoption", exactly the plan's path


@pytest.fixture
def client(tmp_path):
    app = build_app(tmp_path)
    yield app.test_client()
    store.STATE_DIR, events_mod.EVENT_LOG, task_store.DB_PATH = \
        app._fleet_test_cleanup


def _control(client, session_id, action="pause_session",
             reason_code="operator_requested"):
    return client.post(
        f"{PATH}/{session_id}/source/control",
        json={"action": action, "reason_code": reason_code},
        headers=operator_headers())


def _make_adopted(client) -> str:
    """POST a candidate then advance the row to ``adopted`` (the Task 7 step)."""
    created = client.post("/api/adoptions", json=valid_candidate(),
                          headers=operator_headers())
    assert created.status_code == 200
    session_id = created.get_json()["session_id"]
    adoptions = client.application.extensions["fleet"]["services"]["adoptions"]
    adoptions.adoption_repo.update_status(session_id, "adopted")
    return session_id


def _make_revoked(client) -> str:
    """_make_adopted then DELETE (adopted -> revoked via the operator route)."""
    session_id = _make_adopted(client)
    deleted = client.delete(f"/api/adoptions/{session_id}",
                            headers=operator_headers())
    assert deleted.status_code == 202
    return session_id


# ---------------------------------------------------------------------------
# auth isolation — the ingest token can never reach the operator surface
# ---------------------------------------------------------------------------


def test_source_control_requires_operator(client):
    resp = client.post(
        f"{PATH}/adopt_x/source/control",
        json={"action": "pause_session", "reason_code": "operator_requested"},
        headers=ingest_token_headers())
    assert resp.status_code == 401
    assert resp.get_json()["error"] == "unauthorized"


def test_source_control_rejects_runner_credential(client):
    resp = client.post(
        f"{PATH}/adopt_x/source/control",
        json={"action": "pause_session", "reason_code": "operator_requested"},
        headers=runner_headers())
    assert resp.status_code == 401
    assert resp.get_json()["error"] == "unauthorized"


def test_source_control_rejects_supervisor_credential(client):
    resp = client.post(
        f"{PATH}/adopt_x/source/control",
        json={"action": "pause_session", "reason_code": "operator_requested"},
        headers=supervisor_headers())
    assert resp.status_code == 401
    assert resp.get_json()["error"] == "unauthorized"


# ---------------------------------------------------------------------------
# bounded action allowlist — arbitrary / lifecycle actions are refused
# ---------------------------------------------------------------------------


def test_source_control_rejects_arbitrary_action(client):
    session_id = _make_adopted(client)
    resp = _control(client, session_id, action="exec_shell",
                    reason_code="x")
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "unsupported_action"


def test_source_control_rejects_adopt_and_detach_actions(client):
    session_id = _make_adopted(client)
    for bad in ("adopt", "detach"):
        resp = _control(client, session_id, action=bad, reason_code="x")
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "unsupported_action"


# ---------------------------------------------------------------------------
# record-state gates — revoked / pending / unknown seats
# ---------------------------------------------------------------------------


def test_source_control_revoked_session_refused(client):
    session_id = _make_revoked(client)
    resp = _control(client, session_id, action="terminate_session")
    assert resp.status_code == 409
    assert resp.get_json()["error"] == "adoption_revoked"


def test_source_control_before_adopted_conflict(client):
    created = client.post("/api/adoptions", json=valid_candidate(),
                          headers=operator_headers()).get_json()
    resp = _control(client, created["session_id"], action="pause_session")
    assert resp.status_code == 409
    assert resp.get_json()["error"] == "invalid_status_transition"


def test_source_control_unknown_session_is_404(client):
    resp = _control(client, "adopt_missing", action="pause_session")
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "invalid_adoption"


def test_source_control_malformed_session_is_404(client):
    # a path-shaped session id fails the shared id boundary; the public error
    # must never echo the raw marker back.
    resp = _control(client, "adopt_invalid..json", action="pause_session")
    assert resp.status_code == 404
    body = resp.get_json()
    assert body["error"] == "invalid_adoption"
    text = " ".join(str(v) for v in body.values())
    assert ".." not in text
    assert ".json" not in text


# ---------------------------------------------------------------------------
# the happy path — exactly one signed enqueue through the supervisor channel
# ---------------------------------------------------------------------------


def test_source_control_success_enqueues_one_signed_command(client):
    session_id = _make_adopted(client)
    supervisor = client.application.extensions["fleet"]["services"]["supervisor"]

    resp = _control(client, session_id, action="pause_session",
                    reason_code="operator_requested")
    assert resp.status_code == 202
    body = resp.get_json()
    assert body["ok"] is True
    assert body["command_id"]
    assert body["session_id"] == session_id
    assert body["action"] == "pause_session"
    assert body["status"] == "pending"
    assert body["request_id"]

    commands = supervisor.poll(MACHINE)
    pause = [c for c in commands
             if c["action"] == "pause_session"
             and c["target"]["session_id"] == session_id]
    assert len(pause) == 1
    # the OTHER source-control actions stay absent from the poll
    assert not any(c["action"] in ("resume_session", "terminate_session",
                                   "quarantine_session", "cancel_attempt",
                                   "detach")
                   for c in commands)

    # a second identical POST is NOT idempotent at the command level — the
    # operator asked twice, so two distinct signed commands were issued.
    second = _control(client, session_id, action="pause_session",
                      reason_code="operator_requested")
    assert second.status_code == 202
    body2 = second.get_json()
    assert body2["ok"] is True
    assert body2["command_id"] != body["command_id"]


# ---------------------------------------------------------------------------
# leak surface — a 202 body / every 4xx body stays bounded
# ---------------------------------------------------------------------------


def test_source_control_response_never_leaks(client):
    session_id = _make_adopted(client)
    texts = []

    ok = _control(client, session_id, action="pause_session",
                  reason_code="operator_requested")
    assert ok.status_code == 202
    texts.append(" ".join(str(v) for v in ok.get_json().values()))

    # a success payload smuggling raw pid / shell / command / signal keys must
    # NEVER be echoed back (the route reads only action + reason_code)
    smuggled = client.post(
        f"{PATH}/{session_id}/source/control",
        json={"action": "pause_session",
              "reason_code": "operator_requested",
              "pid": DEFAULT_PID, "shell": "/bin/sh",
              "command": "codex session --tour",
              "signal": "SIGKILL"},
        headers=operator_headers())
    assert smuggled.status_code == 202
    texts.append(" ".join(str(v) for v in smuggled.get_json().values()))

    # a run-down of the 4xx bodies
    # unsupported_action 400 (adopted seat, arbitrary action)
    resp = _control(client, session_id, action="exec_shell", reason_code="x")
    assert resp.status_code == 400
    texts.append(" ".join(str(v) for v in resp.get_json().values()))

    # adoption_revoked 409 (a separately-revoked seat)
    revoked_id = _make_revoked(client)
    resp = _control(client, revoked_id, action="terminate_session")
    assert resp.status_code == 409
    texts.append(" ".join(str(v) for v in resp.get_json().values()))

    # invalid_adoption 404 (a truly unknown seat)
    resp = _control(client, "adopt_missing", action="pause_session")
    assert resp.status_code == 404
    texts.append(" ".join(str(v) for v in resp.get_json().values()))

    joined = " ".join(texts)
    assert str(DEFAULT_PID) not in joined
    assert "/usr/local/bin/codex" not in joined
    assert "codex session" not in joined
    assert DEFAULT_STARTED_AT not in joined
    assert "/bin/sh" not in joined
    assert "SIGKILL" not in joined


# ---------------------------------------------------------------------------
# missing / blank body fields
# ---------------------------------------------------------------------------


def test_source_control_missing_action_or_reason(client):
    session_id = _make_adopted(client)

    # missing action
    resp = client.post(f"{PATH}/{session_id}/source/control",
                       json={"reason_code": "x"}, headers=operator_headers())
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "invalid_request"

    # blank action
    resp = client.post(f"{PATH}/{session_id}/source/control",
                       json={"action": "   ", "reason_code": "x"},
                       headers=operator_headers())
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "invalid_request"

    # missing reason_code
    resp = client.post(f"{PATH}/{session_id}/source/control",
                       json={"action": "pause_session"},
                       headers=operator_headers())
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "invalid_request"

    # blank reason_code
    resp = client.post(f"{PATH}/{session_id}/source/control",
                       json={"action": "pause_session", "reason_code": ""},
                       headers=operator_headers())
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "invalid_request"


# ---------------------------------------------------------------------------
# Phase 4/5 — default-off issuance; extra HTTP keys never signed
# ---------------------------------------------------------------------------


def test_phase45_actions_default_off(client):
    session_id = _make_adopted(client)
    for action, payload in (
        ("append_user_turn", {"text": "hello"}),
        ("apply_local_profile", {"profile_id": "local-1"}),
    ):
        resp = client.post(
            f"{PATH}/{session_id}/source/control",
            json={"action": action, "reason_code": "operator_requested",
                  "payload": payload},
            headers=operator_headers())
        assert resp.status_code == 403
        assert resp.get_json()["error"] == "feature_disabled"


def test_phase45_actions_enqueue_when_enabled(tmp_path):
    app = build_app(tmp_path, append_user_turn_enabled=True,
                    apply_local_profile_enabled=True)
    client = app.test_client()
    try:
        session_id = _make_adopted(client)
        turn = client.post(
            f"{PATH}/{session_id}/source/control",
            json={"action": "append_user_turn",
                  "reason_code": "operator_requested",
                  "payload": {"text": "hello"},
                  "pid": DEFAULT_PID, "signal": "SIGKILL"},
            headers=operator_headers())
        assert turn.status_code == 202
        body = turn.get_json()
        assert body["action"] == "append_user_turn"
        assert body["status"] == "pending"
        joined = " ".join(str(v) for v in body.values())
        assert str(DEFAULT_PID) not in joined
        assert "SIGKILL" not in joined

        profile = client.post(
            f"{PATH}/{session_id}/source/control",
            json={"action": "apply_local_profile",
                  "reason_code": "operator_requested",
                  "payload": {"profile_id": "local-1"}},
            headers=operator_headers())
        assert profile.status_code == 202
        assert profile.get_json()["action"] == "apply_local_profile"

        supervisor = client.application.extensions["fleet"]["services"]["supervisor"]
        queued = supervisor.poll(MACHINE)
        turn_cmd = next(c for c in queued if c["action"] == "append_user_turn")
        assert turn_cmd["payload"] == {"text": "hello"}
        assert "pid" not in turn_cmd["payload"]
        profile_cmd = next(
            c for c in queued if c["action"] == "apply_local_profile")
        assert profile_cmd["payload"] == {"profile_id": "local-1"}
    finally:
        store.STATE_DIR, events_mod.EVENT_LOG, task_store.DB_PATH = \
            app._fleet_test_cleanup


def test_phase45_payload_rejects_secrets(tmp_path):
    app = build_app(tmp_path, append_user_turn_enabled=True,
                    apply_local_profile_enabled=True)
    client = app.test_client()
    try:
        session_id = _make_adopted(client)
        resp = client.post(
            f"{PATH}/{session_id}/source/control",
            json={"action": "append_user_turn",
                  "reason_code": "operator_requested",
                  "payload": {"text": "hi", "api_key": "secret"}},
            headers=operator_headers())
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "invalid_payload"
        assert "secret" not in str(resp.get_json())
    finally:
        store.STATE_DIR, events_mod.EVENT_LOG, task_store.DB_PATH = \
            app._fleet_test_cleanup