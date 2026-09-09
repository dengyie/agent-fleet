"""Tests for the explicit AdoptionService use cases (Task 5).

The service turns a discovered, sanitized candidate into a pending adoption
("纳管"): it reads only the latest sanitized snapshot, validates the candidate
against fixed bounded codes, persists a pending adoption row in the isolated
adoption store, appends a code-only audit trace, and signs exactly one
``adopt`` control command through the injected supervisor.

Coverage:

- ``list_candidates`` is a pure discovery view (every row, no filtering) and
  degrades to an empty list when the latest snapshot is missing;
- ``adopt`` rejects stale / unknown candidates with ``stale_candidate``,
  requires ``attachable``, and rejects out-of-set ``agent_family`` values;
- a successful adopt persists a ``pending`` record, appends ``adopt_session``
  to the transcript audit, and enqueues a single ``adopt`` command with no
  signal field;
- adopt is idempotent: an existing pending/adopted record for
  ``(machine_id, pid, started_at)`` is returned without a new session id,
  a new command, or a new audit;
- ``revoke`` transitions the record, audits ``revoke_session`` and enqueues a
  single ``detach`` command; an idempotent second revoke does not re-enqueue;
- all errors carry a bounded code only: ``str(exc) == exc.code`` and never a
  path, PID, command line, or raw text.
"""

import threading
from pathlib import Path

import pytest

from hub.application.adoption_service import (
    _RECONCILE_AUDIT_ACTION,
    AdoptionService,
    AdoptionServiceError,
    Candidate,
)
from hub.application.supervisor_service import SupervisorServiceError
from hub.domain.adoption import Adoption
from hub.infrastructure.adoption_repository import (
    AdoptionRepository,
    AdoptionRepositoryError,
)
from hub.infrastructure.transcript_repository import TranscriptRepository
from session_schema import is_valid_session_id

# ---------------------------------------------------------------------------
# shared factories (used once, the same style as the repo test helpers)
# ---------------------------------------------------------------------------

DEFAULT_PID = 4242
DEFAULT_STARTED_AT = "2026-07-30T09:15:00Z"


def candidate_dict(**overrides):
    """A valid candidate view with bounded, path-free fields."""
    data = {
        "machine_id": "m1",
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
    """A sanitized machine snapshot carrying an instances[] list."""
    return {"machine": "m1", "instances": rows}


# ---------------------------------------------------------------------------
# infrastructure doublets
# ---------------------------------------------------------------------------


class FakeObservationRepo:
    """Duck-typed observation repository: ``read_current`` only."""

    def __init__(self, current=None):
        self._current = current

    def read_current(self, machine):
        return self._current


class RecordingSupervisor:
    """Records every ``enqueue`` call (the Task 4 adoption repo shape)."""

    def __init__(self):
        self.commands = []
        self.fail_with = None

    def enqueue(self, machine, session_id, attempt_id, action, reason_code,
                *, nonce=None, candidate=None, payload=None):
        if self.fail_with is not None:
            err = self.fail_with
            self.fail_with = None
            raise err
        rec = {
            "machine": machine,
            "session_id": session_id,
            "attempt_id": attempt_id,
            "action": action,
            "reason_code": reason_code,
            "nonce": nonce,
        }
        # mirror the real SupervisorService: candidate only rides an adopt
        if candidate is not None:
            rec["candidate"] = candidate
        if payload is not None:
            rec["payload"] = payload
        self.commands.append(rec)
        return {"ok": True, "command_id": f"cmd-{len(self.commands)}"}


def make_service(tmp_path: Path, current=None) -> AdoptionService:
    """Build a fully wired AdoptionService against fresh temp stores."""
    adoption_repo = AdoptionRepository(tmp_path / "adoptions.db")
    adoption_repo.init()
    transcript_repo = TranscriptRepository(tmp_path / "transcripts.db")
    transcript_repo.init()
    return AdoptionService(
        FakeObservationRepo(current),
        adoption_repo,
        RecordingSupervisor(),
        transcript_repo,
    )


@pytest.fixture
def candidate():
    return Candidate(**candidate_dict())


@pytest.fixture
def service(tmp_path, candidate):
    return make_service(
        tmp_path, {**snapshot([instance_row()]), "machine": candidate.machine_id}
    )


# ---------------------------------------------------------------------------
# list_candidates — pure discovery view
# ---------------------------------------------------------------------------


def test_list_candidates_without_snapshot_is_empty(tmp_path):
    service = make_service(tmp_path, None)
    assert service.list_candidates("m1") == []
    service2 = make_service(
        tmp_path, {"machine": "m1", "reachable": True})  # no instances key
    assert service2.list_candidates("m1") == []


def test_list_candidates_is_pure_discovery_view(tmp_path):
    service = make_service(tmp_path, snapshot([
        instance_row(),
        instance_row(pid=9, pgid=0, agent_family="hermes", attachable=False),
    ]))
    rows = service.list_candidates("m1")
    by_pid = {c.pid: c for c in rows}
    assert set(by_pid) == {DEFAULT_PID, 9}
    # a non-attachable / different-family row is still listed (no filtering)
    assert by_pid[9].attachable is False
    assert by_pid[9].agent_family == "hermes"
    assert by_pid[9].pgid == 0


# ---------------------------------------------------------------------------
# Step 1 brief tests
# ---------------------------------------------------------------------------


def test_adopt_rejects_stale_candidate(service):
    with pytest.raises(AdoptionServiceError) as exc:
        service.adopt("m1", 404, "2026-07-30T09:15:00Z", "op")
    assert exc.value.code == "stale_candidate"
    assert str(exc.value) == "stale_candidate"


def test_adopt_enqueues_and_audits(service, candidate):
    row = service.adopt("m1", candidate.pid, candidate.started_at, "op-1")
    assert row.status == "pending"
    assert service.supervisor.commands[-1]["action"] == "adopt"
    assert row.session_id.startswith("adopt_")
    assert is_valid_session_id(row.session_id)
    assert any(x["action"] == "adopt_session"
               for x in service.transcripts.read_audit())


def test_adopt_is_idempotent(service, candidate):
    first = service.adopt("m1", candidate.pid, candidate.started_at, "op-1")
    count = len(service.supervisor.commands)
    second = service.adopt("m1", candidate.pid, candidate.started_at, "op-2")
    assert second.session_id == first.session_id
    assert len(service.supervisor.commands) == count


# ---------------------------------------------------------------------------
# adopt — validation and persistence details
# ---------------------------------------------------------------------------


def test_adopt_persists_pending_best_effort_record(service, candidate):
    row = service.adopt("m1", candidate.pid, candidate.started_at, "op-1")
    stored = service.adoption_repo.get(row.session_id)
    assert stored.session_id == row.session_id
    assert stored.status == "pending"
    assert stored.capture_quality == "best_effort"
    assert stored.machine_id == "m1"
    assert stored.pid == candidate.pid


def test_adopt_rejects_stale_when_snapshot_missing(tmp_path, candidate):
    service = make_service(tmp_path, None)
    with pytest.raises(AdoptionServiceError) as exc:
        service.adopt("m1", candidate.pid, candidate.started_at, "op")
    assert exc.value.code == "stale_candidate"


def test_adopt_rejects_stale_on_started_at_mismatch(tmp_path, candidate):
    service = make_service(
        tmp_path, snapshot([instance_row(started_at="2026-07-30T09:16:00Z")]))
    with pytest.raises(AdoptionServiceError) as exc:
        service.adopt("m1", candidate.pid, candidate.started_at, "op")
    assert exc.value.code == "stale_candidate"


def test_adopt_rejects_not_attachable(tmp_path, candidate):
    service = make_service(
        tmp_path, snapshot([instance_row(attachable=False)]))
    with pytest.raises(AdoptionServiceError) as exc:
        service.adopt("m1", candidate.pid, candidate.started_at, "op")
    assert exc.value.code == "not_attachable"


def test_adopt_rejects_unsupported_family(tmp_path, candidate):
    service = make_service(
        tmp_path, snapshot([instance_row(agent_family="mystery")]))
    with pytest.raises(AdoptionServiceError) as exc:
        service.adopt("m1", candidate.pid, candidate.started_at, "op")
    assert exc.value.code == "unsupported_candidate"


def test_adopt_defensively_rejects_non_positive_pid(tmp_path):
    # a row can never survive sanitize_instances with pid <= 0; if a duck-typed
    # repo still surfaces one, the service treats it as stale (no dedicated code)
    service = make_service(
        tmp_path, snapshot([instance_row(pid=0, started_at="2026-07-30T00:00:00Z")]))
    with pytest.raises(AdoptionServiceError) as exc:
        service.adopt("m1", 0, "2026-07-30T00:00:00Z", "op")
    assert exc.value.code == "stale_candidate"


def test_adopt_issue_exactly_one_command(service, candidate):
    service.adopt("m1", candidate.pid, candidate.started_at, "op-1")
    adopt_cmds = [c for c in service.supervisor.commands if c["action"] == "adopt"]
    assert len(adopt_cmds) == 1
    cmd = adopt_cmds[0]
    assert cmd["reason_code"] == "operator_adopt"
    assert cmd["attempt_id"] is None
    assert cmd["machine"] == "m1"
    assert cmd["session_id"].startswith("adopt_")
    assert cmd["nonce"]
    # the envelope has no signal field
    assert "signal" not in cmd
    assert set(cmd) == {"machine", "session_id", "attempt_id", "action",
                        "reason_code", "nonce", "candidate"}


def test_adopt_envelope_carries_bounded_candidate(service, candidate):
    row = service.adopt("m1", candidate.pid, candidate.started_at, "op-1")
    cand = service.supervisor.commands[-1]["candidate"]
    # the signed adopt command rides the persisted discovery identity only —
    # no cmdline/pgid/acquisition extras, no signal surface.
    assert set(cand) == {"pid", "started_at", "exe_path", "agent_family",
                         "native_file_path"}
    assert cand["pid"] == candidate.pid
    assert cand["started_at"] == candidate.started_at
    assert cand["exe_path"] == candidate.exe_path
    assert cand["agent_family"] == candidate.agent_family
    assert cand["native_file_path"] is None
    assert "cmdline" not in cand
    assert "pgid" not in cand
    assert "signal" not in cand


def test_adopt_audit_is_code_only(service, candidate):
    row = service.adopt("m1", candidate.pid, candidate.started_at, "op-1")
    entries = [x for x in service.transcripts.read_audit()
               if x["action"] == "adopt_session"]
    assert len(entries) == 1
    entry = entries[0]
    # the target is the opaque session id, never the raw pid / path / cmdline
    assert entry["target"] == row.session_id
    for leaked in (str(candidate.cmdline), str(candidate.exe_path),
                   DEFAULT_STARTED_AT):
        assert leaked not in (entry["detail"] or "")


def test_adopt_reuses_existing_row_without_extra_audit(service, candidate):
    first = service.adopt("m1", candidate.pid, candidate.started_at, "op-1")
    audit_count = len([
        x for x in service.transcripts.read_audit()
        if x["action"] == "adopt_session"
    ])
    second = service.adopt("m1", candidate.pid, candidate.started_at, "op-2")
    assert second.session_id == first.session_id
    after = [
        x for x in service.transcripts.read_audit()
        if x["action"] == "adopt_session"
    ]
    assert len(after) == audit_count


# ---------------------------------------------------------------------------
# revoke — detach once, idempotent afterwards
# ---------------------------------------------------------------------------


def _adopted_record(service, candidate) -> Adoption:
    row = service.adopt("m1", candidate.pid, candidate.started_at, "op-1")
    return service.adoption_repo.update_status(row.session_id, "adopted")


def test_revoke_enqueues_detach_and_audits(service, candidate):
    row = _adopted_record(service, candidate)
    service.revoke(row.session_id, "op-1")
    tail = service.supervisor.commands[-1]
    assert tail["action"] == "detach"
    assert tail["reason_code"] == "operator_detach"
    assert tail["machine"] == "m1"
    assert tail["session_id"] == row.session_id
    assert any(x["action"] == "revoke_session"
               for x in service.transcripts.read_audit())
    assert service.adoption_repo.get(row.session_id).status == "revoked"


def test_revoke_is_idempotent_no_second_detach(service, candidate):
    row = _adopted_record(service, candidate)
    service.revoke(row.session_id, "op-1")
    detach_count = sum(1 for c in service.supervisor.commands
                       if c["action"] == "detach")
    assert detach_count == 1
    service.revoke(row.session_id, "op-2")
    assert sum(1 for c in service.supervisor.commands
               if c["action"] == "detach") == detach_count


def test_revoke_unknown_session_is_bounded(service):
    with pytest.raises(AdoptionServiceError) as exc:
        service.revoke("adopt_missing", "op")
    assert exc.value.code == "invalid_adoption"
    assert str(exc.value) == "invalid_adoption"


def test_revoke_pending_to_revoked_is_blocked(service, candidate):
    row = service.adopt("m1", candidate.pid, candidate.started_at, "op-1")
    with pytest.raises(AdoptionServiceError) as exc:
        service.revoke(row.session_id, "op-1")
    assert exc.value.code == "invalid_status_transition"


def test_revoke_does_not_touch_signal_field(service, candidate):
    row = _adopted_record(service, candidate)
    service.revoke(row.session_id, "op-1")
    cmd = service.supervisor.commands[-1]
    assert "signal" not in cmd
    assert set(cmd) == {"machine", "session_id", "attempt_id", "action",
                        "reason_code", "nonce"}


# ---------------------------------------------------------------------------
# reconcile_revoked — probe-guard refusal reconciliation (Task 9)
# ---------------------------------------------------------------------------


def test_reconcile_revoked_transitions_to_revoked_and_audits(service, candidate):
    row = _adopted_record(service, candidate)
    result = service.reconcile_revoked(
        row.session_id, reason="pid_reused", actor="probe")
    assert result == {"session_id": row.session_id, "reconciled": True,
                      "status": "revoked"}
    assert service.adoption_repo.get(row.session_id).status == "revoked"
    entries = [x for x in service.transcripts.read_audit()
               if x["action"] == _RECONCILE_AUDIT_ACTION]
    assert len(entries) == 1
    assert entries[0]["target"] == row.session_id
    detail = entries[0]["detail"] or ""
    assert "pid_reused" in detail
    assert "probe_reject" in detail
    # the probe side already released the entry - NEVER a new command
    assert not any(c["action"] == "detach"
                   for c in service.supervisor.commands)


def test_reconcile_revoked_idempotence_single_transition(service, candidate):
    row = _adopted_record(service, candidate)
    first = service.reconcile_revoked(
        row.session_id, reason="exe_changed", actor="probe")
    assert first["reconciled"] is True
    assert first["status"] == "revoked"
    audit_count = len([x for x in service.transcripts.read_audit()
                       if x["action"] == _RECONCILE_AUDIT_ACTION])
    second = service.reconcile_revoked(
        row.session_id, reason="exe_changed", actor="probe")
    # already revoked -> bounded no-op, no second transition, no second audit
    assert second == {"session_id": row.session_id, "reconciled": False,
                      "status": "ignored"}
    assert len([x for x in service.transcripts.read_audit()
                if x["action"] == _RECONCILE_AUDIT_ACTION]) == audit_count
    assert service.adoption_repo.get(row.session_id).status == "revoked"


def test_reconcile_unknown_session_is_noop(service):
    result = service.reconcile_revoked(
        "adopt_missing", reason="pid_reused", actor="probe")
    assert result == {"session_id": "adopt_missing", "reconciled": False,
                      "status": "ignored"}
    assert service.adoption_repo.list("m1") == []


def test_reconcile_revoked_pending_is_noop(service, candidate):
    # the domain forbids ``pending -> revoked`` directly, so a probe guard
    # refusal on a still-pending record is a bounded no-op (no transition, no
    # audit) — the idempotent/no-op contract holds instead of a raise.
    row = service.adopt("m1", candidate.pid, candidate.started_at, "op-1")
    assert row.status == "pending"
    result = service.reconcile_revoked(
        row.session_id, reason="pid_reused", actor="probe")
    assert result == {"session_id": row.session_id, "reconciled": False,
                      "status": "ignored"}
    assert service.adoption_repo.get(row.session_id).status == "pending"
    assert not any(x["action"] == _RECONCILE_AUDIT_ACTION
                   for x in service.transcripts.read_audit())


def test_reconcile_revoked_already_revoked_is_noop(service, candidate):
    row = _adopted_record(service, candidate)
    service.adoption_repo.update_status(row.session_id, "revoked")
    result = service.reconcile_revoked(
        row.session_id, reason="no_permission", actor="probe")
    assert result["reconciled"] is False
    assert result["status"] == "ignored"
    assert service.adoption_repo.get(row.session_id).status == "revoked"


def test_reconcile_revoked_non_guard_reason_is_noop(service, candidate):
    row = _adopted_record(service, candidate)
    result = service.reconcile_revoked(
        row.session_id, reason="operator_cried", actor="probe")
    assert result["reconciled"] is False
    assert result["status"] == "ignored"
    # the stored record is untouched
    assert service.adoption_repo.get(row.session_id).status == "adopted"
    assert not any(x["action"] == _RECONCILE_AUDIT_ACTION
                   for x in service.transcripts.read_audit())


def test_reconcile_revoked_audit_never_carries_identity(service, candidate):
    row = _adopted_record(service, candidate)
    service.reconcile_revoked(row.session_id, reason="pid_reused", actor="probe")
    detail = (service.transcripts.read_audit()[-1].get("detail") or "")
    for leaked in (str(candidate.pid), candidate.started_at, candidate.exe_path):
        assert leaked not in detail


# ---------------------------------------------------------------------------
# bounded error surface
# ---------------------------------------------------------------------------


def test_service_error_is_code_only():
    err = AdoptionServiceError("stale_candidate")
    assert err.code == "stale_candidate"
    assert str(err) == "stale_candidate"


# ---------------------------------------------------------------------------
# Fix I2 — concurrent scan-then-upsert race resolves to the existing row
# ---------------------------------------------------------------------------


class _OnceRaceRepo:
    """Repository double that simulates another seat winning the race once.

    The FIRST ``upsert`` lands the OTHER seat's active row for the same
    identity directly into the inner store (so the service's earlier
    idempotency scan saw nothing) and then raises the IntegrityError
    translation — exactly what the repository's unique active-row index would
    surface on a real concurrent adopt.  Every later call forwards untouched.
    """

    def __init__(self, inner):
        self._inner = inner
        self._raced = False

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def upsert(self, adoption):
        if not self._raced:
            self._raced = True
            winner = Adoption(
                adoption_id="adp_winner_x",
                machine_id=adoption.machine_id,
                session_id="adopt_winner_x",
                pid=adoption.pid,
                pgid=adoption.pgid,
                started_at=adoption.started_at,
                exe_path=adoption.exe_path,
                agent_family=adoption.agent_family,
                native_file_path=adoption.native_file_path,
                status="pending",
                capture_quality=adoption.capture_quality,
                actor="other-seat",
                created_at=adoption.created_at,
                updated_at=adoption.updated_at,
            )
            self._inner.upsert(winner)
            raise AdoptionRepositoryError("invalid_adoption")
        return self._inner.upsert(adoption)


def test_adopt_race_returns_existing_row_with_no_duplicate_effects(tmp_path):
    service = make_service(tmp_path, snapshot([instance_row()]))
    service.adoption_repo = _OnceRaceRepo(service.adoption_repo)
    result = service.adopt("m1", DEFAULT_PID, DEFAULT_STARTED_AT, "op-1")
    # the winning seat's existing row is resolved: exactly ONE row / session id
    assert result.session_id == "adopt_winner_x"
    assert result.status == "pending"
    assert len(service.adoption_repo.list("m1")) == 1
    assert service.adoption_repo.get("adopt_winner_x") is not None
    # this loser-side invocation enqueued NO audit and NO second command
    assert [a for a in service.transcripts.read_audit()
            if a["action"] == "adopt_session"] == []
    assert [c for c in service.supervisor.commands
            if c["action"] == "adopt"] == []
    # a repeat adopt for the same identity stays idempotent (same session)
    again = service.adopt("m1", DEFAULT_PID, DEFAULT_STARTED_AT, "op-1")
    assert again.session_id == "adopt_winner_x"
    assert len(service.adoption_repo.list("m1")) == 1


# ---------------------------------------------------------------------------
# enqueue failure recovery + explicit retry
# ---------------------------------------------------------------------------


def test_enqueue_failure_does_not_leave_unrecoverable_pending(service, candidate):
    service.supervisor.fail_with = SupervisorServiceError("transport_error", "", 503)
    with pytest.raises(SupervisorServiceError) as exc:
        service.adopt("m1", candidate.pid, candidate.started_at, "op-1")
    assert exc.value.code == "transport_error"
    assert service.adoption_repo.list("m1") == []
    assert service.supervisor.commands == []


def test_retry_pending_reuses_session_and_enqueues_once(service, candidate):
    service.supervisor.fail_with = SupervisorServiceError("transport_error", "", 503)
    with pytest.raises(SupervisorServiceError):
        service.adopt("m1", candidate.pid, candidate.started_at, "op-1")
    # Simulate a leftover pending row (pre-fix stores, or a crash after upsert
    # and before the compensating delete) so retry has a seat to recover.
    leftover = Adoption(
        adoption_id="adp_retry_leftover",
        machine_id="m1",
        session_id="adopt_retry_same",
        pid=candidate.pid,
        pgid=candidate.pgid,
        started_at=candidate.started_at,
        exe_path=candidate.exe_path,
        agent_family=candidate.agent_family,
        native_file_path=candidate.native_file_path,
        status="pending",
        capture_quality="best_effort",
        actor="op-1",
        created_at="2026-01-01T00:00:00.000000Z",
        updated_at="2026-01-01T00:00:00.000000Z",
    )
    service.adoption_repo.upsert(leftover)
    result = service.retry(leftover.session_id, "op-2")
    assert result.session_id == leftover.session_id
    assert result.status == "pending"
    adopt_cmds = [c for c in service.supervisor.commands if c["action"] == "adopt"]
    assert len(adopt_cmds) == 1
    assert adopt_cmds[0]["session_id"] == leftover.session_id
    assert adopt_cmds[0]["nonce"]
    again = service.retry(leftover.session_id, "op-2")
    assert again.session_id == leftover.session_id
    assert len([c for c in service.supervisor.commands if c["action"] == "adopt"]) == 2


def test_retry_adopted_is_idempotent_no_second_command(service, candidate):
    row = _adopted_record(service, candidate)
    count = len(service.supervisor.commands)
    again = service.retry(row.session_id, "op-2")
    assert again.session_id == row.session_id
    assert again.status == "adopted"
    assert len(service.supervisor.commands) == count


def test_retry_revoked_is_bounded(service, candidate):
    row = _adopted_record(service, candidate)
    service.revoke(row.session_id, "op-1")
    with pytest.raises(AdoptionServiceError) as exc:
        service.retry(row.session_id, "op-2")
    assert exc.value.code == "adoption_revoked"


def test_retry_unknown_session_is_bounded(service):
    with pytest.raises(AdoptionServiceError) as exc:
        service.retry("adopt_missing", "op")
    assert exc.value.code == "invalid_adoption"


# ---------------------------------------------------------------------------
# concurrent revoke / drift — exactly one detach
# ---------------------------------------------------------------------------


def test_concurrent_revoke_enqueues_one_detach(service, candidate):
    row = _adopted_record(service, candidate)
    barrier = threading.Barrier(2)
    errors = []

    def run():
        barrier.wait()
        try:
            service.revoke(row.session_id, "op")
        except Exception as exc:  # pragma: no cover - unexpected
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert service.adoption_repo.get(row.session_id).status == "revoked"
    assert sum(1 for c in service.supervisor.commands
               if c["action"] == "detach") == 1


def test_operator_revoke_and_drift_share_one_detach(service, candidate):
    row = _adopted_record(service, candidate)
    cid = "cmd-control-drift"
    service._track(cid, session_id=row.session_id, action="terminate_session",
                   machine_id="m1", actor="op")
    service.revoke(row.session_id, "op-1")
    result = service.on_supervisor_receipt(
        cid, status="rejected", reason="pid_reused")
    assert result["handled"] is False
    assert service.adoption_repo.get(row.session_id).status == "revoked"
    assert sum(1 for c in service.supervisor.commands
               if c["action"] == "detach") == 1


def test_control_phase45_forwards_bounded_payload(service, candidate):
    row = _adopted_record(service, candidate)
    result = service.control(
        row.session_id, action="append_user_turn",
        reason_code="operator_requested", actor="op",
        payload={"text": "hello"})
    assert result["status"] == "pending"
    tail = service.supervisor.commands[-1]
    assert tail["action"] == "append_user_turn"
    assert tail["payload"] == {"text": "hello"}
    profile = service.control(
        row.session_id, action="apply_local_profile",
        reason_code="operator_requested", actor="op",
        payload={"profile_id": "local-1"})
    assert profile["status"] == "pending"
    assert service.supervisor.commands[-1]["payload"] == {
        "profile_id": "local-1"}


def test_control_append_user_turn_hermes_unsupported(tmp_path):
    hermes = instance_row(agent_family="hermes")
    service = make_service(tmp_path, snapshot([hermes]))
    row = _adopted_record(service, Candidate(**candidate_dict(
        agent_family="hermes")))
    with pytest.raises(AdoptionServiceError) as exc:
        service.control(
            row.session_id, action="append_user_turn",
            reason_code="operator_requested", actor="op",
            payload={"text": "hello"})
    assert exc.value.code == "unsupported_action"
    assert not any(c["action"] == "append_user_turn"
                   for c in service.supervisor.commands)
