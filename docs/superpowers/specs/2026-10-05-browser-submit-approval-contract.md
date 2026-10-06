# Browser Submit Approval Contract Specification

> Status: draft for review. This slice adds no runtime code; it defines the approval-gated `browser.submit` contract deferred by T3.6/T3.7 and fixes its policy values before implementation. All browser gates remain default-off. This document authorizes no gate change, external network access, real-browser integration, production credential use, or deployment.

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

All values above are fixed acceptance values for this draft, enforced fail-closed, not tunables.

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

All tests are offline, in-process, and deterministic: injected fake drivers, injected monotonic/UTC clocks, in-process HTTP transport, synthetic markers, fake sockets for the submit path. No test contacts external DNS, the Internet, a VPS, a production browser/profile, or production credentials. The implementation coverage table below records the evidence produced by the implementation commit; Partial entries name exactly what stays unproven, and nothing beyond them is claimed as passing.

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

### Implementation coverage (produced by the implementation commit)

| Case | Status | Evidence and boundaries |
| --- | --- | --- |
| P1 | Covered | `tests/test_platform_submit_approvals.py` grant/consume/consumed-state; `tests/test_platform_node_http_e2e.py::test_browser_submit_is_approval_gated_and_single_use` (one grant → one dispatch → `approval_id` in the durable command arguments, approval row `consumed`); retry-after-consume maps `approval_required`/`approval_consumed` in `tests/test_platform_remote_delivery.py` |
| P2 | Covered | TTL boundary test: `now >= granted_at + 300` → `approval_expired` (boundary expires); pre-boundary consume succeeds; sweep terminalizes. Grant-time lazy TTL sweep inside the grant transaction (`tests/test_platform_submit_approvals.py::test_lapsed_grants_free_budget_at_next_grant`) and unexpired-first consumption (`test_consume_prefers_unexpired_over_lapsed_approval`) keep lapsed rows from shadowing valid grants or occupying budgets |
| P3 | Covered | Idempotent grant returns the same `approval_id`; conflicting content with the same key fails `approval_idempotency_conflict` |
| P4 | Covered | Revoke → `revoked` terminal state; consumption of a revoked approval fails `approval_required` (no active row); revoke of a missing id fails `approval_not_found` |
| P5 | Covered | Node journal, wire receipt, Hub command row, and Run events record opaque `approval_id`; synthetic form-field, page-content, and driver-exception markers are proven absent across every metadata plane (Node journal, wire receipts, Hub command rows, Run events, model transcript, artifact registry, Hub response bodies, run row, captured logs) via `tests/test_platform_node_http_e2e.py::test_browser_submit_markers_stay_off_metadata_planes` |
| P6 | Covered | Broker consumption precedes command creation; denial before grant leaves `driver.submits` empty in the e2e stand-in |
| P7 | Covered | `close_session` terminalizes active approvals in the same transaction; `RunService.cancel` expires them before committing cancellation (`test_run_cancel_terminals_active_approvals`); `LocalRunWorkerService._finish` terminalizes unconsumed approvals whenever the Run leaves the queue — succeeded, failed, unknown alike (`test_run_terminal_state_expires_unconsumed_approvals`); the broker consumes through `SubmitApprovalService.consume_submit_approval` with no test double on the worker (`test_browser_submit_consumes_through_production_service_assembly`) |
| N1 | Covered | The selector-based `browser.submit` carries no URL, so destination policy applies at `navigate`; transport `submit_form` reuses the exact shared normalizer and fails `origin_forbidden` before sending for any unapproved destination origin or scheme (`tests/test_platform_browser_transport.py::test_submit_form_enforces_destination_origin_policy`) |
| N2 | Covered | Consumption matches `(owner, run, session, selector)` exactly; a different selector finds no active row → `approval_required` before dispatch |
| N3 | Covered | Selector and form-field screening reject sensitive markers at serialization time before any request (`sensitive_field_forbidden`) |
| N4 | Blocked | Non-form selectors and missing enclosing forms are driver-level responsibilities; the deterministic driver seam is exercised with a stub; real browser DOM/form parsing is blocked behind the real browser driver gate |
| N5 | Covered | Session 5th-active and workspace 65th-grant-per-hour limits covered with `approval_limit` 429; the 17th-active-per-run boundary is independently exercised with a dedicated second run keeping its own budget (`tests/test_platform_submit_approvals.py::test_run_per_active_limit_boundary`) |
| N6 | Covered | Consumption is scoped by `run_id` in the same WHERE clause as owner/session/selector; dedicated cross-run consumption test proves an approval granted for run R1 cannot be consumed by run R2 (`tests/test_platform_submit_approvals.py::test_cross_run_consumption_is_scoped`) |
| N7 | Covered | After-dispatch failure → `BrowserExecutionError` → `unknown` receipt, run-bound (`test_submit_after_dispatch_failure_is_unknown_and_run_bound`); approval stays consumed (no refund path exists) |
| N8 | Covered | POST redirect → `redirect_denied`, one request, no replay/rewrite/follow-up |
| N9 | Covered | Tool definition omitted when the gate is off; local/remote brokers return `submit_disabled`; Node runtime parent gate (`browser submit requires browser capability`), default off, tool absent from allowed_tools |
| N10 | Covered | 65 fields / over-long name or value / >16 KiB body → `request_too_large` before sending; general request path stays GET/HEAD-only |

### Exit criteria

- All positive/negative cases above are exercised by deterministic offline tests; `compileall` and `git diff --check` pass; the platform doc status table and this spec are updated in the same commit.
- Browser/submit/network gates remain off by default; the owner-grant endpoint is authenticated and rate-bounded; no deployment is performed.
- Anything not listed above must not be claimed as passing. The approval UI, observation surfaces, Take Control/Return Control, writer fencing, real browser drivers, Browserbase, and production deployment remain unimplemented and out of scope.

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
