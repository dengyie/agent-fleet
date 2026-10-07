# Browser Submit Approval Contract Specification

> Status: approved for implementation by the owner on 2026-10-06; implemented and verified as an offline approval/driver/transport contract. All browser gates remain default-off. Real-browser adapters, process-level egress enforcement, external network activation and deployment remain outside this slice.

## 1. Goal

Define the first state-changing browser network action: `browser.submit`, an approval-gated form submission that the model may request only after the owner has approved the exact selector on the current session state. Everything before the submit stays read-only exactly as in T3.6/T3.7; the submit itself is the only path by which a page-originated POST may leave the browser, and only when a durable, owner-issued, single-use approval covers that exact selector.

The approval authorizes one selector activation. It never widens destination policy: the form action URL still passes the same normalized-origin allowlist, global-address classification, and pinned-transport guarantees fixed by T3.7. The approval widens exactly one thing — the request method — from the T3.7 GET/HEAD-only rule to include the single approved form POST, and nothing else.

## 2. Baseline and compatibility

- Preserve every T3.6/T3.7 gate and bound unchanged: `platform_browser_enabled` and `platform_browser_network_enabled` remain default-off with parent-gate behavior; fixed tool allowlists, opaque session scope, cross-run session rejection, manual-only retry, result/artifact budgets, and redaction contracts are untouched.
- T3.7 fixed ceilings stay fixed: 20-second absolute request deadline, 10 redirects, 16 DNS answers, 16 concurrent requests, 64 KiB header / 16 MiB body / 64 MiB session budgets, 256 KiB PNG.
- T3.7 §3.5 froze "only GET and HEAD are permitted; form submission and state-changing browser interaction require a later separately approved contract." This document is that contract. It must not weaken T3.7 transport semantics: the general `request` method of the pinned transport stays GET/HEAD-only; POST exists only on the new bounded submit path defined in §3.4.
- The submit action is additive: adding `browser.submit` must not change the behavior of any existing tool, and a submit without a valid approval is a deterministic failure, not a fallback click.
- No new third-party dependency, OS firewall, proxy service, CONNECT adapter, or deployment change is introduced. The driver seam remains the injected driver contract of T3.6; no CDP evaluate, arbitrary JavaScript, shell, or host access is opened.
- Browser credentials, profile/cookie import, login automation, and secret injection remain forbidden (inherited T3.7 non-goals; see §3.4 sensitive-field screening).

## 3. Contract

### 3.1 Tool surface

One new tool:

- `browser.submit`: arguments `{session_id, selector}`. Risk `write`, scope `browser`, retry class `manual_only`. Visible to the model only when all of: `browser.session` capability present, browser gates enabled, driver available, and the new gate `platform_browser_submit_enabled` (default-off, child of the browser gates) is on.

The model never receives form data in tool arguments. It proposes `browser.submit` with a selector obtained from a prior `browser.snapshot`; the driver reads the current DOM form state at execution time. Field values typed by the model arrive only through the existing `browser.type` (bounded, sensitive-selector-screened) or were pre-filled by the page.

### 3.2 Approval plane (Hub-side, owner-scoped)

- The owner grants an approval through an owner-authenticated Hub REST endpoint, scoped to one run: `POST /api/platform/v1/runs/{run_id}/browser-approvals` with body `{session_id, selector}`; `DELETE` on the approval resource revokes. Hub authenticates the owner, verifies run ownership, verifies the session's durable Hub row is non-terminal, and applies the rate bounds of §4 before creating the approval row. The Node never mints approvals.
- Approvals are durable rows in a new bounded Hub store. Fields: opaque `approval_id`, `owner_id`, `workspace_id`, `run_id`, `node_id`, `session_id`, normalized `selector`, `granted_at` (UTC, injectable clock), fixed TTL, state machine `active → consumed | expired | revoked`.
- A grant is idempotent per `(run_id, owner-supplied idempotency key)`; a duplicate returns the same `approval_id` without consuming or duplicating rows.
- One approval is consumed atomically, inside the same Hub write transaction that creates the `browser.submit` durable command. A second submit against the same approval fails before command creation. If the command later expires undelivered, the approval stays consumed: the fail-safe direction is that no side effect can occur without a consumed approval, and a burned approval only costs a re-grant, never an unauthorized side effect.
- Revocation is owner-initiated and immediate for future admissions. A submit already dispatched before revocation completes with T3.6 unknown/failure semantics unchanged; revocation prevents the next admission only and is never retroactive over an in-flight command.
- Approval metadata excludes page text, page URL path, cookies, credentials, and secrets. Events and receipts carry the opaque `approval_id` for attribution, nothing more.

### 3.3 Admission and dispatch flow

1. Hub admission (`RemoteToolBroker`): look up an `active`, unexpired approval matching `(owner_id, run_id, session_id, normalized selector)`. Absent → failed receipt `approval_required`; expired → `approval_expired`; already consumed → `approval_consumed`; revoked → `approval_revoked`; over a rate bound → `approval_limit`. All of these fail before command creation and before any driver dispatch.
2. On success, consume the approval atomically and create the durable command with retry class `manual_only` (already automatic for browser tools), bounded expiry, and `approval_id` inside the signed arguments (the existing `args_hash` binds it).
3. Node dispatch (`NodeToolExecutor`): re-validate command signature, `args_hash`, target node, expiry (existing), run binding, and local session liveness/cross-run rejection (existing `LocalBrowserBackend` run binding). The Node trusts the signed command for approval binding; it fetches no Hub state during dispatch — dispatch stays single-hop.
4. Dispatch calls the driver seam `driver.submit(selector)`, which resolves the enclosing form of the selector'd element (or the form itself), screens its fields (§3.4), serializes them, and submits through the policy-checked submit path. Pre-dispatch failures map to `backend_failed`; after-dispatch failures retain `BrowserExecutionError` unknown semantics.
5. The submit performs exactly one POST. Any subsequent reading of the result happens through the existing read-only `browser.snapshot`/`browser.screenshot`.

### 3.4 Form submission network semantics

- General `request` on the pinned transport stays GET/HEAD-only (T3.7 unchanged). A new bounded submit path carries the approved POST and is reachable only through the `browser.submit` action.
- The form action URL is resolved by the driver, never supplied by the model. It must pass the identical shared normalizer, exact-origin allowlist, loopback/network-gate rules, and global-address classification as `browser.navigate` before the POST is sent. A disallowed action URL fails `origin_forbidden`/`invalid_url` deterministically before dispatch of the request.
- Method: POST only, with `Content-Type: application/x-www-form-urlencoded` fixed. No file uploads, no multipart, no other content types.
- Body bounds: at most 64 fields, each field name ≤ 128 bytes and each value ≤ 4096 bytes, total serialized body ≤ 16 KiB. Overflow fails `request_too_large` before the request is sent.
- Sensitive-field screening at serialization: if any field name matches the existing `_SENSITIVE_MARKERS`, or the driver detects a password-type input in the form, the submit fails `sensitive_field_forbidden` before any network request. This keeps login automation and credential submission impossible; name/type-based screening is the same mechanism `browser.type` uses today, and its known limitation (non-standard field names) is recorded, not hidden.
- Redirects on the approved POST: any redirect response is denied with `redirect_denied`. The submit path never re-sends the body (no 307/308 replay) and never rewrites POST to GET (no 301/302/303 automatic follow). This keeps T3.7's "no POST rewriting or replay semantics" guarantee intact: the approval covers exactly one POST to one validated destination. The driver may issue a separate read-only GET afterwards through the normal request path, subject to all T3.7 rules.
- The POST shares the same absolute 20-second deadline, pinned resolution, address classification, TLS verification, and error-code family as every T3.7 request.
- No credentials are synthesized, imported, or forwarded: no `Authorization`, `Cookie`, or credential headers on the submit POST; no cookie jar is created.

### 3.5 Error codes and secrecy

- New stable codes, secret-free: `approval_required`, `approval_expired`, `approval_consumed`, `approval_revoked`, `approval_limit`, `submit_disabled`, `request_too_large`. All T3.7 codes (`origin_forbidden`, `invalid_url`, `redirect_denied`, `response_too_large`, `timeout`, `tls_failed`, `invalid_selector`, `sensitive_field_forbidden`, `invalid_session`, `browser_disabled`, `unknown_tool`, `invalid_arguments`, `backend_failed`, `session_not_found`, `session_limit`, `backend_unavailable`) keep their meanings.
- Submit receipts carry only: state, `session_id`, `approval_id`, and a bounded outcome descriptor subject to the same aggregate byte/element-count budgets as `browser.click` results. A driver outcome that cannot be validated inside those budgets becomes `failed`/`result_too_large`.
- No page content, form field name or value, URL, selector value, cookie, credential, or driver exception detail appears in any receipt, journal, event, log, or model context beyond those bounded planes. Marker-trace methodology from T3.7 (end-to-end synthetic marker exclusion across Node journals, Hub command receipts, Run events, model transcripts, artifact rows, Hub response bodies, and captured logs) applies to the submit path unchanged.

## 4. Fixed values

| Value | Bound |
|---|---|
| Selector length / screening | ≤ 512 bytes, same control-character and `_SENSITIVE_MARKERS` screening as `browser.click` |
| Approval TTL | 300 s, Hub injectable UTC clock, checked at grant, admission, and expiry sweeps |
| Active approvals per session | ≤ 4 |
| Active approvals per run | ≤ 16 |
| Grant rate per workspace | ≤ 64 per rolling hour |
| Submit POST fields | ≤ 64 |
| Field name / value size | ≤ 128 B / ≤ 4096 B |
| Serialized body | ≤ 16 KiB |
| Redirects on the submit POST | 0 (any redirect → `redirect_denied`) |
| Request deadline | 20 s absolute, shared with the T3.7 phases |
| Retry class | `manual_only` |
| Result budgets | identical to `browser.click` |

All values above are fixed acceptance values for this contract, enforced fail-closed, not tunables.

## 5. Proposed implementation boundary

- Policy: `browser.submit` added to `_ALLOWED_TOOLS` in `tools/platform/browser_policy.py` with argument contract `{session_id, selector}` and the same selector screening as `browser.click`. `validate_action` stays purely syntactic; approval state checks happen at admission/dispatch, not in policy.
- Backend: `LocalBrowserBackend.execute` dispatches `browser.submit` to a new driver seam method `driver.submit(selector)` (same injected-driver contract style as `navigate`/`click`; no second browser abstraction). Pre-dispatch failures → `backend_failed`; after-dispatch failures → `BrowserExecutionError` unknown semantics.
- Transport: bounded POST submit path added to `tools/platform/browser_transport.py` under the constraints of §3.4, offline-proven with the existing fake-socket/advancing-clock fixtures. The general `request` method stays GET/HEAD-only.
- Broker: `LocalToolBroker` and `RemoteToolBroker` gain the submit gate (`submit_disabled` when off) and, for `RemoteToolBroker`, the approval lookup/consumption around durable command creation. `browser.submit` in `BROWSER_TOOLS` already forces `manual_only` retry.
- Executor/runtime: `NodeToolExecutor`/`NodeRuntimeConfig`/`NodeRuntime` gain `platform_browser_submit_enabled` plumbing (default off, validated to require the browser gates) and `driver.submit` dispatch; tool definitions expose `browser.submit` only when the gate is on.
- Hub: approval store (new bounded SQLite repository or a scoped extension of the browser repository), owner-authenticated grant/revoke endpoints in `hub/web.py`/`hub/http/`, atomic consume in the command-creation transaction, gate wiring in `hub/config.py`/`hub/bootstrap.py`/`hub/application/run_worker_service.py`.
- Driver compatibility: injected fakes prove the tool/approval/transport behavior. A real browser adapter is not introduced by this contract and requires the separate decision plus process-level egress proof already gated by T3.7.

### 5.1 Design decisions to preserve

- Hub-side atomic consumption over Node-side consumption: the Hub owns authorization, the Node owns execution. Node-side ownership would let a compromised Node mint or silently drop approvals.
- Command-carried `approval_id` over Node-fetched approval state: dispatch stays single-hop; the signed command's `args_hash` already binds the exact arguments tuple the Hub admitted, and the Node adds its existing local session-liveness and cross-run checks on top.
- Deny-all redirects on the approved POST over replay/rewrite: no 307/308 body replay, no 301/302/303 method rewrite; the approval covers exactly one POST.
- Owner-scoped grant endpoint over Node-authenticated grant: approvals are human decisions about model behavior; the Node has no authority to grant them.

## 6. Acceptance matrix (offline, deterministic)

All tests are offline, in-process, and deterministic: injected fake drivers, injected monotonic/UTC clocks, in-process HTTP transport, synthetic markers, fake sockets for the submit path. No test contacts external DNS, the Internet, a VPS, a production browser/profile, or production credentials. The implementation evidence for each case is recorded in §9.

### Positive cases

1. Grant → consume happy path: owner grants an approval for `(session, selector)`; the model calls `browser.submit` with that selector; admission consumes the approval atomically; dispatch submits through the driver seam; the receipt carries `approval_id` and a bounded outcome; a second submit with the same approval fails `approval_consumed`.
2. TTL boundary: an approval admitted strictly before `granted_at + 300 s` succeeds; at the boundary or after, `approval_expired`.
3. Grant idempotency: a duplicate grant with the same idempotency key returns the same `approval_id` without consuming or duplicating rows.
4. Revocation: a revoked approval fails admission with `approval_revoked`; a submit dispatched before revocation completes with unchanged T3.6 semantics.
5. Attribution: Node journal, wire receipt, Hub command row, and Run events record the opaque `approval_id`; all planes stay metadata-only, and synthetic page-content/form-field markers stay absent from every metadata plane (same methodology as T3.7 end-to-end marker tests).
6. Submit without any approval fails `approval_required` before driver dispatch; the driver submit count stays zero.
7. Session close or run termination expires remaining approvals deterministically; no dispatch afterwards.

### Negative cases

1. The approval does not widen destination policy: an approved submit whose form action is off-allowlist fails `origin_forbidden` — identical code and behavior as without an approval.
2. Selector mismatch: an approval for selector A does not admit a submit for selector B (no prefix, wildcard, or normalized-variant matching); failure before dispatch.
3. Sensitive-field screening: a form containing a sensitive-marker field name or a password-type input fails `sensitive_field_forbidden` before any network request, at serialization time.
4. Non-form selectors and missing enclosing forms fail deterministically before dispatch; the driver submit count stays zero.
5. Overflow: a 5th active approval per session, 17th per run, or 65th grant per workspace-hour fails `approval_limit` with no row created.
6. Cross-run approval reuse: an approval from run R1 does not admit a submit command from run R2; the existing run-binding checks reject it even with separately valid signatures.
7. After-dispatch driver failure: `driver.submit` raises after dispatch → `BrowserExecutionError` → Node journal `unknown`/`executor_interrupted`, wire receipt `unknown`, matching T3.6/T3.7 semantics; the approval stays consumed (side effect indeterminate, no refund, no automatic replay).
8. Redirect on the approved POST: a 302 response yields `redirect_denied`; no body replay, no method rewrite, no follow-up request from the submit path.
9. Gate-off layering: with `platform_browser_submit_enabled` off, the tool is absent from tool definitions, `LocalToolBroker`/`RemoteToolBroker` return `submit_disabled`, and the Node executor rejects the command regardless of a valid approval.
10. Body/field bounds: 65 fields, an over-long field name/value, or a >16 KiB body fails `request_too_large` before the request is sent; the transport submit path is unreachable for any other caller or method.

### Exit criteria

- All positive/negative cases above are exercised by deterministic offline tests; `compileall` and `git diff --check` pass; the platform doc status table and this spec are updated in the same commit.
- Browser/submit/network gates remain off by default; the owner-grant endpoint is authenticated and rate-bounded; no deployment is performed.
- Anything not listed above is outside this contract's acceptance scope. Approval UI, dedicated observation surfaces, real browser drivers, Browserbase, and production deployment remain unimplemented. `browser.frame`, Take Control/Return Control, and writer fencing are covered by the execution-window continuation recorded in the [development completion plan](../plans/2026-10-06-development-completion.md#audit-repair-q-png-frame-payload-validation-and-browser-writer-fencing-2026-10-06); they do not establish real browser lifecycle or process-level network isolation.

## 7. Implementation entry gate

1. This document must be approved before any runtime code lands; the §4 values are fixed acceptance values, not tunables.
2. The approval state machine must be Hub-single-writer with atomic consumption; any implementation with Node-side approval ownership fails review.
3. Submit dispatch must reuse the injected driver seam without adding a second browser abstraction.
4. `browser.submit` must not become visible to the model until `platform_browser_submit_enabled` exists, is default-off, and requires the browser gates and driver availability (parent-gate behavior).
5. Resulting-request network semantics are inherited from T3.7 unchanged: if a T3.7 gate would block the form action, the submit fails with that bounded code; the approval must not override any network gate.

## 8. Non-goals

- Real Chromium/Playwright or Browserbase production adapter; real browser TLS/CA integration; real DNS fixtures.
- Browser credentials, profile/cookie import or upload, SecretBroker-backed browser credentials, login automation, or secret injection.
- File-upload or multipart form submission; multi-step wizard automation beyond one approved POST per approval.
- Approval UI, observation surfaces, screenshot/frame streaming, Take Control/Return Control, writer fencing.
- CDP evaluate, arbitrary JavaScript, shell/host access, or any new transport beyond the bounded submit path.
- Enabling browser/network/submit gates, contacting external services, production/test-node deployment, or claiming production readiness.
- Modification of T3.6/T3.7 fixed bounds, existing tool semantics, or receipt contracts.


## 9. Implementation and verification record (2026-10-06)

The owner-approved implementation preserves the existing synchronous Flask/SQLite worker architecture. It adds no runtime dependency and makes no claim of production browser readiness.

### API and persistence details

- Grant: `POST /api/platform/v1/runs/{run_id}/browser-approvals`, JSON exactly `{session_id, selector}`. Optional `Idempotency-Key` header supplies grant identity. Workspace and Node come from the durable session; clients cannot supply them.
- Revoke: `DELETE /api/platform/v1/runs/{run_id}/browser-approvals/{approval_id}`. A scoped metadata GET is also available. Wrong owner/run requests cannot revoke another approval.
- Responses expose the opaque approval/run/session identifiers, state, timestamps and consumed command identifier. They omit selector and internal owner/workspace/node metadata. Public timestamps are UTC ISO-8601; SQLite uses injectable UTC epoch seconds.
- Workspace rate keys include owner identity because workspace IDs are owner-scoped. Quota queries use compound indexes and stop after 4/16/64 qualifying rows. TTL sweeps are bounded to 256 records per call; grant/admission/read paths enforce expiry directly, so correctness never depends on a periodic sweep.
- SQLite lifecycle triggers expire approvals in the same write as session closure or Run cancellation/termination, including recovery and task-projection paths. Grant and admission independently verify the durable run/session scope and liveness.
- Command admission checks idempotency, consumes approval, signs the final arguments, and inserts command/outbox under one write transaction. Signing/storage failures roll back the approval; a successfully persisted but undelivered command keeps its approval consumed.
- Local ToolBroker submit remains disabled: only the Hub remote command path owns approval authority. Node requires a valid signature for submit even when legacy unsigned execution is otherwise enabled.
- `platform_browser_submit_enabled` requires browser and network parent flags. Model visibility additionally requires the Node's advertised `browser.submit` capability. NodeRuntime still forces external networking unavailable as required by T3.7.
- The injected `driver.submit(selector)` owns DOM form resolution and password/file-control screening. Backend dispatch grants a context-local single-POST permit; `submit_form` fails outside that activation or on a second request. No real DOM/browser adapter is shipped. Driver fixtures prove the contract, not browser process egress enforcement.

### Acceptance evidence

| Contract cases | Deterministic regression evidence |
|---|---|
| Positive 1, 4–6; negative 2, 3, 4, 7, 8 | `tests/test_platform_submit_e2e.py::test_submit_end_to_end_contract` exercises the owner HTTP grant, real worker, signed command repository, in-process Node HTTP transport, driver and pinned exchange. Its synthetic-marker sweep inspects Run events, Node journal, Hub command receipt, wire receipt, Hub HTTP bodies, model transcript, captured logs, browser artifact-ticket registry and artifact manifest registry. |
| Positive 4, revocation during an admitted submit | The `revoked_in_flight` case in `test_submit_end_to_end_contract` waits until the form POST has completed, revokes the already-consumed approval while the driver call is still in flight, then verifies the command finishes with its original result. The existing `revoked` case continues to prove revocation before admission prevents dispatch. |
| Positive 2 | `test_approval_ttl_is_exclusive_and_old_rows_do_not_shadow_new`, `test_ttl_before_boundary_and_bounded_sweep` in `tests/test_platform_submit_approvals.py`. |
| Positive 3 | `test_grant_idempotency_and_selector_mismatch`, `test_owner_grant_exact_body_idempotency_and_scoped_revoke`. |
| Positive 7 | `test_session_close_and_run_cancel_expire_only_their_approvals`, `test_every_run_terminal_state_expires_approvals`, `test_expiry_targets_exact_run`. |
| Negative 5 | `test_expired_rows_release_active_quota`, `test_run_and_workspace_limits_and_rolling_hour`, `test_workspace_rate_limit_is_owner_scoped`. |
| Negative 6 | Repository scope mismatch tests and `test_signed_submit_reaches_driver_and_records_approval[True]` reject cross-Run submission with a separately valid signature; signature tampering/unsigned commands are checked by `test_unsigned_submit_and_tampered_approval_are_rejected_before_driver`. |
| Negative 9 | `test_submit_parent_gates`, NodeRuntime submit gate tests and local-broker bypass regression. |
| Negative 10 | `test_submit_exact_body_boundaries`, `test_submit_transport_requires_scoped_dispatch_and_single_post` in `tests/test_platform_browser_transport.py`. |
| Atomicity and restart | `test_approval_command_and_outbox_roll_back_together`, `test_atomic_admission_signs_final_arguments_and_is_command_idempotent`, `test_concurrent_commands_cannot_consume_same_approval`, `test_restart_keeps_revocation_and_failed_signing_rolls_back`. |
| Preserved transport semantics | Existing T3.7 transport suite plus `test_submit_redirect_matrix_never_replays` and `test_submit_shares_absolute_deadline_and_session_budget`. |

The [journey matrix](../../testing/journeys.json) now requires `BROWSER-01` / `BROWSER-02`. Local full-gate execution on 2026-10-06 used installed Playwright and Chrome: **2632 passed, 2 skipped, 154 subtests passed; 29/29 journeys passed**. Both skips require Linux `/proc`; all external checks remain `not_run`. This includes product UI tests, but the submit driver is still an injected fixture. Latest focused approval/HTTP/E2E/transport validation: **189 passed**. Python 3.10 AST checks (26 changed Python files), `compileall` for `hub tools tests`, and `git diff --check` passed; an existing probe docstring emits a SyntaxWarning.

Full gate command: `FLEET_PLAYWRIGHT_MODULE=/tmp/pr6-review-browser/node_modules/playwright FLEET_BROWSER_CHANNEL=chrome FLEET_SCREENSHOTS=/tmp/agent-fleet-submit-evidence/browser PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /Users/mango/project/codex/agent-fleet/.venv/bin/python -m pytest tests -q --tb=short -rs -p no:cacheprovider --require-journeys --journey-report=/tmp/agent-fleet-submit-evidence/journeys.json --junitxml=/tmp/agent-fleet-submit-evidence/results.xml`. Evidence records the pre-commit revision plus dirty working-tree state; it is local verification, not a production-release claim. See the [implementation plan](../plans/2026-10-06-browser-submit-approval.md#verification-evidence) for the integration and regression record.
