# Platform diagnostics and release checks

A healthy `/api/status` only proves the observation API responds. It does not prove that the assistant's routes, defaults, model credential or worker are configured.

The authoritative testing entry is [Full-flow tests and gates](testing/README.md), with the executable [journey matrix](testing/journeys.json) and [external release acceptance](testing/release-acceptance.md). CI must run `--require-journeys`; skipped browser tests or missing selectors do not qualify as a passing release.

Current account-mode deployments use username/email and password login with a server-validated HttpOnly session cookie. Every function page validates `GET /api/operator/session` before loading business data; missing/revoked sessions return to `/login`. Registration uses invited email verification, and password changes/resets revoke old sessions. The historical operator-token readiness CLI does not authenticate an account-mode deployment; use `python -m tools.platform.acceptance_check` with a protected credentials file as documented in the release guide.

Readiness only proves configuration. The release acceptance command separately checks actual model execution, tool results, final reply, conversation recovery and logout for each selected model. It creates labeled read-only test conversations and does not replay unknown runs. Real mailbox delivery and remote node execution require their own recorded verification.

## Failure evidence

Catalog and conversation recovery failures have separate messages, retain the request ID when available and offer an explicit retry. Submission stays disabled until the catalog and saved conversation state are loaded. Diagnose the failing request's HTTP status and request ID before changing server configuration.

- HTTP responses include `X-Request-ID`; failed requests log the same ID, method, matched route and status without query strings or headers.
- Provider failures preserve their safe error category, HTTP status, step and attempt in a single durable `run_unknown` event, the result message and a structured `platform_run_failure` log. Known upstream CPU/memory overload and `do_request_failed` codes are retained; arbitrary upstream messages are not.
- Provider HTTP attempts reject redirects with `redirect_rejected` and never forward credentials to a redirect target. `timeout_s` is one total network deadline, including DNS, TCP/TLS, proxy CONNECT, headers and body; trickled bytes cannot prolong it. Deadline and network failures do not replay a possibly accepted POST. SSE `[DONE]` closes the response without waiting for HTTP EOF.
- Unknown outcomes remain unknown and are not automatically replayed. Provider network uncertainty is distinct from accounting failures. A 503 does not prove whether the upstream performed chargeable work.
- Logs retain exception types and bounded file/function/line locations, including explicit causes, without raw exception messages, prompts, provider response bodies or secrets.
- Scheduler, heartbeat and background-worker exceptions are logged. Repeated scheduler tick failures back off up to 30 seconds and resume their normal interval after recovery.
- Local smoke failures stop the temporary Hub before removing runtime files and preserve only `hub.log`. Use `FLEET_SMOKE_ARTIFACT_DIR` to choose the evidence directory; credentials and state are not archived.
- CI uploads JUnit results and browser screenshots on success or failure as `agent-fleet-test-evidence` (7-day retention).

## Regression coverage

The provider fault matrix uses a real local HTTP server returning 403/429/503 and checks the entire worker → event API → durable result/log chain. A separate HTTP tool-contract test validates parameter schemas, writes an actual file and checks the second model request. Browser coverage verifies that HTTP 503 remains visible after reload. Readiness and smoke-failure tests verify nonzero exit and retained evidence, rather than merely checking source strings.

Account browser tests use real account cookies and verify registration, password login/change/recovery, role boundaries, logout and account switching. The full-flow browser test additionally crosses the real scheduler and OpenAI-compatible HTTP adapter, executes four workspace tools, downloads the generated artifact and restores success/failure after page closure. Separate legacy identity tests apply only when account mode is disabled.

No test suite guarantees the absence of all production failures. Production gateway overload, credentials, feature gates and installed agent versions must still be checked against the deployed release.

The local smoke test enables the session data plane and checks that Hub retained both the user message and raw agent output. It reads the events twice to check recovery, then downloads and applies the returned task patch in its disposable project and verifies the recovered file content. A completed task with a missing conversation now fails the smoke test. This fixture uses a local fake CLI; production model/network availability still requires the separate real task canary.

Patch sanitization preserves the exact Git `--- /dev/null` and `+++ /dev/null` headers used for added/deleted files, while continuing to redact secrets in the complete body. Regression tests apply both kinds of patches with Git; a filename in a diff summary is not sufficient artifact verification.
