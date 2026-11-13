# Muse Credential Login Implementation Plan

> **Execution status:** This change is documentation and entry-gate work only; it does not implement or enable password login. Production implementation is blocked until a dedicated browser runtime proves the three-layer network boundary in T3.7, a separate authentication-browser contract is approved, and the exact Muse authentication origins are established. Do not add a password form, credential endpoint, browser dependency, deployment service, or enable a network gate before those entry conditions pass.

**Goal:** Let an authenticated Fleet user save an optional Muse login profile and use it to sign in through a disposable, owner-scoped browser session, then import only the validated Muse session cookie into the existing Muse workspace.

**Architecture:** Keep saved login profiles and temporary browser login sessions separate from the existing Muse account/session store. A dedicated login worker is the only component permitted to decrypt a saved password or inspect browser cookies; it is not the generic browser Node and does not expose assistant tools. The worker must enforce the T3.7 browser-native request gate, pinned egress adapter, and process/container egress policy; after Muse authentication it returns only a bounded `hatch_sess` cookie to the existing `MuseService.import_login` validation and encryption path.

**Tech Stack:** Existing Python 3.10+, Flask, SQLite, `cryptography`, existing Muse integration and frontend ES modules. Any browser runtime, sidecar, or deployment dependency requires a separately reviewed runtime design and passing isolation proof before adoption.

## Repository Baseline and Design Decisions

- The current Muse import API is `POST /api/platform/v1/muse/login`; it calls `MuseService.import_login`, validates `hatch_sess` with `normalize_login`, verifies the session through the native Muse client, then persists the encrypted cookie in `MuseRepository`.
- `MuseRepository` owns a private `state_dir/muse/` store and currently encrypts Muse session cookies using a mode-0600 Fernet key. Saved passwords must use an explicitly separate repository record and must never be included in the existing account/status DTOs.
- The production release installs the Python requirements and starts `hub/web.py`. The repository has no production Chromium/Playwright driver or browser-session UI. Playwright is installed only in CI as a fixture test runner.
- `NodeRuntime` forces generic browser network access and `browser.submit` off. T3.7 forbids credentials in generic browser contexts and requires browser-native interception, pinned per-node egress, and OS/container socket enforcement. Its implementation entry gate says to stop if those controls cannot be proven and not to add a dependency or deployment service without a separately reviewed design.
- Current Muse login/profile behavior remains cookie import. The plan does not assume an IdP host, login endpoint, or login protocol absent from repository-owned evidence. Authentication destinations must be exact-origin and exact-auth-route allowlisted after their legitimate flow has been verified; suffix or wildcard matching is prohibited.
- Fleet assistant/model context, Run tools, command journal, browser artifacts, logs, audit records, and status responses must never contain a password, OTP, `hatch_sess`, full Cookie header, or raw authentication response.
- CAPTCHA solving, MFA bypass, recovery-code storage, automatic permission approval, and automatic retries after an uncertain login submission are out of scope. MFA, CAPTCHA, and unsupported challenges require the user to finish authentication manually in Muse and use the current Cookie-import flow.

## Target User Flow

1. In Muse account management, the user explicitly saves a login profile containing a display label, username/email, and password. The UI states that Fleet stores the password encrypted for future Muse login. Password fields are never populated from an API response.
2. The user selects **Log in with profile**. Fleet creates one short-lived login attempt tied to the owner, profile revision, Muse account target, and browser process. Duplicate starts for the same live attempt do not create another browser or submit credentials twice.
3. The dedicated worker opens a fresh, ephemeral browser context at the fixed Muse login entry point, fills only the recognized Muse authentication form, and submits once. This plan does not expose a server-side frame/input takeover for MFA; if the provider requires an unsupported interactive challenge, the attempt stops and the user completes login in their own browser before using the existing Cookie import flow.
4. After the browser reaches an explicitly allowed Muse origin, the worker reads its cookie jar internally, selects only the valid `hatch_sess` cookie for the exact Muse domain, and transfers that cookie to the Hub through a one-use owner/session/revision-bound secret channel. No browser storage state or other cookie is returned.
5. The Hub calls the existing `MuseService.import_login` path. A successful native Muse session check stores the session cookie with current Fernet encryption, advances the account revision, and clears automatic approval settings as existing re-import behavior requires.
6. The worker closes the browser, deletes its temporary profile/context and one-time secret channel, and marks the attempt terminal. Cancel, expiry, provider rejection, invalid cookie, browser failure, and service restart all terminate or expire the session without replaying a password submission.

## Target Contracts

These are the implementation boundaries; names can follow the repository's established naming once the runtime entry gate passes.

- Profile DTO returned by the Hub: `profile_id`, `label`, `login_hint`, `created_at`, `updated_at`, and `revision`. It must not contain password ciphertext, password plaintext, auth cookies, or IdP tokens.
- Login-attempt DTO returned to the owner: `attempt_id`, `profile_id`, `state`, `created_at`, `expires_at`, `challenge_kind`, and a bounded public error code. It must not include a browser URL with credentials, network trace, response body, cookie, password, or OTP.
- Terminal attempt states: `imported`, `failed`, `cancelled`, `expired`, `unknown`. Nonterminal states: `starting`, `authenticating`, `awaiting_user`, `validating`. `unknown` means the credential submission may have reached Muse; it is terminal, cannot reclaim the password or retry automatically, and requires a new user-approved attempt after manual verification. Only the owner may read or control an attempt. A profile revision change invalidates attempts tied to its older revision.
- Proposed owner-authenticated API surface: `GET /api/platform/v1/muse/login-profiles`, `POST /api/platform/v1/muse/login-profiles`, `DELETE /api/platform/v1/muse/login-profiles/<profile_id>`, `POST /api/platform/v1/muse/login-attempts`, `GET /api/platform/v1/muse/login-attempts/<attempt_id>`, and `POST /api/platform/v1/muse/login-attempts/<attempt_id>/cancel`. There is no frame/input takeover endpoint in this plan. Mutating endpoints retain same-origin protection, request-size limits, CSRF policy, and `Cache-Control: no-store`.
- Internal browser worker contract: start with one-use sealed credentials and a fixed allowlist policy; return a typed state/event stream and, on successful login only, one-use sealed `{name: "hatch_sess", domain: "muse.ai", value, expires}`. The public API never exposes this internal contract.
- Profile write request: `{label, login_hint, password}`. The server validates byte limits before decoding, stores encrypted ciphertext in a separate table, and returns only the safe profile DTO. The owner can delete a profile; deletion removes the active ciphertext and invalidates attempts for that profile revision.

## Global Constraints

- No login code may use the generic `browser.*` tools or relax `platform_browser_network_enabled`.
- Exact auth origins and exact credential-submission paths must be proven from a legitimate Muse login acceptance flow and checked into reviewed operator policy; wildcard domains and arbitrary redirects are rejected.
- External requests are impossible unless all three T3.7 layers are enforced: browser-native request gate, pinned egress adapter, and process/container socket restriction.
- The login browser uses a new isolated context per attempt, no shared profile, no on-disk cookies, no extension, no service worker, no downloads, no WebSocket/WebRTC/QUIC, no proxy environment inheritance, and no network after the attempt terminates.
- Passwords are encrypted at rest and are decrypted only for the single login attempt. Password, OTP, and cookies are redacted before logs, exceptions, event payloads, metrics, browser artifacts, and model-visible data.
- The browser worker has no capability to send Muse chat messages, approve tasks, read conversation history, access workspace files, or operate generic Fleet browser sessions.
- State-changing authentication is submitted at most once per attempt. A pre-submit failure is `failed` and may be retried only with a new attempt; an after-submit uncertainty is terminal `unknown`, retains its audit record, cannot reclaim credentials, and requires manual verification before a new attempt. Automatic resubmission is forbidden.
- Server-side MFA frames, screenshots, keystroke forwarding, Take Control/Return Control, and credential-bearing generic browser sessions are outside this plan and require a separately approved authentication-browser specification.
- Successful cookie transfer goes only through `MuseService.import_login`; do not add a parallel validation, encryption, or account persistence path.
- No browser package, sidecar, service supervisor, firewall rule, runtime configuration, or deployment artifact is introduced before the separate runtime design review and T3.7 isolation acceptance.

## Implementation Files

After the runtime entry gate passes, expected code changes are limited to:

- Create `hub/domain/muse_login.py` for immutable profile/attempt DTOs, exact state transitions, and bounded public error codes.
- Modify `hub/infrastructure/muse_repository.py` to add owner-scoped encrypted profile records, profile revisions, expiring attempt records, and atomic one-use credential claims. Keep profile secrets out of `_public`, `account`, `list_accounts`, and existing Muse status responses.
- Create `hub/application/muse_login_service.py` to coordinate profile CRUD, attempt lifecycle, the dedicated browser worker, and the existing `MuseService.import_login` use case.
- Create `hub/integrations/muse/login_browser.py` for the dedicated browser adapter and its request/cookie boundary. It must not import or call the generic `LocalBrowserBackend`; it also must not expose a frame/input takeover under this plan.
- Create `hub/http/muse_login_routes.py` and register it in `hub/bootstrap.py`; add request byte limits before JSON parsing and owner/revision checks on every operation.
- Modify `frontend/assets/api/muse.js`, `frontend/assets/views/muse.js`, and `frontend/assets/styles/muse.css` for profile management and unsupported-challenge handoff messaging. This task does not add an MFA takeover panel. Never place secrets in `localStorage`, session storage, URL parameters, page events, or DOM attributes.
- Add `tests/test_muse_login.py`, `tests/test_muse_login_browser_boundary.py`, and `tests/fixtures/browser/muse_login.cjs`; extend `tests/test_muse_workspace.py` only for integration with the existing login import path.
- Modify `docs/platform/muse-workspace.md` with the exact supported authentication flow, profile retention/deletion behavior, MFA limits, and deployment requirements only after the production runtime passes.

## Execution Tasks and Acceptance

### Task 1: Prove runtime and authentication policy entry gates

**Files:** Read `docs/superpowers/specs/2026-10-04-t3.7-browser-navigation-network-boundary.md`, `tools/platform/browser_transport.py`, `tools/platform/node_runtime.py`, `deploy/package-release.sh`, `.github/workflows/ci.yml`, and `.github/workflows/live-acceptance.yml`. Add no credential-handling code.

- [x] Confirm production runtime composition and browser feature-gate behavior from repository code.
- [x] Confirm the exact Muse cookie import and validation seam.
- [ ] Demonstrate browser-native interception, pinned adapter routing, and process/container egress restriction on every supported production runtime.
- [ ] Record and review the exact Muse authentication origin and credential-submission path allowlist from an authorized acceptance fixture.
- [x] Run: `PYTHONPATH=. python3 -m pytest tests/test_platform_node_runtime.py::test_node_runtime_forces_browser_external_network_off_without_egress_proof tests/test_platform_node_runtime.py::test_node_runtime_does_not_advertise_submit_without_egress_proof tests/test_muse_workspace.py::test_muse_expiry_is_not_fleet_auth_failure -q` — **3 passed**.
- Expected at this stage: both safety regressions pass. If either runtime proof item is missing, stop before Task 2; do not add the credential UI or endpoint.

**Current result:** blocked. The repo includes only an injected browser driver interface; production release contains no Chromium/Playwright runtime. Existing tests prove generic browser egress and submit remain disabled. Authentication origins and credential-submission routes are not specified by repository code. No code in Tasks 2–6 is safe to expose until these facts change.

### Task 2: Add encrypted owner-scoped credential profiles

**Files:** `hub/domain/muse_login.py`, `hub/infrastructure/muse_repository.py`, `tests/test_muse_login.py`.

- Add a distinct `credential_profiles` SQLite table keyed by `(owner, profile_id)` with encrypted secret bytes, safe login hint, label, revision, and timestamps. Do not reuse the existing Muse account cookie field or include profile records in account list/status queries.
- Add repository operations `create_login_profile(owner, profile_id, label, login_hint, password)`, `list_login_profiles(owner)`, `claim_login_password(owner, profile_id, revision, attempt_id)`, and `delete_login_profile(owner, profile_id, revision)`. `claim_login_password` succeeds once for one live attempt; it returns decrypted data only to the application service, never an HTTP serializer.
- Add owner/profile foreign keys or transaction checks for attempt records, size bounds for UTF-8 input, encrypted-at-rest assertions, revision fencing, and deletion invalidation.
- Test cross-owner reads/deletes, duplicate profile IDs, stale revisions, ciphertext-at-rest, password absence from repr/DTOs, one-time claim concurrency, delete-vs-claim races, and malformed/oversized Unicode input. Use synthetic fixture secrets only.
- Run: `PYTHONPATH=. python -m pytest tests/test_muse_login.py -q`.

### Task 3: Implement dedicated browser login lifecycle and isolation

**Files:** `hub/integrations/muse/login_browser.py`, `tests/test_muse_login_browser_boundary.py`, `tests/fixtures/browser/muse_login.cjs`, and the separately reviewed runtime/deployment files approved by Task 1.

- Implement one isolated browser context per attempt with a bounded monotonic deadline and an explicit `close()`/`finally` cleanup path. The worker accepts only a fixed login profile DTO, a one-use secret claim, and the reviewed static origin/route policy.
- Implement only fixed actions: open the Muse login entry point, detect the known login form, fill the approved login identifier/password fields, submit once, report a bounded state, and extract the `hatch_sess` cookie after an allowed Muse landing state. Unsupported MFA becomes `awaiting_user` followed by a manual Cookie-import handoff; no server-side MFA view is part of this task.
- Enforce the same policy on navigation, redirects, frames, scripts, fetch/XHR, workers and resources. Verify direct sockets fail at process/container level; browser callbacks alone are insufficient.
- Return no raw HTML, JavaScript, request body, URL query, cookie jar, storage state, screenshot, frame, keystroke, or trace to Logs/Run/model. No takeover channel is exposed by this task.
- Test malicious redirects, subresource exfiltration destinations, private and mixed DNS, direct socket attempts, proxy/PAC bypass, WebSocket, WebRTC, QUIC, service workers, duplicate submits, unsupported challenge handoff to Cookie import, cancellation during navigation, timeout, worker crash, and cleanup of browser process/context/temp files.
- Run: `PYTHONPATH=. python -m pytest tests/test_muse_login_browser_boundary.py -q` plus the platform-specific socket-isolation acceptance suite from Task 1. No mocked driver can satisfy the isolation proof.

### Task 4: Add application and HTTP profile/attempt use cases

**Files:** `hub/application/muse_login_service.py`, `hub/http/muse_login_routes.py`, `hub/bootstrap.py`, `tests/test_muse_login.py`, `tests/test_muse_workspace.py`.

- Implement profile list/create/delete and attempt start/status/cancel with exact owner, profile revision, session lease, CSRF/same-origin, content-type, request-byte, no-store, and idempotency checks.
- Permit only one active attempt per owner/profile and one attempt per Muse account target. Persist a one-use claim before browser interaction; never retry password submission after an uncertain worker result.
- On successful browser result, validate a single Muse-domain `hatch_sess` cookie and call the existing `MuseService.import_login` method. Do not persist the cookie directly in the new repository path.
- Test unauthenticated access, cross-owner attempt IDs, expired profile revisions, repeated starts, concurrent start/cancel, worker timeout/failure, successful import revision change, invalid or extra cookie fields, and secret redaction from every HTTP/error response.
- Run: `PYTHONPATH=. python -m pytest tests/test_muse_login.py tests/test_muse_workspace.py -q`.

### Task 5: Add safe profile UI after the authentication-browser contract is approved

**Files:** `frontend/assets/api/muse.js`, `frontend/assets/views/muse.js`, `frontend/assets/styles/muse.css`, `tests/fixtures/browser/muse_login.cjs`, `tests/test_muse_workspace.py`.

- Add an explicitly opted-in profile-save form with password input using `autocomplete="new-password"`; after submit, clear the input immediately and render only the safe profile DTO.
- Add profile selection, rename only if supported by the repository operation, delete, login, progress, unsupported-challenge handoff to Cookie import, cancel, success, expiry, `unknown`, and bounded error states. Use text nodes for all server text.
- Do not add an MFA frame/input takeover in this task. If a separate approved authentication-browser contract later adds one, it must be implemented as a separately reviewed surface with its own owner binding, lifecycle, and redaction tests.
- Test XSS payloads in profile labels/errors, empty/wrong password UI, password clearing, unsupported-challenge handoff, cancel/unmount cleanup, no automatic retry from `unknown`, multiple accounts, and mobile viewport.
- Run the Muse fixture journey and affected frontend regression from the repo journey manifest.

### Task 6: Full regression and production acceptance

**Files:** all feature files plus `docs/platform/muse-workspace.md`, release packaging/configuration approved in Task 1, and the feature's journey manifest entry.

- Run Task 1 socket-isolation acceptance on every supported deployment runtime and run the full Muse auth fixture matrix without external credentials.
- Run `PYTHONPATH=. python -m pytest tests/test_muse_login.py tests/test_muse_login_browser_boundary.py tests/test_muse_workspace.py tests/test_platform_node_runtime.py -q`, `python -m compileall -q hub tools`, `node --check` for changed frontend fixture scripts, and `git diff --check`.
- Verify the release archive has the required runtime but no test browser/profile, credential, browser trace, or local auth state. Verify fresh startup with all login/browser feature flags off.
- Only after all offline and isolation gates pass, perform a separately authorized live Muse login using a test account, with no real user password in CI. Confirm manual MFA if requested, successful `hatch_sess` import, session query, browser process/context cleanup, and credential deletion behavior. Do not deploy as part of this plan.

## Current Execution Record — 2026-10-15

- Worktree created from latest fetched `origin/main` at `1a7ecd5`, on branch `codex/muse-credential-login`; the primary worktree was clean and remains untouched.
- Production runtime audit: `deploy/package-release.sh` ships a Python Hub release; runtime install uses Python requirements; Chromium/Playwright appear only in CI's fixture runner. `NodeRuntime` accepts an injected driver but does not instantiate a production browser adapter.
- Existing network boundary audit: `NodeRuntime` explicitly sets `network_enabled=False`; tests assert external URL navigation is rejected and `browser.submit` is omitted even when config requests it.
- Existing Muse boundary audit: `MuseService.import_login` calls `normalize_login`, verifies via the native Muse client, and stores the accepted cookies through `MuseRepository` encryption. This is the one allowed success handoff for the new flow.
- Documentation-only diff verification: `git diff --check` passed; the updated platform guide points to this plan and clearly states cookie import remains the available login method.
- Runtime isolation and exact authentication-origin entry gates have not passed. Credential collection, persistence, login UI, and browser code were therefore not started; adding them now would create a password vault with no safe login execution path. The plan no longer promises a server-side MFA takeover under the current browser contract.
- No credentials were entered, no real Muse login was attempted, and no external authentication scripts or bundles were inspected.
