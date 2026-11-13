# M1 Durable Local Run Worker

## Goal

Connect the existing Conversation/Run API and finite native runtime through a
durable, opt-in local worker. The worker must survive Hub restarts without
allowing two workers to execute or finish the same Run.

## Contract

- `platform_worker_enabled` is false by default and is forced off when
  `platform_enabled` is false.
- `create_app` only wires `LocalRunWorkerService`; it starts no thread.
- The process entrypoint calls `start_platform_worker(app, owner_id=...)`.
- This worker slice is directory-workspace only. Unsupported workspace
  backends and unavailable providers fail closed.
- The deterministic provider is a local validation provider. It is not a
  production model adapter or a claim of broad compatible-API support.
- `openai_compatible` is a bounded Chat Completions adapter. It requires a
  SecretBroker reference and endpoint in the owner-scoped model profile. Its
  non-secret provider settings are copied into the immutable Run snapshot;
  `secret_ref` is not copied.
- `platform_provider_network_enabled` is a separate default-off gate. An
  injected transport is available for tests; the standard-library urllib
  transport refuses to contact a real endpoint while the gate is off.

## Durable lease

`platform.db.runs` receives additive `lease_id`, `lease_owner`,
`lease_expires_at`, `attempt`, and `result_text` columns. `claim_run` uses
`BEGIN IMMEDIATE` to select the oldest queued Run or reclaim an expired running
Run. Every event and terminal write from a worker carries the current lease
identity; an expired or replaced lease receives `lease_mismatch` and cannot
write back. A run with `cancel_requested` is cancelled before claim, and a
running worker checks cancellation and renews its lease at model/tool
boundaries.

## Execution path

1. Claim one owner-scoped Run and load its immutable config snapshot, trigger
   message, and conversation context.
2. Resolve the owner-scoped model profile and directory workspace. The profile
   provider and non-secret provider settings come from the frozen snapshot;
   the secret is resolved at execution time by the SecretBroker.
3. Acquire an in-process workspace `ResourceLeaseManager` lease with a unique
   attempt owner and construct `ToolBroker`.
4. Run `NativeAssistantRuntime` through `PersistentAssistantWorker`.
5. Persist bounded run events, final text, terminal state, and release the
   workspace lease.

Configuration, workspace, provider, or runtime failures become a bounded
`failed`/`unknown` terminal result. The worker never retries an unknown tool
side effect blindly.

## Verification

`tests/test_platform_run_worker.py` covers successful execution, atomic
two-worker claim, lease expiry/reclaim, owner isolation, missing/unsupported
workspace fail-closed behavior, and cancellation. The platform runtime suite
also verifies event ordering and bounded finite execution.

## Follow-up slices

The follow-up multi-owner scheduler is described in
`docs/platform/agent-fleet-self-hosted-platform.md` and tested in
`tests/test_platform_run_scheduler.py`. It has durable owner fairness leases,
global/workspace worker slots, bounded concurrency, heartbeat renewal, expiry
reclaim, and queue deferral without consuming an attempt. Its independent
configuration gate remains disabled by default.

Still deferred: live provider canary and production credentials, M1.7 fake-Node
end-to-end fixture and unknown-command reconcile UX, production sandbox
launchers, and VPS operations. The
`compatible` provider name still fails closed; use `openai_compatible` only
with an explicit endpoint, broker reference, and network rollout gate.
