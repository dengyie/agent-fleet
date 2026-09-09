# Task 16 — Asset Fix Report (static assets during frontend cutover)

Date: 2026-08-23
Branch: worktree on `arch-v2-primary`, based at `3e35e7b`

## Goal

Make `FRONTEND_CUTOVER` serve the static frontend assets through the same
Flask app, so the standalone `frontend/` release is fully servable by the hub
process during cutover.

## Files changed

- `hub/http/pages.py` — added catch-all asset route `/<path:subpath>`.
- `tests/test_task_view_review.py` — added focused asset-serving, traversal,
  and disabled-mode test classes.
- `docs/HANDOFF.md` — added a bullet stating same-app static asset serving.

## What changed

### `hub/http/pages.py`

- Imported `abort` and `send_file`.
- Added an explicit release-layout allowlist:
  `_ALLOWED_ASSET_FILES = ("config.js", "routes.js")` (exact match) and
  `_ALLOWED_ASSET_DIRS = ("styles/", "api/", "views/", "realtime/", "state/")`
  (prefix match).
- Added `@bp.route("/<path:subpath>")` `static_asset(subpath)`:

  - Active only when `_cutover_enabled()` (`FRONTEND_CUTOVER` true). When
    disabled, `abort(404)` keeps prior behavior (existing API JSON 404
    handler for unknown `/api/*`; plain-text 404 for page paths).
  - Only serves release-layout entries matching the allowlist above; any
    out-of-release path (e.g. `/secret.txt`) is rejected with `abort(404)`
    even if the file exists under `FRONTEND_DIR`.
  - Rejects `..`/`.` path segments explicitly BEFORE resolution (after Flask
    URL decoding). The allowlist prefix check alone is insufficient:
    `api/../secret.txt` passes the `api/` prefix yet resolves to a file
    outside the release layout, and `Path.relative_to(root)` still accepts it
    because it stays inside `FRONTEND_DIR`. Segment rejection closes that gap
    (`subpath.split("/")` and `abort(404)` if any segment is `.`/`..`).
  - Resolves the target under `FRONTEND_DIR`, rejecting path traversal via
    `target.relative_to(root)` (raising `ValueError` on escape).
  - Only serves actual files (`target.is_file()`), never directories.
  - Uses Flask `send_file` for correct MIME types (e.g. `.js` →
    `text/javascript`, `.css` → `text/css`).
  - The `api/` allowlist prefix serves frontend `api/*.js` assets
    (`api/client.js`, `api/contracts.js`) because `frontend/index.html`
    imports them. Existing specific API blueprint routes win via Flask route
    priority, and unknown `/api/*` paths still flow through the existing JSON
    404 handler.
  - Bounded failure: `OSError`/`ValueError` → `abort(404)`.

### `tests/test_task_view_review.py`

Added two test classes backed by a new `_RealFrontendCutoverBase` that uses the
real repository `frontend/` dir:

- `CutoverEnabledAssetServingTests` (cutover on): asserts `/config.js`,
  `/styles/app.css`, `/api/client.js`, `/views/fleet.js` return 200 with
  correct MIME types and body content; traversal (`/../../../etc/passwd`) is
  404; nonexistent asset is 404; an out-of-release `/secret.txt` is 404 even
  when the file exists; `/api/../secret.txt` and `/styles/../secret.txt` are
  404 (allowlist-prefix traversal); unknown `/api/*` still hits the JSON 404
  handler (not the asset handler).
- `CutoverDisabledAssetNotServedTests` (cutover off): asserts `/config.js`,
  `/styles/app.css`, `/api/client.js` are NOT served (404); `/api/*` keeps the
  JSON shape.

Note: platform `mimetypes.guess_type` returns `text/javascript` for `.js`, so
the assertions use `text/javascript` (not `application/javascript`).

## Tests run

- `.venv/bin/python -m unittest tests.test_task_view_review tests.test_release_layout -v` — 33 tests OK, including `test_out_of_release_path_404_even_if_file_exists` and `test_allowlist_prefix_traversal_resolves_to_404` (ResourceWarnings from the test client reading `send_file` responses are benign).
- `.venv/bin/python -m compileall hub tests` — exit 0.
- `node --check frontend/views/task.js` — exit 0.
- `git diff --check` — clean.

## Concerns

- The `send_file` responses in the Flask test client emit benign
  `ResourceWarning: unclosed file` warnings because the response buffers are
  not explicitly closed by the test helper. No functional impact.
- `.js` MIME type is `text/javascript` on this platform rather than the modern
  `application/javascript`; cosmetics only, browsers accept both.
- A `.venv` was created locally for running the test suite (the worktree had no
  venv); it is untracked and not part of the commit.