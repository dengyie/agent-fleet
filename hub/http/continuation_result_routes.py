"""Machine-bound result ingestion; operator-only bounded snapshot reads."""
from flask import Blueprint, current_app, g, jsonify, request

from hub.application.errors import ApplicationError
from hub.application.session_service import SessionServiceError
from hub.application.supervisor_service import SupervisorServiceError
from hub.auth import require_operator, require_runner
from hub.http._common import request_json
from hub.http.errors import error_response

bp = Blueprint("continuation_results", __name__)


def _service():
    service = current_app.extensions["fleet"]["services"].get("continuation_results")
    if service is None:
        raise ApplicationError("continuation_result_unavailable", "", 503)
    return service


@bp.post("/api/runner-continuation-results")
@require_runner
def publish():
    try:
        return jsonify(_service().publish(g.runner_machine, request_json()))
    except (ApplicationError, SessionServiceError, SupervisorServiceError) as exc:
        return error_response(exc)


@bp.get("/api/sessions/<session_id>/results")
@require_operator
def page(session_id):
    try:
        if any(key not in {"limit", "before"} or len(request.args.getlist(key)) != 1 for key in request.args):
            raise ApplicationError("invalid_cursor", "", 400)
        raw_limit = request.args.get("limit", "20")
        raw_before = request.args.get("before")
        for value in (raw_limit, raw_before):
            if value is not None and (not value.isascii() or not value.isdecimal() or len(value) > 19 or value.startswith("0")):
                raise ApplicationError("invalid_cursor", "", 400)
        return jsonify(_service().page(session_id, limit=int(raw_limit), before=int(raw_before) if raw_before is not None else None))
    except (ApplicationError, SessionServiceError) as exc:
        return error_response(exc)


@bp.get("/api/sessions/<session_id>/results/<command_id>")
@require_operator
def detail(session_id, command_id):
    try:
        return jsonify(_service().detail(session_id, command_id))
    except (ApplicationError, SessionServiceError) as exc:
        return error_response(exc)
