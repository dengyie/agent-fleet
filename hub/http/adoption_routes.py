"""hub/http/adoption_routes.py — operator adoption (纳管) HTTP adapter (Task 6).

Routes (all ``@require_operator``; ``g.operator`` is the actor and the
machine/pid/started_at come from the query/POST body, never from the operator
identity):

  GET    /api/adoptions?machine_id=<machine>    operator management view
  POST   /api/adoptions {"machine_id", "pid", "started_at"}
  DELETE /api/adoptions/<session_id>
  POST   /api/adoption/<session_id>/source/control
         {"action": pause_session|resume_session|terminate_session|
          quarantine_session|cancel_attempt|append_user_turn|
          apply_local_profile, "reason_code": str,
          "payload": optional bounded object}
         (singular "adoption" — operator source control for an ADOPTED seat
         via the existing signed supervisor channel; 202 + command id +
         bounded "pending". Phase 4/5 actions are default-off. NO pid/
         signal/shell/command/credential is ever accepted.)
  POST   /api/adoptions/<session_id>/retry
         (plural "adoptions" — re-issue the signed adopt for a leftover
         pending seat; 200 + bounded status projection.  An already-adopted
         seat is idempotent; a revoked seat is 409 ``adoption_revoked``.
         No body is parsed or echoed.)
  POST   /api/adoptions/<session_id>/capture-exact
         (plural "adoptions" — the Task 11 operator exact-capture upgrade: an
         empty state-transition POST that promotes an ADOPTED seat to
         capture_quality "exact"; 200 + bounded quality/status projection, ONE
         "capture_exact" audit.  No body is parsed or echoed.)

Transport only: operator auth -> JSON parse -> ``AdoptionService`` -> framed
bounded error shape, mirroring the supervisor route adapter.  The raw pid /
pgid / exe_path / cmdline / native_file_path / started_at are PROBE-PRIVATE
and never leave this module: the public surface carries the opaque session id
and a bounded projection only.  Adoption errors surface as stable codes
(``stale_candidate`` / ``not_attachable`` / ``unsupported_candidate`` /
``invalid_adoption`` / ``unknown_session`` / ``invalid_status_transition`` /
``capture_quality_immutable``), never exception text, paths, PIDs or names.
"""
from flask import Blueprint, current_app, g, jsonify, request

from hub.application.adoption_service import (
    AdoptionService,
    AdoptionServiceError,
)
from hub.application.supervisor_service import SupervisorServiceError
from hub.auth import require_operator
from hub.http.errors import ApplicationError, error_response, get_request_id
from hub.infrastructure.adoption_repository import AdoptionRepositoryError

bp = Blueprint("adoptions", __name__)

#: Bounded public fields of an adoption record.  Deliberately excludes pid,
#: pgid, exe_path, cmdline, native_file_path and started_at (probe-private).
_BOUNDED_FIELDS = (
    "adoption_id",
    "machine_id",
    "session_id",
    "agent_family",
    "capture_quality",
    "status",
    "actor",
    "created_at",
    "updated_at",
)


def _adoptions() -> AdoptionService:
    fleet = current_app.extensions.get("fleet", {})
    service = fleet.get("services", {}).get("adoptions")
    if service is None:
        raise RuntimeError("adoptions service not wired")
    return service


def _json_object() -> tuple[dict | None, object | None]:
    data = request.get_json(silent=True)
    if data is None:
        return {}, None
    if not isinstance(data, dict):
        return None, error_response(ApplicationError(
            "invalid_json", "请求体必须是 JSON 对象", 400))
    return data, None


def _project(record) -> dict:
    """A bounded public projection of one adoption record (no probe fields)."""
    return {name: getattr(record, name) for name in _BOUNDED_FIELDS}


def _service_error(exc: AdoptionServiceError):
    """Map adoption-service codes to the unified bounded error shape."""
    if exc.code in ("invalid_adoption", "unknown_session"):
        return error_response(ApplicationError(exc.code, "", 404))
    if exc.code in ("invalid_status_transition", "adoption_revoked",
                    "capture_quality_immutable"):
        return error_response(ApplicationError(exc.code, "", 409))
    return error_response(ApplicationError(exc.code, "", 400))


def _supervisor_error(exc: SupervisorServiceError):
    """A supervisor issuance failure (e.g. no signing key) is bounded too."""
    return error_response(ApplicationError(
        exc.code, exc.detail, exc.status))


@bp.route("/api/adoptions", methods=["POST"])
@require_operator
def adopt():
    data, err = _json_object()
    if err:
        return err
    machine_id = data.get("machine_id")
    pid = data.get("pid")
    started_at = data.get("started_at")
    if not isinstance(machine_id, str) or not machine_id.strip():
        return error_response(ApplicationError(
            "invalid_request", "machine_id 缺失", 400))
    actor = g.get("operator") or ""
    try:
        record = _adoptions().adopt(machine_id, pid, started_at, actor)
    except AdoptionServiceError as exc:
        return _service_error(exc)
    except SupervisorServiceError as exc:
        return _supervisor_error(exc)
    return jsonify({
        "ok": True,
        "adoption_id": str(record.adoption_id),
        "session_id": str(record.session_id),
        "machine_id": str(record.machine_id),
        "status": str(record.status),
        "request_id": get_request_id(),
    })


@bp.route("/api/adoptions/<session_id>", methods=["DELETE"])
@require_operator
def revoke(session_id: str):
    actor = g.get("operator") or ""
    try:
        _adoptions().revoke(session_id, actor)
    except AdoptionServiceError as exc:
        return _service_error(exc)
    except AdoptionRepositoryError as exc:
        # A malformed session id fails the shared id boundary inside the
        # service's look-up; surface the same bounded code as the service.
        return _service_error(AdoptionServiceError(exc.code))
    except SupervisorServiceError as exc:
        return _supervisor_error(exc)
    return jsonify({
        "ok": True,
        "session_id": str(session_id),
        "request_id": get_request_id(),
    }), 202


@bp.route("/api/adoptions", methods=["GET"])
@require_operator
def list_records():
    machine_id = request.args.get("machine_id")
    if not isinstance(machine_id, str) or not machine_id.strip():
        return error_response(ApplicationError(
            "invalid_request", "machine_id 缺失", 400))
    items = _adoptions().adoption_repo.list(machine_id)
    return jsonify({
        "ok": True,
        "machine_id": machine_id,
        "items": [_project(record) for record in items],
        "request_id": get_request_id(),
    })


@bp.route("/api/adoption/<session_id>/source/control", methods=["POST"])
@require_operator
def source_control(session_id: str):
    """POST one fixed source-control action for an ADOPTED session.

    Read ``action``, ``reason_code``, and (for Phase 4/5 only) a bounded
    ``payload``.  Attacker-sent ``pid``/``signal``/``shell``/``command``/
    credential keys are never used, never echoed, never signed.  The service
    looks up the seat and enqueues exactly one signed command through the
    existing supervisor channel; the probe guard (Task 9) stays the final
    authority.  Queueing is not execution.
    """
    data, err = _json_object()
    if err:
        return err
    action = data.get("action")
    reason_code = data.get("reason_code")
    if not isinstance(action, str) or not action.strip():
        return error_response(ApplicationError(
            "invalid_request", "action 缺失", 400))
    if not isinstance(reason_code, str) or not reason_code.strip():
        return error_response(ApplicationError(
            "invalid_request", "reason_code 缺失", 400))
    payload = data.get("payload") if action in (
        "append_user_turn", "apply_local_profile") else None
    actor = g.get("operator") or ""
    try:
        result = _adoptions().control(
            session_id, action=action, reason_code=reason_code, actor=actor,
            payload=payload)
    except AdoptionServiceError as exc:
        return _service_error(exc)
    except AdoptionRepositoryError as exc:
        # A malformed session id fails the shared id boundary inside the
        # service's look-up; surface the same bounded code as the service.
        return _service_error(AdoptionServiceError(exc.code))
    except SupervisorServiceError as exc:
        return _supervisor_error(exc)
    return jsonify({
        "ok": True,
        "command_id": result["command_id"],
        "session_id": result["session_id"],
        "action": action,
        "status": result["status"],
        "request_id": get_request_id(),
    }), 202


@bp.route("/api/adoptions/<session_id>/retry", methods=["POST"])
@require_operator
def retry(session_id: str):
    """POST a leftover pending adoption to re-issue its signed adopt.

    A state-transition POST: the request body is ignored.  The service
    reuses the existing session id and enqueues exactly one new signed
    command.  Public fields stay bounded (no pid/path/cmdline).
    """
    actor = g.get("operator") or ""
    try:
        record = _adoptions().retry(session_id, actor)
    except AdoptionServiceError as exc:
        return _service_error(exc)
    except AdoptionRepositoryError as exc:
        return _service_error(AdoptionServiceError(exc.code))
    except SupervisorServiceError as exc:
        return _supervisor_error(exc)
    return jsonify({
        "ok": True,
        "adoption_id": str(record.adoption_id),
        "session_id": str(record.session_id),
        "machine_id": str(record.machine_id),
        "status": str(record.status),
        "request_id": get_request_id(),
    })


@bp.route("/api/adoptions/<session_id>/capture-exact", methods=["POST"])
@require_operator
def capture_exact(session_id: str):
    """POST an ADOPTED session's capture-quality upgrade to ``exact``.

    A state-transition POST, not a payload POST: the request body is
    intentionally ignored (any attacker-sent field is silently dropped —
    never parsed, never echoed, never signed).  The service promotes the seat
    atomically in the adoption store and appends ONE bounded ``capture_exact``
    audit; raw exact capture reuses the existing transcript
    redaction/AEAD/quota/retention machinery.  Idempotent on an
    already-``exact`` adopted seat (200 again, no second audit).  Errors use
    the unified bounded shape (``ok:false`` + ``error`` code): ``unknown_session``
    and malformed ``invalid_adoption`` -> 404; ``capture_quality_immutable``
    (a pending seat) and ``adoption_revoked`` -> 409.
    """
    actor = g.get("operator") or ""
    try:
        record = _adoptions().upgrade_capture_exact(session_id, actor)
    except AdoptionServiceError as exc:
        return _service_error(exc)
    except AdoptionRepositoryError as exc:
        # A malformed session id fails the shared id boundary inside the
        # service's look-up; surface the same bounded code as the service.
        return _service_error(AdoptionServiceError(exc.code))
    return jsonify({
        "ok": True,
        "session_id": str(record.session_id),
        "capture_quality": str(record.capture_quality),
        "status": str(record.status),
        "request_id": get_request_id(),
    })


__all__ = ["bp"]