"""hub/application/adoption_service.py — explicit agent adoption ("纳管").

The service layer an operator calls to adopt a discovered agent.  It reads ONLY
the latest sanitized snapshot for a machine, validates the candidate against
fixed bounded codes, persists a ``pending`` adoption record in the isolated
adoption store (Task 4), appends a code-only audit trace to the transcript
store, and signs exactly one ``adopt`` control command through the injected
supervisor.

Bounded error surface (every error is a stable short code):

- ``stale_candidate``      — machine snapshot missing, pid absent, started_at
  mismatch, or a non-positive pid (defensive: ``sanitize_instances`` already
  drops negative rows);
- ``not_attachable``       — row present but ``attachable`` is falsy;
- ``unsupported_candidate`` — row present but ``agent_family`` is not a
  member of ``report_schema.INSTANCE_FAMILIES``.

``revoke`` translates an ``AdoptionRepositoryError`` verbatim so the operator
surface sees the same code (``invalid_adoption`` / ``invalid_status_transition``).

Idempotency: adopting a candidate that already has a ``pending`` or ``adopted``
record returns the existing row without a second session id, command, or audit;
revoking an already-revoked record never re-issues a ``detach`` command.

Enqueue failure on a freshly created pending row is compensated by deleting
that row so a later ``adopt`` is not stuck on a dead-end.  An operator
``retry`` reuses the existing session id and issues exactly one new signed
``adopt`` per explicit request; an already-``adopted`` seat is a no-op.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from report_schema import INSTANCE_FAMILIES
from hub.domain.adoption import Adoption
from hub.infrastructure.adoption_repository import AdoptionRepositoryError

_NOW_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"

#: A record stops a fresh adopt when it is already in force or waiting to be
#: so.
_BLOCKING_STATUSES = frozenset({"pending", "adopted"})

#: Guard reasons a probe may report for an ADOPTED session's control action
#: (Task 9).  ``reconcile_revoked`` transitions a stored Adoption to
#: ``revoked`` only for one of these fixed bounded codes — an arbitrary wire
#: reason is NEVER trusted for a status transition.
_GUARD_CODES = frozenset({
    "adoption_revoked", "pid_reused", "exe_changed", "no_permission",
})

#: The stored status a probe-guard refusal may actually reconcile to
#: ``revoked``.  Only ``adopted`` transitions: the domain forbids
#: ``pending -> revoked`` directly, so a ``pending`` record is a bounded no-op
#: (``ignored``) rather than a raise.
_RECONCILE_STATUSES = frozenset({"adopted"})

#: Fixed audit action for a probe-guard revocation reconciliation — ONE name
#: (Task 10's receipt hook consumes exactly this).
_RECONCILE_AUDIT_ACTION = "revoke_probe_reject"

#: The ONLY source-control actions the operator surface may issue against an
#: ADOPTED session.  ``adopt``/``detach`` are the adoption-lifecycle commands
#: owned by ``adopt``/``revoke`` — they are NOT operator source controls here.
#: Phase 4/5 tokens (``append_user_turn`` / ``apply_local_profile``) are
#: accepted by name but stay default-off at the supervisor issuance gate.
#: Any action outside this set (``exec_shell`` / ``inject_stdin`` etc.) is
#: forever ``unsupported_action``: no raw pid/signal/shell/command text is
#: ever accepted on this surface.
_SOURCE_CONTROL_ACTIONS = frozenset({
    "pause_session",
    "resume_session",
    "terminate_session",
    "quarantine_session",
    "cancel_attempt",
    "append_user_turn",
    "apply_local_profile",
})

#: Phase 4/5 actions that require a bounded payload (text / profile_id).
_PAYLOAD_CONTROL_ACTIONS = frozenset({
    "append_user_turn",
    "apply_local_profile",
})

#: Bounded audit action for one operator source-control issuance.
_SOURCE_CONTROL_AUDIT_ACTION = "control_session"

#: Fixed audit token for one operator exact-capture upgrade (Task 11).  The
#: brief's golden test reads exactly this token.
_EXACT_CAPTURE_AUDIT_ACTION = "capture_exact"

#: The ONLY probe receipt reason that promotes a ``pending`` adoption to
#: ``adopted`` — the probe's successful attach uploads ``("accepted",
#: "adopted")`` (verbatim from the brief).
ADOPT_SUCCESS = "adopted"

#: Drift codes a refused control receipt may carry: ANY one of these fixed
#: bounded reasons on an ``adopted`` seat means the probe's live identity no
#: longer matches the stored entry and the seat auto-revokes (never a signal).
ADOPT_DRIFT_CODES = frozenset({
    "pid_reused", "exe_changed", "no_permission",
    "unsupported_family", "native_file_unreadable",
})

#: Fixed audit action token for ONE drift auto-revocation (brief golden).
_DRIFT_AUDIT_ACTION = "adoption_drift"
#: Fixed reason_code carried by the ONE detach a drift auto-revocation
#: enqueues (dispatch verbatim).
_DRIFT_DETACH_REASON = "drift_detach"
#: Fixed policy token in the drift audit detail — never raw pid/path/command.
_DRIFT_POLICY_TOKEN = "drift_revoke"
#: Receipts that end a command's lifecycle; entries are dropped from the
#: tracking map then so memory stays bounded.  ``accepted``/``executing``
#: are progress-only and keep an ``adopt`` tracked so a controller-originated
#: replay resolves idempotently against the store instead of re-firing.
_TERMINAL_RECEIPTS = frozenset({
    "succeeded", "already_finished", "failed", "rejected", "expired",
})

#: TTL for a ``_tracked`` entry (M1): an adopt/detach/control command whose
#: settled receipt never arrives must not pin its tracking entry forever.  Any
#: receipt handling or a later enqueue evicts entries older than this fixed
#: bound; a late replayed receipt for an evicted entry is then an inert
#: no-op, while the stored pending row stays visible to the operator for
#: revoke/retry.
_TRACK_TTL_S = 24 * 60 * 60

#: Terminal receipt statuses that mean an ``adopt`` did NOT take effect.  The
#: pending seat is closed (``pending -> revoked``) so the same process can be
#: re-adopted / the operator is not stuck on a dead pending row.
_ADOPT_FAILED_RECEIPTS = frozenset({"rejected", "failed", "expired"})
#: ``rejected`` is always decided by the probe BEFORE (or rolled back after) an
#: attach, so nothing is left bound.  ``failed``/``expired`` are indeterminate
#: (e.g. ``execution_unknown`` after a probe crash) and get ONE cleanup detach.
_ADOPT_INDETERMINATE_RECEIPTS = frozenset({"failed", "expired"})
#: Fixed audit action for an adopt the probe refused / that never completed.
_ADOPT_REJECTED_AUDIT_ACTION = "adopt_rejected"
#: Fixed reason_code for the cleanup detach after an indeterminate adopt.
_ADOPT_CLEANUP_DETACH_REASON = "adopt_cleanup"
#: Adoption session ids are minted as ``adopt_<hex>``; only those are looked up
#: in the adoption store when a receipt is resolved from the durable queue.
_ADOPTION_SESSION_PREFIX = "adopt_"


class AdoptionServiceError(RuntimeError):
    """Bounded adoption-service error: a stable short code only.

    ``str(err)`` and ``code`` carry the bounded code only — never paths, PIDs,
    command lines, exception text, or key material.
    """

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True)
class Candidate:
    """A pure discovery view of one sanitized instance + its machine.

    The view mirrors exactly the fixed ``INSTANCE_FIELDS`` keys from the
    sanitized snapshot; it IS one row of ``list_candidates`` and carries no
    side effects.
    """

    machine_id: str
    pid: int
    pgid: int
    exe_path: str
    cmdline: str
    agent_family: str
    native_file_path: str | None
    started_at: str
    attachable: bool


class AdoptionService:
    """Explicit adoption decisions for the operator surface.

    Constructor-injected repositories keep the use case free of module-level
    paths and credentials:

    - ``observation_repo`` — duck-typed ``read_current(machine)`` returning the
      latest sanitized snapshot (the real ``JsonObservationRepository``
      satisfies this); ``None``/missing ``instances`` reads as an empty set;
    - ``adoption_repo``    — the isolated Task 4 ``AdoptionRepository``;
    - ``supervisor``       — an injection point whose ``enqueue`` matches the
      ``SupervisorService`` shape (a recording double in tests);
    - ``transcript_repo``  — the ``TranscriptRepository`` for bounded audit;
    - ``clock``            — default ``time.time``; stamps ``created_at`` /
      ``updated_at`` in the same ``%Y-%m-%dT%H:%M:%S.%fZ`` format the
      adoption repository uses.
    """

    def __init__(self, observation_repo, adoption_repo, supervisor,
                 transcript_repo, *, clock=time.time):
        self.observation_repo = observation_repo
        self.adoption_repo = adoption_repo
        self.supervisor = supervisor
        self.transcripts = transcript_repo
        self.clock = clock
        # Serialize the adopted-seat decision through command admission.  A
        # revoke must not change the row after control has passed its adopted
        # check but before the signed command is enqueued.
        self._admission_lock = threading.RLock()
        #: command_id -> bounded context ({session_id, action, machine_id,
        #: actor}) for every command THIS service enqueues.  ``SupervisorService``
        #: exposes no accessor for a receipt's target/action, so this is the
        #: only way the receipt fan-in knows which seat a receipt concerns.
        #: Entries are evicted once their FIRST receipt reaches a settled state
        #: (``promoted``/``noop``/terminal for adopt, confirmed or terminal for
        #: detach, ``drift_revoked``/terminal for control) AFTER the mutation
        #: succeeds — ``accepted``/``executing`` progress receipts alone never
        #: settle, and op failures keep the entry so a retry re-promotes.  The
        #: map therefore stays BOUNDED by in-flight commands, never cumulative
        #: adoptions.
        self._tracked: dict[str, dict[str, str]] = {}

    # -- internal helpers ------------------------------------------------------

    def _now(self) -> str:
        return datetime.fromtimestamp(float(self.clock()),
                                      tz=timezone.utc).strftime(_NOW_FMT)

    @staticmethod
    def _as_int(value, *, default: int = 0) -> int:
        """Coerce a sanitized integer field, never raising.

        The sanitizer guarantees real ints; the coercion is defensive against
        duck-typed observation doubles in tests.
        """
        if isinstance(value, bool):
            return default
        if isinstance(value, int):
            return value
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _current_rows(self, machine_id: str) -> list[dict]:
        """The latest snapshot's ``instances[]`` rows for a machine.

        ``None``/missing snapshot and a missing ``instances`` key are treated
        as a stale / empty candidate set, per the discovery contract.
        """
        snapshot = self.observation_repo.read_current(machine_id)
        if not isinstance(snapshot, dict):
            return []
        instances = snapshot.get("instances")
        if not isinstance(instances, list):
            return []
        return [row for row in instances if isinstance(row, dict)]

    def _candidate(self, machine_id: str, row: dict) -> Candidate:
        """A Candidate view over one sanitized row (no filtering)."""
        return Candidate(
            machine_id=machine_id,
            pid=self._as_int(row.get("pid")),
            pgid=self._as_int(row.get("pgid")),
            exe_path=str(row.get("exe_path") or ""),
            cmdline=str(row.get("cmdline") or ""),
            agent_family=str(row.get("agent_family") or ""),
            native_file_path=row.get("native_file_path"),
            started_at=str(row.get("started_at") or ""),
            attachable=bool(row.get("attachable")),
        )

    def _active_for_identity(self, machine_id: str, pid: int,
                             started_at: str) -> Adoption | None:
        """The in-force/active row for ``(machine_id, pid, started_at)``.

        Shared by the idempotency fast-path and the concurrent-race resolution
        in :meth:`adopt`: there is exactly zero-of-one active row per identity.
        """
        for existing in self.adoption_repo.list(machine_id):
            if (existing.status in _BLOCKING_STATUSES
                    and existing.pid == pid
                    and existing.started_at == started_at):
                return existing
        return None

    # -- discovery view ---------------------------------------------------------

    def list_candidates(self, machine_id: str) -> list[Candidate]:
        """Every sanitized row for a machine (including rows that already have
        a pending/adopted record) — a pure discovery view, no filtering."""
        return [self._candidate(machine_id, row)
                for row in self._current_rows(machine_id)]

    # -- adoption ---------------------------------------------------------------

    def adopt(self, machine_id: str, pid: int, started_at: str,
              actor: str) -> Adoption:
        """Adopt a discovered candidate; returns the persisted pending record.

        Raises :class:`AdoptionServiceError` with a stable bounded code when
        the candidate is stale, not attachable, or unsupported.  An existing
        ``pending``/``adopted`` record for ``(machine_id, pid, started_at)``
        is returned as-is: no new session id, no command, no audit.
        """
        pid_i = self._as_int(pid)
        if pid_i <= 0:
            # Defensive: sanitize_instances already drops non-positive rows so
            # this can only surface from a defective duck-typed observation
            # repository; treat as stale (no dedicated code).
            raise AdoptionServiceError("stale_candidate")
        row = None
        for candidate_row in self._current_rows(machine_id):
            if self._as_int(candidate_row.get("pid")) == pid_i:
                row = candidate_row
                break
        if row is None:
            raise AdoptionServiceError("stale_candidate")
        if row.get("started_at") != started_at:
            raise AdoptionServiceError("stale_candidate")
        if not row.get("attachable"):
            raise AdoptionServiceError("not_attachable")
        family = str(row.get("agent_family") or "")
        if family not in INSTANCE_FAMILIES:
            raise AdoptionServiceError("unsupported_candidate")

        # Idempotent adopt: an in-flight/active record for this process
        # identity is returned instead of creating a duplicate.
        existing = self._active_for_identity(machine_id, pid_i, started_at)
        if existing is not None:
            return existing

        session_id = f"adopt_{secrets.token_hex(16)}"
        now = self._now()   # one clock read: created_at never later than updated_at
        record = Adoption(
            adoption_id=secrets.token_hex(16),
            machine_id=machine_id,
            session_id=session_id,
            pid=pid_i,
            pgid=self._positive_pgid(row.get("pgid")),
            started_at=str(row.get("started_at") or ""),
            exe_path=str(row.get("exe_path") or ""),
            agent_family=family,
            native_file_path=row.get("native_file_path"),
            status="pending",
            capture_quality="best_effort",
            actor=str(actor),
            created_at=now,
            updated_at=now,
        )
        try:
            stored = self.adoption_repo.upsert(record)
        except AdoptionRepositoryError as exc:
            # Race between the idempotency scan above and this insert: another
            # operator's ``adopt`` for the SAME (machine_id, pid, started_at)
            # may have landed in the store just now (the repository's unique
            # active-row index turns that collision into an IntegrityError,
            # surfaced here as ``invalid_adoption``).  Resolve to the winning
            # seat's row instead of enqueuing a SECOND adopt/session/audit.
            if exc.code == "invalid_adoption":
                try:
                    winner = self._active_for_identity(
                        machine_id, pid_i, started_at)
                except AdoptionRepositoryError as rex:
                    raise AdoptionServiceError(rex.code) from None
                if winner is not None:
                    return winner
            # Not a duplicate-identity collision: surface the genuine bounded
            # repository failure ("invalid_adoption" for a bad payload, or a
            # store code) verbatim.
            raise AdoptionServiceError(exc.code) from None

        # Exactly one signed adopt command per new adoption.  If enqueue fails
        # the pending row is deleted so a later adopt is not stuck on a
        # dead-end; leftover pending rows (crash after upsert) are recovered
        # through the explicit ``retry`` operator action.
        try:
            self._enqueue_adopt(stored, actor)
        except Exception:
            try:
                self.adoption_repo.delete_pending(session_id)
            except AdoptionRepositoryError:
                pass
            raise
        # Bounded code-only audit: never a pid, path, or command line.
        self.transcripts.append_audit(
            str(actor), "adopt_session", target=session_id,
            detail={"machine_id": machine_id, "agent_family": family})
        return stored

    # -- revocation -------------------------------------------------------------

    def revoke(self, session_id: str, actor: str) -> None:
        """Revoke an adoption: enqueue one ``detach``, then CAS, then audit.

        An already-revoked record is idempotent: it is re-confirmed in the
        store and re-audited but never re-enqueues a ``detach`` command.  The
        detach is enqueued before the ``adopted|pending -> revoked`` CAS so a
        failed enqueue leaves the seat unrevoked (a retried revoke detaches);
        all revoke paths serialize on ``_admission_lock``, so a
        concurrent operator revoke and a drift auto-revoke cannot double-fire.
        A still-``pending`` seat is revocable too (its receipt may never
        arrive); the detach then cleans up an attach that may have landed on
        the probe after all (an unknown session is a harmless rejection).
        """
        with self._admission_lock:
            # Enqueue BEFORE the CAS: a failed signed enqueue (e.g. 503
            # signing unavailable) must leave the seat revocable, so an
            # operator retry still issues the detach.  Every revoke path holds
            # ``_admission_lock``, so the status read here cannot go stale
            # before the CAS below.
            try:
                current = self.adoption_repo.get(session_id)
            except AdoptionRepositoryError as exc:
                raise AdoptionServiceError(exc.code) from None
            if current is None:
                raise AdoptionServiceError("invalid_adoption")
            envelope = None
            if current.status in ("adopted", "pending"):
                envelope = self.supervisor.enqueue(
                    current.machine_id, session_id, None, "detach",
                    "operator_detach", nonce=secrets.token_hex(16))
            try:
                revoked, captured = self.adoption_repo.revoke_cas(session_id)
            except AdoptionRepositoryError as exc:
                # The queued detach is harmless without a CAS winner: the probe
                # rejects a detach for an unknown/unattached session.
                raise AdoptionServiceError(exc.code) from None
            self.transcripts.append_audit(str(actor), "revoke_session",
                                          target=session_id)
            if envelope is None:
                return
            self._track(str(envelope.get("command_id") or ""),
                        session_id=session_id, action="detach",
                        machine_id=revoked.machine_id, actor=actor)

    def retry(self, session_id: str, actor: str) -> Adoption:
        """Re-issue the signed ``adopt`` for a leftover ``pending`` seat.

        Reuses the existing session/adoption id.  An already-``adopted`` seat
        is returned as-is (no second command).  A ``revoked`` seat is
        ``adoption_revoked``.  Each explicit retry of a still-pending seat
        enqueues exactly one new signed command with a fresh nonce.
        """
        # Same admission lock as ``revoke``: a concurrent revoke cannot land
        # between the pending check and the signed adopt enqueue.
        with self._admission_lock:
            try:
                record = self.adoption_repo.get(session_id)
            except AdoptionRepositoryError as exc:
                raise AdoptionServiceError(exc.code) from None
            if record is None:
                raise AdoptionServiceError("invalid_adoption")
            if record.status == "revoked":
                raise AdoptionServiceError("adoption_revoked")
            if record.status == "adopted":
                return record
            if record.status != "pending":
                raise AdoptionServiceError("invalid_status_transition")
            # Re-confirm ``pending`` with a store CAS right before signing: a
            # revoke/drift that slipped past the read above (any path outside
            # this lock) turns the retry into a bounded refusal, never an
            # ``adopt`` issued after the ``detach``.
            try:
                record = self.adoption_repo.update_status(session_id, "pending")
            except AdoptionRepositoryError as exc:
                if exc.code != "invalid_status_transition":
                    raise AdoptionServiceError(exc.code) from None
                try:
                    current = self.adoption_repo.get(session_id)
                except AdoptionRepositoryError as rex:
                    raise AdoptionServiceError(rex.code) from None
                if current is not None and current.status == "adopted":
                    return current
                if current is not None and current.status == "revoked":
                    raise AdoptionServiceError("adoption_revoked")
                raise AdoptionServiceError(exc.code) from None
            self._enqueue_adopt(record, actor)
            return record

    def recover_for_machine(self, machine_id: str, after: str = "") -> dict:
        """Reissue current bindings after a client restart, before controls.

        Only this authenticated machine's active seats are signed. Revocation
        shares the admission lock, so it cannot race an active check/enqueue.
        A failed response can be retried: node-side attach/bridge are idempotent.
        """
        with self._admission_lock:
            records = self.adoption_repo.list_active(machine_id, after=after, limit=21)
            page = records[:20]
            commands = [self._enqueue_adopt(record, record.actor, recovery=True)
                        for record in page]
            return {"commands": commands,
                    "next_cursor": page[-1].session_id if len(records) > 20 else None}

    # -- probe guard reconciliation (Task 9) --------------------------------------

    def reconcile_revoked(self, session_id: str, *, reason: str,
                          actor: str) -> dict:
        """Reconcile a probe-guard refusal to the stored Adoption (bounded).

        Idempotent: an ``adopted`` record transitions to ``revoked`` at most
        once; a ``pending`` record is a bounded no-op (the domain forbids
        ``pending -> revoked`` directly), as is an already-revoked or unknown
        session — every no-op returns the current view unchanged.  Only fixed
        ``_GUARD_CODES`` reasons are ever accepted (anything else is a no-op —
        never trust the wire for a status-transition reason).  Appends ONE
        bounded audit row (target=session_id, detail
        {"reason": reason, "kind": "probe_reject"} — NEVER the pid/path) and
        NEVER enqueues a command (the probe side already released the private
        entry).  Returns ``{"session_id": ..., "reconciled": bool,
        "status": "revoked"|"ignored"}``.  A genuine ``AdoptionRepositoryError``
        is translated verbatim via :class:`AdoptionServiceError` like
        ``revoke``.

        Note (M2): ``on_supervisor_receipt``'s drift fan-in
        (``_drift_revoke``) is the LIVE auto-revoker for a refused control
        receipt; this method is retained as the explicit additive guard API
        for callers that observe a drift independently of a command receipt.
        Both paths are idempotent and funnel a real drift to the same bounded
        audit + a single zero-signal detach.
        """
        with self._admission_lock:
            if reason not in _GUARD_CODES:
                return {"session_id": session_id, "reconciled": False,
                        "status": "ignored"}
            try:
                record = self.adoption_repo.get(session_id)
                if (record is None
                        or record.status not in _RECONCILE_STATUSES):
                    return {"session_id": session_id, "reconciled": False,
                            "status": "ignored"}
                self.adoption_repo.update_status(session_id, "revoked")
            except AdoptionRepositoryError as exc:
                raise AdoptionServiceError(exc.code) from None
            self.transcripts.append_audit(
                str(actor), _RECONCILE_AUDIT_ACTION, target=session_id,
                detail={"reason": reason, "kind": "probe_reject"})
            return {"session_id": session_id, "reconciled": True,
                    "status": "revoked"}

    # -- source control (Task 10) ---------------------------------------------------

    def control(self, session_id: str, *, action: str, reason_code: str,
                actor: str, payload=None) -> dict:
        """Issue one fixed source-control action for an ADOPTED session
        (bounded).

        Only the fixed action names are accepted (else ``unsupported_action``);
        the stored record must exist (else ``invalid_adoption``) and be
        ``adopted`` (a ``pending`` seat => ``invalid_status_transition``; a
        ``revoked`` seat => ``adoption_revoked``).  Exactly one signed
        ``enqueue`` through the injected supervisor — the same channel
        Task 6's ``adopt`` uses.  The probe-side identity guard (Task 9)
        remains the final authority before any signal; this surface ENQUEUES,
        it never signals and never writes stdin.  Returns ``{"status":
        "pending", "session_id": ..., "command_id": ...}`` — envelope
        internals (signature/nonce) and raw pid/path never appear.  Enqueue
        failures surface verbatim as :class:`SupervisorServiceError`
        (``unsupported_action`` / ``feature_disabled`` / ``invalid_payload`` /
        ``resume_superseded`` / ``duplicate_nonce``).
        """
        with self._admission_lock:
            try:
                record = self.adoption_repo.get(session_id)
            except AdoptionRepositoryError as exc:
                raise AdoptionServiceError(exc.code) from None
            if record is None:
                raise AdoptionServiceError("invalid_adoption")
            if record.status == "revoked":
                raise AdoptionServiceError("adoption_revoked")
            if record.status != "adopted":
                raise AdoptionServiceError("invalid_status_transition")
            if action not in _SOURCE_CONTROL_ACTIONS:
                raise AdoptionServiceError("unsupported_action")
            family = str(getattr(record, "agent_family", "") or "")
            if action == "append_user_turn" and family == "hermes":
                # Hermes stays observation-only until a capability probe proves
                # resume; never queue a follow-up turn for it.
                raise AdoptionServiceError("unsupported_action")
            enqueue_kwargs = {"nonce": secrets.token_hex(16)}
            if action in _PAYLOAD_CONTROL_ACTIONS:
                enqueue_kwargs["payload"] = payload
            envelope = self.supervisor.enqueue(
                record.machine_id, session_id, None, action,
                str(reason_code)[:32], **enqueue_kwargs)
            # Track the signed control so a drift receipt (``rejected`` +
            # ``ADOPT_DRIFT_CODES``) can auto-revoke this adopted seat (Task 12).
            self._track(str(envelope.get("command_id") or ""),
                        session_id=session_id, action=action,
                        machine_id=record.machine_id, actor=actor)
            # Bounded code-only audit: NEVER the pid/path/cmdline — only the
            # opaque session id and the fixed action token.
            self.transcripts.append_audit(
                str(actor), _SOURCE_CONTROL_AUDIT_ACTION, target=session_id,
                detail={"action": action})
            return {
                "status": "pending",
                "session_id": session_id,
                "command_id": str(envelope["command_id"]),
            }

    # -- exact-capture upgrade (Task 11) --------------------------------------------

    def upgrade_capture_exact(self, session_id: str, actor: str) -> Adoption:
        """Promote an ADOPTED session's capture quality to ``exact`` (bounded).

        An adopted seat is required: a ``pending`` seat is
        ``capture_quality_immutable`` and a ``revoked`` seat is
        ``adoption_revoked``; a well-formed-but-never-created id is
        ``unknown_session``.  A repeat upgrade on an already-``exact`` adopted
        seat is idempotent — the current record is returned and NO second
        audit is appended.  Otherwise the quality label flips atomically in
        ONE repository call (``update_capture_quality`` already enforces
        ``CAPTURE_QUALITIES``), then ONE bounded ``capture_exact`` audit is
        appended: ``detail`` carries only the fixed quality token — never
        pid/pgid/started_at/exe_path/cmdline/signature/nonce.  Raw exact
        capture reuses the EXISTING transcript machinery (deterministic
        redaction + AEAD + quota + retention + raw-read audit); this surface
        only flips the stored label.  Returns the promoted :class:`Adoption`.
        """
        with self._admission_lock:
            try:
                record = self.adoption_repo.get(session_id)
            except AdoptionRepositoryError as exc:
                raise AdoptionServiceError(exc.code) from None
            if record is None:
                raise AdoptionServiceError("unknown_session")
            if record.status == "revoked":
                raise AdoptionServiceError("adoption_revoked")
            if record.status != "adopted":
                raise AdoptionServiceError("capture_quality_immutable")
            if record.capture_quality == "exact":
                # Idempotent: already-exact adopted seat, no second audit, no
                # second repository write.
                return record
            try:
                promoted = self.adoption_repo.update_capture_quality(
                    session_id, "exact")
            except AdoptionRepositoryError as exc:
                raise AdoptionServiceError(exc.code) from None
            # Bounded code-only audit: never a pid/path/cmdline or envelope
            # internals — only the opaque session id + the quality token.
            self.transcripts.append_audit(
                str(actor), _EXACT_CAPTURE_AUDIT_ACTION, target=session_id,
                detail={"capture_quality": "exact"})
            return promoted

    # -- receipt fan-in (Task 12) ---------------------------------------------------

    def _track(self, command_id: str, *, session_id: str, action: str,
               machine_id: str = "", actor: str = "") -> None:
        """Remember one command this service enqueued (bounded context)."""
        cid = str(command_id or "").strip()
        if not cid:
            return
        # Evict stale entries while we are here (any enqueue is a sweep point).
        self._evict_stale_tracked()
        self._tracked[cid] = {
            "session_id": str(session_id),
            "action": str(action),
            "machine_id": str(machine_id),
            "actor": str(actor),
            # monotonic issued stamp for the M1 TTL bound (never a wire field).
            "_issued_at": time.monotonic(),
        }

    def _evict_stale_tracked(self) -> None:
        """Drop tracked entries older than ``_TRACK_TTL_S`` (bounded TTL).

        A command's settled receipt may never arrive (supervisor lost the
        receipt, hub restart, …); without a TTL an orphaned entry would sit in
        ``_tracked`` forever.  Entries without a stamp are treated as fresh so
        an upgrade never evicts live commands.  After eviction the pending row
        stays visible in the store, so the operator can still revoke/retry; a
        late replayed receipt for the evicted command becomes an inert no-op.
        """
        if not self._tracked:
            return
        cutoff = time.monotonic() - _TRACK_TTL_S
        for cid, entry in list(self._tracked.items()):
            issued = entry.get("_issued_at") if isinstance(entry, dict) else None
            if isinstance(issued, (int, float)) and issued < cutoff:
                self._tracked.pop(cid, None)

    def on_supervisor_receipt(self, command_id: str, *, status: str,
                              reason: str = "") -> dict:
        """Process a Supervisor receipt for a command THIS service issued.

        ``SupervisorService.receipt`` is idempotent but exposes no accessor
        for a receipt's target/action, so the adoption service keeps its own
        ``command_id -> (session_id, action)`` map and the hub's
        ``SupervisorReceiptHook`` fans EVERY recorded receipt in here.  All
        behaviours are bounded and idempotent:

        - untracked / unknown command  -> inert no-op ``{"handled": False}``
          (Task 10's ``cancel_attempt`` receipts and other machines flow
          through untouched);
        - ``adopt``  + ``reason == ADOPT_SUCCESS`` + ``pending`` row ->
          ``pending -> adopted`` (``promoted``); replay on an already adopted
          / revoked / unknown seat is a clean ``noop``;
        - ``detach`` terminal confirmation -> ``{"handled": True,
          "outcome": "dettached"}`` with NO DB mutation;
        - control-class command + ``status == "rejected"`` +
          ``reason in ADOPT_DRIFT_CODES`` on exactly an ``adopted`` seat ->
          auto-revoke (one bounded ``adoption_drift`` audit + ONE zero-signal
          ``detach`` -> ``drift_revoked``); every other reason on a rejected
          control is an inert no-op;
        - the tracking entry is evicted once its FIRST receipt reaches a
          settled state (``promoted`` / ``noop``/terminal for adopt, confirmed
          or terminal for detach, ``drift_revoked`` / terminal no-op for a
          control) so ``_tracked`` stays BOUNDED by in-flight commands; a
          replay after eviction is simply untracked.

        Eviction happens ONLY AFTER a mutation (or a settled no-op) has
        succeeded: a genuine ``AdoptionRepositoryError`` from the store is
        never swallowed here and leaves the entry in place, so a retried
        receipt re-promotes / re-revokes (brief Step 3 idempotency) while the
        hub's 500-protect handler still applies.  Any OTHER receipt-visible
        failure is an inert bounded no-op that never raises.
        """
        cid = str(command_id or "")
        # M1: every receipt handling sweeps the TTL bound first, so a late
        # replayed receipt for an evicted command is untracked -> inert no-op.
        self._evict_stale_tracked()
        tracked = self._tracked.get(cid)
        if tracked is None:
            # ``_tracked`` is only a cache: after a hub restart (or TTL
            # eviction) the receipt is resolved from the DURABLE supervisor
            # command row (session_id / action / machine) instead of dropped.
            tracked = self._resolve_persisted(cid)
            if tracked is None:
                return {"command_id": cid, "handled": False}
        session_id = str(tracked["session_id"])
        action = str(tracked["action"])
        status_text = str(status or "")[:32]
        reason_text = str(reason or "")[:120]
        if action == "adopt":
            result = self._receipt_adopt(cid, session_id, status_text,
                                         reason_text, tracked)
        elif action == "detach":
            result = self._receipt_detach(cid, session_id, status_text)
        else:
            # A source-control / operator-control command.
            if status_text == "rejected" and reason_text in ADOPT_DRIFT_CODES:
                result = self._drift_revoke(cid, session_id, reason_text,
                                            tracked)
            else:
                result = None
            if result is None:
                if status_text in _TERMINAL_RECEIPTS:
                    # A settled non-drift terminal receipt: the seat keeps its
                    # state, no further receipt can do anything — evict.
                    self._tracked.pop(cid, None)
                return {"command_id": cid, "handled": False}
        # Evict the entry once its FIRST receipt reached a settled state
        # (promoted / no-op for adopt, confirmed for detach, drift-revoked /
        # no-op for a control).  ``SupervisorService.receipt`` is idempotent:
        # a replayed receipt re-enters here and the store transitions
        # (``update_status``) are idempotent too, so an entry may safely
        # disappear the moment the command settled.  This keeps ``_tracked``
        # BOUNDED by in-flight commands instead of cumulative adoptions
        # (``accepted``/``executing`` alone never settle an adopt).  A genuine
        # ``AdoptionRepositoryError`` raised ABOVE never reaches this pop, so
        # a store failure keeps the entry and a retried receipt re-promotes
        # (brief Step 3: idempotent under retries).
        if result.get("handled") is True or status_text in _TERMINAL_RECEIPTS:
            self._tracked.pop(cid, None)
        return result

    def _resolve_persisted(self, cid: str) -> dict | None:
        """Rebuild a tracking entry from the durable supervisor command row.

        Only commands targeting an adoption session (``adopt_*``) that exists
        in the adoption store and whose action this service issues are
        resolved; everything else stays an inert untracked receipt.  A
        supervisor lookup failure is treated as untracked (never raised); a
        genuine adoption-store failure propagates like every other store
        error on the receipt path.
        """
        if not cid:
            return None
        lookup = getattr(self.supervisor, "receipt_status", None)
        if not callable(lookup):
            return None
        try:
            command = lookup(cid)
        except Exception:
            return None
        if not isinstance(command, dict):
            return None
        session_id = str(command.get("session_id") or "")
        action = str(command.get("action") or "")
        if not session_id.startswith(_ADOPTION_SESSION_PREFIX):
            return None
        if action not in ({"adopt", "detach"} | _SOURCE_CONTROL_ACTIONS):
            return None
        try:
            record = self.adoption_repo.get(session_id)
        except AdoptionRepositoryError as exc:
            if exc.code == "invalid_adoption":
                return None
            raise
        if record is None:
            return None
        return {
            "session_id": session_id,
            "action": action,
            "machine_id": str(command.get("machine_id") or record.machine_id),
            "actor": str(record.actor or ""),
            "recovery": command.get("reason_code") == "adoption_recovery",
        }

    def _receipt_adopt(self, cid: str, session_id: str, status: str,
                       reason: str, tracked: dict | None = None) -> dict:
        """Promote a ``pending`` seat on the probe's successful adopt receipt.

        A genuine ``AdoptionRepositoryError`` (from the ``get`` OR the
        ``update_status`` store write) propagates UNCHANGED and the tracking
        entry is NOT popped first — so the hub's 500-protect handler sees the
        failure and a retried receipt still finds the entry and re-promotes
        (brief Step 3: idempotent under retries).  Eviction is the caller's
        job, once a result has settled.
        """
        if reason != ADOPT_SUCCESS:
            if (status == "rejected" and reason in ADOPT_DRIFT_CODES
                    and (tracked or {}).get("recovery")):
                drift = self._drift_revoke(cid, session_id, reason, tracked or {})
                if drift.get("handled"):
                    return drift
            # An adopt-denied / refused receipt never promotes the store; a
            # TERMINAL refusal closes the still-pending seat instead of
            # leaving it blocking re-adopt / revoke forever.
            if status in _ADOPT_FAILED_RECEIPTS:
                return self._fail_pending_adopt(cid, session_id, status,
                                                reason, tracked or {})
            return {"command_id": cid, "handled": False}
        record = self.adoption_repo.get(session_id)
        if record is not None and record.status == "pending":
            self.adoption_repo.update_status(session_id, "adopted")
            return {"command_id": cid, "handled": True,
                    "outcome": "promoted", "session_id": session_id}
        # already adopted / already revoked / unknown — clean no-change replay
        return {"command_id": cid, "handled": True,
                "outcome": "noop", "session_id": session_id}

    def _fail_pending_adopt(self, cid: str, session_id: str, status: str,
                            reason: str, tracked: dict) -> dict:
        """Close a ``pending`` seat after a terminal adopt refusal.

        ``pending -> revoked`` only (an ``adopted`` seat — an earlier adopt
        already succeeded — is never touched), ONE bounded audit, and for an
        indeterminate outcome (``failed``/``expired``) ONE cleanup detach.
        """
        with self._admission_lock:
            if not self.adoption_repo.fail_pending(session_id):
                return {"command_id": cid, "handled": True,
                        "outcome": "noop", "session_id": session_id}
            self.transcripts.append_audit(
                str(tracked.get("actor") or ""), _ADOPT_REJECTED_AUDIT_ACTION,
                target=session_id,
                detail={"status": status, "reason": reason[:32]})
            detach_enqueued = False
            if status in _ADOPT_INDETERMINATE_RECEIPTS:
                machine_id = str(tracked.get("machine_id") or "")
                if not machine_id:
                    record = self.adoption_repo.get(session_id)
                    machine_id = record.machine_id if record else ""
                try:
                    envelope = self.supervisor.enqueue(
                        machine_id, session_id, None, "detach",
                        _ADOPT_CLEANUP_DETACH_REASON,
                        nonce=secrets.token_hex(16))
                except Exception:
                    envelope = {}
                detach_cid = str(envelope.get("command_id") or "")
                if detach_cid:
                    detach_enqueued = True
                    self._track(detach_cid, session_id=session_id,
                                action="detach", machine_id=machine_id,
                                actor=str(tracked.get("actor") or ""))
            return {"command_id": cid, "handled": True,
                    "outcome": "rejected", "session_id": session_id,
                    "detach_enqueued": detach_enqueued}

    def _receipt_detach(self, cid: str, session_id: str, status: str) -> dict:
        """A tracked ``detach`` command's terminal confirmation — no mutation."""
        if status in ("succeeded", "already_finished"):
            return {"command_id": cid, "handled": True,
                    "outcome": "dettached", "session_id": session_id}
        return {"command_id": cid, "handled": False}

    def _drift_revoke(self, cid: str, session_id: str, reason: str,
                      tracked: dict) -> dict:
        """Auto-revoke an ADOPTED seat after a drift refusal.

        The domain forbids ``pending -> revoked`` directly, so a pending /
        already-revoked / unknown seat is a neutral, never-raising no-op.  A
        real drift stays: revoke, ONE ``adoption_drift`` audit (bounded
        detail), and exactly ONE ``detach`` with zero signal fields.  A
        genuine ``AdoptionRepositoryError`` from the store write propagates
        with the entry still tracked (never popped before the mutation
        succeeds) so a retried drift receipt still revokes.
        """
        with self._admission_lock:
            record = self.adoption_repo.get(session_id)
            if record is None or record.status != "adopted":
                return {"command_id": cid, "handled": False}
            _revoked, captured = self.adoption_repo.revoke_cas(session_id)
            if not captured:
                # Operator revoke (or another drift) already won the CAS.
                return {"command_id": cid, "handled": False}
            # Bounded code-only detail: only the fixed reason + policy token —
            # never pid/pgid/started_at/exe_path/cmdline/signature/nonce.
            self.transcripts.append_audit(
                str(tracked.get("actor") or ""), _DRIFT_AUDIT_ACTION,
                target=session_id,
                detail={"reason": reason, "policy": _DRIFT_POLICY_TOKEN})
            envelope = self.supervisor.enqueue(
                str(tracked.get("machine_id") or ""), session_id, None,
                "detach", _DRIFT_DETACH_REASON, nonce=secrets.token_hex(16))
            detach_cid = str(envelope.get("command_id") or "")
            if detach_cid:
                self._track(detach_cid, session_id=session_id, action="detach",
                            machine_id=str(tracked.get("machine_id") or ""),
                            actor=str(tracked.get("actor") or ""))
            return {"command_id": cid, "handled": True,
                    "outcome": "drift_revoked", "session_id": session_id,
                    "detach_enqueued": bool(detach_cid)}

    # -- helpers -----------------------------------------------------------------

    def _positive_pgid(self, value) -> int | None:
        """A positive pgid, or None when absent / non-positive."""
        pgid = self._as_int(value)
        return pgid if pgid > 0 else None

    def _enqueue_adopt(self, record: Adoption, actor: str, *, recovery: bool = False) -> dict:
        """Sign exactly one ``adopt`` for ``record`` and track it."""
        envelope = self.supervisor.enqueue(
            record.machine_id, record.session_id, None, "adopt",
            "adoption_recovery" if recovery else "operator_adopt",
            nonce=secrets.token_hex(16),
            candidate={
                "pid": record.pid,
                "started_at": record.started_at,
                "exe_path": str(record.exe_path or ""),
                "agent_family": record.agent_family,
                "native_file_path": record.native_file_path,
            })
        self._track(str(envelope.get("command_id") or ""),
                    session_id=record.session_id, action="adopt",
                    machine_id=record.machine_id, actor=actor)
        if recovery:
            self._tracked[str(envelope["command_id"])]["recovery"] = True
        return envelope


__all__ = ["AdoptionService", "AdoptionServiceError", "Candidate"]
