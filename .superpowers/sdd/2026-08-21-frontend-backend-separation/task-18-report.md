# Task 18 report — versioned API compatibility adapters

## Status
COMPLETE — commit `feat: add versioned API compatibility adapters`.

## Scope decision
- Added `/api/v1` versioned blueprints for the observe (status / machine detail / events / stream) and task (create/list/detail/cancel/retry) surfaces.
- Runner v1 command paths were **not** registered. The full poll/heartbeat/result compatibility contract (lease TTL, attempt_id idempotency, nonce, machine identity from authenticated `g.runner_machine`) is only demonstrated/verified on the legacy `/api/commands/*`; old commands stay authoritative for deployed runners. `hub/http/v1/command_routes.py` documents this decision and registers nothing. A contract test asserts no v1 command URL rule exists and stray runner-style requests fail closed with a bounded JSON error.

## Changed files
- **Create** `hub/http/v1/__init__.py` — package docstring: v1 blueprints are thin transport aliases over existing views.
- **Create** `hub/http/v1/observe_routes.py` — v1 observe blueprint (`/api/v1/status`, `/api/v1/machines/<name>`, `/api/v1/events`, `/api/v1/stream`) re-registering the exact legacy view callables.
- **Create** `hub/http/v1/task_routes.py` — v1 task blueprint (`/api/v1/tasks` POST/GET, `/api/v1/tasks/<id>`, cancel, retry) re-registering the same auth-wrapped callables from `hub/http/task_routes.py`.
- **Create** `hub/http/v1/command_routes.py` — deliberate non-registration; docstring-only rationale.
- **Modify** `hub/bootstrap.py` — import and register the two v1 blueprints with explicit `register_blueprint` calls (prefixes declared in each v1 blueprint constructor via `url_prefix="/api/v1"`).
- **Modify** `tests/test_http_contracts.py` — new `VersionedApiTests` class.

Not changed: `hub/http/errors.py`. `/api/v1/*` paths already begin with `/api/` so `_is_api_path()` in `register_error_handlers` covers v1 404/405/500 with the same bounded JSON contract; no `errors.py` edit was necessary (coordinator guidance: avoid gratuitous edits).

## Public routes added (explicit `/api/v1` prefixes)
| Method | Route | Legacy delegate |
|---|---|---|
| GET | `/api/v1/status` | `hub.http.observe_routes.api_status` |
| GET | `/api/v1/machines/<name>` | `hub.http.observe_routes.api_machine` |
| GET | `/api/v1/events` | `hub.http.observe_routes.api_events` |
| GET | `/api/v1/stream` | `hub.http.observe_routes.api_stream` |
| POST | `/api/v1/tasks` | `hub.http.task_routes.create_task` |
| GET | `/api/v1/tasks` | `hub.http.task_routes.list_tasks` |
| GET | `/api/v1/tasks/<task_id>` | `hub.http.task_routes.get_task` |
| POST | `/api/v1/tasks/<task_id>/cancel` | `hub.http.task_routes.cancel_task` |
| POST | `/api/v1/tasks/<task_id>/retry` | `hub.http.task_routes.retry_task` |
| —   | Runner v1 command paths | intentionally NOT registered (old `/api/commands/*` authoritative) |

Each v1 route is the *same callable* as the legacy route, so it shares the identical injected service objects from `app.extensions["fleet"]["services"]`, the same SSE transport helpers, sanitization, error response shape (`{ok:false,error,detail,request_id}`), and public DTO serialization. No duplicated validation, state transitions, repository access, remote execution, credentials, CORS, or frontend changes were introduced.

## Tests and exact outcomes
- `tests.test_http_contracts.VersionedApiTests` — 12 new tests.
  - status/machine-detail/events old-vs-v1 public-shape equivalence
  - machine-not-found 404 unified shape
  - SSE stream transport headers (`text/event-stream`, `Cache-Control`, `X-Accel-Buffering`, `:connected` handshake)
  - task error preserves stable fields on `{}` body; validation error exactly matches legacy (`invalid_machine`)
  - task CRUD: create 201 → list → detail (public DTO omits internal `attempt_id`/`client_token`) → cancel (`cancelled`) → retry (`queued`) → cancel-404
  - operator identity required on v1 (401 without it); runner credential header cannot impersonate operator; foreign ingest header blocks DEV fallback; observe surface public
  - runner-v1-commands-unregistered contract
- **Focused suite (mandated command):** `.venv/bin/python -m unittest tests.test_http_contracts tests.test_auth_matrix tests.test_task_api tests.test_observability -v` → 99 tests, **1 failure**, which is the **known pre-existing routing failure** `test_unknown_api_route_post_404_is_bounded_json` (405 != 404), recorded in `progress.md` and untouched by this task.
- **Backend suite:** `tests.test_auth_matrix tests.test_task_api tests.test_observability tests.test_regressions tests.test_push_only tests.test_runner` → 130 tests, 1 failure — the other known pre-existing routing failure `test_regressions.IngestApiSecurityTests.test_scan_endpoint_requires_post_and_token` (404 != 405).
- **Full suite:** `unittest discover -s tests` → **396 tests, 2 failures** — exactly the two known pre-existing hub-routing failures listed in `progress.md`:
  1. `test_http_contracts.ApiEdgeErrorContractTests.test_unknown_api_route_post_404_is_bounded_json` — 405 != 404
  2. `test_regressions.IngestApiSecurityTests.test_scan_endpoint_requires_post_and_token` — 404 != 405
  Both reproduce unchanged against the baseline (not introduced by Task 18). Baseline was 384 tests / 2 failures; my additions are +12 tests / +0 new failures.
- `compileall -q hub tests` → exit 0.
- `git diff --check` → clean (no whitespace errors).

## Compatibility / security checks
- Old `/api/*` routes are untouched and remain authoritative; v1 reuses the same view functions, so behavior cannot drift.
- No runner v1 command paths registered → deployed runner compatibility surface unchanged.
- No raw exceptions, absolute paths, SQL fragments, or credentials in new code or error bodies (new v1 errors reuse the shared bounded `error_response`).
- All new v1 task routes reuse `require_operator` / `require_task_store`; observe v1 routes are read-only/public, matching legacy.
- No CORS or frontend changes; auth-domain mutual exclusion remains intact (verified by the 3-domain auth matrix tests running against v1 in `VersionedApiTests`).

## Concerns / notes
- The two pre-existing full-suite routing failures (POST /api-404 differing from 404; /api/scan GET vs POST-status) remain and are deliberately not fixed under the Task 18 扩scope rule. They pre-date this change and are documented in `progress.md`.
- The `frontend/index.html` catch-all asset route (Task 16) remains the reason POST to unknown `/api/*` paths yields 405 instead of 404; separate from Task 18.
- Runner v1 remains an intentional gap recorded in `hub/http/v1/command_routes.py`; the brief nowhere requires shipping it, and the "full contract test" gate was not met, so none was added.