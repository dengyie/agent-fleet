"""Task 12 — adoption lifecycle receipt handling (unit tests).

Direct unit coverage of ``AdoptionService.on_supervisor_receipt`` against a
``RecordingSupervisor`` double mirroring the existing test doubles.  The
receipt fan-in (promotion, drift revocation, detach confirmation) is
exercised as the hub sees it: the service tracks every command it enqueues
(command_id -> session/action), then processes the signed receipt the probe
would upload through the SAME ``SupervisorReceiptHook``.

Coverage:

- an ``adopt`` receipt with reason ``ADOPT_SUCCESS`` promotes ``pending`` ->
  ``adopted`` (outcome ``promoted``), with no extra audit;
- a replayed adopt receipt on an already-adopted seat is a clean no-op
  (``noop``), never a second write;
- an adopt receipt with a non-success reason never promotes;
- an unknown / untracked command is an inert no-op (handled False);
- a drift receipt (a refused control + ``reason in ADOPT_DRIFT_CODES``) on an
  adopted seat revokes the row, appends ONE bounded ``adoption_drift`` audit
  and enqueues exactly ONE `detach` with ZERO signal fields;
- a drift replay never enqueues a second detach;
- a rejected control with a NON-drift reason (``adoption_revoked`` /
  ``scope_mismatch``) is a no-op;
- a drift receipt on a still-pending seat is a no-op (the domain forbids
  ``pending -> revoked`` directly — never a raise into the hub);
- a ``detach`` confirmation receipt is a terminal acknowledgement that NEVER
  mutates the adoption row;
- every return value is bounded; the drift audit detail never carries a raw
  pid / path / cmdline / started_at.
"""
from pathlib import Path

import pytest

from hub.application.adoption_service import (
    ADOPT_SUCCESS,
    _DRIFT_AUDIT_ACTION,
    AdoptionService,
)
from hub.infrastructure.adoption_repository import (
    AdoptionRepository,
    AdoptionRepositoryError,
)
from hub.infrastructure.transcript_repository import TranscriptRepository

# ---------------------------------------------------------------------------
# shared factories (the same shapes test_adoption_service.py uses)
# ---------------------------------------------------------------------------

DEFAULT_PID = 4242
DEFAULT_STARTED_AT = "2026-07-30T09:15:00Z"


def instance_row(**overrides):
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
    return {"machine": "m1", "instances": rows}


class FakeObservationRepo:
    def __init__(self, current=None):
        self._current = current

    def read_current(self, machine):
        return self._current


class RecordingSupervisor:
    """Records every ``enqueue``; returns a deterministic envelope."""

    def __init__(self):
        self.commands = []

    def enqueue(self, machine, session_id, attempt_id, action, reason_code,
                *, nonce=None, candidate=None, payload=None):
        rec = {
            "machine": machine,
            "session_id": session_id,
            "attempt_id": attempt_id,
            "action": action,
            "reason_code": reason_code,
            "nonce": nonce,
        }
        if candidate is not None:
            rec["candidate"] = candidate
        if payload is not None:
            rec["payload"] = payload
        self.commands.append(rec)
        return {"ok": True, "command_id": f"cmd-{len(self.commands)}"}


def make_service(tmp_path: Path):
    adoption_repo = AdoptionRepository(tmp_path / "adoptions.db")
    adoption_repo.init()
    transcript_repo = TranscriptRepository(tmp_path / "transcripts.db")
    transcript_repo.init()
    return AdoptionService(
        FakeObservationRepo(snapshot([instance_row()])),
        adoption_repo,
        RecordingSupervisor(),
        transcript_repo,
    )


def _last_cmd_id(service) -> str:
    """The deterministic command_id the RecordingSupervisor just returned."""
    return f"cmd-{len(service.supervisor.commands)}"


def _count_detaches(service):
    return sum(1 for c in service.supervisor.commands if c["action"] == "detach")


class _FlakyOnceRepo:
    """Wraps ``AdoptionRepository``; fails the NEXT ONE ``update_status``.

    Every other call (including ``get``/``list``/``upsert``) forwards to the
    inner store untouched, so a single write failure can be injected into the
    middle of a receipt fan-in (promotion's ``update_status`` or a drift's
    ``update_status``) without disturbing the rest of the plumbing.
    """

    def __init__(self, inner):
        self._inner = inner
        self._failures = 1

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def update_status(self, session_id, status):
        if self._failures > 0:
            self._failures -= 1
            raise AdoptionRepositoryError("invalid_status_transition")
        return self._inner.update_status(session_id, status)

    def revoke_cas(self, session_id):
        if self._failures > 0:
            self._failures -= 1
            raise AdoptionRepositoryError("adoption_store")
        return self._inner.revoke_cas(session_id)


def _audit(service):
    return service.transcripts.read_audit()


def _a(service, action):
    return [a for a in _audit(service) if a["action"] == action]


# ---------------------------------------------------------------------------
# promotion — pending -> adopted
# ---------------------------------------------------------------------------


def test_accept_receipt_promotes_pending_to_adopted(tmp_path):
    service = make_service(tmp_path)
    row = service.adopt("m1", DEFAULT_PID, DEFAULT_STARTED_AT, "op-1")
    cid = _last_cmd_id(service)
    assert row.status == "pending"

    result = service.on_supervisor_receipt(
        cid, status="accepted", reason=ADOPT_SUCCESS)
    assert result == {
        "command_id": cid,
        "handled": True,
        "outcome": "promoted",
        "session_id": row.session_id,
    }
    assert service.adoption_repo.get(row.session_id).status == "adopted"


def test_adopt_promote_then_replay_is_evicted_inert(tmp_path):
    """After a promotion settles, the entry is EVICTED and a replay stays inert.

    ``_tracked`` must stay bounded by in-flight commands (R1 MINOR #3), and
    ``SupervisorService.receipt`` is idempotent, so a post-settlement replay of
    the same command is simply untracked — never a second store write.
    """
    service = make_service(tmp_path)
    row = service.adopt("m1", DEFAULT_PID, DEFAULT_STARTED_AT, "op-1")
    cid = _last_cmd_id(service)
    promote = service.on_supervisor_receipt(
        cid, status="accepted", reason=ADOPT_SUCCESS)
    assert promote["outcome"] == "promoted"
    assert service.adoption_repo.get(row.session_id).status == "adopted"
    assert cid not in service._tracked

    replay = service.on_supervisor_receipt(
        cid, status="accepted", reason=ADOPT_SUCCESS)
    assert replay == {"command_id": cid, "handled": False}
    # idempotent: the store row is untouched by the replay
    assert service.adoption_repo.get(row.session_id).status == "adopted"


def test_adopt_receipt_wrong_reason_never_promotes(tmp_path):
    service = make_service(tmp_path)
    row = service.adopt("m1", DEFAULT_PID, DEFAULT_STARTED_AT, "op-1")
    cid = _last_cmd_id(service)
    result = service.on_supervisor_receipt(
        cid, status="rejected", reason="pid_reused")
    assert result["handled"] is False
    assert service.adoption_repo.get(row.session_id).status == "pending"


def test_untracked_receipt_is_inert(tmp_path):
    service = make_service(tmp_path)
    result = service.on_supervisor_receipt(
        "cmd-ghost-99", status="succeeded", reason="adopted")
    assert result == {"command_id": "cmd-ghost-99", "handled": False}


# ---------------------------------------------------------------------------
# drift — automatic revocation + single detach + bounded audit
# ---------------------------------------------------------------------------


def _adopt_and_promote(service):
    row = service.adopt("m1", DEFAULT_PID, DEFAULT_STARTED_AT, "op-1")
    lid = _last_cmd_id(service)
    service.on_supervisor_receipt(lid, status="accepted", reason=ADOPT_SUCCESS)
    return row


def test_drift_revokes_with_audit_and_single_zero_signal_detach(tmp_path):
    service = make_service(tmp_path)
    row = _adopt_and_promote(service)
    issued = service.control(
        row.session_id, action="terminate_session", reason_code="test",
        actor="op-2")
    cid = issued["command_id"]
    assert service.adoption_repo.get(row.session_id).status == "adopted"

    result = service.on_supervisor_receipt(cid, status="rejected", reason="pid_reused")
    assert result["handled"] is True
    assert result["outcome"] == "drift_revoked"
    assert result["session_id"] == row.session_id
    assert result["detach_enqueued"] is True

    # the row was revoked by the drift
    assert service.adoption_repo.get(row.session_id).status == "revoked"

    # ONE bounded adoption_drift audit
    entries = _a(service, _DRIFT_AUDIT_ACTION)
    assert len(entries) == 1
    assert entries[0]["target"] == row.session_id
    detail = entries[0]["detail"] or ""
    assert "pid_reused" in detail
    assert "drift_revoke" in detail
    for leaked in (str(DEFAULT_PID), DEFAULT_STARTED_AT,
                   "/usr/local/bin/codex", "codex session"):
        assert leaked not in detail

    # exactly ONE detach, with ZERO signal fields
    detaches = [c for c in service.supervisor.commands if c["action"] == "detach"]
    assert len(detaches) == 1
    cmd = detaches[0]
    assert cmd["reason_code"] == "drift_detach"
    assert cmd["machine"] == "m1"
    assert cmd["session_id"] == row.session_id
    assert "signal" not in cmd
    assert set(cmd) == {"machine", "session_id", "attempt_id", "action",
                        "reason_code", "nonce"}


def test_drift_replay_never_second_detach(tmp_path):
    service = make_service(tmp_path)
    row = _adopt_and_promote(service)
    issued = service.control(
        row.session_id, action="terminate_session", reason_code="t", actor="op")
    cid = issued["command_id"]
    first = service.on_supervisor_receipt(cid, status="rejected", reason="pid_reused")
    assert first["outcome"] == "drift_revoked"
    detach_count = _count_detaches(service)
    assert detach_count == 1

    # the drift receipt was terminal — a replay is an inert no-op
    replay = service.on_supervisor_receipt(cid, status="rejected", reason="pid_reused")
    assert replay == {"command_id": cid, "handled": False}
    assert _count_detaches(service) == detach_count


def test_drift_any_other_rejected_reason_is_noop(tmp_path):
    service = make_service(tmp_path)
    row = _adopt_and_promote(service)
    issued = service.control(
        row.session_id, action="pause_session", reason_code="x", actor="op")
    cid = issued["command_id"]
    for reason in ("adoption_revoked", "scope_mismatch", "unknown_session"):
        result = service.on_supervisor_receipt(cid, status="rejected", reason=reason)
        assert result == {"command_id": cid, "handled": False}
        assert _count_detaches(service) == 0
    assert service.adoption_repo.get(row.session_id).status == "adopted"
    assert _a(service, _DRIFT_AUDIT_ACTION) == []


def test_drift_on_pending_seat_is_bounded_noop(tmp_path):
    service = make_service(tmp_path)
    row = service.adopt("m1", DEFAULT_PID, DEFAULT_STARTED_AT, "op-1")
    # Although the service surface never issues control on a pending seat, a
    # NOT-yet-promoted / drift receipt for such a command is possible; the
    # domain forbids ``pending -> revoked`` so it must stay a bounded no-op.
    cid = "cmd-drift-pending"
    service._track(cid, session_id=row.session_id, action="terminate",
                   machine_id="m1", actor="op")
    result = service.on_supervisor_receipt(cid, status="rejected", reason="pid_reused")
    assert result == {"command_id": cid, "handled": False}
    assert service.adoption_repo.get(row.session_id).status == "pending"
    assert _a(service, _DRIFT_AUDIT_ACTION) == []
    assert _count_detaches(service) == 0


# ---------------------------------------------------------------------------
# detach confirmation — a terminal acknowledgement, never a DB mutation
# ---------------------------------------------------------------------------


def test_detach_confirmation_does_not_mutate_row(tmp_path):
    service = make_service(tmp_path)
    row = _adopt_and_promote(service)
    service.revoke(row.session_id, "op-1")          # row -> revoked + detach
    cid = _last_cmd_id(service)
    assert service.supervisor.commands[-1]["action"] == "detach"
    assert service.adoption_repo.get(row.session_id).status == "revoked"

    result = service.on_supervisor_receipt(
        cid, status="succeeded", reason="detached")
    assert result == {
        "command_id": cid,
        "handled": True,
        "outcome": "dettached",
        "session_id": row.session_id,
    }
    # never a mutation: the row is still revoked, no new audit rows
    assert service.adoption_repo.get(row.session_id).status == "revoked"
    pre = len(_audit(service))
    assert service.adoption_repo.list("m1")[0].status == "revoked"
    assert len(_audit(service)) == pre


# ---------------------------------------------------------------------------
# R1 regression — bounded ``_tracked`` + retry semantics (IMPORTANT #2, MINOR #3)
# ---------------------------------------------------------------------------


def test_promote_is_retried_after_store_failure_not_orphanized(tmp_path):
    """A genuine repo failure KEEPS the entry; a retried receipt re-promotes.

    R1 IMPORTANT #2: the tracking entry must NOT be popped before the mutation
    succeeds.  When ``update_status`` fails the service re-raises (so the hub
    500-protect handler applies) and the SAME command, replayed later, still
    promotes — brief Step 3 idempotency under retries.
    """
    service = make_service(tmp_path)
    row = service.adopt("m1", DEFAULT_PID, DEFAULT_STARTED_AT, "op-1")
    cid = _last_cmd_id(service)
    service.adoption_repo = _FlakyOnceRepo(service.adoption_repo)

    with pytest.raises(AdoptionRepositoryError):
        service.on_supervisor_receipt(cid, status="accepted",
                                      reason=ADOPT_SUCCESS)
    # NOT orphanized: the entry survived the failure.
    assert cid in service._tracked
    assert service.adoption_repo.get(row.session_id).status == "pending"

    retry = service.on_supervisor_receipt(cid, status="accepted",
                                          reason=ADOPT_SUCCESS)
    assert retry["outcome"] == "promoted"
    assert service.adoption_repo.get(row.session_id).status == "adopted"
    # settled -> evicted (bounded ``_tracked``).
    assert cid not in service._tracked


def test_drift_revoke_is_retried_after_store_failure(tmp_path):
    """A drift store failure keeps the control entry; a replay re-revokes."""
    service = make_service(tmp_path)
    row = _adopt_and_promote(service)
    issued = service.control(
        row.session_id, action="terminate_session", reason_code="t", actor="op")
    cid = issued["command_id"]
    service.adoption_repo = _FlakyOnceRepo(service.adoption_repo)

    with pytest.raises(AdoptionRepositoryError):
        service.on_supervisor_receipt(cid, status="rejected", reason="pid_reused")
    assert cid in service._tracked
    assert service.adoption_repo.get(row.session_id).status == "adopted"

    retry = service.on_supervisor_receipt(cid, status="rejected", reason="pid_reused")
    assert retry["outcome"] == "drift_revoked"
    assert service.adoption_repo.get(row.session_id).status == "revoked"
    assert _count_detaches(service) == 1
    assert cid not in service._tracked


def test_hook_lets_store_error_propagate_but_other_receipts_stay_inert(tmp_path):
    """``SupervisorReceiptHook`` honors the 500-protect contract (R1 #2)."""
    from hub.application.control_router import SupervisorReceiptHook

    class _DecoyInner:
        """The Task 9 inner service receipt surface (a bound echo)."""

        def receipt(self, command_id, status, reason="", machine=None):
            return {"status": status, "reason": reason}

    service = make_service(tmp_path)
    row = service.adopt("m1", DEFAULT_PID, DEFAULT_STARTED_AT, "op-1")
    cid = _last_cmd_id(service)
    service.adoption_repo = _FlakyOnceRepo(service.adoption_repo)
    hook = SupervisorReceiptHook(inner=_DecoyInner(), router=None,
                                 adoption=service)

    # A genuine store failure is NOT a stray advisory receipt: it must reach
    # the hub's bounded 500-protect handler instead of being swallowed.
    with pytest.raises(AdoptionRepositoryError):
        hook.receipt(cid, "accepted", "adopted")
    assert cid in service._tracked  # still retry-able

    # Every OTHER receipt (unknown command) is inert and never raises through
    # the hook; the inner receipt result passes through untouched.
    ghost = hook.receipt("cmd-ghost-4321", "succeeded", "adopted")
    assert ghost["status"] == "succeeded"
    assert ghost["reason"] == "adopted"


def test_tracked_is_bounded_progress_never_settles_adopt(tmp_path):
    """Eviction happens on the FIRST settled receipt — not on progress ones.

    ``accepted``/``executing`` alone (on a not-yet-successful adopt) never
    settle; after the promotion the entry is evicted, so ``_tracked`` stays
    sized by in-flight commands, not cumulative adoptions (R1 MINOR #3).
    """
    service = make_service(tmp_path)
    service.adopt("m1", DEFAULT_PID, DEFAULT_STARTED_AT, "op-1")
    cid = _last_cmd_id(service)

    # PROGRESS-only receipts never settle the command, so it STAYS tracked.
    service.on_supervisor_receipt(cid, status="executing", reason="scheduled")
    assert cid in service._tracked
    # a non-successful adopt progress (accepted but reason != adopted) doesn't
    # modify the store either - the command is still in-flight and tracked.
    refused = service.on_supervisor_receipt(
        cid, status="accepted", reason="pid_reused")
    assert refused["handled"] is False
    assert cid in service._tracked
    # a settle on the accepted-adopted receipt evicts the entry.
    promote = service.on_supervisor_receipt(
        cid, status="accepted", reason=ADOPT_SUCCESS)
    assert promote["outcome"] == "promoted"
    assert cid not in service._tracked
    assert service._tracked == {}   # bounded by in-flight, no residue.

    # a replay of the settled entry is an untracked inert no-op - nothing
    # re-enters the map, no second store write.
    replay = service.on_supervisor_receipt(
        cid, status="accepted", reason=ADOPT_SUCCESS)
    assert replay == {"command_id": cid, "handled": False}
    assert service._tracked == {}


# ---------------------------------------------------------------------------
# Fix M1 — stale tracking entries are TTL-bounded; late receipts stay inert
# ---------------------------------------------------------------------------


def test_stale_tracked_entry_evicted_and_late_receipt_inert(tmp_path):
    """An adopt entry whose settled receipt never arrives is evicted after the
    fixed 24h TTL; a late replayed receipt then becomes an inert untracked
    no-op while the pending row stays visible for revoke/retry."""
    import time as _time

    service = make_service(tmp_path)
    row = service.adopt("m1", DEFAULT_PID, DEFAULT_STARTED_AT, "op-1")
    cid = _last_cmd_id(service)
    assert cid in service._tracked
    # age the entry beyond the TTL
    service._tracked[cid]["_issued_at"] = _time.monotonic() - 25 * 3600
    # ANY receipt handling evicts the stale entry -> untracked inert no-op
    result = service.on_supervisor_receipt(
        cid, status="accepted", reason="scheduled")
    assert result == {"command_id": cid, "handled": False}
    assert cid not in service._tracked
    # a late replay — even ADOPT_SUCCESS — is now untracked: no promotion,
    # no second command, and the winner row stays pending (revoke/retry).
    late = service.on_supervisor_receipt(
        cid, status="accepted", reason=ADOPT_SUCCESS)
    assert late == {"command_id": cid, "handled": False}
    assert service.adoption_repo.get(row.session_id).status == "pending"

    # A FRESH entry survives a receipt-handling sweep (the TTL only evicts
    # entries older than the fixed bound, never a live in-flight command).
    fresh_service = make_service(tmp_path / "fresh")
    fresh_service.adopt("m1", DEFAULT_PID, DEFAULT_STARTED_AT, "op-3")
    live_cid = _last_cmd_id(fresh_service)
    assert live_cid in fresh_service._tracked
    progress = fresh_service.on_supervisor_receipt(
        live_cid, status="accepted", reason="scheduled")
    assert progress == {"command_id": live_cid, "handled": False}
    assert live_cid in fresh_service._tracked