"""hub/http — thin HTTP adapters for the fleet hub.

Each blueprint here only performs transport duties: parse the request, obtain
the authenticated identity, invoke one application service, and serialize the
result. All business logic lives in ``hub.application`` services; adapters get
their services from ``current_app.extensions["fleet"]`` and never reference the
legacy module-level storage constants or the hosts-file parser for business
decisions.
"""

from hub.http.errors import ApplicationError, error_response

__all__ = ["ApplicationError", "error_response"]