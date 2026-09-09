"""Unified error contract and per-request request_id for the HTTP layer.

The Task 8 contract:

* ``ApplicationError(code, detail, status=400)`` is the canonical bounded
  application-error type. It is re-exported from the application service so
  every adapter and the services share a single class (no duplicate error
  types to catch at the boundary).
* ``error_response(error, request_id)`` serializes any stable error into the
  stable shape ``{"ok": false, "error": <code>, "detail": <bounded>,
  "request_id": <opaque>}``. It never leaks exception reprs, absolute paths,
  SQLite contents, or credentials: only the bounded ``code`` / ``detail``
  attributes survive, and ``detail`` is truncated.
* ``attach_request_id`` is a Flask ``before_request`` hook that stamps an
  opaque per-request id at the transport boundary.
"""
import uuid

from flask import g, jsonify, request

from hub.application.task_service import ApplicationError

__all__ = [
    "ApplicationError",
    "attach_request_id",
    "error_response",
    "get_request_id",
    "register_error_handlers",
]


def get_request_id() -> str:
    """Return the per-request opaque request id, minting one on first use."""
    rid = g.get("request_id")
    if not rid:
        rid = uuid.uuid4().hex
        g.request_id = rid
    return rid


def attach_request_id():
    """before_request hook: stamp each request with an opaque transport id."""
    g.request_id = uuid.uuid4().hex
    return None


def error_response(error, request_id: str | None = None):
    """Convert a stable error into the unified ``{ok:false,...}`` response.

    ``error`` may be any object exposing ``code`` / ``detail`` / ``status``
    (``ApplicationError``, ``DomainError``, ``ObserveError``). Bounds are
    applied defensively: floats/None details are coerced to text and truncated
    so the public error never exposes internals.
    """
    code = getattr(error, "code", None) or "internal_error"
    detail = getattr(error, "detail", None) or ""
    detail = str(detail)[:200]
    status = int(getattr(error, "status", 400))
    rid = request_id or get_request_id()
    return jsonify({
        "ok": False,
        "error": str(code),
        "detail": detail,
        "request_id": rid,
    }), status


def _is_api_path() -> bool:
    """True when the current request targets an ``/api/*`` endpoint.

    API 404/405/500 responses are converted to the bounded JSON contract;
    page routes keep their compatibility plain-text behavior.
    """
    return request.path.startswith("/api/") or request.path == "/api"


def register_error_handlers(app):
    """Bind the unified HTTP error contract at the app edge.

    - Unknown ``/api/*`` routes (404) and disallowed methods (405) return the
      bounded ``{ok:false,...}`` JSON shape instead of the Flask HTML page.
    - Unexpected programmer exceptions on ``/api/*`` are logged by Flask and
      serialized as a bounded 500 JSON response. ``detail`` is always a fixed
      limited message; no raw exception repr, SQL, path, or credential leaks.
    - Page routes keep their compatibility plain-text response.

    The handlers must be registered after blueprints so ``jsonify`` and
    ``request`` see the final app.
    """

    @app.errorhandler(404)
    def _not_found(exc):
        if _is_api_path():
            return error_response(ApplicationError(
                "not_found", "接口不存在", 404))
        return "not found", 404

    @app.errorhandler(405)
    def _method_not_allowed(exc):
        if _is_api_path():
            return error_response(ApplicationError(
                "method_not_allowed", "接口不支持该请求方法", 405))
        return "method not allowed", 405

    @app.errorhandler(500)
    def _internal_error(exc):
        # Flask already logged the traceback; the public body is bounded.
        if _is_api_path():
            return error_response(ApplicationError(
                "internal_error", "服务器内部错误，请稍后重试", 500))
        return "internal error", 500

    return app
