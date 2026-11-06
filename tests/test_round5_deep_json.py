"""Round-5 A3 (all modules): deeply nested JSON is a bounded 400, never a 500.

``request.get_json(silent=True)`` only swallows ``ValueError``; decoding a
deeply nested array raises ``RecursionError``.  Every route module that reads
a request body goes through ``hub.http._common.request_json`` which maps that
(and only the decode step) to ``invalid_json``.  One endpoint per module.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from hub.bootstrap import create_app
from hub.config import FleetConfig
from test_account_http import ORIGIN   # sibling import (tests/ is a non-package)
from test_adoption_http import (
    build_app,
    ingest_token_headers,
    operator_headers,
    runner_headers,
)

DEEP = "[" * 100000 + "]" * 100000
OPERATOR = "owner@example.test"


def _session_app(tmp_path):
    return build_app(tmp_path)


def _platform_app(tmp_path):
    return create_app(FleetConfig.from_root(
        tmp_path, ingest_token="ingest-only", dev_operator=OPERATOR,
        platform_enabled=True, platform_memory_enabled=True))


def _account_app(tmp_path):
    app = create_app(FleetConfig.from_root(
        tmp_path, platform_enabled=True,
        accounts={"enabled": True, "origin": ORIGIN, "registration": "open",
                  "sender": lambda *a: None}))
    app.testing = True
    return app


_OP = {"Cf-Access-Authenticated-User-Email": OPERATOR}
_PLATFORM_OP = {**_OP, "Origin": "http://localhost"}

CASES = [
    # module, app builder, method, path, headers
    ("adoption_routes", _session_app, "POST", "/api/adoptions", operator_headers()),
    ("command_routes", _session_app, "POST", "/api/commands/poll", runner_headers()),
    ("task_routes", _session_app, "POST", "/api/tasks", operator_headers()),
    ("session_routes:ingest", _session_app, "POST", "/api/session-events",
     ingest_token_headers()),
    ("session_routes:runner", _session_app, "POST", "/api/runner-session-events",
     runner_headers()),
    ("session_routes:message", _session_app, "POST", "/api/sessions/s1/messages",
     operator_headers()),
    ("session_routes:handoff", _session_app, "POST", "/api/sessions/s1/handoff",
     operator_headers()),
    ("observe_routes", _session_app, "POST", "/api/ingest", ingest_token_headers()),
    ("platform_routes", _platform_app, "POST",
     "/api/platform/v1/nodes/n1/credential", _PLATFORM_OP),
    ("memory_routes", _platform_app, "DELETE", "/api/platform/v1/memory/m1", _PLATFORM_OP),
    ("legacy_task_routes", _platform_app, "POST",
     "/api/platform/v1/runs/r1/legacy-task", _PLATFORM_OP),
    ("execution_window_routes", _platform_app, "POST",
     "/api/platform/v1/execution-windows", _PLATFORM_OP),
    ("muse_routes", _platform_app, "POST", "/api/platform/v1/muse/login", _PLATFORM_OP),
    ("accounts.http", _account_app, "POST", "/api/accounts/login", {"Origin": ORIGIN}),
]


@pytest.mark.parametrize("module, builder, method, path, headers", CASES,
                         ids=[case[0] for case in CASES])
def test_deeply_nested_json_is_bounded_invalid_json(tmp_path, module, builder,
                                                    method, path, headers):
    client = builder(tmp_path).test_client()
    kwargs = {"base_url": ORIGIN} if builder is _account_app else {}
    resp = client.open(path, method=method, data=DEEP,
                       content_type="application/json", headers=headers, **kwargs)
    assert resp.status_code == 400, (module, resp.status_code, resp.get_data(as_text=True)[:200])
    assert resp.get_json()["error"] == "invalid_json"


def test_muse_body_error_outside_invoke_is_bounded_not_500(tmp_path):
    # ``body()`` runs before ``invoke`` on /login: a non-object body used to
    # escape as an unhandled MuseError (500).
    client = _platform_app(tmp_path).test_client()
    resp = client.post("/api/platform/v1/muse/login", json=[1],
                       headers=_PLATFORM_OP)
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "invalid_json"
    assert resp.headers["Cache-Control"] == "no-store"


def test_hub_parses_request_bodies_only_through_request_json():
    # Architecture guard: a raw ``get_json`` in a new route would bring back the
    # RecursionError -> 500 path; all body parsing goes through request_json.
    hub = Path(__file__).resolve().parents[1] / "hub"
    offenders = [
        f"{path.relative_to(hub.parent)}:{number}"
        for path in sorted(hub.rglob("*.py"))
        if path.name != "_common.py" or path.parent.name != "http"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"\bget_json\s*\(|\brequest\.json\b|\brequest\.data\b", line)
    ]
    assert offenders == []
