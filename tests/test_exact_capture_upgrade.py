"""HTTP tests for the operator exact-capture upgrade (Task 11).

``POST /api/adoptions/<session_id>/capture-exact`` (PLURAL ``adoptions`` —
this task's route) promotes an ADOPTED session's capture quality to
``exact`` and appends ONE bounded ``capture_exact`` operator audit.  Raw
exact capture reuses the EXISTING transcript machinery — deterministic
redaction + AEAD encrypted raw + quota + retention + raw-read audit — this
surface only flips the stored label.

Optimal wire contract (unified; the field is ``error``, never a second
``code``):

* ``invalid_adoption``          -> 404 (id fails the shared boundary)
* ``unknown_session``           -> 404 (well-formed but never created)
* ``capture_quality_immutable`` -> 409 (a seat still ``pending`` is NOT an
  adopted seat, so the quality label cannot be promoted)
* ``adoption_revoked``          -> 409 (the seat is gone)

The request body is intentionally ignored (a state-transition POST, no
payload echo).  A repeat upgrade on an already-``exact`` adopted seat is
idempotent: 200 again, no second audit.  Foreign auth tokens can never reach
this surface (no DEV fallback).
"""
from __future__ import annotations

import pytest

from hub import events as events_mod
from hub import state as store
from hub import task_store

from test_adoption_http import (   # sibling import (tests/ is a non-package)
    build_app,
    ingest_token_headers,
    operator_headers,
    valid_candidate,
)
from test_session_ingest_api import _event   # the shared valid-event builder

PATH = "/api/adoptions"   # plural ``adoptions``, exactly this task's route


@pytest.fixture
def client(tmp_path):
    app = build_app(tmp_path)
    yield app.test_client()
    store.STATE_DIR, events_mod.EVENT_LOG, task_store.DB_PATH = \
        app._fleet_test_cleanup


def _upgrade(client, session_id):
    return client.post(f"{PATH}/{session_id}/capture-exact",
                       headers=operator_headers())


def _transcripts(client):
    """The SAME TranscriptRepository the AdoptionService audits through."""
    adoptions = client.application.extensions["fleet"]["services"]["adoptions"]
    return adoptions.transcripts


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


def test_upgrade_requires_operator_not_ingest(client):
    resp = client.post(
        f"{PATH}/adopt_00000000000000000000000000000000/capture-exact",
        headers=ingest_token_headers())
    assert resp.status_code == 401
    assert resp.get_json()["error"] == "unauthorized"


# ---------------------------------------------------------------------------
# the happy path — 200, the seat now ``exact``, ONE audited upgrade
# ---------------------------------------------------------------------------


def test_exact_upgrade_is_operator_audited(client):
    session_id = _make_adopted(client)
    resp = _upgrade(client, session_id)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["session_id"] == session_id
    assert body["capture_quality"] == "exact"
    assert body["status"] == "adopted"
    assert body["request_id"]
    assert any(a["action"] == "capture_exact"
               for a in _transcripts(client).read_audit())


# ---------------------------------------------------------------------------
# record-state gates — revoked / pending / unknown seats
# ---------------------------------------------------------------------------


def test_revoked_cannot_upgrade(client):
    session_id = _make_revoked(client)
    resp = _upgrade(client, session_id)
    assert resp.status_code == 409
    assert resp.get_json()["error"] == "adoption_revoked"


def test_pending_not_adopted_cannot_upgrade(client):
    created = client.post("/api/adoptions", json=valid_candidate(),
                          headers=operator_headers()).get_json()
    resp = _upgrade(client, created["session_id"])
    assert resp.status_code == 409
    assert resp.get_json()["error"] == "capture_quality_immutable"


def test_unknown_session_is_404(client):
    resp = _upgrade(client, "adopt_00000000000000000000000000000000")
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "unknown_session"


def test_malformed_session_is_404_bounded(client):
    # a path-shaped session id fails the shared id boundary; the public
    # error must never echo the raw marker or a .json/.db fragment back.
    resp = _upgrade(client, "adopt_invalid..json")
    assert resp.status_code == 404
    body = resp.get_json()
    assert body["error"] == "invalid_adoption"
    text = " ".join(str(v) for v in body.values())
    assert ".." not in text
    assert ".json" not in text


# ---------------------------------------------------------------------------
# idempotency — a repeat upgrade is 200 with NO second audit
# ---------------------------------------------------------------------------


def test_upgrade_idempotent_single_audit(client):
    session_id = _make_adopted(client)
    first = _upgrade(client, session_id)
    assert first.status_code == 200
    second = _upgrade(client, session_id)
    assert second.status_code == 200
    body = second.get_json()
    assert body["ok"] is True
    assert body["capture_quality"] == "exact"
    assert body["status"] == "adopted"
    audit = _transcripts(client).read_audit()
    assert sum(1 for a in audit if a["action"] == "capture_exact") == 1


# ---------------------------------------------------------------------------
# leak surface — the 200 body carries bounded public fields only
# ---------------------------------------------------------------------------


def test_exact_upgrade_response_never_leaks(client):
    session_id = _make_adopted(client)
    resp = _upgrade(client, session_id)
    assert resp.status_code == 200
    body = resp.get_json()
    for raw in ("pid", "pgid", "exe_path", "cmdline", "started_at",
                "signature", "nonce"):
        assert raw not in body


# ---------------------------------------------------------------------------
# raw exact capture — the EXISTING transcript path stays fail-closed
# ---------------------------------------------------------------------------


def test_exact_event_uses_redacted_and_encrypted_raw(client):
    """An exact-quality event stores encrypted raw; live redacted copy
    is passthrough (Route A) so operator query keeps agent text."""
    session_id = _make_adopted(client)
    tr = _transcripts(client)
    evt = _event(1, session_id=session_id, quality="exact",
                 text="password=secret", event_id="evt_1")
    result = tr.ingest(evt)
    assert result.status == "accepted"
    assert result.raw_written is True

    rows = tr.read_redacted(session_id)
    assert rows
    assert rows[0]["payload"]["text"] == "password=secret"

    raw = tr.read_raw(session_id, "evt_1", actor="op")
    assert raw["payload"]["text"] == "password=secret"