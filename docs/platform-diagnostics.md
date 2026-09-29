# Platform diagnostics and release checks

A healthy `/api/status` only proves the observation API responds. It does not prove that the assistant's routes, defaults, model credential or worker are configured.

Before accepting a platform deployment, run from the checked-out release:

```sh
# Set AGENT_FLEET_OPERATOR_TOKEN through the deployment secret mechanism.
python -m tools.platform.readiness_check --endpoint https://fleet.example.com
```

This read-only command fails for missing routes, authentication, disabled workers, an empty/default catalog or missing provider credentials/workspace directories. `/api/platform/v1/readiness` is owner scoped. `configuration_ready` is explicitly not a claim that a scheduler thread is alive, a remote node is reachable, the model gateway is healthy, or every selectable model works. Remote workspaces require separate node verification. After it passes, submit a labeled canary, verify its terminal state and actual file/artifact, and reopen the conversation. Never mark an HTTP 202 receipt as task completion.

## Failure evidence

- HTTP responses include `X-Request-ID`; failed requests log the same ID, method, matched route and status without query strings or headers.
- Provider failures preserve their safe error category, HTTP status, step and attempt in a single durable `run_unknown` event, the result message and a structured `platform_run_failure` log. Known upstream CPU/memory overload and `do_request_failed` codes are retained; arbitrary upstream messages are not.
- Unknown outcomes remain unknown and are not automatically replayed. Provider network uncertainty is distinct from accounting failures. A 503 does not prove whether the upstream performed chargeable work.
- Logs retain exception types and bounded file/function/line locations, including explicit causes, without raw exception messages, prompts, provider response bodies or secrets.
- Scheduler, heartbeat and background-worker exceptions are logged. Repeated scheduler tick failures back off up to 30 seconds and resume their normal interval after recovery.
- Local smoke failures stop the temporary Hub before removing runtime files and preserve only `hub.log`. Use `FLEET_SMOKE_ARTIFACT_DIR` to choose the evidence directory; credentials and state are not archived.
- CI uploads JUnit results and browser screenshots on success or failure as `agent-fleet-test-evidence` (7-day retention).

## Regression coverage

The provider fault matrix uses a real local HTTP server returning 403/429/503 and checks the entire worker → event API → durable result/log chain. A separate HTTP tool-contract test validates parameter schemas, writes an actual file and checks the second model request. Browser coverage verifies that HTTP 503 remains visible after reload. Readiness and smoke-failure tests verify nonzero exit and retained evidence, rather than merely checking source strings.

No test suite guarantees the absence of all production failures. Production gateway overload, credentials, feature gates and installed agent versions must still be checked against the deployed release.

The local smoke test enables the session data plane and checks that Hub retained both the user message and raw agent output. It reads the events twice to check recovery, then downloads and applies the returned task patch in its disposable project and verifies the recovered file content. A completed task with a missing conversation now fails the smoke test. This fixture uses a local fake CLI; production model/network availability still requires the separate real task canary.

Patch sanitization preserves the exact Git `--- /dev/null` and `+++ /dev/null` headers used for added/deleted files, while continuing to redact secrets in the complete body. Regression tests apply both kinds of patches with Git; a filename in a diff summary is not sufficient artifact verification.
