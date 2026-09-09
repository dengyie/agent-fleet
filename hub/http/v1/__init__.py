"""hub/http/v1 — versioned API compatibility adapters.

The ``/api/v1/*`` surface re-registers the exact view functions from the
legacy ``/api/*`` adapters under an explicit versioned prefix. Each blueprint
here is a thin transport alias: it binds an existing view callable to a v1 URL
so business logic, validation, auth, SSE mapping, error serialization, and
public DTOs have exactly one implementation. No state transition, repository
access, or policy body is duplicated in this package.
"""